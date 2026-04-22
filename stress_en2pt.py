"""Stress test reverso: EN -> PT.

Complementa stress_fase0.py (que roda PT->EN). Usa 10 frases em inglês
variadas, sintetiza via Piper en_US, passa pelo pipeline en->pt.
"""

from __future__ import annotations

import argparse
import time

import numpy as np

from bench_fase0 import resample_to_16k
from fase0_poc import ArgosMT, PiperTTS, STT, Stats, detect_device, log

PHRASES_EN = [
    "Hello, how are you doing today?",
    "Let's start the match in five minutes, the team is ready.",
    "Can you hear me? My microphone might be muted again.",
    "Nice play! That was a sick flick on the last round.",
    "Please move to voice channel number three for the briefing.",
    "The server restarted, everyone reconnect now.",
    "I'll go support, who wants to play carry this game?",
    "Marianna is joining after her call, probably around nine.",
    "If it lags again, I'll just reboot the router.",
    "Good game, well played everyone, see you tomorrow.",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--model", default="small")
    parser.add_argument("--device", default="auto")
    args = parser.parse_args()

    if args.device == "auto":
        device, ct = detect_device()
    elif args.device == "cuda":
        device, ct = "cuda", "float16"
    else:
        device, ct = "cpu", "int8"
    log(f"Stress EN->PT: {args.rounds}x{len(PHRASES_EN)}. STT={args.model}/{device}/{ct}")

    stt_en = STT(language="en", model_size=args.model, device=device, compute_type=ct)
    mt = ArgosMT(src="en", tgt="pt")
    tts_en = PiperTTS(lang="en")
    tts_pt = PiperTTS(lang="pt")

    log("Warmup...")
    _ = stt_en.transcribe(np.zeros(16000, dtype=np.float32))
    _ = mt.translate("hello")
    _ = tts_en.synthesize("warm up")
    _ = tts_pt.synthesize("aquecendo")

    precomputed: list[np.ndarray] = []
    for txt in PHRASES_EN:
        a = tts_en.synthesize(txt)
        precomputed.append(resample_to_16k(a, tts_en.sample_rate))

    stats = Stats()
    samples: list[tuple[str, str, str]] = []
    t_start = time.perf_counter()
    for r in range(1, args.rounds + 1):
        round_stats = Stats()
        for i, pcm in enumerate(precomputed):
            t0 = time.perf_counter()
            text = stt_en.transcribe(pcm)
            t_stt = (time.perf_counter() - t0) * 1000
            t0 = time.perf_counter()
            pt = mt.translate(text)
            t_mt = (time.perf_counter() - t0) * 1000
            t0 = time.perf_counter()
            _ = tts_pt.synthesize(pt)
            t_tts = (time.perf_counter() - t0) * 1000
            stats.add(t_stt, t_mt, t_tts)
            round_stats.add(t_stt, t_mt, t_tts)
            if r == 1:
                samples.append((PHRASES_EN[i], text, pt))
        log(
            f"[round {r:02d}] elapsed={time.perf_counter() - t_start:5.0f}s  p50={np.percentile(round_stats.total, 50):.0f}ms"
        )

    print("\n" + "=" * 80)
    print(" QUALIDADE (EN original -> STT -> MT -> PT)")
    print("=" * 80)
    for en, heard, pt in samples:
        print(f"EN   : {en}")
        print(f"OUVIU: {heard}")
        print(f"PT   : {pt}")
        print()

    print("=" * 70)
    print(stats.report())


if __name__ == "__main__":
    main()
