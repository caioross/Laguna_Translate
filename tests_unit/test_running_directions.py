"""Alvo #62 (item 3) — `running` nao pode anunciar direcao morta.

`_workers` so perdia entrada no `/api/stop`. Uma direcao que morre sozinha
(captura perdida, falhas consecutivas, falha de setup) continuava listada, e o
`hello` do WS respondia com ela em `running`: um F5 repintava o painel de verde
"Em execucao", com o Parar habilitado, sobre uma direcao que nao traduz mais.

Sem HTTP nem WS reais: exercitamos `_running_directions()`, a funcao que o
`/api/status` e o `hello` passaram a usar. Os workers sao dubles — o contrato
consumido aqui e so `is_alive()`.
"""

import pytest

import laguna_server


class _WorkerDuble:
    """Duble com o unico contrato que `_running_directions` le."""

    def __init__(self, viva: bool = True) -> None:
        self.viva = viva
        self.stops = 0

    def is_alive(self) -> bool:
        return self.viva

    def stop(self) -> None:  # nunca deve ser chamado no despejo (join custa 2s)
        self.stops += 1


@pytest.fixture(autouse=True)
def _workers_limpos():
    """Isola o dicionario global entre os testes."""
    anterior = dict(laguna_server._workers)
    laguna_server._workers.clear()
    yield
    laguna_server._workers.clear()
    laguna_server._workers.update(anterior)


def test_direcao_viva_continua_listada():
    laguna_server._workers["falar"] = _WorkerDuble()

    assert laguna_server._running_directions() == ["falar"]


def test_direcao_morta_some_de_running_e_do_dicionario():
    morta = _WorkerDuble(viva=False)
    laguna_server._workers["falar"] = morta

    assert laguna_server._running_directions() == []
    # Despejo de verdade: um segundo `hello` nao pode ressuscitar a direcao.
    assert "falar" not in laguna_server._workers
    assert laguna_server._running_directions() == []
    # `stop()` faz join de ate 2s por thread; o worker morto ja fechou os
    # proprios sinks no `finally` de `_run`. Handler async nao paga isso.
    assert morta.stops == 0


def test_uma_direcao_morta_nao_derruba_a_irma_viva():
    laguna_server._workers["falar"] = _WorkerDuble(viva=False)
    laguna_server._workers["escutar"] = _WorkerDuble()

    assert laguna_server._running_directions() == ["escutar"]
    assert list(laguna_server._workers) == ["escutar"]


def test_sem_worker_nenhum_running_e_vazio():
    assert laguna_server._running_directions() == []
