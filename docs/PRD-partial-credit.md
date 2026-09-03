# PRD — Partial credit por teste (granularidade do hidden verifier)

**Status:** Draft para implementação
**Feature:** Per-test partial credit — `hidden tests: 9/12`
**Onda-alvo:** Onda 4 (item 1)
**Esforço estimado:** M (~200-300 LOC + testes)
**RepoBench mínimo:** 0.7.0
**Relação com o PRD mestre:** complementa §42 (verifiers definem corretude; exit codes nunca) e §100 (TrialResult). Não altera nenhum deles — este PRD é aditivo.
**Data:** setembro de 2026

---

# 1. Resumo executivo

Hoje o veredito de um trial é binário: o hidden verifier (patch de testes do PR real)
roda contra a árvore final do agente e o exit code decide SOLVED/UNSOLVED. Vermelho→verde
é **1 bit** — um agente que acerta 11 de 12 testes e um que acerta 0 recebem o mesmo
UNSOLVED.

Em times TDD o teste é a spec escrita antes; "quantos vermelhos o agente apagou" é o
gradiente natural do trabalho. Este PRD adiciona esse gradiente como **finding registrado
ao lado do veredito** — nunca dentro dele.

> **Invariantes inegociáveis**
> 1. A semântica de SOLVED/UNSOLVED não muda em nada (hidden tests + regression, ambos
>    exit 0). Nenhum número novo alimenta o veredito.
> 2. Número nunca é inventado: parsing falhou ou não se aplicou ⇒ campos `None` e a UI
>    mostra `—`. (Mesma regra de `tampered_tests`, issue #18: finding, não veredito.)
> 3. Nenhum comando extra roda: o mesmo processo do verifier produz o relatório.
>    Custo de runtime ≈ 0.

# 2. Problema

- `runs --show` e `report` mostram solve-rate binário por target; dois targets com 40%
  resolvem de formas muito diferentes (falha total vs. quase-lá) e o relatório não
  distingue.
- O benchmark herda testes reais de PRs reais — granularidade por teste já existe no
  dado, estamos descartando-a no exit code.

# 3. Objetivos / não-objetivos

**Objetivos (V1)**

- Extrair contagem por teste (passed/failed/skipped) do run do **hidden verifier** quando
  o `project.test_command` for pytest-shaped.
- Persistir em `TrialResult` (campos aditivos, default `None`).
- Superfície em `runs --show`, `report` (text/json/jsonl/csv).

**Não-objetivos (V1)**

- Vitest/jest/go test (precisam de reporter configurado — fragilidade sem retorno agora;
  V2 se o campo provar valor).
- Parsing do run de **regression** (guard, não intenção; denominador não significa nada).
- Partial credit no tempo de **build** (checks de validação unchanged).
- Qualquer peso por "dificuldade" de teste. Contagem crua, `1 teste = 1 teste`.
- Mudar pass@k, Wilson CI, bootstrap. O headline continua sendo solve-rate.

# 4. Decisões de design

## 4.1 Detecção (auto, com opt-out)

- Config nova em `ProjectConfig`: `test_report: "auto" | "off"` (default `"auto"`).
- `"auto"` anexa `--junitxml` quando o argv do `test_command` contém o token `pytest`
  (cobre `pytest` executável e `python -m pytest`). Wrapper que oculta o pytest
  (ex.: `npm test` que shells pra pytest) ⇒ sem detecção, campos `None` — honesto.
- `"off"` é a válvula para comando que quebra com flag extra (pytest wrapper exótico).
- Sem modo `"on"` forçado em V1: não há parser para comando não-pytest.

## 4.2 Execução

- Flag anexada **apenas** na invocação do task verifier (não no install, não no
  regression): `--junitxml=<verify_ws>/.repobench_junit.xml`.
- pyargs last-wins: pytest usa a **última** ocorrência da flag; a nossa é anexada no fim,
  então um `--junitxml` do usuário é sobrescrito (determinístico, documentado).
- O XML vive dentro do snapshot descartável do verify (`ws.base_dir / "verify"`), que é
  destruído no `_publish`. O parse acontece **dentro de `_verify`, antes do retorno** —
  depois disso o arquivo não existe mais.

## 4.3 Parsing (`repobench/execution/testreport.py`, novo módulo)

- `xml.etree.ElementTree`, schema JUnit (família xunit2, default do pytest).
- Contagem por enumeração de `<testcase>` (lida com `testsuites` aninhados):
  - `passed` = testcase sem filho de falha;
  - `failed` = tem `<failure>` **ou** `<error>`;
  - `skipped` = tem `<skipped>`;
  - `total = passed + failed + skipped`.
- Malformed XML, XML ausente, ou `total == 0` ⇒ `None` em tudo (suíte que reportou zero
  testes não é contagem, é collection error).
- Só contagens são extraídas e persistidas. Nomes de teste e output **não** são
  armazenados em trial/state (o XML morre com o workspace).

## 4.4 Modelo (`core/types.py`, aditivo com default — precedente wave 3.5)

```python
class TrialResult(BaseModel):
    ...
    tests_passed: int | None = None
    tests_failed: int | None = None   # failures + errors
    tests_skipped: int | None = None
    tests_total: int | None = None    # conveniência: passed+failed+skipped
    test_report_source: str | None = None  # ex.: "pytest-junit"; None = não extraído
```

## 4.5 Exibição

- `runs --show`: coluna `TESTS` = `passed/(total−skipped)` — ex. `9/12`; `—` quando
  `None`; skipped aparece só no detail/json (denominador já o exclui).
- `report` text: por target, linha `partial 0.78 (n=14 trials)` — média do ratio por
  trial **entre trials com dado**; `n` sempre visível para não fingir cobertura.
- `report --format json/jsonl/csv`: campos novos, `null` quando ausente.
- SOLVED/UNSOLVED, cores, CIs: untouched.

## 4.6 Rollouts (issue #13)

Campos são por trial. Múltiplos rollouts ⇒ distribuição; V1 reporta média sobre trials
com dado (com `n`). Min/mediana por target ficam para a análise se o campo mostrar valor.

# 5. Edge cases (especificados para não virarem decisão de última hora)

| Caso | Comportamento |
|---|---|
| exit ≠ 0, XML parseável | **O caso partial**: contagens gravadas, veredito False |
| exit 0, XML ausente/inválido | campos `None`, sem crash |
| XML com `total == 0` | campos `None` (collection error ≠ zero testes) |
| Usuário já passa `--junitxml` | o nosso (anexado por último) vence |
| Comando não-pytest | flag não anexada, campos `None` |
| `test_report: "off"` | argv do verifier byte-idêntico ao configurado |
| Rollouts variando (flaky) | por trial; agregação com `n` explícito |
| XML gigante | JUnit de suite real é pequeno; sem cap dedicado em V1 |

# 6. Mapa de implementação

| Arquivo | Mudança |
|---|---|
| `repobench/execution/testreport.py` | novo: parser JUnit → buckets ou None |
| `repobench/execution/runner.py` | `_run_verifier` anexa flag (task run só), lê+parseia antes do retorno; plugar em `_verify` |
| `repobench/core/types.py` | 5 campos aditivos em `TrialResult` |
| `repobench/config.py` | `ProjectConfig.test_report` (`"auto"`/`"off"`, default `"auto"`) |
| `repobench/cli/render.py` | `runs --show`: coluna TESTS |
| `repobench/reporting/{models,terminal,export}.py` | linha `partial` por target; campos json/jsonl/csv |
| `tests/test_testreport.py` (novo) | parser: XMLs fixture (ok, com error/skipped, aninhado, malformed, vazio) |
| `tests/test_runner.py` | e2e hermético: mini-suite real de pytest, agente resolve 2/3 ⇒ UNSOLVED + `tests 2/3`; argv não alterado com `off`; veredito idêntico com/sem flag |
| `tests/test_reporting.py` | superfícies text/json/csv, `—` quando None |

# 7. Critérios de aceitação

- [ ] Suite completa verde; **nenhum** teste existente de veredito/estatística muda.
- [ ] Trial com 11/12 testes passando: `UNSOLVED` (igual a antes) + `TESTS 11/12` visível
      em `runs --show` + campos no json.
- [ ] XML malformed ⇒ `None`/`—` em toda a superfície, zero crash, zero número inventado.
- [ ] `test_report: "off"` ⇒ argv do verifier idêntico ao do usuário (assert no teste).
- [ ] Custo: nenhum processo adicional por trial (mesma invocação do verifier).
- [ ] Docs: README menciona o campo e o knob em uma linha cada.

# 8. Futuro explicitamente fora daqui

- Reporters vitest/jest; parsing do regression run; gates typecheck/lint (segundo item
  da Onda 4, PRD próprio); proximity-to-gold; pesos por dificuldade de teste.
