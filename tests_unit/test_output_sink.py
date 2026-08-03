"""Alvo #38 — reproducao paralela da traducao (`_OutputSink` / `_play`).

O antigo `_play` tocava em cada device com `sd.play(blocking=True)` em serie: com
fone + CABLE a segunda saida so comecava quando a primeira terminava, e o laco de
traducao ficava bloqueado N x duracao-da-frase. Aqui exercitamos a logica do sink
com um stream FALSO (injetado por `open_stream`) — sem PortAudio, sem device, sem
audio: o `write()` do fake so dorme a "duracao" da frase, que e o que um device
real faz.
"""

import threading
import time

import numpy as np
import pytest

from laguna_core import (
    OUT_SINK_MAX_ERRORS,
    DirectionConfig,
    DirectionWorker,
    _OutputSink,
)

SR = 22050
FRASE_S = 0.15  # "duracao" simulada de uma frase
PCM = np.zeros(int(SR * FRASE_S), dtype=np.float32)


class FakeStream:
    """Stream de saida falso: registra quando cada write comecou e terminou."""

    def __init__(self, device: int, samplerate: int, dur: float = FRASE_S) -> None:
        self.device = device
        self.samplerate = samplerate
        self.dur = dur
        self.closed = False
        self.aborted = False
        self.writes: list[tuple[float, float]] = []  # (t_inicio, t_fim)
        self._lock = threading.Lock()

    def write(self, data) -> None:
        t0 = time.perf_counter()
        time.sleep(self.dur)
        with self._lock:
            self.writes.append((t0, time.perf_counter()))

    def abort(self) -> None:
        self.aborted = True

    def stop(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def _factory(store: dict, dur: float = FRASE_S):
    def _open(device: int, samplerate: int):
        st = FakeStream(device, samplerate, dur)
        store[device] = st
        return st

    return _open


def _wait_streams(store: dict, n: int, timeout: float = 2.0) -> None:
    """Espera os sinks abrirem seus streams (abertura ocorre na thread do sink)."""
    limite = time.perf_counter() + timeout
    while len(store) < n and time.perf_counter() < limite:
        time.sleep(0.005)


def _coletor(errors):
    """Callback `on_error(dev, exc, consecutivas)` que so acumula (issue #54)."""

    def _cb(dev, exc, consecutivas):
        errors.append((dev, exc, consecutivas))

    return _cb


def _sinks(devices, store, errors, dur=FRASE_S):
    return [_OutputSink(d, SR, _coletor(errors), _factory(store, dur)) for d in devices]


def test_abre_stream_no_start_e_reusa_entre_frases():
    store, errors = {}, []
    (sink,) = _sinks([7], store, errors)
    try:
        # Stream aberto ja no start do sink, antes de qualquer frase.
        _wait_streams(store, 1)
        assert store[7].samplerate == SR
        for _ in range(3):
            sink.submit(PCM)
            assert sink.wait(2.0)
        assert len(store[7].writes) == 3  # 3 frases, 1 unico stream aberto
        assert not errors
    finally:
        sink.close()


def test_duas_saidas_comecam_juntas_e_tocam_em_paralelo():
    store, errors = {}, []
    sinks = _sinks([1, 2], store, errors)
    try:
        _wait_streams(store, 2)
        t0 = time.perf_counter()
        for s in sinks:
            s.submit(PCM)
        for s in sinks:
            assert s.wait(2.0)
        gasto = time.perf_counter() - t0

        inicios = [store[d].writes[0][0] for d in (1, 2)]
        # Criterio de aceite: delta na casa de dezenas de ms, nao a frase inteira.
        assert abs(inicios[0] - inicios[1]) < FRASE_S / 2
        # E o bloqueio total nao cresce com o numero de saidas (serie seria 2x).
        assert gasto < FRASE_S * 1.8
        assert not errors
    finally:
        for s in sinks:
            s.close()


def test_falha_de_um_device_nao_impede_os_outros():
    store, errors = {}, []
    bom = _OutputSink(1, SR, _coletor(errors), _factory(store))

    def _quebrado(device: int, samplerate: int):
        raise RuntimeError("device sumiu")

    ruim = _OutputSink(2, SR, _coletor(errors), _quebrado)
    try:
        _wait_streams(store, 1)
        for s in (bom, ruim):
            s.submit(PCM)
        for s in (bom, ruim):
            assert s.wait(2.0)
        assert len(store[1].writes) == 1  # o device bom tocou
        assert errors and all(dev == 2 for dev, _, _ in errors)  # erro so do ruim
    finally:
        bom.close()
        ruim.close()


def test_erro_de_escrita_reabre_stream_na_frase_seguinte():
    errors: list = []
    abertos: list[FakeStream] = []

    class _FalhaUmaVez(FakeStream):
        def write(self, data):
            if len(abertos) == 1:  # so o primeiro stream falha
                raise RuntimeError("write falhou")
            super().write(data)

    def _open(device: int, samplerate: int):
        st = _FalhaUmaVez(device, samplerate)
        abertos.append(st)
        return st

    sink = _OutputSink(3, SR, _coletor(errors), _open)
    try:
        sink.submit(PCM)
        assert sink.wait(2.0)
        assert len(errors) == 1 and abertos[0].closed  # stream ruim foi fechado
        sink.submit(PCM)
        assert sink.wait(2.0)
        assert len(abertos) == 2 and len(abertos[1].writes) == 1  # reabriu e tocou
    finally:
        sink.close()


def test_close_encerra_thread_e_fecha_stream():
    store, errors = {}, []
    (sink,) = _sinks([9], store, errors)
    _wait_streams(store, 1)
    sink.close()
    assert not sink._thread.is_alive()
    assert store[9].closed
    sink.close()  # idempotente: fechar de novo nao levanta


class _AbortaLevantando(FakeStream):
    """Stream que so sai do `write()` quando abortado — e ai levanta.

    E o que o PortAudio faz: `close()` chama `abort()` para desbloquear o write
    em andamento, e o write abortado retorna erro. O `FakeStream` base tem
    `abort()` no-op e `write()` que nunca levanta, entao era cego para isto.
    """

    def __init__(self, device: int, samplerate: int, dur: float = FRASE_S) -> None:
        super().__init__(device, samplerate, dur)
        self.no_write = threading.Event()
        self._abortado = threading.Event()

    def write(self, data) -> None:
        self.no_write.set()
        self._abortado.wait(2.0)
        raise RuntimeError("Error writing to stream: stream aborted")

    def abort(self) -> None:
        self.aborted = True
        self._abortado.set()


def test_parar_com_frase_tocando_nao_vira_erro_de_device():
    """Stop nao e falha: `error.play` num Stop normal travava a UI (app.js trata
    erro como terminal) — e trocar de config faz o servidor parar o worker."""
    errors, criados = [], []

    def _open(device: int, samplerate: int):
        st = _AbortaLevantando(device, samplerate)
        criados.append(st)
        return st

    sink = _OutputSink(11, SR, _coletor(errors), _open)
    sink.submit(PCM)
    assert criados[0].no_write.wait(2.0)  # thread esta DENTRO do write
    sink.close()

    assert criados[0].aborted  # o abort e o que desbloqueia o write
    assert not sink._thread.is_alive()  # e a thread realmente encerrou
    assert errors == []  # o write abortado pelo stop NAO vira error.play


def test_falha_de_device_fora_do_stop_continua_reportada():
    """Contraprova do teste acima: o guarda de shutdown nao pode calar erro real."""
    errors: list = []

    class _SempreFalha(FakeStream):
        def write(self, data):
            raise RuntimeError("device sumiu no meio da frase")

    sink = _OutputSink(12, SR, _coletor(errors), lambda d, sr: _SempreFalha(d, sr))
    try:
        sink.submit(PCM)
        assert sink.wait(2.0)
        assert [dev for dev, _, _ in errors] == [12]  # sem stop: erro chega a UI
    finally:
        sink.close()


def test_play_do_worker_nao_cresce_com_o_numero_de_saidas():
    store, errors = {}, []
    cfg = DirectionConfig(name="t", src_lang="pt", tgt_lang="en")
    w = DirectionWorker(cfg, on_event=lambda _e: None)

    def _tempo_de_play(n_saidas: int) -> float:
        w._sinks = _sinks(list(range(100, 100 + n_saidas)), store, errors)
        _wait_streams(store, n_saidas)
        try:
            t0 = time.perf_counter()
            w._play(PCM, SR)
            return time.perf_counter() - t0
        finally:
            w._close_sinks()

    uma = _tempo_de_play(1)
    store.clear()
    duas = _tempo_de_play(2)
    assert uma == pytest.approx(FRASE_S, abs=FRASE_S)
    assert duas < uma * 1.8  # em serie seria ~2x
    assert not errors
    assert w._sinks == []  # _close_sinks nao deixa sink pendurado


# --- issue #54: soluco de UM device nao pode matar a direcao -----------------


def _worker():
    """Worker inerte (nenhuma thread sobe no __init__) + lista de eventos."""
    eventos: list[dict] = []
    cfg = DirectionConfig(name="falar", src_lang="pt", tgt_lang="en")
    return DirectionWorker(cfg, on_event=eventos.append), eventos


def test_falha_isolada_de_saida_e_recuperavel_e_nao_para_a_direcao():
    """O bug da #54: uma falha virava `error.play`, e app.js trata erro
    nao-recuperavel como fim de sessao — Stop desabilitado com o worker vivo."""
    w, eventos = _worker()
    w._on_sink_error(5, RuntimeError("power save"), 1)

    (ev,) = eventos
    assert ev["kind"] == "error" and ev["key"] == "error.output_retry"
    assert ev["recoverable"] is True  # aviso ambar; painel segue 'rodando'
    assert ev["args"] == {
        "dev": 5,
        "attempt": 1,
        "max": OUT_SINK_MAX_ERRORS,
        "detail": "power save",
    }


def test_falhas_consecutivas_ate_o_teto_viram_erro_terminal_com_o_device():
    w, eventos = _worker()
    for n in range(1, OUT_SINK_MAX_ERRORS + 1):
        w._on_sink_error(7, RuntimeError("device sumiu"), n)

    chaves = [e["key"] for e in eventos]
    assert chaves == ["error.output_retry"] * (OUT_SINK_MAX_ERRORS - 1) + ["error.play"]
    fatal = eventos[-1]
    assert not fatal.get("recoverable")  # terminal: agora a saida esta perdida
    assert fatal["args"]["dev"] == 7 and fatal["args"]["max"] == OUT_SINK_MAX_ERRORS


def test_frase_tocada_zera_o_orcamento_do_device():
    """Falhas ESPACADAS (um soluco por hora) nunca podem somar ate o teto."""
    errors: list = []
    falhar = {"v": True}

    class _Controlado(FakeStream):
        def write(self, data):
            if falhar["v"]:
                raise RuntimeError("device engasgou")
            super().write(data)

    sink = _OutputSink(21, SR, _coletor(errors), lambda d, sr: _Controlado(d, sr))
    try:
        for _ in range(2):
            sink.submit(PCM)
            assert sink.wait(2.0)
        assert [n for _, _, n in errors] == [1, 2]

        falhar["v"] = False  # frase boa no meio
        sink.submit(PCM)
        assert sink.wait(2.0)

        falhar["v"] = True
        sink.submit(PCM)
        assert sink.wait(2.0)
        # 1, nao 3: o sucesso zerou o contador e a direcao nao morre.
        assert [n for _, _, n in errors] == [1, 2, 1]
    finally:
        sink.close()


def test_orcamento_e_por_device_e_nao_contamina_o_vizinho():
    errors: list = []

    class _SempreFalha(FakeStream):
        def write(self, data):
            raise RuntimeError("device sumiu")

    a = _OutputSink(31, SR, _coletor(errors), lambda d, sr: _SempreFalha(d, sr))
    b = _OutputSink(32, SR, _coletor(errors), lambda d, sr: _SempreFalha(d, sr))
    try:
        for _ in range(2):
            a.submit(PCM)
            assert a.wait(2.0)
        b.submit(PCM)
        assert b.wait(2.0)

        assert [n for dev, _, n in errors if dev == 31] == [1, 2]
        assert [n for dev, _, n in errors if dev == 32] == [1]  # vizinho no zero
    finally:
        a.close()
        b.close()
