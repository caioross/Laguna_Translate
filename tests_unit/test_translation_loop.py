"""Alvo #45 — resiliencia do laco de traducao (`_translation_loop`).

Antes, o laco inteiro vivia sob o mesmo `try` do setup: uma excecao em UMA frase
(Piper engasgando, hiccup de CUDA) encerrava a direcao pelo resto da sessao — e
sem setar `_stop`, deixando captura/segmentador rodando. Aqui exercitamos o laco
com dubles de STT/MT/TTS: nada de sounddevice, modelo, GPU ou microfone. O
`_play` e neutralizado porque a reproducao ja tem cobertura propria
(`test_output_sink.py`) e depende de sinks/devices.
"""

import queue

import numpy as np

from laguna_core import SEGMENT_MAX_FAILURES, DirectionConfig, DirectionWorker

SEG = np.zeros(160, dtype=np.float32)  # segmento fake; nenhum duble olha o audio


class FakeSTT:
    """STT falso: levanta nas chamadas cujo indice (1-based) esta em `falhas`."""

    def __init__(self, falhas=(), texto="ola mundo", lang="pt") -> None:
        self.falhas = set(falhas)
        self.texto = texto
        self.lang = lang
        self.chamadas = 0

    def transcribe_with_lang(self, seg):
        self.chamadas += 1
        if self.chamadas in self.falhas:
            raise RuntimeError(f"stt explodiu na chamada {self.chamadas}")
        return self.texto, self.lang

    def transcribe(self, seg):
        return self.transcribe_with_lang(seg)[0]


class FakeMT:
    def __init__(self, falhas=()) -> None:
        self.falhas = set(falhas)
        self.chamadas = 0

    def translate(self, text):
        self.chamadas += 1
        if self.chamadas in self.falhas:
            raise RuntimeError(f"mt explodiu na chamada {self.chamadas}")
        return f"[en] {text}"


class FakeTTS:
    sample_rate = 22050

    def __init__(self, falhas=()) -> None:
        self.falhas = set(falhas)
        self.chamadas = 0

    def synthesize(self, text):
        self.chamadas += 1
        if self.chamadas in self.falhas:
            raise RuntimeError(f"tts explodiu na chamada {self.chamadas}")
        return np.zeros(128, dtype=np.float32)


def _worker(eventos: list, **cfg_kw) -> DirectionWorker:
    cfg = DirectionConfig(name="falar", src_lang="pt", tgt_lang="en", **cfg_kw)
    w = DirectionWorker(cfg, on_event=eventos.append)
    w._play = lambda pcm, sr: None  # reproducao tem cobertura propria (#38)
    return w


class _FilaQueEsvazia(queue.Queue):
    """Fila com N segmentos que pede stop ao esvaziar.

    O laco de traducao so sai por `_stop` ou por falha fatal, e aqui nao ha
    thread de captura alimentando a fila. Sinalizar no `get` vazio (o mesmo
    ponto onde o laco real espera) encerra o teste sem timer nem sleep — e sem
    tocar o caminho de excecao, que e justamente o que esta sob teste.
    """

    def __init__(self, worker: DirectionWorker, n: int) -> None:
        super().__init__()
        self._worker = worker
        for _ in range(n):
            self.put(SEG)

    def get(self, *a, **kw):
        try:
            return super().get(block=False)
        except queue.Empty:
            self._worker._stop.set()
            raise


def _rodar(w: DirectionWorker, n: int, stt, mt, tts) -> None:
    """Roda o laco sobre `n` segmentos (ou ate ele encerrar sozinho no fatal)."""
    w._translation_loop(_FilaQueEsvazia(w, n), stt, mt, tts)


def _erros(eventos: list) -> list[dict]:
    return [e for e in eventos if e["kind"] == "error"]


