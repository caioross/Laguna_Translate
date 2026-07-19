"""Issue #21 — contador/throttle de descartes de fila (`DropCounter`).

Sob sobrecarga, `DirectionWorker` descarta frames (audio_q) e segmentos (seg_q)
para preservar o tempo real. Antes, o descarte era um `except queue.Full: pass`
mudo — a UI seguia "Ouvindo" e a latencia parecia boa porque so media o que
sobreviveu. `DropCounter` conta os descartes e decide, com throttle, quando
emitir o evento `overload`.

E 100% puro: recebe o instante (`now`) por parametro (sem `time.perf_counter`
real), sem tocar audio/modelo/rede — segue o padrao de `tests_unit/`.
"""

import pytest

from laguna_core import DropCounter


def test_conta_frames_e_segmentos_independentes():
    dc = DropCounter()
    dc.record("frame", now=0.0)
    dc.record("frame", now=0.0)
    dc.record("segment", now=0.0)
    assert dc.frames == 2
    assert dc.segments == 1


def test_primeiro_descarte_de_cada_tipo_sinaliza_first():
    dc = DropCounter()
    _, first_f1 = dc.record("frame", now=0.0)
    _, first_f2 = dc.record("frame", now=0.0)
    _, first_s1 = dc.record("segment", now=0.0)
    assert first_f1 is True   # 1o frame
    assert first_f2 is False  # 2o frame ja nao e first
    assert first_s1 is True   # 1o segmento e independente do frame


def test_primeiro_descarte_sempre_emite():
    dc = DropCounter(min_emit_interval=1.0)
    should_emit, _ = dc.record("frame", now=1234.5)
    assert should_emit is True  # `_last_emit` inicial = -inf


def test_throttle_suprime_dentro_da_janela():
    dc = DropCounter(min_emit_interval=1.0)
    e0, _ = dc.record("frame", now=10.0)   # emite
    e1, _ = dc.record("frame", now=10.3)   # <1s depois -> suprime
    e2, _ = dc.record("frame", now=10.9)   # ainda <1s do ultimo emit -> suprime
    assert (e0, e1, e2) == (True, False, False)


def test_throttle_reabre_apos_intervalo():
    dc = DropCounter(min_emit_interval=1.0)
    dc.record("frame", now=10.0)           # emite (last_emit=10.0)
    e_mid, _ = dc.record("frame", now=10.5)  # suprime
    e_after, _ = dc.record("frame", now=11.0)  # exatamente 1s depois -> emite
    assert e_mid is False
    assert e_after is True


def test_throttle_e_global_entre_frame_e_segmento():
    # O throttle governa a EMISSAO do evento overload, nao cada tipo:
    # um segmento logo apos um frame ainda cai na janela.
    dc = DropCounter(min_emit_interval=1.0)
    e_frame, _ = dc.record("frame", now=5.0)
    e_seg, _ = dc.record("segment", now=5.2)
    assert e_frame is True
    assert e_seg is False


def test_contadores_acumulam_mesmo_com_emissao_suprimida():
    # Suprimir o evento NAO pode perder a contagem — o payload sempre leva o total.
    dc = DropCounter(min_emit_interval=1.0)
    for _ in range(50):
        dc.record("frame", now=10.0)  # so o 1o emite; todos contam
    assert dc.frames == 50


def test_kind_invalido_levanta():
    dc = DropCounter()
    with pytest.raises(ValueError):
        dc.record("bogus", now=0.0)
