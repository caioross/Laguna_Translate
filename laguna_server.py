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

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from laguna_core import DirectionConfig, DirectionWorker, detect_laguna_devices, list_devices

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


@app.get("/api/status")
async def api_status() -> JSONResponse:
    return JSONResponse(
        {
            "running": list(_workers.keys()),
        }
    )


@app.post("/api/start/{direction}")
async def api_start(direction: str, cfg: dict) -> JSONResponse:
    if direction not in ("falar", "escutar"):
        return JSONResponse({"error": "direction deve ser falar|escutar"}, status_code=400)
    with _lock:
        existing = _workers.get(direction)
        if existing is not None:
            existing.stop()
            _workers.pop(direction, None)
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
        return JSONResponse({"error": "direction deve ser falar|escutar"}, status_code=400)
    with _lock:
        w = _workers.get(direction)
    if w is None:
        return JSONResponse({"ok": True, "note": "worker parado; ganho sera aplicado no proximo start"})
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
        await ws.send_text(json.dumps({"kind": "hello", "running": list(_workers.keys())}))
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
