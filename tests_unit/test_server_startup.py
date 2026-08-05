"""Alvo #58 — startup do servidor deixa de morrer em silencio.

O atalho do Desktop roda `pythonw laguna_server.py` (sem console): falha de
start nao pode sumir. Estes testes exercitam so a logica de startup — porta
livre/ocupada, deteccao de "ja e o Laguna", espera pelo listen, log e codigo de
saida. Nada de audio, modelo, GPU ou rede externa: os unicos sockets usados sao
loopback em porta efemera, criados e fechados pelo proprio teste (o `conftest`
ja substitui sounddevice/fastapi/uvicorn por stubs).
"""

from __future__ import annotations

import asyncio
import socket
import threading
from contextlib import closing, contextmanager, suppress

import pytest

import laguna_server


def _http_response(body: bytes, status: str = "200 OK") -> bytes:
    return (
        f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode() + body


@contextmanager
def _fake_server(response: bytes, accept_timeout: float = 1.0):
    """Servidor loopback de UMA resposta fixa.

    Devolve `(porta, recebido)`; `recebido` fica vazio se ninguem conectou — e
    e assim que os testes provam que nenhum trafego foi para onde nao devia.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    srv.settimeout(accept_timeout)
    recebido: list[bytes] = []

    def _serve_once() -> None:
        with suppress(Exception):
            conn, _ = srv.accept()
            with closing(conn):
                conn.settimeout(1.0)
                with suppress(Exception):
                    recebido.append(conn.recv(65535))
                conn.sendall(response)

    thread = threading.Thread(target=_serve_once, daemon=True)
    thread.start()
    try:
        yield srv.getsockname()[1], recebido
    finally:
        thread.join(timeout=accept_timeout + 2.0)
        srv.close()


@pytest.fixture
def log_to_tmp(tmp_path, monkeypatch):
    """Redireciona `laguna.log` para o tmp do teste (nunca escreve no repo)."""
    path = tmp_path / "laguna.log"
    monkeypatch.setattr(laguna_server, "LOG_PATH", path)
    return path


@pytest.fixture
def listening_port():
    """Sobe um listener loopback e devolve a porta que ele ocupa."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        yield srv.getsockname()[1]


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- _is_headless ----------------------------------------------------------


@pytest.mark.parametrize(
    "executable,stderr,expected",
    [
        (r"C:\Python313\pythonw.exe", object(), True),  # atalho do Desktop
        (r"C:\Python313\PYTHONW.EXE", object(), True),  # case-insensitive
        (r"C:\Python313\python.exe", None, True),  # stderr ausente = sem console
        (r"C:\Python313\python.exe", object(), False),  # console normal
        ("/usr/bin/python3", object(), False),
        ("", object(), False),
    ],
)
def test_is_headless(executable, stderr, expected):
    assert laguna_server._is_headless(executable, stderr) is expected


def test_is_headless_com_console_oculto():
    """Fallback do atalho (`Laguna.vbs:11-13`): ha console e stderr, mas escondido."""
    py = r"C:\Python313\python.exe"
    assert laguna_server._is_headless(py, object(), console_hidden=True) is True
    assert laguna_server._is_headless(py, object(), console_hidden=False) is False


def test_console_is_hidden_fora_do_windows(monkeypatch):
    monkeypatch.setattr(laguna_server.sys, "platform", "linux")
    assert laguna_server._console_is_hidden() is False


def test_notify_error_fora_do_windows_so_escreve_no_stderr(monkeypatch, capsys):
    """Nenhum ctypes fora do Windows — e a mensagem nunca se perde."""
    monkeypatch.setattr(laguna_server.sys, "platform", "linux")

    laguna_server._notify_error("porta ocupada")

    assert "porta ocupada" in capsys.readouterr().err


def test_messagebox_sempre_tem_prazo():
    """Sem ninguem para clicar (sessao 0, tarefa agendada), a caixa nao pode

    prender o processo: trocar "morre calado" por "trava calado" seria pior.
    """
    assert 0 < laguna_server.NOTIFY_TIMEOUT_MS <= 300_000


# --- porta -----------------------------------------------------------------


def test_port_is_free_quando_ninguem_escuta():
    assert laguna_server._port_is_free("127.0.0.1", _free_port()) is True


def test_port_is_free_falso_com_porta_ocupada(listening_port):
    assert laguna_server._port_is_free("127.0.0.1", listening_port) is False


def test_wait_until_listening_encontra_listener(listening_port):
    assert laguna_server._wait_until_listening("127.0.0.1", listening_port, timeout=2.0) is True


def test_wait_until_listening_desiste_no_timeout():
    assert (
        laguna_server._wait_until_listening("127.0.0.1", _free_port(), timeout=0.3, poll=0.05)
        is False
    )


# --- deteccao de "ja e o Laguna" -------------------------------------------


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"running": []}, True),
        ({"running": ["falar", "escutar"]}, True),
        ({}, False),
        ({"running": "falar"}, False),  # tipo errado = nao e o nosso /api/status
        ({"status": "ok"}, False),
        (None, False),
        ([], False),
        ("running", False),
    ],
)
def test_is_laguna_status(payload, expected):
    assert laguna_server._is_laguna_status(payload) is expected


