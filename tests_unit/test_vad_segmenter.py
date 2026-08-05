"""Alvo #17 — máquina de estados do segmentador VAD (`VADSegmenter`).

Exercita a política pura de segmentação (pré-buffer / hangover / segmento
mín-máx) extraída do laço de `segmenter_worker` para `laguna_pipeline.
VADSegmenter`. Sem áudio, sem `webrtcvad`, sem threads/queues: o `is_speech` de
cada frame é roteirizado pelo teste, então as asserções são determinísticas.

Os limiares em frames derivam das constantes (com `VAD_FRAME_MS=30`):
pré-buffer=10, hangover=20, mín=13, máx=400. Os testes recomputam esses limiares
a partir das constantes importadas — se as constantes mudarem, os testes seguem.
"""

import inspect

import numpy as np

import laguna_core
from laguna_pipeline import (
    MAX_SEGMENT_MS,
    MIN_SPEECH_MS,
    PRE_SPEECH_BUFFER_MS,
    SILENCE_HANGOVER_MS,
    VAD_FRAME_MS,
    VAD_FRAME_SAMPLES,
    VADSegmenter,
)

PRE_MAX = PRE_SPEECH_BUFFER_MS // VAD_FRAME_MS       # 10
SILENCE_MAX = SILENCE_HANGOVER_MS // VAD_FRAME_MS    # 20
MIN_SPEECH = MIN_SPEECH_MS // VAD_FRAME_MS           # 13
MAX_FRAMES = MAX_SEGMENT_MS // VAD_FRAME_MS          # 400


def _frame(fill: float) -> np.ndarray:
    return np.full(VAD_FRAME_SAMPLES, fill, dtype=np.float32)


def _drive(seg: VADSegmenter, script) -> list[np.ndarray]:
    """Empurra tuplas `(fill, is_speech, n)` e devolve os segmentos emitidos."""
    out: list[np.ndarray] = []
    for fill, is_speech, n in script:
        for _ in range(n):
            r = seg.push(_frame(fill), is_speech)
            if r is not None:
                out.append(r)
    return out


# --- (a) nada é emitido enquanto o segmento apenas acumula fala --------------


def test_nada_emitido_enquanto_acumula_fala():
    # Fala contínua abaixo do teto MÁX: sem hangover e sem force-flush o
    # segmento fica aberto e NADA sai, mesmo depois de cruzar MIN_SPEECH.
    seg = VADSegmenter()
    for i in range(MAX_FRAMES - 1):
        assert seg.push(_frame(1.0), True) is None, f"emitiu cedo no frame {i}"


# --- (b) o pré-buffer é prependido ao 1º segmento ---------------------------


def test_pre_buffer_prependido_no_primeiro_segmento():
    pre = 5  # < PRE_MAX, cabe inteiro no deque
    script_fala = [(0.9, True, MIN_SPEECH), (0.0, False, SILENCE_MAX)]

    com_pre = _drive(VADSegmenter(), [(0.3, False, pre), *script_fala])
    sem_pre = _drive(VADSegmenter(), script_fala)
    assert len(com_pre) == 1 and len(sem_pre) == 1
    seg_pre, seg_nopre = com_pre[0], sem_pre[0]

    # O 1º segmento com pré-fala é exatamente `pre` frames mais longo e começa
    # com o conteúdo de pré-fala (0.3); logo em seguida vem a fala (0.9).
    assert len(seg_pre) - len(seg_nopre) == pre * VAD_FRAME_SAMPLES
    assert np.allclose(seg_pre[: pre * VAD_FRAME_SAMPLES], 0.3)
    assert np.allclose(seg_pre[pre * VAD_FRAME_SAMPLES : (pre + 1) * VAD_FRAME_SAMPLES], 0.9)


def test_pre_buffer_limitado_a_pre_max():
    # Pré-fala mais longa que o deque: só os últimos PRE_MAX frames sobrevivem,
    # e o frame de fala que dispara o estado ocupa 1 slot do deque cheio — então
    # entram PRE_MAX-1 frames de pré-fala (comportamento exato do deque maxlen).
    over = PRE_MAX + 7
    script_fala = [(0.9, True, MIN_SPEECH), (0.0, False, SILENCE_MAX)]

    seg_pre = _drive(VADSegmenter(), [(0.3, False, over), *script_fala])[0]
    seg_nopre = _drive(VADSegmenter(), script_fala)[0]

    assert len(seg_pre) - len(seg_nopre) == (PRE_MAX - 1) * VAD_FRAME_SAMPLES
    assert np.allclose(seg_pre[: (PRE_MAX - 1) * VAD_FRAME_SAMPLES], 0.3)


