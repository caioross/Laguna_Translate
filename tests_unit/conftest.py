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
