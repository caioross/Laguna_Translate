---
name: laguna-fleet-ops
description: Receitas operacionais da frota autônoma do Laguna Translate (Curador/Resolvedor/PR Doctor). Use SEMPRE que uma rotina agendada da frota for reivindicar issue, criar worktree, rodar o gate, abrir PR, conduzir quórum, mergear ou limpar branches no repo caioross/Laguna_Translate. A lei está em docs/fleet/HANDBOOK.md; aqui estão os comandos prontos.
---

# laguna-fleet-ops — receitas da frota

A lei é `docs/fleet/HANDBOOK.md` (labels §4, claim §5, gate §6, doutrina §7, tetos §8). Aqui só o COMO. Comandos em bash (Git Bash); o path do clone TEM ESPAÇO — mantenha as aspas.

```bash
REPO="caioross/Laguna_Translate"
CLONE="/e/Projetos/Programas/Laguna Translate"   # NUNCA editar arquivos aqui
WT_BASE="/e/Projetos/Programas/Laguna-wt"        # worktrees da frota (sem espaço)
PY="/c/Python313/python.exe"                     # Python canônico 3.13
```

## §1. Estado do mundo (início de toda rodada)

```bash
git -C "$CLONE" fetch origin --prune
gh issue list -R "$REPO" --state open --json number,title,labels,updatedAt --limit 50
gh pr list -R "$REPO" --state open --json number,title,isDraft,mergeable,headRefName,labels,updatedAt
```

## §2. Checagens anti-colisão antes de reivindicar a issue N

1. labels da issue não contêm `em-resolucao`/`blocked`/`epic`/`decisao-dono`;
2. `git ls-remote --heads origin "auto/issue-<N>-*"` vazio;
3. `gh pr list -R "$REPO" --state open --search "<N>"` não referencia a issue.

Reivindique: `gh issue edit <N> -R "$REPO" --add-label em-resolucao`.

## §3. Worktree (Resolvedor)

```bash
mkdir -p "$WT_BASE"
git -C "$CLONE" worktree add "$WT_BASE/i<N>" -b "auto/issue-<N>-<slug-curto>" origin/main
cd "$WT_BASE/i<N>"
git rev-list --left-right --count origin/main...HEAD   # tem que ser "0	0"
```

Worktrees nascem só com arquivos rastreados: sem `models_cache/`, sem WAVs, sem `.claude/` local além do versionado. Nunca crie worktree dentro de `"$CLONE"` nem use `.claude/worktrees/*` (são de sessões interativas).

## §4. Gate (HANDBOOK §6) — rodar NA worktree

**T1 — sempre:**
```bash
"$PY" -m compileall -q . && echo COMPILE_OK
"$PY" -c "import fase0_poc, laguna_core, laguna_server; print('IMPORTS_OK')"
```
(Import não carrega modelos; carrega libs — alguns segundos é normal.)

**T2 — tocou `laguna_core.py`/`fase0_poc.py`/sucessor:**
```bash
"$PY" test_offline.py "$CLONE/dry_pt2en.wav" --direction pt2en --model small --device auto -o out_gate.wav
```
Verde = exit 0, latências impressas, `out_gate.wav` gerado (já é gitignorado). Sem GPU livre o `--device auto` cai pra CPU — mais lento, ainda válido. Se o WAV não existir no clone, diga isso honestamente no corpo da PR em vez de fingir que rodou.

