"""Laguna Translator — servidor FastAPI + WebSocket.

Serve UI web em http://127.0.0.1:7531 e expoe WebSocket /ws para live
updates. Gerencia dois DirectionWorkers (falar/escutar) simultaneos.

Uso:
    C:/Python313/python.exe laguna_server.py

Por padrao abre o navegador automaticamente.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import socket
import sys
import threading
import time
import traceback
import webbrowser
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent
STATIC = ROOT / "static"
HOST = "127.0.0.1"
PORT = 7531
URL = f"http://{HOST}:{PORT}"

# Diagnostico de startup (o atalho do Desktop roda sob pythonw, sem console:
# sem isto, qualquer falha de start some sem deixar rastro). `*.log` e ignorado
# pelo .gitignore — o arquivo nunca entra no repo.
LOG_PATH = ROOT / "laguna.log"
READY_TIMEOUT_S = 15.0  # espera maxima ate o servidor aceitar conexao
READY_POLL_S = 0.1
PROBE_TIMEOUT_S = 1.5  # probe HTTP em quem ja ocupa a porta
PROBE_MAX_BYTES = 4096  # teto do corpo lido de quem ocupa a porta (pode ser hostil)
NOTIFY_TIMEOUT_MS = 120_000  # prazo do MessageBox: sem ninguem para clicar, ele fecha sozinho


# ---------------------------------------------------------------------------
# Startup: diagnostico visivel (#58)
#
# O atalho do Desktop roda `pythonw laguna_server.py` com janela oculta: sem
# console, stdout/stderr vao para o vazio. Todo caminho de erro precisa deixar
# rastro em disco (laguna.log) e, quando nao ha console, sinal na tela
# (MessageBox do Windows). Nada disso toca o caminho feliz.
#
# Estes tres helpers ficam ANTES dos imports pesados de proposito: dependencia
# faltando (foi o #56) e o modo de falha mais comum do atalho, e ele acontece
# no topo do modulo — antes de qualquer linha de `main()` poder registrar algo.
# Eles usam so a stdlib, entao sempre estao disponiveis.
# ---------------------------------------------------------------------------


def _log(msg: str, exc: Optional[BaseException] = None) -> None:
    """Anexa uma linha (e o traceback, se houver) em `laguna.log`.

    Nunca levanta: log e diagnostico, nao pode ser o motivo de o app cair.
    """
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = f"[{stamp}] {msg}\n"
    if exc is not None:
        text += "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    with suppress(Exception):
        with open(LOG_PATH, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(text)


def _is_headless(executable: str, stderr: object, console_hidden: bool = False) -> bool:
    """True quando nao ha console que o usuario possa ler.

    Tres formas do mesmo problema: sob `pythonw.exe` o Python nao anexa console
    e `sys.stderr` e None; o nome do executavel cobre ambientes que ainda assim
    entregam um stderr qualquer; e `console_hidden` cobre o fallback do atalho
    (`Laguna.vbs:11-13` roda `python.exe` com window style 0 — ha console, ha
    stderr, e ninguem ve nada).

    O basename e extraido na mao (e nao via `Path`) porque o caminho e sempre
    do Windows: num `PosixPath`, `C:\\Python313\\pythonw.exe` nao teria
    separador e o teste desta funcao rodaria diferente na CI (Linux).
    """
    if stderr is None or console_hidden:
        return True
    name = (executable or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name.startswith("pythonw")


def _console_is_hidden() -> bool:
    """No Windows, o console deste processo esta ausente ou oculto?

    `GetConsoleWindow() == 0` = sem console; janela existente porem invisivel =
    o caso do `wscript` rodando `python.exe` escondido. Qualquer erro aqui
    degrada para False (o MessageBox e um extra, nunca um requisito).
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if not hwnd:
            return True
        return not bool(ctypes.windll.user32.IsWindowVisible(hwnd))
    except Exception:
        return False


