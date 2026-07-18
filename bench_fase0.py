"""Benchmark offline Fase 0: 10 frases variadas PT->EN.

Sintetiza cada frase via Piper PT, roda pelo pipeline (STT->MT->TTS) e
reporta latencias e qualidade subjetiva. Valida criterios 1 e 3 do §13.

Uso:
    python bench_fase0.py --model small --device cuda
    python bench_fase0.py --model small --device cpu
"""

from __future__ import annotations

import argparse
import time
import wave
from pathlib import Path

import numpy as np

from laguna_pipeline import ArgosMT, PiperTTS, STT, Stats, detect_device, log

PHRASES_PT = [
    "Olá, tudo bem com você?",
    "Estou testando o tradutor de voz em tempo real no Discord.",
    "Por favor, entre no canal de voz número dois.",
    "O jogo começa às oito da noite, não esqueça.",
    "Você consegue ouvir a minha voz claramente?",
    "Prefiro jogar como suporte, quem vai de carregado hoje?",
    "Mariana vai entrar depois do trabalho, por volta das nove.",
    "A latência do meu microfone está muito alta, espera um segundo.",
    "Caramba, isso foi demais! Parabéns pela jogada.",
    "Se travar de novo, vou reiniciar o roteador e o computador.",
]


def write_wav(path: Path, pcm: np.ndarray, sr: int) -> None:
    pcm_i16 = np.clip(pcm * 32768.0, -32768, 32767).astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm_i16.tobytes())


def resample_to_16k(pcm: np.ndarray, sr: int) -> np.ndarray:
    if sr == 16000:
        return pcm.astype(np.float32)
    import scipy.signal
    from math import gcd

    g = gcd(sr, 16000)
    return scipy.signal.resample_poly(pcm, 16000 // g, sr // g).astype(np.float32)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="small")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--outdir", default="bench_out")
    args = parser.parse_args()

    if args.device == "auto":
        device, ct = detect_device()
    elif args.device == "cuda":
        device, ct = "cuda", "float16"
    else:
        device, ct = "cpu", "int8"
    log(f"STT: {args.model} / {device} / {ct}")

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

    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    stats = Stats()
    rows: list[tuple[int, str, str, str, float, float, float]] = []

    for i, txt_pt in enumerate(PHRASES_PT, 1):
        # Gera audio PT via Piper
        audio_pt = tts_pt.synthesize(txt_pt)
        sr_in = tts_pt.sample_rate
        write_wav(out / f"{i:02d}_input_pt.wav", audio_pt, sr_in)
        pcm_16k = resample_to_16k(audio_pt, sr_in)
        dur = len(pcm_16k) / 16000

        t0 = time.perf_counter()
        heard = stt_pt.transcribe(pcm_16k)
        t_stt = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        en = mt.translate(heard)
        t_mt = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        audio_en = tts_en.synthesize(en)
        t_tts = (time.perf_counter() - t0) * 1000
        write_wav(out / f"{i:02d}_output_en.wav", audio_en, tts_en.sample_rate)

        stats.add(t_stt, t_mt, t_tts)
        rows.append((i, txt_pt, heard, en, t_stt, t_mt, t_tts))
        log(
            f"[{i:02d}] dur={dur:.1f}s  STT={t_stt:.0f}  MT={t_mt:.0f}  TTS={t_tts:.0f}  total={t_stt+t_mt+t_tts:.0f}ms"
        )

    print("\n" + "=" * 80)
    print(" QUALIDADE (original PT -> STT ouviu -> MT traduziu)")
    print("=" * 80)
    for i, pt, heard, en, *_ in rows:
        print(f"[{i:02d}] PT   : {pt}")
        print(f"     OUVIU: {heard}")
        print(f"     EN   : {en}")
        print()

    print("=" * 80)
    print(stats.report())
    print("=" * 80)
    print(f"WAVs salvos em {out}/")


if __name__ == "__main__":
    main()
