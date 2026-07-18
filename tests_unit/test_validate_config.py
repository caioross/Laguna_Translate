"""Alvo #26 — validacao de devices no start (laguna_server.validate_direction_config).

`api_start` deve rejeitar (HTTP 400) uma config com device inexistente, device
de saida usado como captura, indice stale ou saida sem canal — ANTES de criar o
worker — em vez de deixar o erro chegar cru (PaErrorCode) la no thread _capture.

A funcao e pura: so consulta `sd.query_devices` / `sd.check_*` (nunca abre stream
nem carrega modelo), entao da para exercita-la com um `sd` falso, sem PortAudio
nem hardware. O `conftest.py` ja substitui `sounddevice` por um stub vazio; aqui
injetamos um fake mais rico em `laguna_server.sd`.
"""

from __future__ import annotations

import pytest

import laguna_server
from laguna_core import DirectionConfig


class _FakePortAudioError(Exception):
    pass


class _FakeWasapiSettings:
    """Simula a WasapiSettings do sounddevice 0.5.x: NAO aceita `loopback`."""

    def __init__(self, **kwargs):
        if "loopback" in kwargs:
            raise TypeError("WasapiSettings.__init__() got an unexpected keyword argument 'loopback'")


class _FakeSd:
    PortAudioError = _FakePortAudioError
    WasapiSettings = _FakeWasapiSettings

    def __init__(self, devices: list[dict]):
        self._devices = devices

    def query_devices(self, idx=None):
        if idx is None:
            return self._devices
        if not (0 <= idx < len(self._devices)):
            raise self.PortAudioError(f"Error querying device {idx}")
        return self._devices[idx]

    def check_input_settings(self, device=None, channels=None, dtype=None,
                             extra_settings=None, samplerate=None):
        d = self._devices[device]
        if int(d.get("max_input_channels") or 0) < int(channels or 1):
            raise self.PortAudioError("Invalid number of channels [PaErrorCode -9998]")

    def check_output_settings(self, device=None, channels=None, dtype=None,
                              extra_settings=None, samplerate=None):
        d = self._devices[device]
        if int(d.get("max_output_channels") or 0) < int(channels or 1):
            raise self.PortAudioError("Invalid number of channels [PaErrorCode -9998]")


# Layout de devices: 0=mic (so entrada), 1=alto-falante (so saida),
# 2=cabo virtual render (so saida, alvo de loopback).
_DEVICES = [
    {"name": "Mic", "max_input_channels": 2, "max_output_channels": 0},
    {"name": "Speakers", "max_input_channels": 0, "max_output_channels": 2},
    {"name": "Virtual Cable (render)", "max_input_channels": 0, "max_output_channels": 2},
]


@pytest.fixture
def fake_sd(monkeypatch):
    fake = _FakeSd(_DEVICES)
    monkeypatch.setattr(laguna_server, "sd", fake)
    return fake


def _cfg(**kw) -> DirectionConfig:
    base = dict(name="falar", src_lang="pt", tgt_lang="en")
    base.update(kw)
    return DirectionConfig(**base)


def _keys(errors) -> list[str]:
    return [e["error_key"] for e in errors]


def test_config_valida_sem_erros(fake_sd):
    cfg = _cfg(capture_device=0, output_devices=[1])
    assert laguna_server.validate_direction_config(cfg) == []


def test_captura_indice_inexistente(fake_sd):
    errors = laguna_server.validate_direction_config(_cfg(capture_device=99, output_devices=[1]))
    assert _keys(errors) == ["error.start_capture_device_invalid"]
    assert errors[0]["args"]["device"] == "99"


def test_captura_usa_device_de_saida(fake_sd):
    # device 1 so tem saida -> check_input_settings rejeita (0 canais de entrada)
    errors = laguna_server.validate_direction_config(_cfg(capture_device=1, output_devices=[1]))
    assert _keys(errors) == ["error.start_capture_device_invalid"]


def test_saida_indice_inexistente(fake_sd):
    errors = laguna_server.validate_direction_config(_cfg(capture_device=0, output_devices=[99]))
    assert _keys(errors) == ["error.start_output_device_invalid"]
    assert errors[0]["args"]["device"] == "99"


def test_saida_sem_canal_de_saida(fake_sd):
    # device 0 e so entrada -> nao serve como saida
    errors = laguna_server.validate_direction_config(_cfg(capture_device=0, output_devices=[0]))
    assert _keys(errors) == ["error.start_output_device_invalid"]


def test_loopback_em_endpoint_de_saida_ok(fake_sd):
    # captura loopback de um device de render valido (device 2): sem erro.
    # A checagem profunda degrada (WasapiSettings desta versao nao tem loopback).
    cfg = _cfg(name="escutar", capture_device=2, use_loopback=True, output_devices=[1])
    assert laguna_server.validate_direction_config(cfg) == []


def test_loopback_em_device_sem_saida_invalido(fake_sd):
    # loopback exige um endpoint de saida; device 0 (so entrada) nao serve.
    cfg = _cfg(name="escutar", capture_device=0, use_loopback=True, output_devices=[1])
    assert _keys(laguna_server.validate_direction_config(cfg)) == ["error.start_capture_device_invalid"]


def test_passthrough_device_invalido(fake_sd):
    cfg = _cfg(capture_device=0, output_devices=[1], passthrough=True, passthrough_device=99)
    assert _keys(laguna_server.validate_direction_config(cfg)) == ["error.start_output_device_invalid"]


def test_passthrough_none_nao_valida(fake_sd):
    # passthrough ativo mas sem device: nao e fatal aqui (o _passthrough_run
    # trata) -> nao gera erro de validacao.
    cfg = _cfg(capture_device=0, output_devices=[1], passthrough=True, passthrough_device=None)
    assert laguna_server.validate_direction_config(cfg) == []


def test_multiplos_erros_acumulam(fake_sd):
    errors = laguna_server.validate_direction_config(_cfg(capture_device=99, output_devices=[99, 0]))
    assert _keys(errors) == [
        "error.start_capture_device_invalid",
        "error.start_output_device_invalid",
        "error.start_output_device_invalid",
    ]


def test_falha_do_validador_degrada_para_vazio(monkeypatch):
    # Se o proprio validador quebra (ex.: query_devices explode), nunca bloqueia
    # o start: retorna [] ("nao validado").
    class _Boom:
        PortAudioError = _FakePortAudioError

        def query_devices(self, idx=None):
            raise RuntimeError("PortAudio indisponivel")

    monkeypatch.setattr(laguna_server, "sd", _Boom())
    assert laguna_server.validate_direction_config(_cfg(capture_device=0)) == []
