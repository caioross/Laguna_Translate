"""Laguna Translator — servidor FastAPI + WebSocket.

Serve UI web em http://127.0.0.1:7531 e expoe WebSocket /ws para live
updates. Gerencia dois DirectionWorkers (falar/escutar) simultaneos.

Uso:
    C:/Python313/python.exe laguna_server.py

Por padrao abre o navegador automaticamente.
"""

from __future__ import annotations

import asyncio
import json
import threading
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import sounddevice as sd
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from laguna_core import DirectionConfig, DirectionWorker
from laguna_devices import detect_laguna_devices, list_devices
from laguna_pipeline import SAMPLE_RATE

ROOT = Path(__file__).parent
STATIC = ROOT / "static"
HOST = "127.0.0.1"
PORT = 7531

# estado global
_workers: dict[str, DirectionWorker] = {}
_clients: set[WebSocket] = set()
_loop: Optional[asyncio.AbstractEventLoop] = None
_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _loop
    _loop = asyncio.get_running_loop()
    yield
    for w in list(_workers.values()):
        w.stop()


app = FastAPI(title="Laguna Translator", lifespan=lifespan)


def _broadcast(msg: dict) -> None:
    """Enviado de threads de worker; agenda envio no loop asyncio."""
    if _loop is None:
        return
    payload = json.dumps(msg)
    asyncio.run_coroutine_threadsafe(_broadcast_async(payload), _loop)


async def _broadcast_async(payload: str) -> None:
    dead = []
    for ws in _clients:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _clients.discard(ws)


@app.get("/api/devices")
async def api_devices() -> JSONResponse:
    data = list_devices()
    data["laguna"] = detect_laguna_devices()
    return JSONResponse(data)


def _running_directions() -> list[str]:
    """Direcoes REALMENTE vivas, despejando as que morreram sozinhas.

    `_workers` so perdia entrada no `/api/stop`. Uma direcao que morre por conta
    propria (captura perdida, falhas consecutivas, falha de setup) continuava
    listada, e o `hello` do WS respondia com ela em `running`: um F5 — reacao
    natural de quem acabou de ver um erro — repintava o painel de verde "Em
    execucao" sobre uma direcao que nao traduz mais (issue #62).

    So remove do dicionario; nao chama `stop()`. O worker morto ja fechou os
    proprios sinks no `finally` de `_run`, e `stop()` faz `join` de ate 2s por
    thread — caro demais para um handler async.
    """
    with _lock:
        for name in [n for n, w in _workers.items() if not w.is_alive()]:
            _workers.pop(name, None)
        return list(_workers.keys())


@app.get("/api/status")
async def api_status() -> JSONResponse:
    return JSONResponse(
        {
            "running": _running_directions(),
        }
    )


def _dev_str(idx: Optional[int]) -> str:
    return "default" if idx is None else str(idx)


def validate_direction_config(dcfg: DirectionConfig) -> list[dict]:
    """Valida os devices de um DirectionConfig ANTES de criar o worker.

    Retorna lista de erros (cada um `{error_key, args:{device, detail}, msg}`);
    lista vazia = configuracao valida. E BARATA (so `query_devices` + `check_*`,
    nunca abre stream nem carrega modelo) para nao regredir a latencia de start.

    Falha do PROPRIO validador (sounddevice sem PortAudio, ambiente atipico,
    excecao inesperada) degrada para "nao validado" -> retorna `[]` e deixa o
    fluxo de start seguir. Nunca bloqueia o start por um bug do validador.
    """
    try:
        return _validate_direction_config(dcfg)
    except Exception:
        return []


def _validate_direction_config(dcfg: DirectionConfig) -> list[dict]:
    errors: list[dict] = []
    n_devices = len(sd.query_devices())
    pa_error = getattr(sd, "PortAudioError", Exception)

    def _in_range(idx: Optional[int]) -> bool:
        return idx is None or (isinstance(idx, int) and 0 <= idx < n_devices)

    def _max_out_ch(idx: Optional[int]) -> Optional[int]:
        if idx is None:
            return None
        try:
            return int(sd.query_devices(idx).get("max_output_channels") or 0)
        except Exception:
            return None

    def _err(key: str, device: Optional[int], detail: str, msg: str) -> None:
        errors.append(
            {"error_key": key, "args": {"device": _dev_str(device), "detail": detail}, "msg": msg}
        )

    # --- captura ------------------------------------------------------------
    cap = dcfg.capture_device
    if not _in_range(cap):
        _err(
            "error.start_capture_device_invalid", cap, "indice fora do intervalo",
            f"dispositivo de captura [{_dev_str(cap)}] nao existe mais",
        )
    elif dcfg.use_loopback:
        # Loopback captura de um endpoint de SAIDA (render): precisa existir e ter
        # canal de saida. A validacao profunda com WasapiSettings(loopback=...)
        # varia por versao do sounddevice; se essa versao nao suportar o kwarg,
        # degrada (nao bloqueia) — o _capture faz o mesmo fallback.
        out_ch = _max_out_ch(cap)
        if out_ch is not None and out_ch <= 0:
            _err(
                "error.start_capture_device_invalid", cap, "sem canal de saida para loopback",
                f"dispositivo de captura [{_dev_str(cap)}] nao e uma saida valida para loopback",
            )
        else:
            try:
                extra = sd.WasapiSettings(loopback=True)
            except Exception:
                extra = None
            if extra is not None:
                try:
                    sd.check_input_settings(
                        device=cap, channels=2, dtype="float32",
                        samplerate=SAMPLE_RATE, extra_settings=extra,
                    )
                except pa_error as e:
                    _err(
                        "error.start_capture_device_invalid", cap, str(e),
                        f"dispositivo de captura [{_dev_str(cap)}] rejeitou o loopback",
                    )
    else:
        # Captura normal: mesmos parametros do _capture (mono, 16 kHz, float32).
        try:
            sd.check_input_settings(
                device=cap, channels=1, dtype="float32", samplerate=SAMPLE_RATE
            )
        except pa_error as e:
            _err(
                "error.start_capture_device_invalid", cap, str(e),
                f"dispositivo de captura [{_dev_str(cap)}] invalido",
            )

    # --- saidas (output_devices + passthrough) ------------------------------
    out_targets = list(dcfg.output_devices or [])
    if dcfg.passthrough and dcfg.passthrough_device is not None:
        out_targets.append(dcfg.passthrough_device)
    for dev in out_targets:
        if not _in_range(dev):
            _err(
                "error.start_output_device_invalid", dev, "indice fora do intervalo",
                f"dispositivo de saida [{_dev_str(dev)}] nao existe mais",
            )
            continue
        out_ch = _max_out_ch(dev)
        if out_ch is not None and out_ch <= 0:
            _err(
                "error.start_output_device_invalid", dev, "sem canal de saida",
                f"dispositivo de saida [{_dev_str(dev)}] nao tem canal de saida",
            )
            continue
        try:
            sd.check_output_settings(device=dev, channels=1, dtype="float32")
        except pa_error as e:
            _err(
                "error.start_output_device_invalid", dev, str(e),
                f"dispositivo de saida [{_dev_str(dev)}] invalido",
            )

    return errors


