"""Alvo #47 — filtro de alucinacao do STT (`is_hallucination`).

Quando o VAD fecha um segmento sem fala de verdade, o faster-whisper nao devolve
vazio: devolve uma frase plausivel ("Legendas pela comunidade Amara.org"). Como o
Laguna FALA a traducao no cabo virtual, isso vira o app conversando sozinho na
call. Aqui exercitamos a decisao pura — nenhum audio, modelo, GPU ou rede — e o
laco de traducao com dubles, para provar que o segmento reprovado nao chega ao
MT/TTS mas ainda avisa a UI.

O vies dos testes acompanha o vies do filtro: barrar fala real e pior que deixar
passar ruido, entao os casos de "NAO pode barrar" sao tao importantes quanto os
de "tem que barrar".
"""

import queue

import numpy as np
import pytest

from laguna_core import DirectionConfig, DirectionWorker
from laguna_pipeline import (
    AVG_LOGPROB_MIN,
    NO_SPEECH_PROB_MAX,
    STTResult,
    _aggregate_segments,
    is_hallucination,
    normalize_for_filter,
)

# ---------------------------------------------------------------- normalizacao


def test_normalizacao_ignora_caixa_acento_e_pontuacao():
    assert normalize_for_filter("Obrigado por assistir!") == "obrigado por assistir"
    assert normalize_for_filter("  Até   o próximo VÍDEO...  ") == "ate o proximo video"


def test_normalizacao_de_texto_so_com_pontuacao_fica_vazia():
    assert normalize_for_filter("...") == ""
    assert normalize_for_filter("?!") == ""


# ------------------------------------------------------------------- blocklist


@pytest.mark.parametrize(
    "texto",
    [
        "Legendas pela comunidade Amara.org",
        "legendas pela comunidade Amara.org.",
        "Obrigado por assistir!",
        "Até o próximo vídeo.",
        "Tchau!",
        "Subtitles by the Amara.org community",
        "Thank you.",
        "Thanks for watching!",
        "Bye.",
        "You",
        "...",
    ],
)
def test_frases_lixo_conhecidas_sao_barradas(texto):
    assert is_hallucination(texto) is True


@pytest.mark.parametrize(
    "texto",
    [
        "sim",
        "ok",
        "yes",
        "não",
        "Obrigado",  # sozinho e fala legitima — so o "obrigado por assistir" e lixo
        "Thank you very much for the help with the build",
        "vou entrar na call agora",
        "consegue me ouvir",
        "beleza, fechado",
    ],
)
def test_fala_legitima_nunca_e_barrada(texto):
    assert is_hallucination(texto) is False


# ------------------------------------------------------------------ repeticao


@pytest.mark.parametrize(
    "texto",
    [
        "sim sim sim sim",
        "sim, sim, sim, sim, sim!",
        "não não não não não não não",
        "sim não sim não sim não",  # loop curto alternado
    ],
)
def test_repeticao_patologica_e_barrada(texto):
    assert is_hallucination(texto) is True


@pytest.mark.parametrize(
    "texto",
    [
        "sim sim",  # enfase normal: 2 palavras nao bastam
        "sim sim sim",  # 3 tambem nao — o limiar comeca em 4
        "eu quero muito muito isso",
        "vai vai vai time",
    ],
)
def test_enfase_curta_nao_e_repeticao(texto):
    assert is_hallucination(texto) is False


# ------------------------------------------------------------------ metadados


def test_metadados_ruins_nos_dois_eixos_barram():
    assert is_hallucination("uma frase qualquer", NO_SPEECH_PROB_MAX + 0.1, AVG_LOGPROB_MIN - 0.1) is True


@pytest.mark.parametrize(
    "no_speech, logprob",
    [
        (NO_SPEECH_PROB_MAX, AVG_LOGPROB_MIN - 0.5),  # borda: nao passa do maximo
        (NO_SPEECH_PROB_MAX + 0.1, AVG_LOGPROB_MIN),  # borda: nao fica abaixo do minimo
        (NO_SPEECH_PROB_MAX + 0.3, -0.2),  # so no_speech ruim: logprob salva
        (0.05, AVG_LOGPROB_MIN - 0.5),  # so logprob ruim: frase curta legitima
    ],
)
def test_um_eixo_so_nao_derruba_fala(no_speech, logprob):
    """A conjuncao e deliberada: qualquer um dos dois sozinho barra fala real."""
    assert is_hallucination("uma frase qualquer", no_speech, logprob) is False


def test_metadado_ausente_nao_opina():
    assert is_hallucination("uma frase qualquer", None, None) is False
    assert is_hallucination("uma frase qualquer", 0.99, None) is False
    assert is_hallucination("uma frase qualquer", None, -5.0) is False


