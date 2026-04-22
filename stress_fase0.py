"""Stress test Fase 0: roda N rodadas do bench no mesmo processo.

Valida:
 - critério §13.2: sem crash por 30 min
 - latência p50/p95/p99 com amostra grande (N rodadas x 10 frases)
 - estabilidade (latência não degrada com tempo)

Uso:
    python stress_fase0.py --rounds 10 --model small --device cuda
"""

from __future__ import annotations

import argparse
import time

import numpy as np

from bench_fase0 import PHRASES_PT, resample_to_16k
from fase0_poc import ArgosMT, PiperTTS, STT, Stats, detect_device, log


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
    log(f"Stress: {args.rounds} rodadas x {len(PHRASES_PT)} frases. STT={args.model}/{device}/{ct}")

    stt_pt = STT(language="pt", model_size=args.model, device=device, compute_type=ct)
    mt = ArgosMT(src="pt", tgt="en")
    tts_pt = PiperTTS(lang="pt")
    tts_en = PiperTTS(lang="en")

    log("Warmup...")
    _ = stt_pt.transcribe(np.zeros(16000, dtype=np.float32))
    _ = mt.translate("olá")
    _ = tts_pt.synthesize("aquecendo")
    _ = tts_en.synthesize("warm up")
    log("Warmup OK")

    # pré-sintetiza as 10 frases em PT (audio fixo) pra não poluir medida com Piper PT
    precomputed: list[np.ndarray] = []
    for txt in PHRASES_PT:
        a = tts_pt.synthesize(txt)
        precomputed.append(resample_to_16k(a, tts_pt.sample_rate))

    global_stats = Stats()
    per_round: list[Stats] = []

    t_start = time.perf_counter()
    for r in range(1, args.rounds + 1):
        round_stats = Stats()
        for i, pcm in enumerate(precomputed, 1):
            t0 = time.perf_counter()
            text = stt_pt.transcribe(pcm)
            t_stt = (time.perf_counter() - t0) * 1000
            t0 = time.perf_counter()
            en = mt.translate(text)
            t_mt = (time.perf_counter() - t0) * 1000
            t0 = time.perf_counter()
            _ = tts_en.synthesize(en)
            t_tts = (time.perf_counter() - t0) * 1000
            round_stats.add(t_stt, t_mt, t_tts)
            global_stats.add(t_stt, t_mt, t_tts)
        per_round.append(round_stats)
        p50 = np.percentile(round_stats.total, 50)
        p95 = np.percentile(round_stats.total, 95)
        elapsed = time.perf_counter() - t_start
        log(f"[round {r:02d}] elapsed={elapsed:6.0f}s  p50={p50:.0f}  p95={p95:.0f}ms")

    total_elapsed = time.perf_counter() - t_start
    print("\n" + "=" * 70)
    print(f" STRESS CONCLUÍDO: {args.rounds} rodadas em {total_elapsed:.0f}s")
    print("=" * 70)
    print(global_stats.report())
    print("\nLatência p50 por rodada (deve ficar estável):")
    for r, s in enumerate(per_round, 1):
        print(f"  round {r:02d}: p50={np.percentile(s.total, 50):.0f}ms  p95={np.percentile(s.total, 95):.0f}ms  max={max(s.total):.0f}ms")


if __name__ == "__main__":
    main()
