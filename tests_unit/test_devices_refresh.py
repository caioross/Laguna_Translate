"""Alvo #37 — `/api/devices?refresh=1` re-inicializa o PortAudio (com guarda).

O PortAudio enumera os devices uma vez por processo: sem re-init, o botao 🔄
mostra eternamente a mesma lista congelada. O endpoint passa a fazer o ciclo
`_terminate()/_initialize()` — mas NUNCA com worker vivo, porque o ciclo derruba
streams e reatribui os indices que o worker guarda como int.

Nada aqui toca audio real: o `conftest.py` ja substitui `sounddevice` por um
stub vazio e aqui injetamos um fake mais rico em `laguna_devices.sd` (mesmo
padrao de `test_validate_config.py`). `api_devices` e async e as rotas viram
funcoes normais sob o stub de FastAPI do conftest — chamamos via `asyncio.run`.
"""

from __future__ import annotations

import asyncio

import pytest

import laguna_devices
import laguna_server


class _FakeSd:
    """Fake do sounddevice: conta o ciclo de re-init e troca a lista ao aplica-lo."""

    def __init__(self, devices, devices_after_reinit=None, has_private_api=True):
        self._devices = devices
        self._after = devices_after_reinit
        self.terminate_calls = 0
        self.initialize_calls = 0
        if has_private_api:
            self._terminate = self._do_terminate
            self._initialize = self._do_initialize

    def _do_terminate(self):
        self.terminate_calls += 1

    def _do_initialize(self):
        self.initialize_calls += 1
        # o hardware novo so aparece depois do ciclo completo
        if self._after is not None:
            self._devices = self._after

    def query_devices(self, idx=None):
        return self._devices if idx is None else self._devices[idx]

    def query_hostapis(self):
        return [{"name": "Windows WASAPI"}]


_MIC = {"name": "Mic", "hostapi": 0, "max_input_channels": 2, "max_output_channels": 0}
_SPK = {"name": "Speakers", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2}
# device que so existe depois do re-init (ex.: CABLE Output renomeado)
_NEW = {"name": "Laguna Translator Mic", "hostapi": 0, "max_input_channels": 2, "max_output_channels": 0}


def _call_devices(refresh):
    return asyncio.run(laguna_server.api_devices(refresh=refresh)).content


@pytest.fixture
def no_workers():
    """Garante estado limpo de `_workers` (global do modulo) antes e depois."""
    saved = dict(laguna_server._workers)
    laguna_server._workers.clear()
    yield laguna_server._workers
    laguna_server._workers.clear()
    laguna_server._workers.update(saved)


def _install(monkeypatch, fake):
    monkeypatch.setattr(laguna_devices, "sd", fake)
    return fake


def test_refresh_sem_workers_reinicializa_e_ve_device_novo(monkeypatch, no_workers):
    fake = _install(monkeypatch, _FakeSd([_MIC, _SPK], devices_after_reinit=[_MIC, _SPK, _NEW]))

    data = _call_devices(1)

    assert (fake.terminate_calls, fake.initialize_calls) == (1, 1)
    assert data["refresh"] == {"requested": True, "applied": True, "reason": None}
    assert [d["name"] for d in data["inputs"]] == ["Mic", "Laguna Translator Mic"]
    # o badge 🌊 depende dessa deteccao — e o motivo pratico do refresh
    assert data["laguna"]["has_laguna_name"] is True


def test_sem_parametro_nao_reinicializa(monkeypatch, no_workers):
    fake = _install(monkeypatch, _FakeSd([_MIC, _SPK], devices_after_reinit=[_MIC, _SPK, _NEW]))

    data = _call_devices(0)

    # boot da UI continua barato: nada de Pa_Terminate/Pa_Initialize
    assert (fake.terminate_calls, fake.initialize_calls) == (0, 0)
    assert data["refresh"] == {"requested": False, "applied": False, "reason": None}
    assert [d["name"] for d in data["inputs"]] == ["Mic"]


def test_refresh_com_worker_rodando_nao_reinicializa(monkeypatch, no_workers):
    fake = _install(monkeypatch, _FakeSd([_MIC, _SPK], devices_after_reinit=[_MIC, _SPK, _NEW]))
    no_workers["falar"] = object()  # worker vivo: indices dele nao podem mudar

    data = _call_devices(1)

    assert (fake.terminate_calls, fake.initialize_calls) == (0, 0)
    assert data["refresh"] == {"requested": True, "applied": False, "reason": "workers_running"}
    # lista intacta -> a UI explica o porque em vez de mentir que atualizou
    assert [d["name"] for d in data["inputs"]] == ["Mic"]


def test_sounddevice_sem_api_privada_degrada_sem_excecao(monkeypatch, no_workers):
    fake = _install(monkeypatch, _FakeSd([_MIC, _SPK], has_private_api=False))

    data = _call_devices(1)

    assert data["refresh"] == {"requested": True, "applied": False, "reason": "unsupported"}
    assert [d["name"] for d in data["inputs"]] == ["Mic"]


def test_falha_no_ciclo_de_reinit_nao_quebra_o_endpoint(monkeypatch, no_workers):
    fake = _install(monkeypatch, _FakeSd([_MIC, _SPK]))
    monkeypatch.setattr(fake, "_initialize", _raise, raising=False)

    data = _call_devices(1)

    assert data["refresh"] == {"requested": True, "applied": False, "reason": "unsupported"}
    assert [d["name"] for d in data["inputs"]] == ["Mic"]


def _raise():
    raise RuntimeError("PortAudio nao inicializou")


def test_reinit_portaudio_isolado_reporta_sucesso(monkeypatch):
    fake = _install(monkeypatch, _FakeSd([_MIC]))
    assert laguna_devices.reinit_portaudio() is True
    assert (fake.terminate_calls, fake.initialize_calls) == (1, 1)