@app.post("/api/start/{direction}")
async def api_start(direction: str, cfg: dict) -> JSONResponse:
    if direction not in ("falar", "escutar"):
        return JSONResponse(
            {
                "error_key": "error.invalid_direction",
                "error": "direction deve ser falar|escutar",
            },
            status_code=400,
        )
    dcfg = DirectionConfig(
        name=direction,
        src_lang=cfg["src_lang"],
        tgt_lang=cfg["tgt_lang"],
        capture_device=cfg.get("capture_device"),
        use_loopback=bool(cfg.get("use_loopback", False)),
        output_devices=[int(x) for x in cfg.get("output_devices", []) if x is not None],
        model_size=cfg.get("model_size", "small"),
        device=cfg.get("device", "auto"),
        compute_type=cfg.get("compute_type"),
        skip_same_lang=bool(cfg.get("skip_same_lang", True)),
        passthrough=bool(cfg.get("passthrough", False)),
        passthrough_device=cfg.get("passthrough_device"),
        output_gain_db=float(cfg.get("output_gain_db", 0.0)),
        passthrough_gain_db=float(cfg.get("passthrough_gain_db", 0.0)),
    )

    # Pre-validacao barata: config invalida -> 400 e NENHUM worker e criado
    # (nem sequer para o worker que ja estiver rodando nesta direcao).
    errors = validate_direction_config(dcfg)
    if errors:
        first = errors[0]
        return JSONResponse(
            {
                "error_key": first["error_key"],
                "args": first.get("args", {}),
                "error": first.get("msg", "configuracao de dispositivo invalida"),
                "errors": errors,
            },
            status_code=400,
        )

    with _lock:
        existing = _workers.get(direction)
        if existing is not None:
            existing.stop()
            _workers.pop(direction, None)
        worker = DirectionWorker(dcfg, on_event=_broadcast)
        _workers[direction] = worker
        worker.start()
    return JSONResponse({"ok": True, "direction": direction})


@app.post("/api/stop/{direction}")
async def api_stop(direction: str) -> JSONResponse:
    with _lock:
        w = _workers.pop(direction, None)
    if w is not None:
        w.stop()
    return JSONResponse({"ok": True, "direction": direction})


@app.post("/api/gain/{direction}")
async def api_gain(direction: str, body: dict) -> JSONResponse:
    """Ajusta ganho de saida e/ou passthrough sem reiniciar o worker."""
    if direction not in ("falar", "escutar"):
        return JSONResponse(
            {
                "error_key": "error.invalid_direction",
                "error": "direction deve ser falar|escutar",
            },
            status_code=400,
        )
    with _lock:
        w = _workers.get(direction)
    if w is None:
        return JSONResponse(
            {
                "ok": True,
                "note_key": "note.worker_stopped_gain_deferred",
                "note": "worker parado; ganho sera aplicado no proximo start",
            }
        )
    if "output_gain_db" in body:
        w.set_output_gain_db(body["output_gain_db"])
    if "passthrough_gain_db" in body:
        w.set_passthrough_gain_db(body["passthrough_gain_db"])
    return JSONResponse(
        {
            "ok": True,
            "direction": direction,
            "output_gain_db": w.cfg.output_gain_db,
            "passthrough_gain_db": w.cfg.passthrough_gain_db,
        }
    )


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    _clients.add(ws)
    try:
        await ws.send_text(json.dumps({"kind": "hello", "running": _running_directions()}))
        while True:
            _ = await ws.receive_text()  # mantem conexao; comandos via REST
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(ws)


# Static mount por ultimo para nao sobrepor rotas /api e /ws
if STATIC.is_dir():
    app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")


def main() -> None:
    def _open():
        import time as _t

        _t.sleep(1.2)
        try:
            webbrowser.open(f"http://{HOST}:{PORT}")
        except Exception:
            pass

    threading.Thread(target=_open, daemon=True).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
