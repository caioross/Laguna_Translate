"""Laguna Translator — core bidirecional.

Cada DirectionWorker representa uma direcao independente (FALAR ou ESCUTAR).
Modelos sao carregados lazy por worker. Eventos sao despachados via callback
on_event(dict) — a camada servidor (laguna_server.py) converte em WebSocket.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import sounddevice as sd

from laguna_pipeline import (
    ArgosMT,
    PiperTTS,
    SAMPLE_RATE,
    STT,
    VAD_FRAME_SAMPLES,
    VADSegmenter,
    WebRTCVADGate,
    detect_device,
    log,
)

# Reexport por compat (issue #40): os helpers de dispositivo saíram daqui para
# laguna_devices.py — bloco autocontido que só precisava de `sounddevice`, e que
# obrigava quem quer listar devices (laguna_server) a arrastar o DirectionWorker
# inteiro. Quem já importava de laguna_core continua funcionando; código novo
# importa direto de laguna_devices.
from laguna_devices import (  # noqa: F401
    LAGUNA_KEYWORD,
    VB_CABLE_IN_KEYWORD,
    VB_CABLE_OUT_KEYWORD,
    _device_label,
    _device_tags,
    detect_laguna_devices,
    list_devices,
)

EventCb = Callable[[dict], None]

# Reconexao de captura (issue #12): quando o InputStream cai (device removido,
# driver reiniciado) ou estagna em silencio (callbacks param sem excecao) SEM
# que o worker tenha sido parado, tenta reabrir com backoff curto em vez de
# matar a thread mentindo "Ouvindo". Esgotadas as tentativas, emite erro sticky.
CAPTURE_MAX_RETRIES = 5       # tentativas antes de declarar captura perdida
CAPTURE_BACKOFF_S = 0.5       # base do backoff (cresce por tentativa)
CAPTURE_MAX_BACKOFF_S = 3.0   # teto do backoff entre tentativas
CAPTURE_STALL_S = 3.0         # so mic: sem callbacks por mais que isso = captura morta
CAPTURE_HEALTHY_S = 5.0       # sessao que durou isso reseta o orcamento de retries

# Resiliencia do laco de traducao (issue #45): uma excecao processando UMA frase
# (Piper engasgando num texto esquisito do Argos, hiccup de CUDA) nao pode
# encerrar a direcao pelo resto da sessao. Falha isolada emite erro recuperavel e
# o laco segue para o proximo segmento; so falhas CONSECUTIVAS (o engine morreu
# de verdade) sao fatais. Uma frase boa zera o contador — mesma doutrina do
# orcamento de retries da captura (CAPTURE_MAX_RETRIES/CAPTURE_HEALTHY_S). Numero
# de robustez, nao de latencia: o caminho feliz nunca toca este contador.
SEGMENT_MAX_FAILURES = 3      # falhas consecutivas de segmento antes do fatal

# Reproducao da traducao (issue #38): um _OutputSink por device de saida, com
# stream proprio (aberto uma vez e reusado) e thread propria, de modo que as
# saidas toquem em PARALELO. O laco de traducao ficava bloqueado N x duracao-da-
# frase (fone so comecava quando o CABLE terminava) porque `sd.play(blocking=
# True)` roda em serie por device. `sd.play()` tambem nao serve para paralelizar
# com threads: ele opera sobre um stream global do modulo sounddevice — duas
# chamadas concorrentes se cancelam — e reabre o stream a cada frase (dezenas de
# ms no WASAPI, em cima do alvo de p50 ~450ms).
OUT_SINK_JOIN_S = 2.0         # espera maxima pela thread do sink ao fechar
OUT_SINK_WAIT_MARGIN_S = 2.0  # folga sobre a duracao da frase ao esperar o sink
OUT_SINK_POLL_S = 0.2         # granularidade do stop na thread do sink

# Robustez da saida (issue #54): o sink ja se recupera sozinho de uma falha — o
# stream cai, a frase seguinte reabre — mas o relato para fora era terminal, e a
# UI marcava a direcao inteira como parada (com o Stop desabilitado) por causa de
# um soluco de driver, com o worker ainda traduzindo. Mesma doutrina do orcamento
# de retries da captura (CAPTURE_MAX_RETRIES) e das frases (SEGMENT_MAX_FAILURES):
# falha isolada e recuperavel, so falhas CONSECUTIVAS no MESMO device sao fatais.
# 3 (e nao os 5 da captura) porque aqui cada falha ja custou uma frase muda naquele
# device — o usuario precisa saber cedo. Numero de robustez, nao de latencia: o
# caminho feliz nunca toca este contador.
OUT_SINK_MAX_ERRORS = 3       # falhas consecutivas por device antes do fatal


def _open_output_stream(device: int, samplerate: int):
    """Abre e inicia o OutputStream mono de um device de saida.

    Isolado em funcao para que os testes injetem um stream falso (`_OutputSink`
    recebe `open_stream`) sem tocar PortAudio. Mono/float32 espelha o que o
    `sd.play()` anterior fazia com o PCM 1-D do Piper.
    """
    stream = sd.OutputStream(
        device=device,
        samplerate=samplerate,
        channels=1,
        dtype="float32",
    )
    stream.start()
    return stream


class _OutputSink:
    """Reproduz frases traduzidas num device de saida, em thread propria.

    `submit()` nunca bloqueia: enfileira a frase (fila de 1) e volta na hora, de
    modo que o chamador alimente todos os devices praticamente ao mesmo tempo.
    `wait()` espera a frase terminar — o laco de traducao espera todos os sinks
    uma vez so, ficando bloqueado ~1 frase, nao N.

    Falha de device e local: a excecao fecha so este stream (a proxima frase
    reabre) e vai para `on_error(device, exc, consecutivas)`; os outros sinks
    seguem tocando. O contador de falhas CONSECUTIVAS vive aqui (issue #54) —
    e este objeto que sabe se o `write()` deu certo, e um contador por sink ja
    e um contador por device: a falha de um nunca contamina o do outro. Quem
    decide se `consecutivas` ja e fatal e o worker (`_on_sink_error`).
    """

    def __init__(
        self,
        device: int,
        samplerate: int,
        on_error: Callable[[int, Exception, int], None],
        open_stream: Optional[Callable[[int, int], object]] = None,
    ) -> None:
        self.device = int(device)
        self.samplerate = int(samplerate)
        self._on_error = on_error
        self._erros = 0  # falhas consecutivas; so a thread do sink toca
        self._open_stream = open_stream or _open_output_stream
        self._q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=1)
        self._done = threading.Event()
        self._done.set()  # ocioso ate a 1a frase
        self._stop = threading.Event()
        self._lock = threading.Lock()  # protege self._stream entre as threads
        self._stream: Optional[object] = None
        self._logou_descarte = False  # log de frase descartada: so o 1o
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"laguna-out-{self.device}"
        )
        self._thread.start()

    def submit(self, pcm: np.ndarray) -> None:
        """Enfileira uma frase e retorna imediatamente."""
        self._done.clear()
        try:
            self._q.put_nowait(pcm)
        except queue.Full:
            # Sink ainda ocupado com a frase anterior (device lento/travado):
            # descarta so nele, sem segurar os demais. Loga uma vez para o
            # descarte nao ser invisivel — contabilizar no DropCounter/`overload`
            # exige um terceiro `kind` e muda o payload do evento, entao fica
            # como fatia propria (issue #38).
            if not self._logou_descarte:
                self._logou_descarte = True
                log(f"saida dev={self.device}: device atrasado, frase descartada")
            self._done.set()

    def wait(self, timeout: float) -> bool:
        """Espera a frase atual terminar. False = estourou o timeout."""
        return self._done.wait(timeout)

    def close(self) -> None:
        """Aborta o que estiver tocando, encerra a thread e fecha o stream."""
        self._stop.set()
        with self._lock:
            stream = self._stream
        if stream is not None:
            try:
                stream.abort()  # desbloqueia um write() em andamento
            except Exception:
                pass
        self._thread.join(timeout=OUT_SINK_JOIN_S)
        self._close_stream()

    # --- interno (thread do sink) -------------------------------------------
    def _run(self) -> None:
        # Abre ja no start: a 1a frase nao paga a abertura e um device invalido
        # vira erro antes de alguem falar.
        try:
            self._ensure_stream()
        except Exception as e:
            self._close_stream()
            self._report(e)

        while not self._stop.is_set():
            try:
                pcm = self._q.get(timeout=OUT_SINK_POLL_S)
            except queue.Empty:
                continue
            try:
                if not self._stop.is_set():
                    self._ensure_stream().write(np.ascontiguousarray(pcm, dtype=np.float32))
            except Exception as e:
                self._close_stream()  # forca reabertura na proxima frase
                self._report(e)
            else:
                # Frase tocada: o device esta vivo de novo. Zera o orcamento para
                # que falhas ESPACADAS ao longo de uma call (um soluco por hora)
                # nunca somem ate o teto e matem a direcao (issue #54).
                self._erros = 0
            finally:
                self._done.set()

    def _report(self, exc: Exception) -> None:
        """Emite falha de device — exceto quando a falha E o proprio shutdown.

        `close()` chama `stream.abort()` justamente para desbloquear um `write()`
        em andamento, e o PortAudio faz esse write falhar. Sem este guarda, todo
        Stop (ou troca de config, que faz `laguna_server` parar o worker antigo)
        com uma frase tocando acendia `error.play` na UI — e `static/app.js`
        trata erro como terminal, deixando o painel travado. Parar nao e erro.

        O contador so anda em falha REPORTADA: o write abortado pelo Stop nao
        conta, senao o shutdown envenenaria o orcamento de um sink que sequer
        vai sobreviver a ele.
        """
        if self._stop.is_set():
            return
        self._erros += 1
        self._on_error(self.device, exc, self._erros)

    def _ensure_stream(self):
        with self._lock:
            if self._stream is None:
                self._stream = self._open_stream(self.device, self.samplerate)
            return self._stream

    def _close_stream(self) -> None:
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass


@dataclass
class DirectionConfig:
    name: str  # "falar" | "escutar" (id legivel)
    src_lang: str  # "pt" | "en"
    tgt_lang: str  # "pt" | "en"
    capture_device: Optional[int] = None
    use_loopback: bool = False
    output_devices: list[int] = field(default_factory=list)  # toca em cada um
    model_size: str = "small"
    device: str = "auto"  # auto | cuda | cpu
    compute_type: Optional[str] = None  # None=auto
    skip_same_lang: bool = True  # se detected == tgt, pula
    passthrough: bool = False  # roteia audio bruto da captura -> passthrough_device
    passthrough_device: Optional[int] = None  # geralmente = fone
    output_gain_db: float = 0.0  # ganho aplicado na saida da traducao (-24..+12)
    passthrough_gain_db: float = 0.0  # ganho aplicado no audio passthrough


def _resolve_device(req: str, ct: Optional[str]) -> tuple[str, str]:
    if req == "auto":
        d, c = detect_device()
        return d, c
    if req == "cuda":
        return "cuda", ct or "float16"
    return "cpu", ct or "int8"


class DropCounter:
    """Contabiliza descartes de fila do DirectionWorker sob sobrecarga.

    Puro (sem audio/modelo): so incrementa contadores e decide, via throttle,
    quando vale emitir um evento `overload`. Vive no caminho de excecao
    (`except queue.Full`) — o caminho feliz nunca o toca, entao nao ha custo
    por frame quando nao ha descarte.

    Frames (audio_q) e segmentos (seg_q) sao descartados em threads distintas;
    um `Lock` (adquirido so no descarte, ja fora do caminho quente) mantem
    contadores e throttle coerentes entre elas.
    """

    def __init__(self, min_emit_interval: float = 1.0) -> None:
        self.frames = 0
        self.segments = 0
        self._min_interval = min_emit_interval
        self._last_emit = float("-inf")  # garante emit no 1o descarte
        self._seen_frame = False
        self._seen_segment = False
        self._lock = threading.Lock()

    def record(self, kind: str, now: float) -> tuple[bool, bool]:
        """Registra um descarte de `kind` ("frame"|"segment") ocorrido em `now`
        (segundos monotonicos, ex.: `time.perf_counter()`).

        Retorna `(should_emit, first_of_kind)`:
        - `should_emit`: True se o chamador deve emitir `overload` agora
          (throttle: no maximo 1 a cada `min_emit_interval` segundos);
        - `first_of_kind`: True apenas no primeiro descarte deste `kind`
          (para log unico, sem inundar o console).
        """
        with self._lock:
            if kind == "frame":
                self.frames += 1
                first = not self._seen_frame
                self._seen_frame = True
            elif kind == "segment":
                self.segments += 1
                first = not self._seen_segment
                self._seen_segment = True
            else:
                raise ValueError(f"kind invalido: {kind!r}")
            should_emit = (now - self._last_emit) >= self._min_interval
            if should_emit:
                self._last_emit = now
        return should_emit, first


class DirectionWorker:
    def __init__(self, cfg: DirectionConfig, on_event: EventCb) -> None:
        self.cfg = cfg
        self.on_event = on_event
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lat_total: deque[float] = deque(maxlen=50)
        self._lat_stt: deque[float] = deque(maxlen=50)
        self._lat_mt: deque[float] = deque(maxlen=50)
        self._lat_tts: deque[float] = deque(maxlen=50)
        self._in_rms = 0.0
        self._out_rms = 0.0
        self._last_level_emit = 0.0
        self._last_cb = 0.0  # ultimo callback de captura (watchdog de estagnacao)
        self._last_xrun_log = float("-inf")  # throttle de log de xrun
        self._drops = DropCounter()  # descartes de fila sob sobrecarga
        self._sinks: list[_OutputSink] = []  # saidas da traducao (abertas em _run)
        # Devices de saida ja declarados PERDIDOS (terminal `error.play` emitido).
        # So a thread de cada sink escreve, e cada uma escreve a propria chave —
        # `set.add`/`in` de builtin bastam, sem lock no caminho de erro.
        self._sinks_perdidos: set[int] = set()
        # volume control (linear factors; atualizados via setters threadsafe)
        self._out_gain = 10.0 ** (float(cfg.output_gain_db) / 20.0)
        self._pt_gain = 10.0 ** (float(cfg.passthrough_gain_db) / 20.0)

    def set_output_gain_db(self, db: float) -> None:
        db = max(-60.0, min(12.0, float(db)))
        self._out_gain = 10.0 ** (db / 20.0)
        self.cfg.output_gain_db = db

    def set_passthrough_gain_db(self, db: float) -> None:
        db = max(-60.0, min(12.0, float(db)))
        self._pt_gain = 10.0 ** (db / 20.0)
        self.cfg.passthrough_gain_db = db

    def start(self) -> None:
        t = threading.Thread(target=self._run, daemon=True, name=f"laguna-{self.cfg.name}")
        t.start()
        self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        # Libera os devices de saida na hora: o servidor recria o worker logo em
        # seguida ao trocar de config, e o stream fica aberto mesmo em silencio.
        self._close_sinks()
        for t in self._threads:
            t.join(timeout=2)

    def is_alive(self) -> bool:
        """A direcao ainda pode traduzir?

        `False` depois de `stop()` e tambem quando ela morreu SOZINHA — captura
        perdida, falhas consecutivas de segmento ou falha de setup: todo caminho
        de saida de `_run` seta `_stop`. O servidor le isto para nao anunciar
        direcao morta em `running` (issue #62).
        """
        return not self._stop.is_set()

    def _emit(self, kind: str, **data) -> None:
        try:
            self.on_event({"dir": self.cfg.name, "kind": kind, **data})
        except Exception:
            pass

    def _emit_key(
        self,
        kind: str,
        key: str,
        args: Optional[dict] = None,
        msg: Optional[str] = None,
        **data,
    ) -> None:
        """Emite evento com chave i18n + args para o cliente resolver.

        `msg` e o fallback em portugues (usado se o cliente nao tem a chave
        ou em logs de console). `**data` vai direto no payload do evento, para
        campos que a UI le sem traduzir (ex.: `recoverable`, issue #45).
        """
        args = args or {}
        fallback = msg if msg is not None else key
        self._emit(kind, key=key, args=args, msg=fallback, **data)

    def _run(self) -> None:
        try:
            device, ct = _resolve_device(self.cfg.device, self.cfg.compute_type)
            self._emit_key(
                "status",
                "status.loading_models",
                {"model": self.cfg.model_size, "device": device},
                msg=f"Carregando modelos ({self.cfg.model_size}/{device})...",
            )
            stt = STT(
                language=self.cfg.src_lang,
                model_size=self.cfg.model_size,
                device=device,
                compute_type=ct,
            )
            mt = ArgosMT(src=self.cfg.src_lang, tgt=self.cfg.tgt_lang)
            tts = PiperTTS(lang=self.cfg.tgt_lang)

            self._emit_key("status", "status.warmup", msg="Aquecendo...")
            _ = stt.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32))
            _ = mt.translate("olá" if self.cfg.src_lang == "pt" else "hello")
            _ = tts.synthesize("warm up")

            self._open_sinks(tts.sample_rate)

            vad = WebRTCVADGate(aggressiveness=2)
            audio_q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=400)
            seg_q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=4)

            cap_t = threading.Thread(target=self._capture, args=(audio_q,), daemon=True)
            seg_t = threading.Thread(target=self._segmenter, args=(audio_q, seg_q, vad), daemon=True)
            cap_t.start()
            seg_t.start()
            self._threads.extend([cap_t, seg_t])

            if self.cfg.passthrough and self.cfg.passthrough_device is not None:
                pt_t = threading.Thread(target=self._passthrough_run, daemon=True, name=f"laguna-{self.cfg.name}-passthrough")
                pt_t.start()
                self._threads.append(pt_t)

            self._emit_key("status", "status.listening", msg="Ouvindo")
            self._emit("ready")

            self._translation_loop(seg_q, stt, mt, tts)
        except Exception as e:
            # Falha de SETUP (modelo que nao carrega, warmup) segue matando a
            # direcao na hora: "sem modelo" nao vira retry infinito. O laco de
            # traducao trata as proprias falhas em _translation_loop.
            self._emit("error", msg=f"{type(e).__name__}: {e}")
        finally:
            # Saiu de `_run` = direcao acabou, por qualquer caminho. `_stop`
            # aqui e a rede final da doutrina do #45: cobre tambem a falha de
            # SETUP acima (modelo que nao carrega), que nao passa pelo laco de
            # traducao e ate agora deixava `_stop` limpo — worker morto que o
            # servidor continuava listando como `running` (issue #62).
            self._stop.set()
            # Fecha streams de saida e junta as threads dos sinks: parar/reiniciar
            # a direcao varias vezes nao pode vazar device (issue #38).
            self._close_sinks()

    def _translation_loop(self, seg_q: "queue.Queue[np.ndarray]", stt, mt, tts) -> None:
        """Consome segmentos ate o stop — sobrevivendo a falha de UMA frase.

        Antes (issue #45) o laco inteiro vivia sob o mesmo `try` do setup: uma
        excecao em qualquer engine encerrava a direcao pelo resto da sessao,
        deixando `_capture`/`_segmenter` rodando (o `_stop` nunca era setado) e o
        app "vivo" sem traduzir. Aqui a falha e por frase: emite erro recuperavel
        e segue. So `SEGMENT_MAX_FAILURES` falhas CONSECUTIVAS sao fatais — e
        nesse caso o `_stop` e setado ANTES de sair, para nao deixar thread
        zumbi segurando o device de captura e enchendo fila.
        """
        fails = 0  # falhas consecutivas; uma frase boa zera
        while not self._stop.is_set():
            try:
                seg = seg_q.get(timeout=0.2)
            except queue.Empty:
                continue

            try:
                self._process_segment(seg, stt, mt, tts)
            except Exception as e:
                if self._stop.is_set():
                    return  # parar no meio de uma frase nao e falha
                fails += 1
                detail = f"{type(e).__name__}: {e}"
                log(
                    f"[{self.cfg.name}] falha ao processar segmento "
                    f"({fails}/{SEGMENT_MAX_FAILURES}): {detail}"
                )
                if fails >= SEGMENT_MAX_FAILURES:
                    # Fatal: encerra a direcao de verdade. `_stop` primeiro, para
                    # que captura e segmentador saiam junto com este laco.
                    self._stop.set()
                    self._emit_key(
                        "error",
                        "error.direction_lost",
                        {"max": SEGMENT_MAX_FAILURES, "detail": detail},
                        msg=(
                            f"Direcao encerrada: {SEGMENT_MAX_FAILURES} frases seguidas "
                            f"falharam. ({detail})"
                        ),
                    )
                    return
                # Recuperavel: a UI mostra o aviso mas NAO derruba os botoes —
                # a direcao continua traduzindo a proxima frase.
                self._emit_key(
                    "error",
                    "error.segment_failed",
                    {"attempt": fails, "max": SEGMENT_MAX_FAILURES, "detail": detail},
                    msg=f"Frase perdida ({fails}/{SEGMENT_MAX_FAILURES}): {detail}",
                    recoverable=True,
                )
            else:
                fails = 0

    def _process_segment(self, seg: np.ndarray, stt, mt, tts) -> None:
        """Traduz UM segmento: STT -> MT -> TTS -> reproducao nos sinks.

        Extraido do laco (issue #45) para que a falha de uma frase seja isolavel
        num `try` proprio e para que o caminho quente seja testavel com dubles,
        sem sounddevice, modelo ou GPU. Excecao aqui sobe para o chamador — as
        latencias so entram em `_lat_*` no fim, entao segmento que falhou nao
        polui a metrica (a metrica nao pode mentir por omissao, cf. issue #21).
        """
        t0 = time.perf_counter()
        if self.cfg.skip_same_lang:
            text, detected = stt.transcribe_with_lang(seg)
        else:
            text = stt.transcribe(seg)
            detected = self.cfg.src_lang
        t_stt = (time.perf_counter() - t0) * 1000
        if not text.strip():
            return

        self._emit("stt", text=text, lang=detected, ms=round(t_stt))

        if self.cfg.skip_same_lang and detected == self.cfg.tgt_lang:
            self._emit("skipped", reason=f"detected={detected} == tgt; sem traducao")
            self._lat_stt.append(t_stt)
            self._push_latency()
            return

        t0 = time.perf_counter()
        translated = mt.translate(text)
        t_mt = (time.perf_counter() - t0) * 1000
        self._emit("mt", text=translated, ms=round(t_mt))

        t0 = time.perf_counter()
        audio_out = tts.synthesize(translated)
        t_tts = (time.perf_counter() - t0) * 1000

        total = t_stt + t_mt + t_tts
        self._lat_stt.append(t_stt)
        self._lat_mt.append(t_mt)
        self._lat_tts.append(t_tts)
        self._lat_total.append(total)
        self._push_latency()

        self._play(audio_out, tts.sample_rate)

    def _update_level(self, kind: str, arr: np.ndarray) -> None:
        """Atualiza medidor de nivel (in/out) com EMA + emite throttled (10Hz)."""
        if arr.size == 0:
            return
        try:
            flat = arr.astype(np.float32, copy=False)
            if flat.ndim > 1:
                flat = flat.mean(axis=1)
            rms = float(np.sqrt(float(np.mean(flat * flat)) + 1e-12))
        except Exception:
            return
        # clip e smoothing EMA
        rms = min(max(rms, 0.0), 1.0)
        if kind == "in":
            self._in_rms = self._in_rms * 0.6 + rms * 0.4
        else:
            self._out_rms = self._out_rms * 0.6 + rms * 0.4
        now = time.perf_counter()
        if now - self._last_level_emit >= 0.08:  # ~12 Hz
            self._last_level_emit = now
            self._emit(
                "level",
                in_level=round(self._in_rms, 4),
                out_level=round(self._out_rms, 4),
            )

    def _on_drop(self, kind: str) -> None:
        """Registra um descarte de fila (audio_q/seg_q) e avisa quem precisa.

        Chamado so no `except queue.Full` — nunca no caminho feliz. Conta,
        loga o primeiro descarte de cada tipo (uma vez) e emite `overload`
        throttled (<=1/s) com os contadores acumulados, para a metrica de
        latencia parar de mentir por omissao (a UI ainda ignora o evento; badge
        de sobrecarga e fatia propria — ver issue #21).
        """
        should_emit, first = self._drops.record(kind, time.perf_counter())
        if first:
            what = "frames de audio" if kind == "frame" else "segmentos (frases)"
            log(f"[{self.cfg.name}] sobrecarga: descartando {what} (fila cheia)")
        if should_emit:
            self._emit(
                "overload",
                dropped_frames=self._drops.frames,
                dropped_segments=self._drops.segments,
            )

    def _push_latency(self) -> None:
        def _p(xs: deque[float], p: int) -> int:
            if not xs:
                return 0
            return int(np.percentile(xs, p))

        self._emit(
            "latency",
            total_p50=_p(self._lat_total, 50),
            total_p95=_p(self._lat_total, 95),
            stt_p50=_p(self._lat_stt, 50),
            mt_p50=_p(self._lat_mt, 50),
            tts_p50=_p(self._lat_tts, 50),
            samples=len(self._lat_total),
            dropped_frames=self._drops.frames,
            dropped_segments=self._drops.segments,
        )

    def _note_xrun(self, status) -> None:
        """Loga xruns (overflow/underflow) da captura, throttled a 1/s.

        Chamado so quando o `status` do callback e nao-vazio: o caminho quente
        (sem glitch) fica intocado, so paga um `if status:` por frame.
        """
        now = time.perf_counter()
        if now - self._last_xrun_log >= 1.0:
            self._last_xrun_log = now
            log(f"[{self.cfg.name}] xrun na captura: {status}")

    def _capture_device_label(self) -> str:
        """Rotulo acionavel do device de captura (nome + indice) para erros."""
        dev = self.cfg.capture_device
        if dev is None:
            return "padrao"
        try:
            return f"{sd.query_devices(dev)['name']} (#{dev})"
        except Exception:
            return f"#{dev}"

    def _capture(self, audio_q: "queue.Queue[np.ndarray]") -> None:
        def cb(indata, frames, time_info, status):
            self._last_cb = time.perf_counter()
            if status:
                self._note_xrun(status)
            mono = indata[:, 0] if indata.ndim > 1 else indata
            self._update_level("in", mono)
            try:
                audio_q.put_nowait(mono.copy())
            except queue.Full:
                self._on_drop("frame")

        def cb_loopback(indata, frames, time_info, status):
            self._last_cb = time.perf_counter()
            if status:
                self._note_xrun(status)
            mono = indata.mean(axis=1) if indata.ndim > 1 else indata
            self._update_level("in", mono)
            try:
                audio_q.put_nowait(mono.astype(np.float32).copy())
            except queue.Full:
                self._on_drop("frame")

        kwargs = dict(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=VAD_FRAME_SAMPLES,
            device=self.cfg.capture_device,
            callback=cb,
        )
        if self.cfg.use_loopback:
            try:
                kwargs["extra_settings"] = sd.WasapiSettings(loopback=True)
                kwargs["channels"] = 2
                kwargs["callback"] = cb_loopback
            except Exception as e:
                self._emit_key(
                    "error",
                    "error.loopback_unavailable",
                    {"detail": str(e)},
                    msg=f"loopback indisponivel: {e}",
                )

        # Laco de reconexao (issue #12): reabre o stream em vez de morrer em
        # silencio. Cobre tanto a excecao do PortAudio quanto a estagnacao muda
        # (watchdog: callbacks do PortAudio disparam mesmo em silencio, entao
        # parada > CAPTURE_STALL_S significa captura morta).
        #
        # ATENCAO: o watchdog de estagnacao vale SO para captura de mic (input
        # comum entrega callbacks continuos mesmo em silencio). No WASAPI loopback
        # o motor de audio do Windows para de entregar buffers quando o endpoint
        # de render fica ocioso (nada tocando) — ausencia de callback e o estado
        # NORMAL, nao "device sumido". Aplicar o stall ali derrubaria um stream
        # saudavel a cada pausa de audio. Para loopback dependemos so do caminho
        # de excecao (device removido faz o PortAudio levantar), que o laco cobre.
        watchdog = not self.cfg.use_loopback
        attempt = 0
        announce = False  # re-anuncia "Ouvindo" quando o audio volta pos-retry
        while not self._stop.is_set():
            try:
                opened = time.perf_counter()
                self._last_cb = opened
                with sd.InputStream(**kwargs):
                    while not self._stop.is_set():
                        time.sleep(0.1)
                        now = time.perf_counter()
                        if announce and self._last_cb > opened:
                            announce = False
                            attempt = 0
                            self._emit_key("status", "status.listening", msg="Ouvindo")
                        if watchdog and now - self._last_cb > CAPTURE_STALL_S:
                            raise RuntimeError(
                                f"sem audio ha >{CAPTURE_STALL_S:.0f}s (device sumiu?)"
                            )
                return  # saida limpa do `with` so ocorre com _stop setado
            except Exception as e:
                if self._stop.is_set():
                    return
                label = self._capture_device_label()
                # sessao que rodou tempo suficiente reseta o orcamento de retries
                if time.perf_counter() - opened >= CAPTURE_HEALTHY_S:
                    attempt = 0
                attempt += 1
                log(
                    f"[{self.cfg.name}] captura caiu [{label}] "
                    f"(tentativa {attempt}/{CAPTURE_MAX_RETRIES}): {e}"
                )
                if attempt >= CAPTURE_MAX_RETRIES:
                    # Fatal: mesma doutrina do laco de traducao (#45) — `_stop`
                    # ANTES de sair, para que segmentador e laco de traducao
                    # saiam junto. Sem isto a captura morre sozinha e o resto
                    # segue girando com a fila vazia, os devices de saida
                    # presos e o app "vivo" sem traduzir (issue #62).
                    self._stop.set()
                    self._emit_key(
                        "error",
                        "error.capture_lost",
                        {"device": label, "detail": str(e)},
                        msg=f"Captura perdida [{label}] — verifique o dispositivo. ({e})",
                    )
                    return
                announce = True
                self._emit_key(
                    "status",
                    "status.capture_retry",
                    {
                        "device": label,
                        "attempt": attempt,
                        "max": CAPTURE_MAX_RETRIES,
                        "detail": str(e),
                    },
                    msg=f"Reconectando captura [{label}]... {attempt}/{CAPTURE_MAX_RETRIES} ({e})",
                )
                backoff = min(CAPTURE_BACKOFF_S * attempt, CAPTURE_MAX_BACKOFF_S)
                slept = 0.0
                while slept < backoff and not self._stop.is_set():
                    time.sleep(0.1)
                    slept += 0.1
                if self._stop.is_set():
                    return
                # re-checa o device alvo antes de reabrir (pode ter voltado/sumido)
                try:
                    if self.cfg.capture_device is not None:
                        sd.query_devices(self.cfg.capture_device)
                except Exception:
                    pass  # ainda ausente; a proxima tentativa reavalia

    def _passthrough_run(self) -> None:
        """Stream paralelo: roteia audio bruto da captura -> passthrough_device (normalmente fone).

        Permite ao usuario escutar o audio original (Discord) simultaneamente a traducao.
        Usa sd.Stream (full-duplex) a sample rate nativo da fonte. Fallback: queue-based
        com resample se os devices nao batem na sample rate.
        """
        src = self.cfg.capture_device
        dst = self.cfg.passthrough_device
        if src is None or dst is None or src == dst:
            self._emit_key(
                "error",
                "error.passthrough_invalid_devices",
                {"src": src, "dst": dst},
                msg=f"passthrough: devices invalidos (src={src}, dst={dst})",
            )
            return

        try:
            src_info = sd.query_devices(src)
            dst_info = sd.query_devices(dst)
        except Exception as e:
            self._emit_key(
                "error",
                "error.passthrough_query",
                {"detail": str(e)},
                msg=f"passthrough query_devices: {e}",
            )
            return

        src_sr = int(src_info["default_samplerate"])
        dst_sr = int(dst_info["default_samplerate"])
        in_ch = 2 if self.cfg.use_loopback else max(1, min(int(src_info.get("max_input_channels") or 1), 2))
        out_ch = max(1, min(int(dst_info.get("max_output_channels") or 1), 2))

        # Tenta caminho rapido: sd.Stream full-duplex ao mesmo sample rate
        if src_sr == dst_sr:
            try:
                self._run_fullduplex(src, dst, src_sr, in_ch, out_ch)
                return
            except Exception as e:
                self._emit_key(
                    "status",
                    "status.passthrough_duplex_fallback",
                    {"detail": str(e)},
                    msg=f"passthrough duplex falhou ({e}); usando pipeline queue",
                )

        # Fallback: streams independentes com queue
        try:
            self._run_split_passthrough(src, dst, src_sr, dst_sr, in_ch, out_ch)
        except Exception as e:
            self._emit_key(
                "error",
                "error.passthrough_generic",
                {"detail": str(e)},
                msg=f"passthrough: {e}",
            )

    def _run_fullduplex(self, src: int, dst: int, sr: int, in_ch: int, out_ch: int) -> None:
        def cb(indata, outdata, frames, time_info, status):
            if in_ch == out_ch:
                outdata[:] = indata
            elif in_ch == 2 and out_ch == 1:
                outdata[:, 0] = indata.mean(axis=1)
            elif in_ch == 1 and out_ch == 2:
                outdata[:, 0] = indata[:, 0]
                outdata[:, 1] = indata[:, 0]
            else:
                outdata.fill(0)
            g = self._pt_gain
            if g != 1.0:
                np.multiply(outdata, g, out=outdata)
                np.clip(outdata, -1.0, 1.0, out=outdata)
            self._update_level("out", outdata)

        extra = (sd.WasapiSettings(loopback=True), None) if self.cfg.use_loopback else (None, None)
        with sd.Stream(
            device=(src, dst),
            samplerate=sr,
            channels=(in_ch, out_ch),
            dtype="float32",
            callback=cb,
            latency="low",
            extra_settings=extra,
        ):
            while not self._stop.is_set():
                time.sleep(0.1)

    def _run_split_passthrough(
        self, src: int, dst: int, src_sr: int, dst_sr: int, in_ch: int, out_ch: int
    ) -> None:
        """Streams independentes: captura -> queue -> reproducao.

        Se sample rates diferem, resample linear simples (qualidade aceitavel para monitor).
        """
        q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=20)

        def cb_in(indata, frames, time_info, status):
            if in_ch == out_ch:
                buf = indata.copy()
            elif in_ch == 2 and out_ch == 1:
                buf = indata.mean(axis=1, keepdims=True)
            elif in_ch == 1 and out_ch == 2:
                buf = np.repeat(indata, 2, axis=1)
            else:
                buf = indata[:, :out_ch].copy()
            if src_sr != dst_sr:
                ratio = dst_sr / src_sr
                n_out = int(buf.shape[0] * ratio)
                idx = (np.arange(n_out) / ratio).astype(np.int64)
                idx = np.clip(idx, 0, buf.shape[0] - 1)
                buf = buf[idx]
            try:
                q.put_nowait(buf)
            except queue.Full:
                pass

        def cb_out(outdata, frames, time_info, status):
            try:
                buf = q.get_nowait()
            except queue.Empty:
                outdata.fill(0)
                return
            n = min(len(buf), len(outdata))
            outdata[:n] = buf[:n]
            if n < len(outdata):
                outdata[n:].fill(0)
            g = self._pt_gain
            if g != 1.0:
                np.multiply(outdata, g, out=outdata)
                np.clip(outdata, -1.0, 1.0, out=outdata)

        extra_in = sd.WasapiSettings(loopback=True) if self.cfg.use_loopback else None
        in_kwargs = dict(
            device=src,
            samplerate=src_sr,
            channels=in_ch,
            dtype="float32",
            callback=cb_in,
            latency="low",
        )
        if extra_in is not None:
            in_kwargs["extra_settings"] = extra_in

        with sd.InputStream(**in_kwargs), sd.OutputStream(
            device=dst,
            samplerate=dst_sr,
            channels=out_ch,
            dtype="float32",
            callback=cb_out,
            latency="low",
        ):
            while not self._stop.is_set():
                time.sleep(0.1)

    def _segmenter(
        self,
        audio_q: "queue.Queue[np.ndarray]",
        seg_q: "queue.Queue[np.ndarray]",
        vad: WebRTCVADGate,
    ) -> None:
        # A politica de segmentacao (pre-buffer / hangover / min-max) vive em
        # VADSegmenter (laguna_pipeline), a mesma classe coberta por
        # tests_unit/test_vad_segmenter.py e usada pela CLI (fase0_poc). Aqui a
        # thread cuida so de queues, do fatiamento em frames de VAD_FRAME_SAMPLES
        # e do `leftover` — a captura entrega blocos de tamanho arbitrario e
        # `VADSegmenter.push` assume frame ja alinhado (issue #44).
        segmenter = VADSegmenter()
        leftover = np.zeros(0, dtype=np.float32)

        while not self._stop.is_set():
            try:
                raw = audio_q.get(timeout=0.1)
            except queue.Empty:
                continue
            buf = np.concatenate([leftover, raw]) if len(leftover) else raw
            i = 0
            while i + VAD_FRAME_SAMPLES <= len(buf):
                frame = buf[i : i + VAD_FRAME_SAMPLES]
                i += VAD_FRAME_SAMPLES
                seg = segmenter.push(frame, vad.is_speech(frame))
                if seg is not None:
                    try:
                        seg_q.put_nowait(seg)
                    except queue.Full:
                        self._on_drop("segment")
            leftover = buf[i:] if i < len(buf) else np.zeros(0, dtype=np.float32)

    def _open_sinks(self, samplerate: int) -> None:
        """Cria um sink por device de saida (sample rate fixo = o do Piper)."""
        if self._stop.is_set():
            # Stop chegou durante o warmup (carga de modelo estoura o join de 2s
            # do stop()): nao adianta abrir device para fechar no finally.
            return
        self._sinks_perdidos.clear()  # sinks novos, orcamento e veredito zerados
        self._sinks = [
            _OutputSink(dev, samplerate, self._on_sink_error)
            for dev in self.cfg.output_devices
        ]

    def _close_sinks(self) -> None:
        sinks, self._sinks = self._sinks, []
        for sink in sinks:
            sink.close()

    def _on_sink_error(self, dev: int, exc: Exception, consecutivas: int) -> None:
        """Decide se a falha de UM device de saida e soluco ou perda definitiva.

        Antes, toda falha virava `error.play` — e `static/app.js` trata erro
        nao-recuperavel como fim de sessao (status 'error', `running.delete`,
        `toggleButtons(false)`). Resultado: um fone entrando em power save
        desabilitava o Stop com o worker ainda capturando e mandando audio pro
        Discord, e a unica saida era recarregar a pagina (issue #54).

        O degrau e o mesmo do laco de traducao (issue #45): falha isolada e
        `recoverable=True` — aviso ambar transitorio, a direcao segue rodando e o
        Stop segue habilitado — e so `OUT_SINK_MAX_ERRORS` falhas CONSECUTIVAS no
        mesmo device viram o terminal `error.play`. Preferimos `recoverable` a um
        `kind:"status"` novo porque status pinta o painel de VERDE ("rodando")
        com um texto de falha e nao volta sozinho; o caminho recuperavel ja existe
        na UI, entao nenhuma linha de `app.js` precisa mudar para isto funcionar.

        O veredito terminal e SEM VOLTA para aquele device (`_sinks_perdidos`).
        Sem essa trava, o zerar-em-sucesso abriria um estado que nao existia antes
        desta issue: depois do `error.play` a UI ja derrubou os botoes
        (`app.js`: `toggleButtons(false)` + `running.delete`), e uma frase boa
        seguida de nova falha voltaria como `recoverable` — o painel repintaria
        VERDE "Rodando" com o Parar desabilitado. Um device que ja custou
        `OUT_SINK_MAX_ERRORS` frases seguidas nao volta a ser soluco; tambem evita
        re-emitir o terminal a cada frase.
        """
        if dev in self._sinks_perdidos:
            return
        if consecutivas >= OUT_SINK_MAX_ERRORS:
            self._sinks_perdidos.add(dev)
            self._emit_key(
                "error",
                "error.play",
                {"dev": dev, "max": OUT_SINK_MAX_ERRORS, "detail": str(exc)},
                msg=(
                    f"Saida perdida (dev={dev}): {OUT_SINK_MAX_ERRORS} frases "
                    f"seguidas falharam. ({exc})"
                ),
            )
            return
        self._emit_key(
            "error",
            "error.output_retry",
            {
                "dev": dev,
                "attempt": consecutivas,
                "max": OUT_SINK_MAX_ERRORS,
                "detail": str(exc),
            },
            msg=f"Saida falhou (dev={dev}) {consecutivas}/{OUT_SINK_MAX_ERRORS}: {exc}",
            recoverable=True,
        )

    def _play(self, pcm: np.ndarray, sr: int) -> None:
        # Lista fixada numa local: um _close_sinks() concorrente (stop) nao pode
        # fazer o laco de espera enxergar um conjunto diferente do que submeteu.
        sinks = self._sinks
        if len(pcm) == 0 or not sinks or self._stop.is_set():
            return
        gain = self._out_gain
        if gain != 1.0:
            pcm = np.clip(pcm.astype(np.float32, copy=False) * gain, -1.0, 1.0)
        self._update_level("out", pcm)  # medidor: 1x por frase, nao 1x por device
        for sink in sinks:
            sink.submit(pcm)
        # As saidas tocam em paralelo: espera-se uma frase (a mais lenta), nao N.
        # O deadline unico evita que um device travado multiplique a espera; sem
        # ele o laco poderia ficar parado N x timeout.
        deadline = time.perf_counter() + len(pcm) / float(sr) + OUT_SINK_WAIT_MARGIN_S
        for sink in sinks:
            sink.wait(max(0.0, deadline - time.perf_counter()))