def _notify_error(msg: str) -> None:
    """Sinaliza falha de startup: stderr sempre; MessageBox se ninguem le stderr.

    So no caminho de erro e so no Windows — `ctypes` e stdlib, sem dependencia
    nova. Um MessageBox aqui e o unico jeito de o usuario do atalho saber que
    algo falhou (hoje ele so ve "nada acontece").

    A caixa e SEMPRE com prazo (`MessageBoxTimeoutW`): sem ninguem para clicar
    — tarefa agendada, sessao 0, shell automatizado — um MessageBoxW comum
    prende o processo para sempre e troca "morre calado" por "trava calado".
    """
    with suppress(Exception):
        print(f"[laguna] {msg}", file=sys.stderr)
    if sys.platform != "win32":
        return
    if not _is_headless(sys.executable, sys.stderr, _console_is_hidden()):
        return
    with suppress(Exception):
        import ctypes

        user32 = ctypes.windll.user32
        flags = 0x10 | 0x40000 | 0x1000  # MB_ICONERROR | MB_TOPMOST | MB_SYSTEMMODAL
        box = getattr(user32, "MessageBoxTimeoutW", None)
        if box is not None:
            box(0, msg, "Laguna Translator", flags, 0, int(NOTIFY_TIMEOUT_MS))
        else:  # pragma: no cover — API presente em todo Windows suportado
            user32.MessageBoxW(0, msg, "Laguna Translator", flags)


try:
    import sounddevice as sd
    import uvicorn
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import JSONResponse
    from fastapi.staticfiles import StaticFiles

    from laguna_core import DirectionConfig, DirectionWorker
    from laguna_devices import detect_laguna_devices, list_devices
    from laguna_pipeline import SAMPLE_RATE
except BaseException as _import_exc:
    # Sem isto, `pip install` incompleto = duplo-clique que nao faz nada.
    # A excecao original segue subindo (quem roda no console ve o traceback
    # de sempre); o que muda e que agora fica registrada e visivel.
    _log("falha ao importar as dependencias do servidor", _import_exc)
    _notify_error(
        "O Laguna não conseguiu iniciar: falta uma dependência.\n\n"
        f"{type(_import_exc).__name__}: {_import_exc}\n\n"
        "Instale as dependências com:\n"
        "C:\\Python313\\python.exe -m pip install -r requirements.txt\n\n"
        f"Detalhes em: {LOG_PATH}"
    )
    raise

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
    """Direcoes REALMENTE vivas — filtra a lista, nao despeja o dicionario.

    `_workers` so perdia entrada no `/api/stop`. Uma direcao que morre por conta
    propria (captura perdida, falhas consecutivas, falha de setup) continuava
    listada, e o `hello` do WS respondia com ela em `running`: um F5 — reacao
    natural de quem acabou de ver um erro — repintava o painel de verde "Em
    execucao" sobre uma direcao que nao traduz mais (issue #62).

    Filtrar basta: `_stop` nunca e limpo, entao `is_alive()` e monotonico e a
    direcao morta jamais reaparece em `running`. Remover a entrada aqui seria
    jogar fora a UNICA referencia ao worker num instante em que ele AINDA segura
    device — `_stop` e setado no comeco do `finally` de `_run` e `_close_sinks()`
    so roda depois que o laco de traducao retorna (pode levar segundos, ou nunca
    se um engine pendurar). Sem a referencia, o `existing.stop()` do `/api/start`
    e o `stop()` do `/api/stop` viram no-op silencioso e o device de saida fica
    preso ate o processo morrer — exatamente o vazamento que a #38 fechou. Quem
    solta o worker do dicionario continua sendo `/api/stop` (ou o proximo start).
    """
    with _lock:
        return [n for n, w in _workers.items() if w.is_alive()]


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


