"""Alvo #1 — ARGOS_CODE_MAP / mapeamento pt→pb (fase0_poc.py).

O Argos usa `pb` para português; o resto do pipeline (Whisper, UI) usa `pt`.
A conversão acontece em `ArgosMT.__init__` via `ARGOS_CODE_MAP.get(code, code)`.
A distinção pt/pb é intencional (ver CLAUDE.md) — estes testes fixam o contrato.
"""

from fase0_poc import ARGOS_CODE_MAP


def test_pt_mapeia_para_pb():
    assert ARGOS_CODE_MAP["pt"] == "pb"


def test_variantes_pt_br_mapeiam_para_pb():
    assert ARGOS_CODE_MAP["pt-br"] == "pb"
    assert ARGOS_CODE_MAP["pt-BR"] == "pb"


def test_codigos_desconhecidos_passam_direto():
    # ArgosMT usa ARGOS_CODE_MAP.get(code, code): idiomas fora do mapa
    # (ex.: en) devem permanecer inalterados.
    assert ARGOS_CODE_MAP.get("en", "en") == "en"
    assert ARGOS_CODE_MAP.get("es", "es") == "es"


def test_map_nao_altera_pb():
    # pb já é o código do Argos — não deve ser remapeado.
    assert ARGOS_CODE_MAP.get("pb", "pb") == "pb"
