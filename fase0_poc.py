"""Fase 0 — Prova de Conceito (sem torch).

Pipeline: mic -> WebRTC VAD -> faster-whisper -> Argos Translate -> Piper TTS -> speaker.
Sem UI, sem Discord. Valida latencia ponta a ponta.

Uso:
    python fase0_poc.py --direction pt2en
    python fase0_poc.py --direction en2pt
    python fase0_poc.py --list-devices
"""

from __future__ import annotations

import argparse
import io
import queue
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import sounddevice as sd

# As engines do pipeline (STT, ArgosMT, PiperTTS, WebRTCVADGate, detect_device,
# _register_cuda_dlls, ARGOS_CODE_MAP e as constantes de VAD/sample rate) foram
# extraídas para laguna_pipeline.py (issue #7, fatia 1 do epic #6). São
# reexportadas aqui para compatibilidade total com os importadores existentes
# (laguna_core, bench_fase0, test_offline, stress_*, fase1_app). Código novo deve
# importar direto de laguna_pipeline — não adicione lógica nova neste módulo.
from laguna_pipeline import (  # noqa: F401
    ARGOS_CODE_MAP,
    MAX_SEGMENT_MS,
    MIN_SPEECH_MS,
    MODELS_DIR,
    PIPER_VOICES,
    PRE_SPEECH_BUFFER_MS,
    ROOT,
    SAMPLE_RATE,
    SILENCE_HANGOVER_MS,
    VAD_FRAME_MS,
    VAD_FRAME_SAMPLES,
    ArgosMT,
    PiperTTS,
    STT,
    WebRTCVADGate,
    _register_cuda_dlls,
    detect_device,
    log,
)


@dataclass
class Stats:
    total: list[float] = field(default_factory=list)
    stt: list[float] = field(default_factory=list)
    mt: list[float] = field(default_factory=list)
    tts: list[float] = field(default_factory=list)

    def add(self, t_stt: float, t_mt: float, t_tts: float) -> None:
        self.stt.append(t_stt)
        self.mt.append(t_mt)
        self.tts.append(t_tts)
        self.total.append(t_stt + t_mt + t_tts)

    @staticmethod
    def _pct(xs: list[float], p: float) -> float:
        if not xs:
            return 0.0
        return float(np.percentile(xs, p))

    def report(self) -> str:
        if not self.total:
            return "(sem dados)"
        lines = [f"--- N={len(self.total)} segmentos ---"]
        for name, xs in [("TOTAL", self.total), ("STT  ", self.stt), ("MT   ", self.mt), ("TTS  ", self.tts)]:
            lines.append(
                f"  {name}  p50={self._pct(xs, 50):7.0f}ms  "
                f"p95={self._pct(xs, 95):7.0f}ms  "
                f"p99={self._pct(xs, 99):7.0f}ms  "
                f"max={max(xs):7.0f}ms"
            )
        return "\n".join(lines)


def play(pcm: np.ndarray, sr: int) -> None:
    if len(pcm) == 0:
        return
    sd.play(pcm, samplerate=sr, blocking=True)


def save_debug_wav(pcm: np.ndarray, path: Path, sr: int = SAMPLE_RATE) -> None:
    pcm_i16 = np.clip(pcm * 32768.0, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm_i16.tobytes())


def segmenter_worker(
    audio_q: "queue.Queue[np.ndarray]",
    seg_q: "queue.Queue[np.ndarray]",
    vad: WebRTCVADGate,
    stop_evt: threading.Event,
) -> None:
    pre_max = PRE_SPEECH_BUFFER_MS // VAD_FRAME_MS
    silence_max = SILENCE_HANGOVER_MS // VAD_FRAME_MS
    min_speech = MIN_SPEECH_MS // VAD_FRAME_MS
    max_frames = MAX_SEGMENT_MS // VAD_FRAME_MS

    pre_buf: deque[np.ndarray] = deque(maxlen=pre_max)
    speaking: list[np.ndarray] = []
    silence_run = 0
    in_speech = False

    while not stop_evt.is_set():
        try:
            frame = audio_q.get(timeout=0.1)
        except queue.Empty:
            continue

        is_speech = vad.is_speech(frame)

        if not in_speech:
            pre_buf.append(frame)
            if is_speech:
                in_speech = True
                speaking = list(pre_buf)
                pre_buf.clear()
                silence_run = 0
        else:
            speaking.append(frame)
            silence_run = 0 if is_speech else silence_run + 1
            force_flush = len(speaking) >= max_frames
            if silence_run >= silence_max or force_flush:
                if len(speaking) >= min_speech:
                    seg = np.concatenate(speaking)
                    try:
                        seg_q.put_nowait(seg)
                    except queue.Full:
                        log("seg_q cheia, descartando segmento antigo")
                in_speech = False
                speaking = []
                silence_run = 0