def _port_is_free(host: str, port: int) -> bool:
    """True se ainda da para escutar em host:port (no Windows o bind e exclusivo).

    O socket e fechado antes de entregar a porta ao uvicorn; a janela de corrida
    e irrelevante num app de desktop single-user (e o `uvicorn.run` que falhar
    cai no tratamento de erro do `main`).
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, port))
        return True
    except OSError:
        return False


def _is_laguna_status(payload: object) -> bool:
    """O corpo de `/api/status` tem a cara do Laguna? (chave `running` = lista)."""
    return isinstance(payload, dict) and isinstance(payload.get("running"), list)


def _probe_laguna(host: str = HOST, port: int = PORT, timeout: float = PROBE_TIMEOUT_S) -> bool:
    """Quem ocupa a porta e outra instancia do Laguna?

    Conexao crua com `http.client` — e NAO `urllib.request` — porque o opener
    default do urllib faz duas coisas incompativeis com a promessa 100% local:
    herda o proxy do sistema/`http_proxy` (que nao faz bypass de loopback, entao
    o request sairia da maquina) e segue redirect 3xx para host arbitrario. Esta
    funcao so roda quando a porta esta ocupada por um processo DESCONHECIDO —
    exatamente o cenario em que seguir instrucao de terceiro seria ruim. Aqui o
    socket e 127.0.0.1 e ponto final: sem proxy, sem redirect.
    """
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", "/api/status")
        resp = conn.getresponse()
        if resp.status != 200:
            return False
        # Leitura limitada: um servidor hostil que goteja bytes nao prende o startup.
        return _is_laguna_status(json.loads(resp.read(PROBE_MAX_BYTES).decode("utf-8", "replace")))
    except Exception:
        return False
    finally:
        with suppress(Exception):
            conn.close()


def _wait_until_listening(
    host: str, port: int, timeout: float = READY_TIMEOUT_S, poll: float = READY_POLL_S
) -> bool:
    """Poll curto ate o servidor aceitar conexao. Substitui o `sleep(1.2)` fixo."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        with suppress(OSError):
            with socket.create_connection((host, port), timeout=0.2):
                return True
        time.sleep(poll)
    return False


def _open_browser_when_ready() -> None:
    """Abre a UI SO depois que o servidor esta de fato ouvindo."""
    if not _wait_until_listening(HOST, PORT):
        _log(f"servidor nao comecou a ouvir em {URL} em {READY_TIMEOUT_S:.0f}s; navegador nao aberto")
        return
    with suppress(Exception):
        webbrowser.open(URL)


def _serve() -> int:
    """Sobe o servidor (ou explica por que nao subiu). Retorna o codigo de saida."""
    if not _port_is_free(HOST, PORT):
        if _probe_laguna():
            _log(f"porta {PORT} ja atendida por outra instancia do Laguna; abrindo {URL}")
            with suppress(Exception):
                print(f"[laguna] ja esta rodando em {URL} - abrindo o navegador", file=sys.stderr)
            with suppress(Exception):
                webbrowser.open(URL)
            return 0
        _log(f"porta {PORT} ocupada por um processo que nao responde como Laguna")
        _notify_error(
            f"A porta {PORT} está ocupada por outro programa, então o Laguna não pode iniciar.\n\n"
            f"Feche o programa que usa a porta {PORT} e abra o Laguna de novo.\n\n"
            f"Detalhes em: {LOG_PATH}"
        )
        return 1

    threading.Thread(target=_open_browser_when_ready, daemon=True, name="laguna-open-browser").start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
    return 0


def _exit_code(code: object) -> int:
    """Normaliza o `code` de um SystemExit para um codigo de saida inteiro."""
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    return 1  # SystemExit("mensagem") = erro, com a mensagem impressa pelo Python


def main() -> int:
    _log(f"iniciando laguna_server em {URL} (python {sys.version.split()[0]})")
    try:
        return _serve()
    except KeyboardInterrupt:
        raise
    except SystemExit as exc:
        # O uvicorn sai por `sys.exit(1)` quando o bind falha ou o startup ASGI
        # quebra — o caminho mais provavel de "o servidor nao subiu". Sem este
        # ramo, esses dois casos voltam a morrer calados sob pythonw.
        code = _exit_code(exc.code)
        if code == 0:
            raise
        _log(f"servidor encerrou com codigo {code} sem subir", exc)
        _notify_error(
            f"O Laguna não conseguiu iniciar (o servidor encerrou com o código {code}).\n\n"
            f"Detalhes em: {LOG_PATH}"
        )
        return code
    except BaseException as exc:  # noqa: BLE001 — ultimo anteparo: sem isto o erro some
        _log("falha fatal no startup", exc)
        _notify_error(
            f"O Laguna não conseguiu iniciar.\n\n{type(exc).__name__}: {exc}\n\n"
            f"Traceback completo em: {LOG_PATH}"
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