def test_probe_laguna_falso_quando_ninguem_atende():
    """Sem servidor na porta, o probe degrada para False (nunca levanta)."""
    assert laguna_server._probe_laguna("127.0.0.1", _free_port(), timeout=0.3) is False


def test_probe_reconhece_instancia_viva_do_laguna():
    with _fake_server(_http_response(b'{"running": ["falar"]}')) as (port, recebido):
        assert laguna_server._probe_laguna("127.0.0.1", port, timeout=1.0) is True
    assert recebido and recebido[0].startswith(b"GET /api/status")


def test_probe_recusa_quem_ocupa_a_porta_sem_ser_o_laguna():
    with _fake_server(_http_response(b'{"hello": "world"}')) as (port, _):
        assert laguna_server._probe_laguna("127.0.0.1", port, timeout=1.0) is False


def test_probe_recusa_status_diferente_de_200():
    with _fake_server(_http_response(b'{"running": []}', status="500 Internal Server Error")) as (
        port,
        _,
    ):
        assert laguna_server._probe_laguna("127.0.0.1", port, timeout=1.0) is False


def test_probe_reconhece_o_corpo_real_de_api_status():
    """Contrato: se `/api/status` mudar de forma, este teste cai junto.

    Sem ele, alguem enriquece a rota, `_is_laguna_status` para de reconhecer o
    proprio Laguna e o usuario com o app rodando leva "porta ocupada por outro
    programa" + exit 1.
    """
    payload = asyncio.run(laguna_server.api_status()).content
    assert laguna_server._is_laguna_status(payload) is True


def test_probe_nao_usa_o_proxy_do_sistema(monkeypatch):
    """Regressao (§1 — 100% local): `urllib.request` herdaria `http_proxy`.

    `proxy_bypass` NAO isenta 127.0.0.1, entao o probe via urllib mandava o
    request para o proxy — ou seja, para FORA da maquina. Aqui o proxy fica de
    boca aberta e nao recebe byte nenhum.
    """
    with _fake_server(_http_response(b'{"running": []}')) as (proxy_port, proxy_recebeu):
        monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy_port}")
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy_port}")
        monkeypatch.setenv("ALL_PROXY", f"http://127.0.0.1:{proxy_port}")
        assert laguna_server._probe_laguna("127.0.0.1", _free_port(), timeout=0.5) is False
    assert proxy_recebeu == []


def test_probe_nao_segue_redirect(monkeypatch):
    """Regressao (§1): quem ocupa a porta e desconhecido — nao se obedece 302.

    `urlopen` seguia o `Location:` sozinho, o que transformava um squatter local
    num gatilho de request para host arbitrario na saida do startup.
    """
    with _fake_server(_http_response(b'{"running": []}'), accept_timeout=1.5) as (
        alvo_port,
        alvo_recebeu,
    ):
        redirect = (
            f"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:{alvo_port}/api/status\r\n"
            "Content-Length: 0\r\nConnection: close\r\n\r\n"
        ).encode()
        with _fake_server(redirect) as (port, _):
            assert laguna_server._probe_laguna("127.0.0.1", port, timeout=1.0) is False
    assert alvo_recebeu == []


# --- _open_browser_when_ready ----------------------------------------------


