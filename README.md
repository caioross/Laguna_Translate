# TransDualDisc — Fase 0 (POC)

Tradutor de voz em tempo real PT<->EN, 100% local, para uso com Discord.
Plano técnico completo em [docs/plano-tecnico-v2.md](docs/plano-tecnico-v2.md).

## Dependências (Python 3.13 global)

Já instaladas / baixadas na máquina do dev:

- `faster-whisper` 1.2.1 + CTranslate2 4.6 (STT)
- `argostranslate` 1.10 + pacotes `en->pb`, `pb->en` (MT — usamos pt-BR via código `pb`; `pt` no Argos é pt-PT e gera "estás/equipa/juntar-se")
- `piper-tts` 1.4.2 + vozes `en_US-lessac-medium`, `pt_BR-faber-medium`
- `webrtcvad-wheels` 2.0.14 (VAD)
- `sounddevice`, `numpy`, `scipy`
- `nvidia-cublas-cu12` 12.9, `nvidia-cudnn-cu12` 9.19 (GPU)

## Rodar

```bash
# Listar dispositivos de entrada (anote o índice do seu mic)
C:/Python313/python.exe fase0_poc.py --list-devices

# PT -> EN (fale em português, ouve em inglês)
C:/Python313/python.exe fase0_poc.py --direction pt2en --model small --device cuda

# EN -> PT
C:/Python313/python.exe fase0_poc.py --direction en2pt --model small --device cuda

# CPU fallback (sem GPU)
C:/Python313/python.exe fase0_poc.py --direction pt2en --model small --device cpu

# Debug: salva WAVs dos segmentos
C:/Python313/python.exe fase0_poc.py --direction pt2en --debug
```

Ctrl+C encerra e mostra estatísticas p50/p95/p99 por estágio.

## Teste offline (sem mic)

```bash
C:/Python313/python.exe test_offline.py dry_en2pt.wav --direction pt2en --model small --device cuda -o out.wav
```

## Resultados medidos (1 frase, 4s de áudio PT)

Frase: *"Hello everyone, this is a real-time translator test for Discord."* (sintetizada via Piper pt_BR).

| Stack | STT | MT | TTS | Total | Qualidade STT |
|---|---|---|---|---|---|
| tiny CPU int8 | 410ms | 317ms | 272ms | **999ms** | baixa (erros) |
| small CPU int8 | 2179ms | 292ms | 296ms | **2767ms** | perfeita |
| small CUDA fp16 | 568ms | 269ms | 276ms | **1113ms** | perfeita |
| medium CUDA fp16 | 775ms | 317ms | 246ms | **1338ms** | leve hallucination |

**Decisão:** small + CUDA é o sweet spot — mais rápido E mais preciso que medium para nosso caso. Medium ficou com `"tradutora"` e traduziu "Discord" → "discórdia"; small acertou tudo.

Transcrição small: `"Olá a todos, este é um teste de tradutor em tempo real para Discord."` (100% correta)
Tradução: `"Hello everyone, this is a real-time translator test for Discord."` (100% correta)

### Observação sobre pt-BR (Argos)

O Argos tem dois pacotes portugueses: `pt` (Europeu, "estás/equipa/juntar-se") e `pb` (Brasileiro, "está/time/se juntar"). O código `fase0_poc.py` mapeia `pt -> pb` automaticamente (ver `ARGOS_CODE_MAP`). Whisper STT continua usando `pt` (o modelo não distingue variantes).

## Critérios de saída Fase 0 (§13 do plano)

1. Latência p50 total < 2500ms CPU, < 1500ms GPU — GPU small:
   - PT→EN (pb→en): **p50 458ms / p95 562ms / p99 600ms** (200 seg) ✓
   - EN→PT (en→pb): **p50 424ms / p95 496ms / p99 534ms** (200 seg) ✓
2. Sem crash — **200 segmentos contínuos sem erro em 87s por direção, p50 estável rodada-a-rodada ✓** (30min ao vivo pendente)
3. Qualidade subjetiva OK em 10 frases mistas — **9/10 corretas** ✓
   - Limitações Argos (slang gaming): *sick flick* → *filme doentio*, *carry* omitido em frases complexas, "good game" duplicado ocasionalmente. Base gramatical sólida.

