"""Teste offline: roda o pipeline em um WAV existente.

Util para validar o pipeline sem microfone. Gera um WAV de saida com o audio traduzido.

Uso:
    python test_offline.py audio_in.wav --direction pt2en -o audio_out.wav
"""

from __future__ import annotations

import argparse
import time
import wave
from pathlib import Path

import numpy as np

from laguna_pipeline import ArgosMT, PiperTTS, STT, detect_device, log


def read_wav_mono_16k(path: Path) -> np.ndarray:
    import scipy.signal

    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        nframes = w.getnframes()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(nframes)
    if sw == 2:
        arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 4:
        arr = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / (2**31)
    else:
        raise ValueError(f"sample width {sw} nao suportado")
    if ch > 1:
        arr = arr.reshape(-1, ch).mean(axis=1)
    if sr != 16000:
        from math import gcd

        g = gcd(sr, 16000)
        arr = scipy.signal.resample_poly(arr, 16000 // g, sr // g).astype(np.float32)
    return arr


def write_wav(path: Path, pcm: np.ndarray, sr: int) -> None:
    pcm_i16 = np.clip(pcm * 32768.0, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm_i16.tobytes())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("wav_in")
    parser.add_argument("--direction", choices=["pt2en", "en2pt"], default="pt2en")
    parser.add_argument("--model", default="small")
    parser.add_argument("--device", default="auto")
    parser.add_argument("-o", "--output", default="out.wav")
    args = parser.parse_args()

    src, tgt = args.direction.split("2")
    if args.device == "auto":
        device, compute_type = detect_device()
    elif args.device == "cuda":
        device, compute_type = "cuda", "float16"
    else:
        device, compute_type = "cpu", "int8"

    pcm = read_wav_mono_16k(Path(args.wav_in))
    log(f"Audio entrada: {len(pcm)/16000:.2f}s")

    stt = STT(language=src, model_size=args.model, device=device, compute_type=compute_type)
    mt = ArgosMT(src=src, tgt=tgt)
    tts = PiperTTS(lang=tgt)

    t = time.perf_counter()
    text = stt.transcribe(pcm)
    log(f"STT({src}): {text!r}  [{(time.perf_counter()-t)*1000:.0f}ms]")

    t = time.perf_counter()
    translated = mt.translate(text)
    log(f"MT({tgt}): {translated!r}  [{(time.perf_counter()-t)*1000:.0f}ms]")

    t = time.perf_counter()
    audio_out = tts.synthesize(translated)
    log(f"TTS: {len(audio_out)/tts.sample_rate:.2f}s  [{(time.perf_counter()-t)*1000:.0f}ms]")

    write_wav(Path(args.output), audio_out, tts.sample_rate)
    log(f"Salvou em {args.output}")


if __name__ == "__main__":
    main()
