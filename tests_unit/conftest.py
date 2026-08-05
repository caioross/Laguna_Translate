"""Configuração compartilhada dos testes unitários de lógica pura.

Estes testes NÃO tocam áudio, modelos, GPU, microfone ou rede. Para importar
os módulos do pipeline (`fase0_poc`, `laguna_core`) sem exigir PortAudio, o
módulo nativo `sounddevice` é substituído por um stub vazio — o código só usa
`sd.*` em runtime (captura/reprodução), nunca no import nem nos construtores
exercitados aqui.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

# Raiz do repo = pai de tests_unit/ — garante que os módulos do pipeline
# sejam importáveis independentemente do diretório de invocação do pytest.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Stub de sounddevice: importar o pipeline não deve exigir PortAudio nem
# acesso a dispositivos. Substituído de forma hermética (mesmo se o real
# estiver instalado) para que os testes rodem igual em qualquer ambiente.
_sd_stub = types.ModuleType("sounddevice")
_sd_stub.__doc__ = "stub de teste (tests_unit/conftest.py) — sem áudio real"
sys.modules["sounddevice"] = _sd_stub

# Stub de uvicorn + fastapi: alguns testes de lógica pura importam
# `laguna_server` (para exercitar `validate_direction_config`), mas nunca sobem
# o servidor HTTP. fastapi e uvicorn são deps declaradas do app, mas o job
# "lógica pura" da CI instala só `pytest numpy` — nenhum dos dois está lá, e
# não devem passar a estar (o job é rápido de propósito). Cobrimos só o que
# `laguna_server` toca no import: `FastAPI(...)`, os decorators `@app.get/post/
# websocket`, `app.mount`, `StaticFiles`, `JSONResponse` e `WebSocket*`. Hermético
# (mesmo se o real estiver instalado), como o stub de sounddevice acima — estes
# testes não exercitam a camada HTTP real.
_uvicorn_stub = types.ModuleType("uvicorn")
_uvicorn_stub.run = lambda *a, **k: None
sys.modules["uvicorn"] = _uvicorn_stub


def _identity_decorator(*_a, **_k):
    def _wrap(func):
        return func

    return _wrap


class _StubFastAPI:
    def __init__(self, *a, **k):
        pass

    # @app.get/post/put/delete/patch/websocket(...) -> devolve o func inalterado
    get = post = put = delete = patch = websocket = _identity_decorator

    def mount(self, *a, **k):
        return None

    def add_middleware(self, *a, **k):
        return None


class _StubWebSocket:  # usado só como type hint / elemento de set
    pass


class _StubWebSocketDisconnect(Exception):  # usado em `except WebSocketDisconnect`
    pass


class _StubJSONResponse:  # construído em runtime pelas rotas; não exercitado aqui
    def __init__(self, content=None, status_code=200, *a, **k):
        self.content = content
        self.status_code = status_code


class _StubStaticFiles:  # app.mount(StaticFiles(...)) no import
    def __init__(self, *a, **k):
        pass


_fastapi_stub = types.ModuleType("fastapi")
_fastapi_stub.FastAPI = _StubFastAPI
_fastapi_stub.WebSocket = _StubWebSocket
_fastapi_stub.WebSocketDisconnect = _StubWebSocketDisconnect

_fastapi_responses = types.ModuleType("fastapi.responses")
_fastapi_responses.JSONResponse = _StubJSONResponse
_fastapi_stub.responses = _fastapi_responses

_fastapi_staticfiles = types.ModuleType("fastapi.staticfiles")
_fastapi_staticfiles.StaticFiles = _StubStaticFiles
_fastapi_stub.staticfiles = _fastapi_staticfiles

sys.modules["fastapi"] = _fastapi_stub
sys.modules["fastapi.responses"] = _fastapi_responses
sys.modules["fastapi.staticfiles"] = _fastapi_staticfiles
