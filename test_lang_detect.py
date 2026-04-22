"""Valida deteccao de idioma via STT.transcribe_with_lang().

Sintetiza frases PT e EN via Piper, passa pelo STT, confere:
 - precisao da deteccao
 - overhead de latencia vs transcribe() (idioma fixo)
"""

from __future__ import annotations

import time

import numpy as np

from bench_fase0 import resample_to_16k
from fase0_poc import PiperTTS, STT, detect_device, log

PHRASES_PT = [
    "Olá, tudo bem com vocês?",
    "Vamos começar o jogo em cinco minutos.",
    "O roteador travou de novo, vou reiniciar.",
    "Quem vai de suporte nessa partida?",
]
PHRASES_EN = [
    "Hello, how are you doing today?",
    "Let's start the match in five minutes.",
    "The router froze again, I'll reboot it.",
    "Who's going support this round?",
]


def main() -> None:
    device, ct = detect_device()
    log(f"STT small/{device}/{ct}")

    stt = STT(language="pt", model_size="small", device=device, compute_type=ct)
    tts_pt = PiperTTS(lang="pt")
    tts_en = PiperTTS(lang="en")

    log("Warmup...")
    _ = stt.transcribe(np.zeros(16000, dtype=np.float32))

    # pre-sintetiza
    audio_pt = [resample_to_16k(tts_pt.synthesize(t), tts_pt.sample_rate) for t in PHRASES_PT]
    audio_en = [resample_to_16k(tts_en.synthesize(t), tts_en.sample_rate) for t in PHRASES_EN]

    print("\n" + "=" * 72)
    print(" DETECCAO DE IDIOMA (esperado vs detectado)")
    print("=" * 72)
    correct = 0
    total = 0
    lat_detect: list[float] = []
    lat_fixed: list[float] = []
    for expected, audios in [("pt", audio_pt), ("en", audio_en)]:
        for pcm in audios:
            t0 = time.perf_counter()
            text, detected = stt.transcribe_with_lang(pcm)
            lat_detect.append((time.perf_counter() - t0) * 1000)
            ok = detected == expected
            correct += int(ok)
            total += 1
            mark = "OK " if ok else "!! "
            snippet = (text[:55] + "...") if len(text) > 55 else text
            print(f" {mark} expected={expected} detected={detected}  |  {snippet}")

    print(f"\nAcuracia: {correct}/{total} ({100*correct/total:.0f}%)")

    # comparar latencia com transcribe() fixo
    stt_en = STT(language="en", model_size="small", device=device, compute_type=ct)
    _ = stt_en.transcribe(np.zeros(16000, dtype=np.float32))
    for pcm in audio_en:
        t0 = time.perf_counter()
        _ = stt_en.transcribe(pcm)
        lat_fixed.append((time.perf_counter() - t0) * 1000)

    print("\n" + "=" * 72)
    print(" LATENCIA STT (ms)")
    print("=" * 72)
    print(f"  transcribe_with_lang (auto):  p50={np.percentile(lat_detect, 50):.0f}  mean={np.mean(lat_detect):.0f}")
    print(f"  transcribe (fixed lang):      p50={np.percentile(lat_fixed, 50):.0f}  mean={np.mean(lat_fixed):.0f}")
    overhead = np.mean(lat_detect) - np.mean(lat_fixed)
    print(f"  overhead deteccao:            {overhead:+.0f}ms")


if __name__ == "__main__":
    main()
