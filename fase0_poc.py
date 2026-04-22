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
import os
import queue
import sys
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import sounddevice as sd

ROOT = Path(__file__).parent
MODELS_DIR = ROOT / "models_cache"
MODELS_DIR.mkdir(exist_ok=True)
os.environ.setdefault("HF_HOME", str(MODELS_DIR / "hf"))
os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(MODELS_DIR / "hf"))


def _register_cuda_dlls() -> None:
    if sys.platform != "win32":
        return
    try:
        import importlib.util

        for pkg_name in ("nvidia.cublas", "nvidia.cudnn"):
            spec = importlib.util.find_spec(pkg_name)
            if spec is None or spec.submodule_search_locations is None:
                continue
            for base in spec.submodule_search_locations:
                bin_dir = Path(base) / "bin"
                if bin_dir.is_dir():
                    os.add_dll_directory(str(bin_dir))
                    os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
    except Exception as e:
        print(f"[warn] nao foi possivel registrar DLLs CUDA: {e}", file=sys.stderr)


_register_cuda_dlls()

SAMPLE_RATE = 16000
VAD_FRAME_MS = 30
VAD_FRAME_SAMPLES = SAMPLE_RATE * VAD_FRAME_MS // 1000  # 480
PRE_SPEECH_BUFFER_MS = 300
SILENCE_HANGOVER_MS = 600
MIN_SPEECH_MS = 400
MAX_SEGMENT_MS = 12000

PIPER_VOICES = {
    "en": ("en_US-lessac-medium", "en/en_US/lessac/medium"),
    "pt": ("pt_BR-faber-medium", "pt/pt_BR/faber/medium"),
}


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


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class WebRTCVADGate:
    def __init__(self, aggressiveness: int = 2) -> None:
        log(f"Carregando WebRTC VAD (agressividade={aggressiveness})...")
        import webrtcvad

        self.vad = webrtcvad.Vad(aggressiveness)
        self.sr = SAMPLE_RATE

    def is_speech(self, frame_pcm: np.ndarray) -> bool:
        if len(frame_pcm) != VAD_FRAME_SAMPLES:
            return False
        pcm_i16 = np.clip(frame_pcm * 32768.0, -32768, 32767).astype(np.int16)
        return self.vad.is_speech(pcm_i16.tobytes(), self.sr)


class STT:
    def __init__(self, language: str, model_size: str, device: str, compute_type: str) -> None:
        log(f"Carregando faster-whisper {model_size} ({device}/{compute_type})...")
        from faster_whisper import WhisperModel

        self.model = WhisperModel(
            model_size,
            device=device,
            compute_type=compute_type,
            download_root=str(MODELS_DIR / "whisper"),
        )
        self.language = language

    def transcribe(self, pcm: np.ndarray) -> str:
        segments, _ = self.model.transcribe(
            pcm,
            language=self.language,
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
        )
        return " ".join(s.text.strip() for s in segments).strip()

    def transcribe_with_lang(self, pcm: np.ndarray) -> tuple[str, str]:
        segments, info = self.model.transcribe(
            pcm,
            language=None,
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        return text, info.language


ARGOS_CODE_MAP = {"pt": "pb", "pt-br": "pb", "pt-BR": "pb"}


class ArgosMT:
    def __init__(self, src: str, tgt: str) -> None:
        src_a = ARGOS_CODE_MAP.get(src, src)
        tgt_a = ARGOS_CODE_MAP.get(tgt, tgt)
        log(f"Carregando Argos Translate {src_a}->{tgt_a}...")
        import argostranslate.package
        import argostranslate.translate

        self.translate_mod = argostranslate.translate
        installed = argostranslate.translate.get_installed_languages()
        codes = [lang.code for lang in installed]
        if src_a not in codes or tgt_a not in codes or not _argos_pair_exists(installed, src_a, tgt_a):
            log(f"Instalando pacote Argos {src_a}->{tgt_a}...")
            argostranslate.package.update_package_index()
            available = argostranslate.package.get_available_packages()
            pkg = next(p for p in available if p.from_code == src_a and p.to_code == tgt_a)
            path = pkg.download()
            argostranslate.package.install_from_path(path)
            installed = argostranslate.translate.get_installed_languages()
        self.src_lang = next(lang for lang in installed if lang.code == src_a)
        self.tgt_lang = next(lang for lang in installed if lang.code == tgt_a)
        self.translator = self.src_lang.get_translation(self.tgt_lang)

    def translate(self, text: str) -> str:
        if not text.strip():
            return ""
        return self.translator.translate(text)


def _argos_pair_exists(installed, src: str, tgt: str) -> bool:
    src_l = next((l for l in installed if l.code == src), None)
    tgt_l = next((l for l in installed if l.code == tgt), None)
    if src_l is None or tgt_l is None:
        return False
    return src_l.get_translation(tgt_l) is not None


class PiperTTS:
    def __init__(self, lang: str) -> None:
        voice_name, voice_subpath = PIPER_VOICES[lang]
        model_file = MODELS_DIR / "piper" / f"{voice_name}.onnx"
        config_file = MODELS_DIR / "piper" / f"{voice_name}.onnx.json"
        if not model_file.exists() or not config_file.exists():
            self._download_voice(voice_name, voice_subpath, model_file, config_file)
        log(f"Carregando Piper {voice_name}...")
        from piper import PiperVoice

        self.voice = PiperVoice.load(str(model_file), config_path=str(config_file))
        self.sample_rate = int(getattr(self.voice.config, "sample_rate", 22050))

    @staticmethod
    def _download_voice(name: str, subpath: str, model_file: Path, config_file: Path) -> None:
        import urllib.request

        model_file.parent.mkdir(parents=True, exist_ok=True)
        base = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
        url_model = f"{base}/{subpath}/{name}.onnx"
        url_config = f"{base}/{subpath}/{name}.onnx.json"
        log(f"Baixando Piper: {url_model}")
        urllib.request.urlretrieve(url_model, model_file)
        urllib.request.urlretrieve(url_config, config_file)

    def synthesize(self, text: str) -> np.ndarray:
        if not text.strip():
            return np.zeros(0, dtype=np.float32)
        chunks: list[np.ndarray] = []
        for chunk in self.voice.synthesize(text):
            arr = chunk.audio_float_array
            if arr.dtype != np.float32:
                arr = arr.astype(np.float32)
            chunks.append(arr)
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks)


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


def detect_device() -> tuple[str, str]:
    try:
        import ctranslate2

        devs = ctranslate2.get_supported_compute_types("cuda") if ctranslate2.get_cuda_device_count() > 0 else []
        if devs:
            preferred = "float16" if "float16" in devs else ("int8_float16" if "int8_float16" in devs else devs[0])
            return "cuda", preferred
    except Exception:
        pass
    return "cpu", "int8"


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
