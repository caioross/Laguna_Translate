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


def test_direcao_morta_some_de_running_mas_a_referencia_fica():
    morta = _WorkerDuble(viva=False)
    laguna_server._workers["falar"] = morta

    assert laguna_server._running_directions() == []
    # `is_alive()` e monotonico (`_stop` nunca e limpo): um segundo `hello` nao
    # ressuscita a direcao sem precisar despejar nada.
    assert laguna_server._running_directions() == []
    # A referencia PRECISA sobreviver: quando `_running_directions` roda, o
    # worker acabou de setar `_stop` e ainda pode estar segurando os devices de
    # saida (`_close_sinks()` so roda no fim do `finally` de `_run`). Despeja-la
    # aqui deixaria `/api/stop` e o `existing.stop()` do start sem ninguem para
    # chamar, e o device preso ate o processo morrer (doutrina da #38).
    assert laguna_server._workers["falar"] is morta
    # Consultar quem esta vivo e leitura: `/api/status` e o `hello` nao param
    # worker nenhum (`stop()` faz join de ate 2s por thread).
    assert morta.stops == 0


def test_uma_direcao_morta_nao_derruba_a_irma_viva():
    laguna_server._workers["falar"] = _WorkerDuble(viva=False)
    laguna_server._workers["escutar"] = _WorkerDuble()

    assert laguna_server._running_directions() == ["escutar"]
    assert sorted(laguna_server._workers) == ["escutar", "falar"]


def test_consultar_running_nao_muta_o_dicionario():
    """`/api/status` e GET: sonda de monitoramento ou prefetch nao mexem no estado."""
    laguna_server._workers["falar"] = _WorkerDuble(viva=False)
    laguna_server._workers["escutar"] = _WorkerDuble()
    antes = dict(laguna_server._workers)

    laguna_server._running_directions()

    assert laguna_server._workers == antes


def test_sem_worker_nenhum_running_e_vazio():
    assert laguna_server._running_directions() == []