def test_blocklist_vence_metadado_bom():
    """O caso Amara costuma vir com logprob otimo — por isso a segunda camada."""
    assert is_hallucination("Legendas pela comunidade Amara.org", 0.01, -0.1) is True


# ----------------------------------------------------- agregacao dos segmentos


class _Seg:
    def __init__(self, text, start, end, no_speech_prob=None, avg_logprob=None):
        self.text = text
        self.start = start
        self.end = end
        self.no_speech_prob = no_speech_prob
        self.avg_logprob = avg_logprob


def test_agregacao_pondera_por_duracao():
    # 3s com no_speech 0.1 e 1s com 0.9 -> (0.1*3 + 0.9*1)/4 = 0.3
    texto, ns, lp = _aggregate_segments(
        [_Seg(" olá ", 0.0, 3.0, 0.1, -0.2), _Seg("mundo", 3.0, 4.0, 0.9, -1.0)]
    )
    assert texto == "olá mundo"
    assert ns == pytest.approx(0.3)
    assert lp == pytest.approx((-0.2 * 3 + -1.0 * 1) / 4)


def test_agregacao_sem_metadados_devolve_none():
    texto, ns, lp = _aggregate_segments([_Seg("olá", 0.0, 1.0)])
    assert (texto, ns, lp) == ("olá", None, None)


def test_agregacao_de_lista_vazia_nao_explode():
    assert _aggregate_segments([]) == ("", None, None)


def test_agregacao_com_duracao_zerada_usa_peso_neutro():
    """Duble/segmento degenerado nao pode virar divisao por zero no caminho quente."""
    _, ns, _ = _aggregate_segments([_Seg("a", 0.0, 0.0, 0.2), _Seg("b", 0.0, 0.0, 0.4)])
    assert ns == pytest.approx(0.3)


# ------------------------------------------- integracao com o laco de traducao


class _STTFixo:
    def __init__(self, res: STTResult) -> None:
        self.res = res

    def transcribe_detailed(self, seg, detect_lang=False):
        return self.res


class _MTContador:
    def __init__(self) -> None:
        self.chamadas = 0

    def translate(self, text):
        self.chamadas += 1
        return f"[en] {text}"


class _TTSContador:
    sample_rate = 22050

    def __init__(self) -> None:
        self.chamadas = 0

    def synthesize(self, text):
        self.chamadas += 1
        return np.zeros(64, dtype=np.float32)


def _rodar_um_segmento(res: STTResult):
    eventos: list = []
    cfg = DirectionConfig(name="falar", src_lang="pt", tgt_lang="en")
    w = DirectionWorker(cfg, on_event=eventos.append)
    tocou: list = []
    w._play = lambda pcm, sr: tocou.append(pcm)
    mt, tts = _MTContador(), _TTSContador()
    w._process_segment(np.zeros(160, dtype=np.float32), _STTFixo(res), mt, tts)
    return eventos, mt, tts, tocou


def test_alucinacao_nao_vira_fala_mas_avisa_a_ui():
    eventos, mt, tts, tocou = _rodar_um_segmento(
        STTResult(text="Legendas pela comunidade Amara.org", language="pt", no_speech_prob=0.1, avg_logprob=-0.3)
    )

    assert mt.chamadas == 0 and tts.chamadas == 0 and tocou == []  # nada saiu no cabo
    kinds = [e["kind"] for e in eventos]
    assert "stt" in kinds  # o usuario ve O QUE foi barrado...
    skipped = [e for e in eventos if e["kind"] == "skipped"]
    assert len(skipped) == 1  # ...e que foi descartado: painel nao fica mudo
    assert skipped[0]["key"] == "mt.hallucination"
    assert skipped[0]["reason"] == "hallucination"


def test_fala_real_continua_passando_inteira():
    eventos, mt, tts, tocou = _rodar_um_segmento(
        STTResult(text="bom dia, consegue me ouvir?", language="pt", no_speech_prob=0.05, avg_logprob=-0.3)
    )

    assert mt.chamadas == 1 and tts.chamadas == 1 and len(tocou) == 1
    assert [e for e in eventos if e["kind"] == "skipped"] == []


def test_segmento_barrado_ainda_mede_latencia_de_stt():
    """A metrica nao pode mentir por omissao (#21): o STT rodou, entao contou."""
    eventos, _, _, _ = _rodar_um_segmento(
        STTResult(text="...", language="pt", no_speech_prob=0.9, avg_logprob=-2.0)
    )
    assert any(e["kind"] == "latency" for e in eventos)
