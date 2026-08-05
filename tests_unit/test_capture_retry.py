"""Alvo #62 (item 1) — captura perdida encerra a direcao, nao vira zumbi.

O laco de reconexao da captura (#12) esgota `CAPTURE_MAX_RETRIES` e sai — mas
saia sem setar `_stop` e o segmentador, o laco de traducao e os `_OutputSink`
seguem girando para sempre com a fila vazia: o app fica "vivo", nada traduz e os
devices continuam presos. E a mesma doutrina que o #45 aplicou ao laco de
traducao (ver `test_translation_loop.py`), agora na porta ao lado.

Sem sounddevice real: o `sd` importado por `laguna_core` e o stub do conftest e
aqui recebe um `InputStream` duble. Backoff zerado para o teste nao dormir.
"""

import pytest

import laguna_core
from laguna_core import CAPTURE_MAX_RETRIES, DirectionConfig, DirectionWorker


@pytest.fixture(autouse=True)
def _sem_backoff(monkeypatch):
    """Retry imediato: o que esta sob teste e o desfecho, nao a espera."""
    monkeypatch.setattr(laguna_core, "CAPTURE_BACKOFF_S", 0.0)
    monkeypatch.setattr(laguna_core, "CAPTURE_MAX_BACKOFF_S", 0.0)


class _StreamQueAbre:
    """InputStream duble que abre — e para a direcao ao ser usado.

    O laco real so sai do `with` quando `_stop` e setado (usuario apertando
    Parar); sinalizar no `__enter__` encerra o teste sem timer nem sleep, sem
    tocar o caminho de excecao.
    """

    def __init__(self, worker: DirectionWorker) -> None:
        self._worker = worker

    def __enter__(self):
        self._worker._stop.set()
        return self

    def __exit__(self, *exc):
        return False


def _worker(eventos: list) -> DirectionWorker:
    cfg = DirectionConfig(name="falar", src_lang="pt", tgt_lang="en")
    return DirectionWorker(cfg, on_event=eventos.append)


def _erros(eventos: list) -> list[dict]:
    return [e for e in eventos if e["kind"] == "error"]


def _chaves(eventos: list, kind: str) -> list[str]:
    return [e["key"] for e in eventos if e["kind"] == kind and "key" in e]


def test_captura_perdida_seta_stop_e_emite_erro_terminal(monkeypatch):
    eventos: list = []
    w = _worker(eventos)
    aberturas = {"n": 0}

    def _sempre_falha(**_kw):
        aberturas["n"] += 1
        raise RuntimeError("device sumiu")

    monkeypatch.setattr(laguna_core.sd, "InputStream", _sempre_falha, raising=False)

    w._capture(laguna_core.queue.Queue())

    assert aberturas["n"] == CAPTURE_MAX_RETRIES  # gastou o orcamento inteiro
    erros = _erros(eventos)
    assert [e["key"] for e in erros] == ["error.capture_lost"]
    assert "recoverable" not in erros[0]  # captura perdida NAO e recuperavel
    # O ponto da issue: sem isto, segmentador/traducao/sinks viram zumbis.
    assert w._stop.is_set()
    assert w.is_alive() is False


def test_falha_transitoria_nao_encerra_a_direcao(monkeypatch):
    """A morte e so no fim do orcamento: uma queda isolada segue recuperavel."""
    eventos: list = []
    w = _worker(eventos)
    aberturas = {"n": 0}

    def _falha_uma_vez(**_kw):
        aberturas["n"] += 1
        if aberturas["n"] == 1:
            raise RuntimeError("hiccup do driver")
        return _StreamQueAbre(w)

    monkeypatch.setattr(laguna_core.sd, "InputStream", _falha_uma_vez, raising=False)

    w._capture(laguna_core.queue.Queue())

    assert aberturas["n"] == 2  # reabriu e seguiu ouvindo
    assert _erros(eventos) == []  # nenhum erro terminal pela queda isolada
    assert "status.capture_retry" in _chaves(eventos, "status")


def test_stop_do_usuario_durante_a_captura_nao_vira_erro(monkeypatch):
    eventos: list = []
    w = _worker(eventos)

    monkeypatch.setattr(
        laguna_core.sd, "InputStream", lambda **_kw: _StreamQueAbre(w), raising=False
    )

    w._capture(laguna_core.queue.Queue())

    assert _erros(eventos) == []


def test_falha_de_setup_tambem_encerra_a_direcao(monkeypatch):
    """`_run` que morre no setup (modelo que nao carrega) nao pode ficar 'viva'.

    Este caminho nunca chega ao laco de traducao, entao ate a #62 saia de `_run`
    com `_stop` limpo — worker morto que o servidor seguia listando em `running`.
    """
    eventos: list = []
    w = _worker(eventos)

    def _explode(*_a, **_k):
        raise RuntimeError("modelo nao carregou")

    monkeypatch.setattr(laguna_core, "_resolve_device", _explode)

    w._run()

    assert _erros(eventos)  # o erro de setup continua sendo reportado
    assert w.is_alive() is False


def test_direcao_recem_criada_esta_viva():
    """Guarda-corpo: `is_alive` nao pode nascer False (o servidor a consulta)."""
    assert _worker([]).is_alive() is True
