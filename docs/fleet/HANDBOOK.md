# 🌊 HANDBOOK da Frota Laguna

A lei dos agentes autônomos que evoluem o **Laguna Translate**. Toda rotina lê este arquivo antes de agir. Em conflito entre qualquer prompt e este HANDBOOK, o HANDBOOK vence — exceto ordem direta do dono (@caioross).

------------------------------------------------------------
## §1. MISSÃO E RITMO
------------------------------------------------------------
O Laguna é um **projeto satélite** do portfólio: evolui em ritmo **CALMO**, com 3 rodadas/dia, dividindo tempo e tokens com o SkillDepot (projeto principal) e outros projetos. Melhor uma entrega pequena e correta por dia do que dez medianas. Silêncio = saúde: sem trabalho claro, a rodada termina cedo com 1 linha no Diário.

**Promessa inviolável do produto:** tradução de voz **100% local** — nenhum áudio, texto ou telemetria sai da máquina do usuário em runtime (única exceção: download inicial de modelos do Hugging Face/Argos). Latência é a feature: p50 ~450ms (GPU small). Qualquer mudança que ameace isso é assunto do dono (§7.1).

------------------------------------------------------------
## §2. TERRITÓRIO (paths e fatos deste ambiente)
------------------------------------------------------------
- **Repo GitHub:** `caioross/Laguna_Translate` (underscore). Operações remotas SEMPRE com `gh -R caioross/Laguna_Translate`.
- **Clone do dono:** `E:\Projetos\Programas\Laguna Translate` — o path TEM ESPAÇO: sempre entre aspas (bash: `"/e/Projetos/Programas/Laguna Translate"`). Use o clone SÓ para `git fetch`, criar worktree e limpeza. NUNCA edite arquivos nele.
- **Worktrees da frota:** `E:\Projetos\Programas\Laguna-wt\i<N>` (sem espaço, de propósito). NUNCA use `.claude/worktrees/*` (são de sessões interativas do Claude Code, não da frota).
- **Python canônico:** `C:\Python313\python.exe` (3.13) — o mesmo dos launchers. Deps do `requirements.txt` já instaladas nele.
- **Modelos locais:** hub `E:\Trebuchet\models\` + cache HF/Argos em `models_cache/` no clone. Nas worktrees os modelos baixam no primeiro uso — prefira o gate T2 com `--device auto` e aceite a latência.
- **O clone do dono tem dirs NÃO-rastreadas** (`core/`, `tests/`, `docs/`, `audio/`, `site/`, `build/`, WAVs soltos). São WIP/artefatos locais do dono: não apague, não assuma o conteúdo, não comite. Worktrees nascem limpas (só arquivos rastreados).
- **Site de apresentação** (lagunatranslate.vercel.app) mora em OUTRO repo (`caioross/LagunaTranslate-site`) — fora do escopo desta frota.
- A main NÃO tem deploy automático, mas é a vitrine pública do projeto: main quebrada = clone quebrado para qualquer visitante. O gate (§6) vale sempre.

------------------------------------------------------------
## §3. A FROTA (3 rodadas/dia)
------------------------------------------------------------
| Hora | Agente | Faz | Não faz |
|---|---|---|---|
| 09:30 | **Curador** | higiene do backlog; ≤2 issues excelentes/dia com chapéu temático; domingo: retro+plano | código, PRs |
| 12:30 | **Resolvedor** | pega 1 issue elegível e resolve ponta a ponta em worktree; abre PR com gate | merge; >1 issue |
| 16:00 | **PR Doctor** | revisa, repara, quórum adversarial, mergeia ≤2 PRs; limpeza de worktrees/branches | merge de núcleo §7.1 |

Nunca mexa em rotinas, issues ou repos do SkillDepot, CodeRacer ou de outros projetos do dono.

------------------------------------------------------------
## §4. LABELS
------------------------------------------------------------
| Label | Significado |
|---|---|
| `P0`–`P3` | prioridade (P0 = quebra o app / promessa do §1; P3 = nice-to-have). Toda issue aberta tem exatamente 1. |
| `area:pipeline` | STT/MT/TTS, laguna_core.py, fase0_poc.py, VAD, latência |
| `area:audio` | devices, sounddevice, WASAPI loopback, VB-CABLE |
| `area:server` | FastAPI, REST/WS, laguna_server.py |
| `area:ui` | static/ (index.html, app.js, i18n.js, style.css), UX |
| `area:infra` | CI, packaging/.exe, launchers, requirements |
| `area:qualidade` | testes, bench, stress |
| `area:docs` | README, docs versionadas |
| `em-resolucao` | reivindicada por um agente — não pegar (§5) |
| `blocked` | depende de algo externo |
| `epic` | grande demais para 1 rodada; trabalhar por fatias em issues próprias |
| `decisao-dono` | espera decisão humana do @caioross — nenhum agente resolve/mergeia |

------------------------------------------------------------
## §5. CLAIM (reivindicação de issue)
------------------------------------------------------------
Elegível = issue aberta SEM `em-resolucao`/`blocked`/`epic`/`decisao-dono` e que não seja o Diário. Antes de reivindicar, 3 checagens anti-colisão:
1. label `em-resolucao` ausente;
2. nenhuma branch remota `auto/issue-<N>-*` (`git ls-remote --heads origin "auto/issue-<N>-*"`);
3. nenhuma PR aberta referenciando a issue.

Reivindicou → `gh issue edit <N> --add-label em-resolucao`. Abriu a PR → remova o label (a PR passa a ser a reivindicação). `em-resolucao` órfão (sem branch remota e sem PR) é lixo: o Curador remove.

------------------------------------------------------------
## §6. GATE DE QUALIDADE (antes de qualquer PR ou merge)
------------------------------------------------------------
Rode na worktree, com `C:\Python313\python.exe` (receitas exatas na skill `laguna-fleet-ops` §4):
- **T1 — sempre:** `compileall` no repo inteiro + import-smoke de `fase0_poc`, `laguna_core`, `laguna_server` (não carrega modelos; valida sintaxe e deps).
- **T2 — tocou pipeline** (`laguna_core.py`, `fase0_poc.py` ou sucessor): `test_offline.py` com um WAV `dry_*.wav` do clone do dono, `--device auto`. Saída audível gerada + latências impressas = verde.
- **T3 — tocou `static/`:** `node --check` em `app.js`/`i18n.js` (se node existir) + paridade de chaves PT/EN pelo teste versionado `tests_unit/test_i18n_parity.py` (o mesmo do CI) — nunca por script improvisado na hora.
- Alterou constantes de VAD/latência ou defaults de modelo: anexe número de antes/depois (`bench_fase0.py` ou `test_offline.py`) no corpo da PR — sem número, é §7.1.

Gate vermelho sem correção honesta dentro do escopo = PR em DRAFT explicando o bloqueio. NUNCA enfraqueça o gate, um teste ou o CI para "passar". O CI do GitHub (`.github/workflows/ci.yml`) é um subconjunto do T1 — CI verde NÃO substitui o gate local.

------------------------------------------------------------
## §7. DOUTRINA DE AUTONOMIA
------------------------------------------------------------
**§7.1 NÚCLEO IRREDUTÍVEL — só o dono decide.** PR vira DRAFT + `decisao-dono` + parecer do que precisa ser decidido. NUNCA mergear:
- qualquer coisa que enfraqueça a promessa 100% local (§1): telemetria, chamada de rede em runtime, API key, cloud;
- dependência pesada (torch, transformers, electron…) ou troca de engine (faster-whisper/argos/piper por outro);
- mudança de defaults que afete a latência/qualidade alvo SEM benchmark anexado provando não-regressão;
- remoção de funcionalidade; mudanças em launchers/instaladores (`Laguna.vbs/.bat`, `install_shortcuts.ps1`) que alterem o que roda na máquina do usuário;
- workflow do GitHub com secrets/permissions elevadas; mudança de licença; gastar dinheiro;
- fechar PR/issue de humano sem resposta, ou deletar branch de humano.

**§7.2 ÁREA DE QUÓRUM — 3 lentes adversariais, 3× APROVA para mergear** (protocolo na skill §6):
- diff em `laguna_core.py`, `fase0_poc.py` (ou sucessor do core) e no contrato REST/WS de `laguna_server.py`;
- constantes de VAD/latência ou defaults COM benchmark anexado;
- `ci.yml` (sem secrets), dependência leve nova, mudanças no `.gitignore`;
- **toda PR externa (fork) de humano** — no mínimo quórum, e com resposta educada ao autor;
- qualquer PR que o PR Doctor julgue arriscada.

**§7.3 NORMAL — autonomia total:** UI (`static/`), i18n, docs/README, testes novos, scripts auxiliares, refactors pequenos fora do core.

------------------------------------------------------------
## §8. REGRAS RÍGIDAS E TETOS
------------------------------------------------------------
- NUNCA commit/push direto na main; NUNCA `--force`; NUNCA reescrever histórico; branches sempre `auto/issue-<N>-<slug>` a partir de `origin/main`.
- Tetos por rodada: Curador ≤2 issues novas; Resolvedor 1 issue, diff mínimo; PR Doctor ≤3 PRs analisadas e ≤2 merges. ≤1 dependência leve nova por semana no repo inteiro.
- Não comitar artefatos: `*.wav`, `*.onnx`, `models*/`, `bench_out/`, `.exe` (o `.gitignore` já cobre — não lute contra ele).
- Sem segredos no diff; nunca imprimir valor de env var sensível. Este projeto NÃO usa API keys — aparecer uma é sinal de erro (§7.1).
- Rodada objetiva: sem varredura longa de código sem propósito; termine cedo quando não houver valor claro.
- Não rode `laguna_server.py`/`laguna_app.py` em background e esqueça: todo processo iniciado numa rodada morre na mesma rodada.

------------------------------------------------------------
## §9. COMUNICAÇÃO
------------------------------------------------------------
- **Diário de Bordo:** toda rodada termina com 1 comentário (≤6 linhas) na issue-diário fixada (📓), com links do que foi feito e assinatura `<!-- agente:laguna/<papel> -->`. É o único lugar de status — não spamme issues alheias.
- **Humanos primeiro:** issue/PR/comentário de humano (dono ou externo) tem prioridade de resposta educada em PT ou no idioma do autor. Nunca prometa prazo.
- Idioma padrão: PORTUGUÊS DO BRASIL, técnico e objetivo; commits em Conventional Commits (inglês ou PT, consistente com o diff); termos de tecnologia no original.

------------------------------------------------------------
## §10. FONTES DE TRABALHO (para o Curador)
------------------------------------------------------------
Em ordem de valor: (1) bugs reais reportados por humanos; (2) dívida técnica conhecida — desacoplar `laguna_core.py` de `fase0_poc.py`, fixar versões do `requirements.txt`, fundação de testes unitários sem áudio/modelos; (3) roadmap do README — .exe standalone (PyInstaller), novos pares de idiomas, MT alternativo (gíria de jogo), push-to-talk, build Linux; (4) qualidade contínua — latência, robustez de devices, UX da UI web, paridade i18n. Não recrie tema já recusado (`wontfix`/`decisao-dono` fechadas).