### Executar stress test

```bash
C:/Python313/python.exe stress_fase0.py --rounds 20 --model small --device cuda   # PT->EN
C:/Python313/python.exe stress_en2pt.py --rounds 20 --model small --device cuda    # EN->PT
```

## Laguna Translator — UI web (painel bidirecional)

```bash
C:/Python313/python.exe E:/TransDualDisc/laguna_server.py
```

Abre automaticamente http://127.0.0.1:7531 no navegador.

### Atalhos Desktop + Start Menu

```powershell
powershell -ExecutionPolicy Bypass -File E:\TransDualDisc\install_shortcuts.ps1
```

Cria **Laguna Translator.lnk** no Desktop e no Start Menu. O atalho usa `Laguna.vbs` (launcher silencioso — sem janela de console; fallback para `Laguna.bat` que mostra console).

Para remover: `uninstall_shortcuts.ps1`.

### Interface

- **Tema claro/escuro**: botão 🌙/☀️ no topo (atalho: **Shift+T**). Persiste no localStorage.
- **Tooltips**: passe o mouse em qualquer campo para ver detalhes (latências esperadas, o que cada device faz, overhead do skip-same-lang, etc).
- **Badge de dispositivos**: no topo, indica se VB-CABLE está instalado e se foi renomeado para "Laguna".

### Empacotamento para distribuição (a fazer)

A distribuição standalone (.exe com Python + deps embutidos) não foi implementada. Plano:
- `PyInstaller --onedir laguna_server.py` com hooks para `faster_whisper`, `piper`, `ctranslate2`, `nvidia.cublas`, `nvidia.cudnn`, `argostranslate`.
- Bundle de modelos: Whisper small (~500MB), Piper voices (~100MB), Argos pb↔en (~200MB) — ~800MB + CUDA libs (~600MB) = instalador ~1.5GB.
- Wrapper em **Inno Setup** para gerar `.exe` de instalação com ícone, associações e desinstalador.
- First-run bootstrap pode baixar modelos sob demanda (reduz instalador para ~300MB).

Até lá, o fluxo é: clonar repo, `pip install -r requirements.txt`, rodar `install_shortcuts.ps1`.

**Recursos:**
- Dois painéis independentes: **FALAR** (você → Discord) e **ESCUTAR** (Discord → você), **simultâneos**
- Cada painel tem configs próprias (mic, saída, idiomas, modelo)
- Detecção automática de idioma — se detectar o idioma alvo, pula a tradução (toggle "skip same lang"; adiciona ~130ms ao STT — 8/8 acerto no teste)
- Live transcription + translation via WebSocket
- Latência rolling p50/p95 por direção
- Detecta e destaca dispositivos renomeados para **"Laguna Translator"** (🌊)

### Configurar Discord (VB-CABLE + renomear)

1. Baixe e instale VB-CABLE: https://vb-audio.com/Cable/ (grátis, reinicie após instalar).
2. Abra **Configurações de som do Windows → Mais opções de som**.
3. Aba **Gravação**: clique direito em "CABLE Output" → Propriedades → Geral → renomeie para `Laguna Translator Mic`.
4. Aba **Reprodução**: clique direito em "CABLE Input" → Propriedades → Geral → renomeie para `Laguna Translator Output`.
5. Recarregue o app web — o badge do topo mostra "🌊 Laguna: dispositivos renomeados OK".
6. No Discord → Configurações → Voz e Vídeo:
   - **Entrada:** `Laguna Translator Mic`
   - **Saída:** seu fone real (não o virtual)
7. No app:
   - **FALAR → "Saída virtual"** = `Laguna Translator Output`
   - **ESCUTAR → "Captura"** = `Laguna Translator Mic` (ou marque loopback e selecione o device de saída que o Discord usa)

Sem VB-CABLE, o app ainda funciona — só não aparece "invisível" como mic no Discord.

## Fase 1 (legado) — painel PySide6

Painel desktop mais simples em `fase1_app.py` (substituído pela UI web Laguna).

```bash
C:/Python313/python.exe E:/TransDualDisc/fase1_app.py
```
