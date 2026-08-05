"""Laguna Translator — helpers de dispositivo de audio.

Enumeracao e rotulagem dos devices do PortAudio (via sounddevice) e deteccao do
par virtual renomeado para "Laguna" (fallback: VB-CABLE). Bloco extraido de
laguna_core.py SEM mudanca de comportamento (issue #40): listar dispositivos nao
precisa do motor de traducao — laguna_server importa daqui e deixa de arrastar o
DirectionWorker (numpy, threading, laguna_pipeline) so para popular os selects.

laguna_core reexporta estes simbolos por compat; codigo novo importa daqui.
"""

from __future__ import annotations

from typing import Optional

import sounddevice as sd


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


def reinit_portaudio() -> bool:
    """Re-inicializa o PortAudio para que a proxima enumeracao veja hardware novo.

    O PortAudio enumera os devices uma unica vez (no `Pa_Initialize` que o
    sounddevice dispara no primeiro uso do processo) e serve a lista de um cache
    estatico: sem este ciclo, `list_devices()` nunca ve um fone plugado depois,
    um VB-CABLE instalado depois nem um device renomeado (issue #37).

    `_terminate`/`_initialize` sao API PRIVADA do sounddevice: acesso defensivo
    (`getattr` + `try/except`). Versao sem esses simbolos, ou ciclo que falha,
    degrada para a enumeracao normal (retorna False) — nunca quebra quem chama.

    CUIDADO (responsabilidade de quem chama): `_terminate()` derruba streams
    abertos e REATRIBUI os indices dos devices. So chame com nenhum worker vivo.
    """
    terminate = getattr(sd, "_terminate", None)
    initialize = getattr(sd, "_initialize", None)
    if not callable(terminate) or not callable(initialize):
        return False
    try:
        terminate()
    except Exception:
        return False
    try:
        initialize()
        return True
    except Exception:
        # PortAudio ficou terminado: uma segunda tentativa e o melhor esforco
        # para nao deixar o processo sem enumeracao ate reiniciar o servidor.
        try:
            initialize()
        except Exception:
            pass
        return False


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
