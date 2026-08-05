"""Alvo #58 — startup do servidor deixa de morrer em silencio.

O atalho do Desktop roda `pythonw laguna_server.py` (sem console): falha de
start nao pode sumir. Estes testes exercitam so a logica de startup — porta
livre/ocupada, deteccao de "ja e o Laguna", espera pelo listen, log e codigo de
saida. Nada de audio, modelo, GPU ou rede externa: os unicos sockets usados sao
loopback em porta efemera, criados e fechados pelo proprio teste (o `conftest`
ja substitui sounddevice/fastapi/uvicorn por stubs).
"""

from __future__ import annotations

import socket
from contextlib import closing

import pytest

import laguna_server


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


def test_probe_laguna_falso_quando_ninguem_atende(monkeypatch):
    """Sem servidor na URL, o probe degrada para False (nunca levanta)."""
    monkeypatch.setattr(laguna_server, "URL", f"http://127.0.0.1:{_free_port()}")
    assert laguna_server._probe_laguna(timeout=0.3) is False


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