# --- (c) hangover fecha após exatamente SILENCE_MAX frames silenciosos -------


def test_hangover_fecha_em_silence_max_frames():
    seg = VADSegmenter()
    for _ in range(MIN_SPEECH):
        assert seg.push(_frame(1.0), True) is None
    for i in range(SILENCE_MAX - 1):
        assert seg.push(_frame(0.0), False) is None, f"fechou cedo no silêncio {i}"
    out = seg.push(_frame(0.0), False)
    assert out is not None
    # Segmento = fala + os frames de hangover (silêncio conta no segmento).
    assert len(out) == (MIN_SPEECH + SILENCE_MAX) * VAD_FRAME_SAMPLES


def test_fala_no_meio_do_silencio_reseta_hangover():
    seg = VADSegmenter()
    for _ in range(MIN_SPEECH):
        seg.push(_frame(1.0), True)
    for _ in range(SILENCE_MAX - 1):
        assert seg.push(_frame(0.0), False) is None
    # 1 frame de fala zera o silence_run: o segmento NÃO fecha aqui.
    assert seg.push(_frame(1.0), True) is None
    # Agora exige SILENCE_MAX silêncios NOVOS para fechar.
    for i in range(SILENCE_MAX - 1):
        assert seg.push(_frame(0.0), False) is None, f"fechou cedo pós-reset ({i})"
    assert seg.push(_frame(0.0), False) is not None


# --- (d) force-flush ao atingir MAX_SEGMENT_MS ------------------------------


def test_force_flush_em_max_segment():
    seg = VADSegmenter()
    # Fala contínua (sem silêncio): só o teto MÁX pode fechar o segmento.
    for i in range(MAX_FRAMES - 1):
        assert seg.push(_frame(1.0), True) is None, f"fechou antes do teto no frame {i}"
    out = seg.push(_frame(1.0), True)
    assert out is not None
    assert len(out) == MAX_FRAMES * VAD_FRAME_SAMPLES


# --- (e) segmento curto (< MIN_SPEECH) é descartado -------------------------


def test_segmento_curto_e_descartado():
    # Nota: com as constantes de produção um fechamento por hangover sempre traz
    # >= SILENCE_MAX (20) frames >= MIN_SPEECH (13); o guard de descarte por
    # "curto" nunca dispara pelo caminho natural. Para exercitá-lo isolamos o
    # limiar: MESMO roteiro, dois `min_speech` — o padrão emite, o alto descarta.
    script = [(1.0, True, 3), (0.0, False, SILENCE_MAX)]  # 3 frames de fala + hangover

    padrao = _drive(VADSegmenter(), script)
    assert len(padrao) == 1  # 3 + 20 = 23 frames >= MIN_SPEECH → emite

    alto = VADSegmenter()
    alto.min_speech = 1000  # exige mais frames do que o hangover jamais junta
    descartado = _drive(alto, script)
    assert descartado == []  # fecha, mas < min_speech → descartado (retorna None)
    assert alto.in_speech is False  # e o estado volta a repouso após o descarte


# --- (f) é ESTA classe que o app real executa (issue #44) -------------------
#
# Os testes acima só valem alguma coisa se `DirectionWorker._segmenter` — o
# segmentador da UI web e da janela nativa — delegar a política a `VADSegmenter`
# em vez de manter uma cópia inline. A cópia era equivalente linha a linha, o que
# é o pior estado possível: verde na suíte, drift silencioso no produto. Estes
# dois testes travam a delegação para que o próximo refactor não a desfaça em
# silêncio.

_POLICY_NAMES = (
    "PRE_SPEECH_BUFFER_MS",
    "SILENCE_HANGOVER_MS",
    "MIN_SPEECH_MS",
    "MAX_SEGMENT_MS",
    "pre_buf",
    "silence_run",
    "in_speech",
)


def test_direction_worker_delega_a_vad_segmenter():
    src = inspect.getsource(laguna_core.DirectionWorker._segmenter)
    assert "VADSegmenter()" in src, "_segmenter não instancia a máquina de estados testada"
    assert laguna_core.VADSegmenter is VADSegmenter  # a mesma classe, não um homônimo


def test_laguna_core_nao_reimplementa_a_politica_de_vad():
    src = inspect.getsource(laguna_core.DirectionWorker._segmenter)
    duplicados = [n for n in _POLICY_NAMES if n in src]
    assert duplicados == [], f"política de VAD duplicada em laguna_core._segmenter: {duplicados}"
    # E os limiares não voltam pela porta do import (só VADSegmenter os deriva).
    reimportados = [n for n in _POLICY_NAMES[:4] if hasattr(laguna_core, n)]
    assert reimportados == [], f"laguna_core reimporta limiares de VAD: {reimportados}"
