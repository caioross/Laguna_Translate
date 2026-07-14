"""Engines do pipeline de tradução do Laguna Translate.

STT (faster-whisper), MT (Argos Translate), TTS (Piper) e o gate de VAD (WebRTC),
mais `detect_device` e o registro de DLLs CUDA. Extraído de `fase0_poc.py` (fatia 1
do desacoplamento — issue #7 / epic #6) SEM alterar lógica: movimentação de código.

IMPORTANTE: `_register_cuda_dlls()` roda na importação deste módulo, ANTES de
qualquer `import faster_whisper` (que é lazy dentro de `STT`), preservando a ordem
original de `fase0_poc.py`. Não reordene os imports do topo.
"""

from __future__ import annotations

import os
import sys
import time
from collections import deque
from pathlib import Path

import numpy as np

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


class VADSegmenter:
    """Máquina de estados pura do segmentador de fala (pré-buffer / hangover /
    segmento mín-máx), extraída do laço de `segmenter_worker` (fase0_poc) para
    ser determinística e testável sem threads, queues, microfone ou webrtcvad.

    Alimente frame a frame com `push(frame, is_speech)`; devolve o segmento
    (`np.ndarray`) no instante em que ele fecha, ou `None` enquanto acumula (ou
    quando o segmento fechado é curto demais e é descartado). A decisão de
    `is_speech` é do chamador (o VAD real ou um roteiro de teste): aqui só mora
    a política de segmentação. Comportamento idêntico ao laço original — é
    refactor, não mudança da política de VAD (issue #17)."""

    def __init__(self) -> None:
        self.pre_max = PRE_SPEECH_BUFFER_MS // VAD_FRAME_MS
        self.silence_max = SILENCE_HANGOVER_MS // VAD_FRAME_MS
        self.min_speech = MIN_SPEECH_MS // VAD_FRAME_MS
        self.max_frames = MAX_SEGMENT_MS // VAD_FRAME_MS

        self.pre_buf: deque[np.ndarray] = deque(maxlen=self.pre_max)
        self.speaking: list[np.ndarray] = []
        self.silence_run = 0
        self.in_speech = False

    def push(self, frame: np.ndarray, is_speech: bool) -> np.ndarray | None:
        if not self.in_speech:
            self.pre_buf.append(frame)
            if is_speech:
                self.in_speech = True
                self.speaking = list(self.pre_buf)
                self.pre_buf.clear()
                self.silence_run = 0
            return None

        self.speaking.append(frame)
        self.silence_run = 0 if is_speech else self.silence_run + 1
        force_flush = len(self.speaking) >= self.max_frames
        if self.silence_run >= self.silence_max or force_flush:
            seg = np.concatenate(self.speaking) if len(self.speaking) >= self.min_speech else None
            self.in_speech = False
            self.speaking = []
            self.silence_run = 0
            return seg
        return None


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
