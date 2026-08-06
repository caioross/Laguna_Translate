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
import re
import sys
import time
import unicodedata
from collections import Counter, deque
from dataclasses import dataclass, field
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


# --- Filtro de alucinacao do STT (issue #47) --------------------------------
#
# Segmento sem fala de verdade (respiracao, clique de teclado, musica ao fundo)
# nao volta vazio do faster-whisper: volta uma frase plausivel. Como o Laguna
# FALA o resultado no cabo virtual, a alucinacao vira o app conversando sozinho
# numa call de Discord — o pior modo de falha do produto.
#
# Duas camadas, ambas locais e sem custo relevante (so texto, ja fora do STT):
# metadados que o proprio Whisper calcula, e uma blocklist normalizada para as
# frases-lixo classicas, que as vezes vem com logprob bom.
#
# REGRA DE OURO: barrar fala real e MUITO pior que deixar passar um ruido. Todo
# limiar aqui e conservador e, na duvida, o filtro APROVA.

NO_SPEECH_PROB_MAX = 0.6  # acima disso o proprio Whisper acha que nao ha fala
AVG_LOGPROB_MIN = -1.0  # abaixo disso o texto e chute de baixa confianca
REPEAT_MIN_WORDS = 4  # repeticao so e suspeita a partir daqui ("sim" passa)
# 1 palavra ocupando >=80% do segmento. Em 0.75, "vai vai vai time" (grito de
# torcida, 3/4) era barrado — fala real. Em 0.8 o menor caso barrado com 4
# palavras e o 4/4 puro ("sim sim sim sim"), que e o alvo da issue.
REPEAT_DOMINANT_RATIO = 0.8
REPEAT_LOOP_MIN_WORDS = 6  # "sim nao sim nao sim nao": poucas palavras, muitas vezes
REPEAT_LOOP_DISTINCT_RATIO = 1 / 3

# Frases-lixo classicas do Whisper em silencio/musica, ja normalizadas
# (minusculas, sem acento e sem pontuacao). Lista curta de proposito: cada
# entrada precisa ser algo que ninguem diz numa call. "obrigado" sozinho, por
# exemplo, NAO entra — e fala legitima comum.
HALLUCINATION_BLOCKLIST = frozenset(
    {
        # PT — creditos de legenda e encerramento de video
        "legendas pela comunidade amara org",
        "legendado pela comunidade amara org",
        "legendas pela comunidade",
        "obrigado por assistir",
        "obrigada por assistir",
        "obrigado por assistirem",
        "ate o proximo video",
        "inscreva se no canal",
        "tchau",
        # EN — idem
        "subtitles by the amara org community",
        "subtitles by amara org community",
        "thanks for watching",
        "thank you for watching",
        "thank you",
        "thanks",
        "bye",
        "you",  # saida tipica do Whisper em silencio puro
    }
)

_PONTUACAO = re.compile(r"[^\w\s]")
_ESPACOS = re.compile(r"\s+")


def normalize_for_filter(text: str) -> str:
    """Minusculas, sem acento, sem pontuacao, espacos colapsados.

    Serve para comparar com a blocklist e para contar repeticao: "Obrigado por
    assistir!" e "obrigado por assistir" tem que cair na mesma chave.
    """
    sem_acento = "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )
    return _ESPACOS.sub(" ", _PONTUACAO.sub(" ", sem_acento.lower())).strip()


def _is_repetition(words: list[str]) -> bool:
    """Repeticao patologica: o modelo travou numa palavra ou num par delas."""
    if len(words) < REPEAT_MIN_WORDS:
        return False
    dominante = Counter(words).most_common(1)[0][1]
    if dominante / len(words) >= REPEAT_DOMINANT_RATIO:
        return True
    # Loop curto alternado ("sim nao sim nao sim nao"): muitas palavras, quase
    # nenhuma distinta. Exige segmento mais longo para nao pegar fala real.
    return (
        len(words) >= REPEAT_LOOP_MIN_WORDS
        and len(set(words)) <= len(words) * REPEAT_LOOP_DISTINCT_RATIO
    )