**T3 — tocou `static/`:**
```bash
command -v node >/dev/null && node --check static/app.js && node --check static/i18n.js && echo JS_OK
"$PY" -m pytest tests_unit/test_i18n_parity.py -q
```
A paridade PT/EN é do teste versionado (`tests_unit/test_i18n_parity.py`), o mesmo que roda no CI: parser robusto e asserts que falham se nenhuma chave for extraída. Não improvise heredoc de regex aqui — a versão antiga extraía 0 chaves e dava falso verde (#34). Se o teste quebrar por mudança de formato do `i18n.js`, conserte o parser do teste, nunca o contorne.

## §5. PR (Resolvedor)

Commits Conventional referenciando `#N`; capriche no 1º commit — vira título do squash. Push da branch (nunca main, nunca `--force`):
```bash
git push -u origin "auto/issue-<N>-<slug>"
gh pr create -R "$REPO" --base main --title "<tipo>: <específico>" --body "<contexto; o que mudou e por quê; resultado REAL do gate (T1/T2/T3); riscos; Closes #N>"
```
`Closes #N` SÓ se resolve a issue inteira; fatia parcial usa `Refs #N`. Classifique pela doutrina §7 ANTES: núcleo §7.1 → `gh pr create --draft` + label `decisao-dono`; quórum §7.2 → corpo contém a linha exata `Solicito quórum (HANDBOOK §7)`. Depois: remova `em-resolucao` da issue e comente nela com o link da PR.

## §6. Protocolo de quórum (PR Doctor)

Pré-requisitos: CI verde, `mergeable`, diff lido INTEIRO. Então 3 subagentes adversariais em paralelo, cada um com default VETAR e obrigação de vetor concreto `arquivo:linha`:
1. **Lente Pipeline/Latência** — corrompe áudio? adiciona bloqueio/alocação no caminho quente? quebra VAD/segmentação? regressão de latência sem número?
2. **Lente Privacidade/Robustez** — abre rede em runtime (viola §1)? engole exceção de device? quebra fallback CPU? path Windows com espaço mal-quotado? injeta comando?
3. **Lente Produto/UX** — quebra contrato REST/WS com a UI? diverge i18n PT/EN? piora onboarding do README?

3× APROVA → squash-merge registrando o veredito das 3 lentes no comentário do parecer. Veto real → repare e re-convoque UMA vez; persistiu → DRAFT + `decisao-dono` + parecer. Idempotência: não repita parecer no mesmo head SHA sem fato novo.

## §7. Merge e limpeza (PR Doctor)

```bash
gh pr merge <N> -R "$REPO" --squash --delete-branch   # exit 1 ao deletar branch local é esperado; confirme estado MERGED
```
Limpeza no clone do dono (toda rodada):
```bash
git -C "$CLONE" worktree list
# para cada worktree em $WT_BASE cuja branch já foi MERGEADA:
git -C "$CLONE" worktree remove "$WT_BASE/i<N>" && git -C "$CLONE" worktree prune
git -C "$CLONE" branch -D "auto/issue-<N>-<slug>"     # só se mergeada
```
NUNCA remova worktree de branch não-mergeada com commits não enviados; NUNCA toque em `.claude/worktrees/*`.

## §8. PR externa (fork de humano)

- Você normalmente NÃO consegue push na branch do fork — não tente reparar por push; se precisar de mudança, peça educadamente ao autor com sugestão concreta (ou `gh pr checkout <N>` numa worktree só para TESTAR).
- Rode o gate completo localmente com o código da PR. Merge exige quórum (§7.2) mesmo que o diff pareça trivial.
- PR externa boa e parada há meses: priorize — contribuidor esperando é a pior dívida. Responda SEMPRE no idioma do autor.

## §9. Gotchas do Laguna

- Repo é `Laguna_Translate` (underscore); a pasta local tem espaço — quote tudo, sempre.
- `pt` vs `pb`: Argos usa `pb` (brasileiro) via `ARGOS_CODE_MAP` em `fase0_poc.py`; Whisper usa `pt`. Não "conserte" isso.
- CUDA no Windows: `_register_cuda_dlls()` roda no import de `fase0_poc` ANTES de `faster_whisper` — não reordene imports do topo.
- `laguna_core.py` importa de `fase0_poc.py` por design LEGADO (dívida conhecida, epic aberta) — não adicione import novo de `fase0_poc` em código novo.
- Modelos: 1º uso numa worktree baixa para `models_cache/` local dela (HF pode rate-limitar). Alternativa: exporte `HF_HOME` apontando para o cache do clone se o download travar.
- VAD: constantes (`PRE_SPEech_BUFFER_MS`… ver `fase0_poc.py`) afetam corte de fonemas — mudanças exigem número antes/depois (§6/§7).
- `fase1_app.py` (PySide6) é legado morto; não invista rodadas nele.
- Não deixe `laguna_server.py` rodando ao fim da rodada (porta 7531).
