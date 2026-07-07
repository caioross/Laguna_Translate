# CLAUDE.md — Laguna Translate

Tradutor de voz em tempo real, **100% local** (PT↔EN), feito pra Discord. Pipeline: mic/loopback → WebRTC VAD → faster-whisper (STT) → Argos Translate (MT) → Piper TTS → device virtual. Windows-first, Python 3.13.

## Regras de ouro
- **Promessa inviolável:** nenhum áudio/texto/telemetria sai da máquina em runtime (exceção única: download inicial de modelos). Latência é a feature (p50 ~450ms em GPU small). Nada pode competir com isso.
- Sem torch e sem dependência pesada nova; diff mínimo; convenções vizinhas.
- `pt` vs `pb` no Argos é intencional (`ARGOS_CODE_MAP` em `fase0_poc.py`); Whisper usa `pt`. Não "consertar".
- `laguna_core.py` importa de `fase0_poc.py` por legado — dívida conhecida; NÃO adicionar imports novos de `fase0_poc` em código novo.
- Não comitar `*.wav`, `*.onnx`, `models*/`, `bench_out/` (gitignore já cobre).

## Comandos (Python canônico: `C:\Python313\python.exe`)
```bash
python laguna_server.py                 # UI web em http://127.0.0.1:7531
python laguna_app.py                    # janela nativa (pywebview)
python fase0_poc.py --list-devices      # CLI/debug
python test_offline.py <wav 16k mono> --direction pt2en -o out.wav   # pipeline sem mic
python bench_fase0.py --model small --device cuda                    # benchmark (alvo p50 < 450ms)
```

## Gate antes de qualquer PR
1. `python -m compileall -q .` + `python -c "import fase0_poc, laguna_core, laguna_server"`
2. Tocou pipeline → `test_offline.py` num WAV de referência; tocou `static/` → `node --check` + paridade i18n PT/EN.
3. Mudou VAD/defaults de modelo → benchmark antes/depois no corpo da PR.

## Mapa
- `laguna_core.py` — DirectionWorker/DirectionConfig (2 direções simultâneas)
- `fase0_poc.py` — STT, ArgosMT, PiperTTS, WebRTCVADGate, detect_device (legado ainda em produção)
- `laguna_server.py` — FastAPI + WS `/ws` (porta 7531); `static/` — UI web (app.js, i18n.js)
- `fase1_app.py` — legado morto (PySide6); não investir
- Site de apresentação = repo separado (`caioross/LagunaTranslate-site`)

## Frota autônoma
Este repo é evoluído diariamente por 3 rotinas de agentes (Curador 09:30, Resolvedor 12:30, PR Doctor 16:00). A lei: [docs/fleet/HANDBOOK.md](docs/fleet/HANDBOOK.md). Receitas: skill `.claude/skills/laguna-fleet-ops/`. Issues com `decisao-dono` esperam o @caioross.