def capture_worker(audio_q: "queue.Queue[np.ndarray]", stop_evt: threading.Event, device: Optional[int]) -> None:
    def callback(indata, frames, time_info, status):
        if status:
            log(f"Stream status: {status}")
        mono = indata[:, 0] if indata.ndim > 1 else indata
        try:
            audio_q.put_nowait(mono.copy())
        except queue.Full:
            pass

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        blocksize=VAD_FRAME_SAMPLES,
        device=device,
        callback=callback,
    ):
        log("Capturando do microfone. Fale algo. Ctrl+C para parar.")
        while not stop_evt.is_set():
            time.sleep(0.1)


def run(direction: str, model_size: str, device_spec: str, input_device: Optional[int], debug: bool) -> None:
    src, tgt = direction.split("2")
    src_lang = "pt" if src == "pt" else "en"
    tgt_lang = "en" if tgt == "en" else "pt"

    if device_spec == "auto":
        device, compute_type = detect_device()
    elif device_spec == "cuda":
        device, compute_type = "cuda", "float16"
    else:
        device, compute_type = "cpu", "int8"
    log(f"STT device: {device} / {compute_type}")

    vad = WebRTCVADGate(aggressiveness=2)
    stt = STT(language=src_lang, model_size=model_size, device=device, compute_type=compute_type)
    mt = ArgosMT(src=src_lang, tgt=tgt_lang)
    tts = PiperTTS(lang=tgt_lang)

    log("Warmup dos modelos...")
    warm_pcm = np.zeros(SAMPLE_RATE, dtype=np.float32)
    _ = stt.transcribe(warm_pcm)
    _ = mt.translate("hello" if src_lang == "en" else "olá")
    _ = tts.synthesize("warm up")
    log("Warmup concluido.")

    audio_q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=200)
    seg_q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=4)
    stop_evt = threading.Event()
    stats = Stats()

    cap_t = threading.Thread(target=capture_worker, args=(audio_q, stop_evt, input_device), daemon=True)
    seg_t = threading.Thread(target=segmenter_worker, args=(audio_q, seg_q, vad, stop_evt), daemon=True)
    cap_t.start()
    seg_t.start()

    seg_idx = 0
    try:
        while True:
            try:
                segment = seg_q.get(timeout=0.5)
            except queue.Empty:
                continue
            seg_idx += 1
            dur_s = len(segment) / SAMPLE_RATE
            log(f"[{seg_idx}] segmento {dur_s:.2f}s")
            if debug:
                save_debug_wav(segment, ROOT / f"debug_seg_{seg_idx}.wav")

            t0 = time.perf_counter()
            text = stt.transcribe(segment)
            t_stt = (time.perf_counter() - t0) * 1000
            if not text.strip():
                log(f"[{seg_idx}] (sem texto)  STT={t_stt:.0f}ms")
                continue
            log(f"[{seg_idx}] STT({src_lang})={text!r}  [{t_stt:.0f}ms]")

            t0 = time.perf_counter()
            translated = mt.translate(text)
            t_mt = (time.perf_counter() - t0) * 1000
            log(f"[{seg_idx}] MT({tgt_lang})={translated!r}  [{t_mt:.0f}ms]")

            t0 = time.perf_counter()
            audio_out = tts.synthesize(translated)
            t_tts = (time.perf_counter() - t0) * 1000
            log(f"[{seg_idx}] TTS {len(audio_out)/tts.sample_rate:.2f}s audio  [{t_tts:.0f}ms]")

            stats.add(t_stt, t_mt, t_tts)
            log(f"[{seg_idx}] TOTAL={t_stt + t_mt + t_tts:.0f}ms (STT+MT+TTS)")

            play(audio_out, tts.sample_rate)
            print(stats.report(), flush=True)

    except KeyboardInterrupt:
        log("Encerrando. Estatisticas finais:")
        print(stats.report(), flush=True)
        stop_evt.set()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--direction", choices=["pt2en", "en2pt"], default="pt2en")
    parser.add_argument("--model", default="small", help="tiny|base|small|medium|large-v3")
    parser.add_argument("--device", default="auto", help="auto|cuda|cpu")
    parser.add_argument("--input-device", type=int, default=None)
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return

    run(args.direction, args.model, args.device, args.input_device, args.debug)


if __name__ == "__main__":
    main()
