"""Alvo #2 — conversão de ganho dB → fator linear (laguna_core.py).

O caminho de áudio aplica ganho como fator linear `10 ** (dB / 20)`. Os setters
threadsafe fazem clamp em [-60, +12] dB antes de converter. `DirectionWorker`
computa os fatores no __init__ e NÃO abre streams de áudio ali (isso só ocorre
em `start()`), então dá para exercitar a lógica pura sem tocar dispositivos.
"""

import pytest

from laguna_core import DirectionConfig, DirectionWorker


def _worker(**kw) -> DirectionWorker:
    cfg = DirectionConfig(name="t", src_lang="pt", tgt_lang="en", **kw)
    return DirectionWorker(cfg, on_event=lambda _e: None)


def test_zero_db_e_fator_unitario():
    w = _worker()
    assert w._out_gain == pytest.approx(1.0)
    assert w._pt_gain == pytest.approx(1.0)


def test_formula_no_init():
    w = _worker(output_gain_db=6.0, passthrough_gain_db=-6.0)
    assert w._out_gain == pytest.approx(10.0 ** (6.0 / 20.0))
    assert w._pt_gain == pytest.approx(10.0 ** (-6.0 / 20.0))


def test_setter_output_aplica_formula():
    w = _worker()
    w.set_output_gain_db(12.0)
    assert w._out_gain == pytest.approx(10.0 ** (12.0 / 20.0))
    assert w.cfg.output_gain_db == 12.0


def test_setter_passthrough_aplica_formula():
    w = _worker()
    w.set_passthrough_gain_db(-24.0)
    assert w._pt_gain == pytest.approx(10.0 ** (-24.0 / 20.0))
    assert w.cfg.passthrough_gain_db == -24.0


def test_setter_faz_clamp_no_teto():
    w = _worker()
    w.set_output_gain_db(30.0)  # acima de +12 → clampa em +12
    assert w.cfg.output_gain_db == 12.0
    assert w._out_gain == pytest.approx(10.0 ** (12.0 / 20.0))


def test_setter_faz_clamp_no_piso():
    w = _worker()
    w.set_passthrough_gain_db(-100.0)  # abaixo de -60 → clampa em -60
    assert w.cfg.passthrough_gain_db == -60.0
    assert w._pt_gain == pytest.approx(10.0 ** (-60.0 / 20.0))