def test_navegador_so_abre_depois_do_listen(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(laguna_server.webbrowser, "open", opened.append)
    monkeypatch.setattr(laguna_server, "_wait_until_listening", lambda *a, **k: True)

    laguna_server._open_browser_when_ready()

    assert opened == [laguna_server.URL]


def test_navegador_nao_abre_se_servidor_nao_subiu(monkeypatch, log_to_tmp):
    opened: list[str] = []
    monkeypatch.setattr(laguna_server.webbrowser, "open", opened.append)
    monkeypatch.setattr(laguna_server, "_wait_until_listening", lambda *a, **k: False)

    laguna_server._open_browser_when_ready()

    assert opened == []
    assert "navegador nao aberto" in log_to_tmp.read_text(encoding="utf-8")


# --- _serve / main ---------------------------------------------------------


@pytest.fixture
def serve_env(monkeypatch):
    """Instrumenta os efeitos colaterais de `_serve`: browser, uvicorn, notify."""

    class _Env:
        def __init__(self) -> None:
            self.opened: list[str] = []
            self.served: list[dict] = []
            self.errors: list[str] = []

    env = _Env()
    monkeypatch.setattr(laguna_server.webbrowser, "open", env.opened.append)
    monkeypatch.setattr(laguna_server.uvicorn, "run", lambda *a, **k: env.served.append(k))
    monkeypatch.setattr(laguna_server, "_notify_error", env.errors.append)
    monkeypatch.setattr(laguna_server, "_open_browser_when_ready", lambda: None)
    return env


def test_serve_caminho_feliz_sobe_uvicorn(serve_env, monkeypatch, log_to_tmp):
    monkeypatch.setattr(laguna_server, "_port_is_free", lambda *a: True)

    assert laguna_server._serve() == 0
    assert len(serve_env.served) == 1
    assert serve_env.served[0]["port"] == laguna_server.PORT
    assert serve_env.errors == []


def test_serve_porta_ocupada_pelo_proprio_laguna_abre_e_sai_zero(
    serve_env, monkeypatch, log_to_tmp
):
    monkeypatch.setattr(laguna_server, "_port_is_free", lambda *a: False)
    monkeypatch.setattr(laguna_server, "_probe_laguna", lambda *a, **k: True)

    assert laguna_server._serve() == 0
    assert serve_env.opened == [laguna_server.URL]  # abre a instancia que ja roda
    assert serve_env.served == []  # e nao tenta subir uma segunda
    assert serve_env.errors == []


def test_serve_porta_ocupada_por_outro_programa_falha_sem_navegador(
    serve_env, monkeypatch, log_to_tmp
):
    monkeypatch.setattr(laguna_server, "_port_is_free", lambda *a: False)
    monkeypatch.setattr(laguna_server, "_probe_laguna", lambda *a, **k: False)

    assert laguna_server._serve() == 1
    assert serve_env.opened == []  # nada de abrir "nao foi possivel acessar esse site"
    assert serve_env.served == []
    assert len(serve_env.errors) == 1
    assert str(laguna_server.PORT) in serve_env.errors[0]
    assert "ocupada" in log_to_tmp.read_text(encoding="utf-8")


def test_main_registra_traceback_e_sai_com_erro(serve_env, monkeypatch, log_to_tmp):
    def _boom():
        raise RuntimeError("ModuleNotFoundError simulado no startup")

    monkeypatch.setattr(laguna_server, "_serve", _boom)

    assert laguna_server.main() == 1

    log = log_to_tmp.read_text(encoding="utf-8")
    assert "falha fatal no startup" in log
    assert "RuntimeError: ModuleNotFoundError simulado no startup" in log
    assert "Traceback (most recent call last)" in log  # traceback completo, nao so a msg
    assert len(serve_env.errors) == 1  # e o usuario sem console recebe sinal


@pytest.mark.parametrize(
    "code,expected", [(None, 0), (0, 0), (1, 1), (2, 2), ("mensagem de erro", 1)]
)
def test_exit_code_normaliza_systemexit(code, expected):
    assert laguna_server._exit_code(code) == expected


def test_main_avisa_quando_o_servidor_sai_com_erro(serve_env, monkeypatch, log_to_tmp):
    """`uvicorn` sai por `sys.exit(1)` quando o bind ou o startup ASGI falha.

    Sem este ramo, o caminho MAIS provavel de "o servidor nao subiu" voltava a
    morrer calado sob pythonw — a doenca que a #58 existe para curar.
    """

    def _uvicorn_falha():
        raise SystemExit(1)

    monkeypatch.setattr(laguna_server, "_serve", _uvicorn_falha)

    assert laguna_server.main() == 1
    assert "codigo 1" in log_to_tmp.read_text(encoding="utf-8")
    assert len(serve_env.errors) == 1


def test_main_deixa_passar_saida_limpa(monkeypatch, log_to_tmp):
    """`SystemExit(0)` e encerramento normal: nada de log de erro nem MessageBox."""

    def _saida_limpa():
        raise SystemExit(0)

    monkeypatch.setattr(laguna_server, "_serve", _saida_limpa)
    with pytest.raises(SystemExit):
        laguna_server.main()


def test_main_nao_engole_ctrl_c(monkeypatch, log_to_tmp):
    def _interrupt():
        raise KeyboardInterrupt

    monkeypatch.setattr(laguna_server, "_serve", _interrupt)
    with pytest.raises(KeyboardInterrupt):
        laguna_server.main()


def test_log_nunca_levanta_quando_o_arquivo_e_inacessivel(monkeypatch, tmp_path):
    """Diretorio inexistente/sem permissao nao pode derrubar o app."""
    monkeypatch.setattr(laguna_server, "LOG_PATH", tmp_path / "nao" / "existe" / "laguna.log")
    laguna_server._log("mensagem", RuntimeError("x"))  # nao levanta
