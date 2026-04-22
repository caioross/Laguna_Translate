"""TransDualDisc — Fase 1 MVP (painel visual).

Painel PySide6 com:
 - seleção de dispositivos (mic, loopback, fone, saída virtual p/ Discord)
 - modo FALAR / ESCUTAR / AMBOS
 - transcrição STT ao vivo + tradução MT exibidas em tempo real
 - stats de latência (p50/p95/p99)

Pré-requisitos p/ injeção no Discord:
 Instale VB-CABLE (grátis) em https://vb-audio.com/Cable/ e escolha
 "CABLE Input (VB-Audio Virtual Cable)" como Saída Virtual.
 No Discord, escolha "CABLE Output (VB-Audio Virtual Cable)" como microfone.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import sounddevice as sd

from fase0_poc import (
    ArgosMT,
    MAX_SEGMENT_MS,
    MIN_SPEECH_MS,
    PIPER_VOICES,
    PRE_SPEECH_BUFFER_MS,
    PiperTTS,
    SAMPLE_RATE,
    SILENCE_HANGOVER_MS,
    STT,
    Stats,
    VAD_FRAME_MS,
    VAD_FRAME_SAMPLES,
    WebRTCVADGate,
    detect_device,
)

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

MODES = ["FALAR (eu -> Discord)", "ESCUTAR (Discord -> eu)", "AMBOS"]
DIRECTIONS = [("PT -> EN", "pt", "en"), ("EN -> PT", "en", "pt")]


def list_input_devices() -> list[tuple[int, str]]:
    devs = sd.query_devices()
    return [(i, f"[{i}] {d['name']}") for i, d in enumerate(devs) if d["max_input_channels"] > 0]


def list_output_devices() -> list[tuple[int, str]]:
    devs = sd.query_devices()
    return [(i, f"[{i}] {d['name']}") for i, d in enumerate(devs) if d["max_output_channels"] > 0]


def list_wasapi_output_devices() -> list[tuple[int, str]]:
    """Dispositivos WASAPI de saída — usados como loopback de entrada."""
    devs = sd.query_devices()
    hostapis = sd.query_hostapis()
    out = []
    for i, d in enumerate(devs):
        if d["max_output_channels"] > 0 and hostapis[d["hostapi"]]["name"] == "Windows WASAPI":
            out.append((i, f"[{i}] {d['name']} (loopback)"))
    return out


def find_vb_cable_output() -> Optional[int]:
    """Procura VB-CABLE Input (saída virtual que o Discord pode ouvir)."""
    for i, d in enumerate(sd.query_devices()):
        name = d["name"].lower()
        if d["max_output_channels"] > 0 and "cable input" in name:
            return i
    return None


@dataclass
class PipelineConfig:
    src_lang: str
    tgt_lang: str
    model_size: str
    device: str
    compute_type: str
    input_device: Optional[int]
    loopback_device: Optional[int]
    fone_device: Optional[int]
    virtual_device: Optional[int]
    play_on_fone: bool
    play_on_virtual: bool


class PipelineSignals(QObject):
    status = Signal(str)
    stt_text = Signal(str)  # (texto transcrito)
    mt_text = Signal(str)
    latency = Signal(float, float, float, float)  # stt, mt, tts, total
    error = Signal(str)


class PipelineWorker:
    """Uma direção do pipeline (mic OU loopback -> STT -> MT -> TTS -> saídas)."""

    def __init__(
        self,
        cfg: PipelineConfig,
        capture_device: Optional[int],
        use_loopback: bool,
        signals: PipelineSignals,
        shared_models: Optional[tuple[STT, ArgosMT, PiperTTS]] = None,
    ) -> None:
        self.cfg = cfg
        self.capture_device = capture_device
        self.use_loopback = use_loopback
        self.signals = signals
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._shared_models = shared_models

    def start(self) -> None:
        t = threading.Thread(target=self._run, daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2)

    def _run(self) -> None:
        try:
            if self._shared_models is not None:
                stt, mt, tts = self._shared_models
            else:
                self.signals.status.emit("Carregando modelos...")
                stt = STT(
                    language=self.cfg.src_lang,
                    model_size=self.cfg.model_size,
                    device=self.cfg.device,
                    compute_type=self.cfg.compute_type,
                )
                mt = ArgosMT(src=self.cfg.src_lang, tgt=self.cfg.tgt_lang)
                tts = PiperTTS(lang=self.cfg.tgt_lang)

                self.signals.status.emit("Aquecendo modelos...")
                _ = stt.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32))
                _ = mt.translate("olá" if self.cfg.src_lang == "pt" else "hello")
                _ = tts.synthesize("warm up")

            vad = WebRTCVADGate(aggressiveness=2)

            audio_q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=400)
            seg_q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=4)

            cap_t = threading.Thread(
                target=self._capture, args=(audio_q,), daemon=True
            )
            seg_t = threading.Thread(
                target=self._segmenter, args=(audio_q, seg_q, vad), daemon=True
            )
            cap_t.start()
            seg_t.start()
            self._threads.extend([cap_t, seg_t])

            self.signals.status.emit("Ouvindo...")

            stats = Stats()
            while not self._stop.is_set():
                try:
                    seg = seg_q.get(timeout=0.2)
                except queue.Empty:
                    continue

                t0 = time.perf_counter()
                text = stt.transcribe(seg)
                t_stt = (time.perf_counter() - t0) * 1000
                if not text.strip():
                    continue
                self.signals.stt_text.emit(text)

                t0 = time.perf_counter()
                translated = mt.translate(text)
                t_mt = (time.perf_counter() - t0) * 1000
                self.signals.mt_text.emit(translated)

                t0 = time.perf_counter()
                audio_out = tts.synthesize(translated)
                t_tts = (time.perf_counter() - t0) * 1000

                stats.add(t_stt, t_mt, t_tts)
                total = t_stt + t_mt + t_tts
                self.signals.latency.emit(t_stt, t_mt, t_tts, total)

                self._play(audio_out, tts.sample_rate)
        except Exception as e:
            self.signals.error.emit(f"{type(e).__name__}: {e}")

    def _capture(self, audio_q: "queue.Queue[np.ndarray]") -> None:
        def cb(indata, frames, time_info, status):
            if status:
                pass
            mono = indata[:, 0] if indata.ndim > 1 else indata
            try:
                audio_q.put_nowait(mono.copy())
            except queue.Full:
                pass

        kwargs = dict(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=VAD_FRAME_SAMPLES,
            device=self.capture_device,
            callback=cb,
        )
        if self.use_loopback:
            try:
                kwargs["extra_settings"] = sd.WasapiSettings(loopback=True)
                kwargs["channels"] = 2
            except Exception:
                pass

            def cb_loopback(indata, frames, time_info, status):
                mono = indata.mean(axis=1) if indata.ndim > 1 else indata
                try:
                    audio_q.put_nowait(mono.astype(np.float32).copy())
                except queue.Full:
                    pass

            kwargs["callback"] = cb_loopback

        with sd.InputStream(**kwargs):
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

        while not self._stop.is_set():
            try:
                frame = audio_q.get(timeout=0.1)
            except queue.Empty:
                continue
            if len(frame) != VAD_FRAME_SAMPLES:
                # loopback pode vir em blocos maiores - fatiar
                i = 0
                while i + VAD_FRAME_SAMPLES <= len(frame):
                    sub = frame[i : i + VAD_FRAME_SAMPLES]
                    self._handle_frame(sub, vad, pre_buf, speaking, seg_q, pre_max)
                    i += VAD_FRAME_SAMPLES
                continue
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
                            pass
                    in_speech = False
                    speaking = []
                    silence_run = 0

    @staticmethod
    def _handle_frame(frame, vad, pre_buf, speaking, seg_q, pre_max):
        # Fallback simples usado quando frame vem do loopback em tamanho diferente
        pre_buf.append(frame)

    def _play(self, pcm: np.ndarray, sr: int) -> None:
        if len(pcm) == 0:
            return
        targets: list[int] = []
        if self.cfg.play_on_fone and self.cfg.fone_device is not None:
            targets.append(self.cfg.fone_device)
        if self.cfg.play_on_virtual and self.cfg.virtual_device is not None:
            targets.append(self.cfg.virtual_device)
        if not targets:
            sd.play(pcm, samplerate=sr, blocking=True)
            return
        # Tocar em cada saída (sequencialmente; paralelo causaria mais atraso)
        for dev in targets:
            try:
                sd.play(pcm, samplerate=sr, device=dev, blocking=True)
            except Exception as e:
                self.signals.error.emit(f"play(dev={dev}): {e}")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("TransDualDisc — Fase 1")
        self.resize(1100, 720)
        self._workers: list[PipelineWorker] = []
        self._lat_stt: deque[float] = deque(maxlen=50)
        self._lat_total: deque[float] = deque(maxlen=50)

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)

        # === Painel esquerdo: configuração ===
        cfg_box = QGroupBox("Configuração")
        cfg_form = QFormLayout(cfg_box)

        self.direction = QComboBox()
        for label, *_ in DIRECTIONS:
            self.direction.addItem(label)
        cfg_form.addRow("Direção", self.direction)

        self.mode = QComboBox()
        for m in MODES:
            self.mode.addItem(m)
        cfg_form.addRow("Modo", self.mode)

        self.model = QComboBox()
        self.model.addItems(["tiny", "base", "small", "medium"])
        self.model.setCurrentText("small")
        cfg_form.addRow("Modelo STT", self.model)

        self.device_choice = QComboBox()
        self.device_choice.addItems(["auto", "cuda", "cpu"])
        cfg_form.addRow("STT device", self.device_choice)

        self.mic_combo = QComboBox()
        for idx, name in list_input_devices():
            self.mic_combo.addItem(name, idx)
        cfg_form.addRow("Microfone", self.mic_combo)

        self.loopback_combo = QComboBox()
        self.loopback_combo.addItem("(nenhum)", None)
        for idx, name in list_wasapi_output_devices():
            self.loopback_combo.addItem(name, idx)
        cfg_form.addRow("Loopback (escutar)", self.loopback_combo)

        self.fone_combo = QComboBox()
        for idx, name in list_output_devices():
            self.fone_combo.addItem(name, idx)
        cfg_form.addRow("Fone/Alto-falante", self.fone_combo)

        self.virtual_combo = QComboBox()
        self.virtual_combo.addItem("(nenhum — VB-CABLE não detectado)", None)
        vb = find_vb_cable_output()
        for idx, name in list_output_devices():
            self.virtual_combo.addItem(name, idx)
        if vb is not None:
            # Pré-seleciona VB-CABLE
            for i in range(self.virtual_combo.count()):
                if self.virtual_combo.itemData(i) == vb:
                    self.virtual_combo.setCurrentIndex(i)
                    break
        cfg_form.addRow("Saída virtual p/ Discord", self.virtual_combo)

        self.chk_play_fone = QCheckBox("Ouvir no fone (monitor)")
        self.chk_play_fone.setChecked(False)
        cfg_form.addRow(self.chk_play_fone)

        self.chk_play_virtual = QCheckBox("Enviar para saída virtual (Discord)")
        self.chk_play_virtual.setChecked(vb is not None)
        cfg_form.addRow(self.chk_play_virtual)

        self.btn_start = QPushButton("▶ Iniciar")
        self.btn_start.clicked.connect(self._on_start)
        cfg_form.addRow(self.btn_start)

        self.btn_stop = QPushButton("■ Parar")
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_stop.setEnabled(False)
        cfg_form.addRow(self.btn_stop)

        self.status_lbl = QLabel("Pronto.")
        self.status_lbl.setWordWrap(True)
        cfg_form.addRow("Status", self.status_lbl)

        self.lat_lbl = QLabel("— ms")
        f = QFont()
        f.setFamily("Consolas")
        f.setPointSize(10)
        self.lat_lbl.setFont(f)
        cfg_form.addRow("Latência", self.lat_lbl)

        root.addWidget(cfg_box, 1)

        # === Painel direito: transcrição ao vivo ===
        live_box = QGroupBox("Transcrição ao vivo")
        live_layout = QVBoxLayout(live_box)

        live_layout.addWidget(QLabel("<b>STT (origem)</b>"))
        self.stt_view = QTextEdit()
        self.stt_view.setReadOnly(True)
        self.stt_view.setFont(f)
        live_layout.addWidget(self.stt_view)

        live_layout.addWidget(QLabel("<b>MT (destino)</b>"))
        self.mt_view = QTextEdit()
        self.mt_view.setReadOnly(True)
        self.mt_view.setFont(f)
        live_layout.addWidget(self.mt_view)

        btns = QHBoxLayout()
        btn_clear = QPushButton("Limpar")
        btn_clear.clicked.connect(self._clear_views)
        btns.addWidget(btn_clear)
        btns.addStretch(1)
        live_layout.addLayout(btns)

        root.addWidget(live_box, 2)

    def _selected_direction(self) -> tuple[str, str]:
        i = self.direction.currentIndex()
        _, src, tgt = DIRECTIONS[i]
        return src, tgt

    def _on_start(self) -> None:
        src, tgt = self._selected_direction()
        dev_choice = self.device_choice.currentText()
        if dev_choice == "auto":
            device, ct = detect_device()
        elif dev_choice == "cuda":
            device, ct = "cuda", "float16"
        else:
            device, ct = "cpu", "int8"

        cfg = PipelineConfig(
            src_lang=src,
            tgt_lang=tgt,
            model_size=self.model.currentText(),
            device=device,
            compute_type=ct,
            input_device=self.mic_combo.currentData(),
            loopback_device=self.loopback_combo.currentData(),
            fone_device=self.fone_combo.currentData(),
            virtual_device=self.virtual_combo.currentData(),
            play_on_fone=self.chk_play_fone.isChecked(),
            play_on_virtual=self.chk_play_virtual.isChecked(),
        )

        mode_idx = self.mode.currentIndex()
        # 0=FALAR, 1=ESCUTAR, 2=AMBOS
        self.signals_falar = PipelineSignals()
        self.signals_escutar = PipelineSignals()
        self._wire_signals(self.signals_falar, "FALAR")
        self._wire_signals(self.signals_escutar, "ESCUTAR")

        self._workers.clear()

        if mode_idx in (0, 2):  # FALAR ou AMBOS
            w = PipelineWorker(
                cfg=cfg,
                capture_device=cfg.input_device,
                use_loopback=False,
                signals=self.signals_falar,
            )
            self._workers.append(w)
        if mode_idx in (1, 2):  # ESCUTAR ou AMBOS
            if cfg.loopback_device is None:
                self.status_lbl.setText("❌ Selecione um dispositivo de loopback para modo ESCUTAR.")
                return
            # No modo escutar a direção é invertida: se usuário escolheu PT->EN,
            # provavelmente quer ouvir EN (do Discord) e traduzir pra PT.
            cfg_listen = PipelineConfig(
                src_lang=tgt,  # inverte
                tgt_lang=src,
                model_size=cfg.model_size,
                device=cfg.device,
                compute_type=cfg.compute_type,
                input_device=None,
                loopback_device=cfg.loopback_device,
                fone_device=cfg.fone_device,
                virtual_device=None,
                play_on_fone=True,
                play_on_virtual=False,
            )
            w = PipelineWorker(
                cfg=cfg_listen,
                capture_device=cfg.loopback_device,
                use_loopback=True,
                signals=self.signals_escutar,
            )
            self._workers.append(w)

        for w in self._workers:
            w.start()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.status_lbl.setText("Iniciando...")

    def _wire_signals(self, signals: PipelineSignals, prefix: str) -> None:
        signals.status.connect(lambda s, p=prefix: self.status_lbl.setText(f"[{p}] {s}"))
        signals.stt_text.connect(lambda s, p=prefix: self._append(self.stt_view, f"[{p}] {s}"))
        signals.mt_text.connect(lambda s, p=prefix: self._append(self.mt_view, f"[{p}] {s}"))
        signals.latency.connect(self._on_latency)
        signals.error.connect(lambda e, p=prefix: self.status_lbl.setText(f"[{p}] ERRO: {e}"))

    def _append(self, view: QTextEdit, text: str) -> None:
        view.moveCursor(QTextCursor.MoveOperation.End)
        view.insertPlainText(text + "\n")
        view.moveCursor(QTextCursor.MoveOperation.End)

    def _on_latency(self, t_stt: float, t_mt: float, t_tts: float, total: float) -> None:
        self._lat_stt.append(t_stt)
        self._lat_total.append(total)
        p50 = float(np.percentile(list(self._lat_total), 50))
        p95 = float(np.percentile(list(self._lat_total), 95))
        self.lat_lbl.setText(
            f"last: STT={t_stt:.0f} MT={t_mt:.0f} TTS={t_tts:.0f} total={total:.0f}ms\n"
            f"rolling(n={len(self._lat_total)}): p50={p50:.0f} p95={p95:.0f}"
        )

    def _on_stop(self) -> None:
        for w in self._workers:
            w.stop()
        self._workers.clear()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.status_lbl.setText("Parado.")

    def _clear_views(self) -> None:
        self.stt_view.clear()
        self.mt_view.clear()
        self._lat_stt.clear()
        self._lat_total.clear()
        self.lat_lbl.setText("— ms")

    def closeEvent(self, event) -> None:
        self._on_stop()
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