def test_falha_em_um_segmento_nao_impede_o_proximo():
    eventos: list = []
    w = _worker(eventos)
    stt, mt, tts = FakeSTT(falhas=(1,)), FakeMT(), FakeTTS()

    _rodar(w, 2, stt, mt, tts)

    assert stt.chamadas == 2  # o laco chegou ao segundo segmento
    assert tts.chamadas == 1  # e ele foi traduzido ate o fim
    erros = _erros(eventos)
    assert [e["key"] for e in erros] == ["error.segment_failed"]
    assert erros[0]["recoverable"] is True
    assert "stt explodiu" in erros[0]["args"]["detail"]


def test_falha_no_meio_do_pipeline_tambem_e_recuperavel():
    """A guarda tem que cobrir MT e TTS, nao so o STT."""
    eventos: list = []
    w = _worker(eventos)
    stt, mt, tts = FakeSTT(), FakeMT(falhas=(1,)), FakeTTS(falhas=(1,))

    _rodar(w, 3, stt, mt, tts)

    # frase 1: MT falha. frase 2: TTS falha. frase 3: passa inteira.
    assert mt.chamadas == 3 and tts.chamadas == 2
    keys = [e["key"] for e in _erros(eventos)]
    assert keys == ["error.segment_failed", "error.segment_failed"]
    assert any(e["kind"] == "latency" for e in eventos)  # a frase boa mediu


def test_frase_boa_zera_o_contador_de_falhas_consecutivas():
    eventos: list = []
    w = _worker(eventos)
    # falha, sucesso, falha, sucesso, falha: 5 falhas alternadas, nunca 3 seguidas
    stt = FakeSTT(falhas=(1, 3, 5))

    _rodar(w, 5, stt, FakeMT(), FakeTTS())

    keys = [e["key"] for e in _erros(eventos)]
    assert keys == ["error.segment_failed"] * 3  # 3 falhas, nenhuma virou fatal
    assert stt.chamadas == 5  # o laco consumiu a fila inteira: direcao viva


def test_falhas_consecutivas_encerram_a_direcao_e_setam_stop():
    eventos: list = []
    w = _worker(eventos)
    n = SEGMENT_MAX_FAILURES
    stt = FakeSTT(falhas=range(1, n + 1))

    # Fila MAIOR que o limiar: o laco tem que encerrar sozinho no fatal, sem
    # consumir o resto (a rede de seguranca do _rodar nem chega a disparar).
    _rodar(w, n + 2, stt, FakeMT(), FakeTTS())

    assert stt.chamadas == n  # parou no limiar, nao seguiu consumindo
    erros = _erros(eventos)
    assert [e["key"] for e in erros] == (
        ["error.segment_failed"] * (n - 1) + ["error.direction_lost"]
    )
    assert "recoverable" not in erros[-1]  # fatal NAO e recuperavel
    # O ponto da issue: sem isto, _capture/_segmenter viram zumbis segurando o
    # device de captura e enchendo fila com o app "vivo" e sem traduzir.
    assert w._stop.is_set()


def test_parar_no_meio_de_uma_frase_nao_vira_erro():
    """Stop nao e falha: `stop()` aborta streams e o engine pode levantar."""
    eventos: list = []
    w = _worker(eventos)

    class _FalhaNoStop:
        chamadas = 0

        def transcribe_with_lang(self, seg):
            self.chamadas += 1
            w._stop.set()  # como se o usuario tivesse apertado Parar agora
            raise RuntimeError("stream aborted")

    w._translation_loop(_FilaQueEsvazia(w, 2), _FalhaNoStop(), FakeMT(), FakeTTS())

    assert _erros(eventos) == []


def test_texto_vazio_nao_conta_como_falha_nem_emite_evento():
    """Segmento sem fala e caminho normal — nao pode gastar orcamento de falhas."""
    eventos: list = []
    w = _worker(eventos)
    stt = FakeSTT(texto="   ")

    _rodar(w, SEGMENT_MAX_FAILURES + 1, stt, FakeMT(), FakeTTS())

    assert _erros(eventos) == []  # nem recuperavel, nem fatal
    assert stt.chamadas == SEGMENT_MAX_FAILURES + 1  # consumiu a fila inteira
