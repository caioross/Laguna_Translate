"""Laguna Translator — launcher com janela nativa (pywebview + WebView2).

Inicia o servidor FastAPI num thread em segundo plano e abre uma janela
desktop apontando para http://127.0.0.1:7531. Sem barra de URL, sem
browser — comporta como um programa Windows clássico.

Uso:
    C:/Python313/python.exe laguna_app.py

Se pywebview nao estiver disponivel, cai no modo navegador padrao
(laguna_server.main) como fallback.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from contextlib import suppress

HOST = "127.0.0.1"
PORT = 7531
URL = f"http://{HOST}:{PORT}"


def _wait_server_ready(timeout: float = 15.0) -> bool:
    """Tenta conectar no socket ate o server aceitar conexao."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        with suppress(OSError):
            with socket.create_connection((HOST, PORT), timeout=0.5):
                return True
        time.sleep(0.15)
    return False


def _run_server() -> None:
    import uvicorn

    from laguna_server import app

    # log_level='warning' pra nao poluir a janela no console
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


def main() -> None:
    # inicia o server num daemon thread
    t = threading.Thread(target=_run_server, daemon=True, name="laguna-server")
    t.start()

    # espera ate o socket responder antes de abrir a janela
    if not _wait_server_ready():
        print("[laguna] server nao respondeu a tempo; abrindo navegador como fallback", file=sys.stderr)
        import webbrowser

        webbrowser.open(URL)
        # mantem processo vivo pro server seguir rodando
        t.join()
        return

    try:
        import webview  # pywebview
    except Exception as exc:  # pragma: no cover
        print(f"[laguna] pywebview indisponivel ({exc}); abrindo navegador", file=sys.stderr)
        import webbrowser

        webbrowser.open(URL)
        t.join()
        return

    # janela nativa (WebView2 no Windows 11). Tamanho confortável p/ dois paineis.
    webview.create_window(
        title="Laguna Translator",
        url=URL,
        width=1280,
        height=860,
        min_size=(900, 640),
        background_color="#0b1220",
        resizable=True,
        text_select=True,
    )
    # gui='edgechromium' forca o WebView2 no Windows. Se indisponivel,
    # pywebview escolhe automaticamente.
    try:
        webview.start(gui="edgechromium", debug=False)
    except Exception:
        webview.start(debug=False)


if __name__ == "__main__":
    main()
