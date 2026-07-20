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
    MAX_SEGMENT_MS,
    MIN_SPEECH_MS,
    PRE_SPEECH_BUFFER_MS,
    PiperTTS,
    SAMPLE_RATE,
    SILENCE_HANGOVER_MS,
    STT,
    VAD_FRAME_MS,
    VAD_FRAME_SAMPLES,
    WebRTCVADGate,
    detect_device,
    log,
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
        for t in self._threads:
            t.join(timeout=2)

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
    ) -> None:
        """Emite evento com chave i18n + args para o cliente resolver.

        `msg` e o fallback em portugues (usado se o cliente nao tem a chave
        ou em logs de console).
        """
        args = args or {}
        fallback = msg if msg is not None else key
        self._emit(kind, key=key, args=args, msg=fallback)

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

            while not self._stop.is_set():
                try:
                    seg = seg_q.get(timeout=0.2)
                except queue.Empty:
                    continue

                t0 = time.perf_counter()
                if self.cfg.skip_same_lang:
                    text, detected = stt.transcribe_with_lang(seg)
                else:
                    text = stt.transcribe(seg)
                    detected = self.cfg.src_lang
                t_stt = (time.perf_counter() - t0) * 1000
                if not text.strip():
                    continue

                self._emit("stt", text=text, lang=detected, ms=round(t_stt))

                if self.cfg.skip_same_lang and detected == self.cfg.tgt_lang:
                    self._emit("skipped", reason=f"detected={detected} == tgt; sem traducao")
                    self._lat_stt.append(t_stt)
                    self._push_latency()
                    continue

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
        except Exception as e:
            self._emit("error", msg=f"{type(e).__name__}: {e}")

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
        pre_max = PRE_SPEECH_BUFFER_MS // VAD_FRAME_MS
        silence_max = SILENCE_HANGOVER_MS // VAD_FRAME_MS
        min_speech = MIN_SPEECH_MS // VAD_FRAME_MS
        max_frames = MAX_SEGMENT_MS // VAD_FRAME_MS

        pre_buf: deque[np.ndarray] = deque(maxlen=pre_max)
        speaking: list[np.ndarray] = []
        silence_run = 0
        in_speech = False
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
                is_speech = vad.is_speech(frame)
                if not in_speech:
                    pre_buf.append(frame)
                    if is_speech:
                        in_speech = True
                        speaking = list(pre_buf)
                        pre_buf.clear()
                        silence_run = 0
                else:
                    speaking.append(frame)
                    silence_run = 0 if is_speech else silence_run + 1
                    force_flush = len(speaking) >= max_frames
                    if silence_run >= silence_max or force_flush:
                        if len(speaking) >= min_speech:
                            seg = np.concatenate(speaking)
                            try:
                                seg_q.put_nowait(seg)
                            except queue.Full:
                                self._on_drop("segment")
                        in_speech = False
                        speaking = []
                        silence_run = 0
            leftover = buf[i:] if i < len(buf) else np.zeros(0, dtype=np.float32)

    def _play(self, pcm: np.ndarray, sr: int) -> None:
        if len(pcm) == 0 or not self.cfg.output_devices:
            return
        gain = self._out_gain
        if gain != 1.0:
            pcm = np.clip(pcm.astype(np.float32, copy=False) * gain, -1.0, 1.0)
        self._update_level("out", pcm)
        for dev in self.cfg.output_devices:
            try:
                sd.play(pcm, samplerate=sr, device=dev, blocking=True)
            except Exception as e:
                self._emit_key(
                    "error",
                    "error.play",
                    {"dev": dev, "detail": str(e)},
                    msg=f"play(dev={dev}): {e}",
                )


# ---- device helpers ----------------------------------------------------------

LAGUNA_KEYWORD = "laguna"
VB_CABLE_IN_KEYWORD = "cable input"   # saida (Laguna -> Discord)
VB_CABLE_OUT_KEYWORD = "cable output"  # entrada (Discord -> Laguna)


def list_devices() -> dict:
    """Retorna dict com inputs, outputs, loopbacks (WASAPI outputs utilizados como captura)."""
    devs = sd.query_devices()
    apis = sd.query_hostapis()
    inputs: list[dict] = []
    outputs: list[dict] = []
    loopbacks: list[dict] = []
    for i, d in enumerate(devs):
        api = apis[d["hostapi"]]["name"]
        entry = {
            "index": i,
            "name": d["name"],
            "hostapi": api,
            "in_ch": d["max_input_channels"],
            "out_ch": d["max_output_channels"],
            "label": _device_label(d, api),
            "tags": _device_tags(d),
        }
        if d["max_input_channels"] > 0:
            inputs.append(entry)
        if d["max_output_channels"] > 0:
            outputs.append(entry)
            if api == "Windows WASAPI":
                loopbacks.append(entry)
    return {"inputs": inputs, "outputs": outputs, "loopbacks": loopbacks}


def _device_label(d: dict, api: str) -> str:
    name = d["name"]
    marks = []
    low = name.lower()
    if LAGUNA_KEYWORD in low:
        marks.append("🌊 Laguna")
    if "cable" in low or "vb-audio" in low:
        marks.append("VB-CABLE")
    suffix = f"  [{', '.join(marks)}]" if marks else ""
    return f"{name} ({api}){suffix}"


def _device_tags(d: dict) -> list[str]:
    tags = []
    low = d["name"].lower()
    if LAGUNA_KEYWORD in low:
        tags.append("laguna")
    if "cable input" in low:
        tags.append("vb_cable_in")
    if "cable output" in low:
        tags.append("vb_cable_out")
    return tags


def detect_laguna_devices() -> dict:
    """Tenta achar dispositivos renomeados p/ 'Laguna'; fallback p/ VB-CABLE."""
    devs = sd.query_devices()
    laguna_in: Optional[int] = None    # entrada capturada (Discord -> nos): CABLE Output renomeado
    laguna_out: Optional[int] = None   # saida virtual (nos -> Discord): CABLE Input renomeado
    fallback_in: Optional[int] = None
    fallback_out: Optional[int] = None
    for i, d in enumerate(devs):
        low = d["name"].lower()
        if LAGUNA_KEYWORD in low:
            if d["max_input_channels"] > 0 and laguna_in is None:
                laguna_in = i
            if d["max_output_channels"] > 0 and laguna_out is None:
                laguna_out = i
        if "cable output" in low and d["max_input_channels"] > 0 and fallback_in is None:
            fallback_in = i
        if "cable input" in low and d["max_output_channels"] > 0 and fallback_out is None:
            fallback_out = i
    return {
        "virtual_in": laguna_in if laguna_in is not None else fallback_in,
        "virtual_out": laguna_out if laguna_out is not None else fallback_out,
        "has_laguna_name": laguna_in is not None or laguna_out is not None,
    }