def is_hallucination(
    text: str,
    no_speech_prob: float | None = None,
    avg_logprob: float | None = None,
) -> bool:
    """Decide se a transcricao deve ser descartada antes de virar voz.

    Pura: so texto e numeros, sem audio, modelo ou GPU — testavel na CI.

    Os metadados so reprovam em CONJUNCAO (`no_speech_prob` alto E `avg_logprob`
    baixo): sozinho, qualquer um dos dois derruba fala real (frase curta legitima
    tem logprob ruim com frequencia). Metadado ausente (`None`) nao opina.
    """
    norm = normalize_for_filter(text)
    if not norm:  # "...", "!!!", so pontuacao: nao ha o que falar
        return True
    if norm in HALLUCINATION_BLOCKLIST:
        return True
    if _is_repetition(norm.split()):
        return True
    if (
        no_speech_prob is not None
        and avg_logprob is not None
        and no_speech_prob > NO_SPEECH_PROB_MAX
        and avg_logprob < AVG_LOGPROB_MIN
    ):
        return True
    return False


@dataclass(frozen=True)
class STTResult:
    """Saida do STT com os metadados que o faster-whisper ja calcula.

    `no_speech_prob` e `avg_logprob` sao a media ponderada por duracao dos
    segmentos (ver `_aggregate_segments`); ficam `None` quando o modelo nao os
    expoe. Existe para que `laguna_core` possa julgar a confianca da frase sem
    quebrar quem so quer o texto (`transcribe`/`transcribe_with_lang`).
    """

    text: str
    language: str
    no_speech_prob: float | None = None
    avg_logprob: float | None = None


def _aggregate_segments(segments) -> tuple[str, float | None, float | None]:
    """Junta os segmentos num texto e agrega os metadados por duracao.

    Ponderar por duracao (e nao por contagem) evita que um "hm" de 0.2s pese o
    mesmo que uma frase de 3s. Segmento sem duracao util (ou sem o atributo,
    caso de dubles em teste) entra com peso 1.0.
    """
    partes: list[str] = []
    soma_ns = soma_lp = soma_peso = 0.0
    tem_ns = tem_lp = False
    for s in segments:
        partes.append(s.text.strip())
        peso = float(getattr(s, "end", 0.0) or 0.0) - float(getattr(s, "start", 0.0) or 0.0)
        if peso <= 0:
            peso = 1.0
        ns = getattr(s, "no_speech_prob", None)
        lp = getattr(s, "avg_logprob", None)
        if ns is not None:
            soma_ns += float(ns) * peso
            tem_ns = True
        if lp is not None:
            soma_lp += float(lp) * peso
            tem_lp = True
        soma_peso += peso
    texto = " ".join(partes).strip()
    if soma_peso <= 0:
        return texto, None, None
    return (
        texto,
        soma_ns / soma_peso if tem_ns else None,
        soma_lp / soma_peso if tem_lp else None,
    )


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

    def transcribe_detailed(self, pcm: np.ndarray, detect_lang: bool = False) -> STTResult:
        """Transcreve devolvendo texto + metadados de confianca (issue #47).

        `detect_lang=True` deixa o Whisper decidir o idioma (o que
        `transcribe_with_lang` fazia); caso contrario usa o idioma fixo da
        direcao. E o unico ponto que fala com o modelo — os dois metodos
        historicos delegam aqui para nao existirem dois jeitos de transcrever.
        """
        segments, info = self.model.transcribe(
            pcm,
            language=None if detect_lang else self.language,
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
        )
        text, no_speech_prob, avg_logprob = _aggregate_segments(segments)
        lang = info.language if detect_lang else self.language
        return STTResult(text=text, language=lang, no_speech_prob=no_speech_prob, avg_logprob=avg_logprob)

    def transcribe(self, pcm: np.ndarray) -> str:
        return self.transcribe_detailed(pcm).text

    def transcribe_with_lang(self, pcm: np.ndarray) -> tuple[str, str]:
        res = self.transcribe_detailed(pcm, detect_lang=True)
        return res.text, res.language


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
