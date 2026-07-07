"""Alvo #4 — paridade de chaves i18n PT/EN (static/i18n.js).

Falha se uma chave existir só em um idioma. Parse leve por regex: extrai os
blocos `pt: { ... }` e `en: { ... }` de `window.LAGUNA_I18N` e compara os
conjuntos de chaves. Não executa JS nem depende de node.
"""

import re
from pathlib import Path

I18N_FILE = Path(__file__).resolve().parent.parent / "static" / "i18n.js"

# Chaves iniciam a linha: `  "chave": "valor",`. Ancorar em ^ (MULTILINE)
# evita capturar aspas dentro dos valores (que ficam após os dois-pontos).
_KEY_RE = re.compile(r'^\s*"((?:[^"\\]|\\.)*)"\s*:', re.MULTILINE)


def _extract_keys(text: str, lang: str) -> set[str]:
    # Corpo do bloco do idioma: de `<lang>: {` até o primeiro fechamento `},`.
    m = re.search(rf"\n\s*{lang}:\s*\{{(.*?)\n\s*\}},", text, re.DOTALL)
    assert m, f"bloco '{lang}' não encontrado em i18n.js"
    return set(_KEY_RE.findall(m.group(1)))


def test_arquivo_i18n_existe():
    assert I18N_FILE.is_file(), f"não encontrado: {I18N_FILE}"


def test_paridade_de_chaves_pt_en():
    text = I18N_FILE.read_text(encoding="utf-8")
    pt = _extract_keys(text, "pt")
    en = _extract_keys(text, "en")

    assert pt, "nenhuma chave PT extraída — parser ou arquivo mudou de formato"
    assert en, "nenhuma chave EN extraída — parser ou arquivo mudou de formato"

    so_pt = sorted(pt - en)
    so_en = sorted(en - pt)
    assert not so_pt and not so_en, (
        f"chaves i18n sem paridade PT/EN.\n"
        f"  só em PT ({len(so_pt)}): {so_pt}\n"
        f"  só em EN ({len(so_en)}): {so_en}"
    )
