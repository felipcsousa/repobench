# PRD — RepoBench Adaptive Candidate Mining

## From hard rejection to evidence-based candidate selection

**Status:** Draft
**Versão proposta:** 0.9
**Área:** Candidate Mining / Benchmark Construction
**Prioridade:** P0
**Objetivo primário:** aumentar substancialmente o número de repositories que conseguem produzir um benchmark útil out-of-the-box, sem reduzir a confiabilidade metodológica das tasks.

---

# 1. Executive Summary

Hoje o RepoBench utiliza limites absolutos de tamanho, como LOC e quantidade de arquivos modificados, para decidir se um Pull Request histórico pode seguir no pipeline de construção de evals.

Exemplo atual:

```yaml
task_mining:
  min_implementation_loc: 20
  max_implementation_loc: 400
  max_implementation_files: 8
```

Essa abordagem possui uma propriedade desejável:

> evita transformar mudanças extremamente grandes e difíceis de reconstruir em evals frágeis.

Porém, field tests em repositories reais mostraram que ela cria um problema de produto mais grave:

> repositories perfeitamente válidos podem produzir zero candidates simplesmente porque o estilo de desenvolvimento daquele projeto produz PRs maiores que os thresholds arbitrários.

O problema não é apenas tuning dos números.

É conceitual.

**Tamanho do diff não deve ser um hard gate de validade.**

Uma alteração de 900 LOC pode ter:

* issue excelente;
* teste específico;
* gold reproduzível;
* um único comportamento alterado;
* forte verifier;
* ambiente estável.

Enquanto uma alteração de 70 LOC pode envolver:

* múltiplos subsistemas;
* comportamento emergente;
* side effects;
* testes frágeis;
* baixa clareza de intenção.

A V0.9 substituirá parte dos hard filters por um sistema de:

# Candidate Assessment

Cada PR recebe avaliações independentes de:

```text
instruction quality
verification evidence
reconstructability
change scope
isolation
environment confidence
```

Em vez de:

```text
LOC > 400
→ reject
```

teremos:

```text
LOC = 950
+
strong verifier
+
clear issue
+
single subsystem
+
reconstructable history
→ candidate
→ complexity: large
→ confidence: high
```

Hard rejection continuará existindo para propriedades metodologicamente necessárias.

O objetivo desta mudança não é “aceitar mais PRs”.

É:

> **encontrar mais evals confiáveis entre os PRs que hoje descartamos cedo demais.**

---

# 2. Problema

O funil atual pode produzir:

```text
repository
    ↓
hundreds of merged PRs
    ↓
hard LOC/files filtering
    ↓
0 candidates
```

Do ponto de vista do usuário:

```text
pip install repobench
repobench init
repobench analyze

→ no usable candidates
```

Esse é um failure mode de activation extremamente grave.

O usuário não chega a experimentar:

* benchmark generation;
* agent execution;
* quality comparison;
* cost comparison;
* benchmark health;
* decision layer.

Portanto:

# Candidate yield é atualmente uma métrica de produto, não apenas de infraestrutura.

---

# 3. Evidência

Field tests já mostraram repositories reais em que os thresholds default eliminam praticamente todo o espaço de candidates.

Especialmente aplicações modernas TypeScript podem ter PRs maiores por natureza devido a:

* componentes;
* schemas;
* migrations;
* generated types;
* API definitions;
* tests;
* snapshots;
* configuration;
* package metadata;
* frontend/backend changes simultâneas.

O tamanho bruto da mudança acaba correlacionando mal com a dificuldade real da tarefa.

---

# 4. Objetivo

Aumentar:

# Validated Eval Yield

definido como:

```text
VALID tasks
──────────────
relevant PRs
```

sem reduzir:

# Task Validity Precision

Ou seja, não queremos:

```text
mais tasks
↓
benchmark pior
```

Queremos:

```text
melhor candidate recall
+
mesmos hard methodological standards
```

---

# 5. Métrica North Star

## Repository Benchmarkability Rate

Percentual dos repositories suportados que conseguem gerar um benchmark minimamente útil.

Definição inicial:

```text
repository benchmarkable =
≥10 VALID tasks
```

Target para beta:

```text
≥70% dos repos considerados suportados
```

após no máximo:

```text
1 ajuste manual de configuração
```

Esse target deverá ser recalibrado após a field-validation wave.

---

# 6. Métricas secundárias

### Candidate Recall

```text
DISCOVERED candidates
─────────────────────
relevant PRs
```

### Validation Yield

```text
VALID
──────
DISCOVERED
```

### End-to-end Eval Yield

```text
VALID
──────
relevant PRs
```

### Zero-result Rate

```text
repositories com 0 VALID tasks
──────────────────────────────
repositories analisados
```

Target:

reduzir drasticamente.

### Time to First Benchmark

```text
install
→
benchmark ready
```

### Manual Intervention Rate

Percentual de repositories que exigem:

* alterar thresholds;
* adicionar cwd;
* alterar test command;
* alterar mining config.

---

# 7. Non-goals

Esta mudança NÃO deve:

* relaxar no-op validation;
* relaxar oracle validation;
* aceitar verifier flaky;
* substituir tests por LLM judge;
* gerar synthetic tests;
* garantir que qualquer PR possa virar eval;
* remover complexity estimation;
* aceitar automaticamente tasks cross-repository;
* resolver environments arbitrários;
* resolver long-context agents;
* dividir automaticamente qualquer PR gigante em várias tasks na primeira versão.

---

# 8. Princípio central

Separar:

# Eligibility

de:

# Difficulty

Hoje parte da difficulty é implicitamente usada como eligibility.

Exemplo:

```text
950 LOC
→ too large
→ rejected
```

Novo modelo:

```text
950 LOC
→ high complexity

then:

Can we reconstruct it?
Can we verify it?
Can we describe it?
Can we isolate it?

If yes:
→ valid candidate
```

Isso é uma distinção fundamental.

---

# 9. Hard Gates que permanecem

Algumas condições continuam gerando rejeição imediata.

## Mandatory history

Precisamos reconstruir:

```text
BASE
+
GOLD
```

Sem isso:

```text
HISTORY_UNSUPPORTED
```

---

## Implementation exists

Mudança precisa alterar comportamento implementado.

Sem implementation diff:

```text
NO_IMPLEMENTATION_CHANGE
```

---

## Verifier evidence

Para V1 atual, precisa existir teste histórico relevante ou verifier equivalente suportado.

Sem isso:

```text
NO_TEST_CHANGE
```

continua válido.

---

## Instruction

Precisa haver instruction provenance aceitável.

Sem instruction:

```text
NO_INSTRUCTION
```

---

## Validation

Depois da construção:

```text
NOOP must fail
GOLD must pass
DETERMINISM must pass
REGRESSION must pass
```

Nenhum score compensa falha nesses gates.

---

# 10. Hard Gates removidos

Os seguintes não devem mais rejeitar automaticamente:

```text
max_implementation_loc
max_implementation_files
```

Eles passam a alimentar:

```text
Change Scope Score
```

e:

```text
Complexity
```

---

# 11. Novo modelo: Candidate Assessment

Cada PR recebe uma estrutura:

```json
{
  "candidate_id": "...",

  "eligibility": {
    "history": true,
    "instruction": true,
    "implementation": true,
    "verifier_evidence": true
  },

  "assessment": {
    "instruction_quality": 0.92,
    "verifier_strength_precheck": 0.86,
    "reconstructability": 0.95,
    "change_scope": 0.48,
    "isolation_confidence": 0.81,
    "environment_confidence": 0.73
  },

  "complexity": "large",

  "candidate_score": 0.81,

  "risk_flags": [
    "large_diff"
  ]
}
```

---

# 12. Candidate Score

Score inicial:

```text
CandidateScore =

0.25 × Instruction Quality
+
0.25 × Verifier Evidence
+
0.20 × Reconstructability
+
0.10 × Isolation Confidence
+
0.10 × Environment Confidence
+
0.10 × Scope Confidence
```

Importante:

`CandidateScore` não determina correctness.

Ele determina:

> **quais candidates vale a pena tentar validar primeiro.**

---

# 13. Por que usar score

Validation real pode custar:

* dependency install;
* build;
* multiple test executions;
* filesystem operations.

Não queremos gastar minutos validando candidates ruins enquanto candidates excelentes estão disponíveis.

Portanto:

```text
discovery
    ↓
cheap candidate assessment
    ↓
rank
    ↓
expensive validation
```

---

# 14. Instruction Quality

Inputs:

```text
linked issue exists
issue predates solution
description length
problem-oriented wording
solution-contamination signals
```

Exemplo:

### Score alto

Issue anterior ao PR:

> Users receive duplicated invoices when webhook retries happen.

```text
Instruction Quality = 0.95
```

### Score médio

PR:

> Fix invoice retry handling.

```text
0.70
```

### Score baixo

PR:

> Refactor WebhookProcessor using IdempotencyRegistry.

```text
0.35
```

Porque descreve parte da solução.

---

# 15. Verifier Evidence Score

Antes da validação completa, podemos estimar a qualidade potencial do verifier.

Sinais:

```text
tests added
tests changed
assertion count
test-to-implementation ratio
number of test files
specificity of changed tests
fixtures/snapshots
```

Não usar isso como verdict.

Apenas prioritization.

---

# 16. Reconstructability Score

Inputs:

```text
base SHA exists
head SHA exists
merge SHA exists
merge style understood
diff reconstruction clean
repository object available
```

Exemplo:

```text
clean squash merge
→ 1.0

complex rebased sequence
→ 0.5
```

Se não for possível reconstruir:

hard reject.

---

# 17. Change Scope

Substitui hard LOC threshold.

Inputs:

```text
implementation LOC
files changed
directories touched
packages touched
languages touched
generated files
test LOC
```

Normalizar dentro do próprio repository.

Isso é importante.

Um PR com 900 LOC pode ser enorme num package pequeno e normal em outro projeto.

---

# 18. Relative Scope

Calcular percentis do repository:

```text
PR A: 900 LOC

Repository distribution:

P50 = 320 LOC
P75 = 780 LOC
P90 = 1800 LOC

PR A ≈ P78
```

Portanto:

```text
large
```

mas não necessariamente:

```text
unsupported
```

---

# 19. Complexity V2

Complexity deve passar a ser relativa.

Exemplo:

```text
small   = P0–P35
medium  = P35–P80
large   = P80+
```

Opcionalmente:

```text
xlarge = P95+
```

Isso melhora também Benchmark Representativeness.

---

# 20. Generated File Discount

LOC bruto não deve tratar igualmente:

```text
src/payment.ts
```

e:

```text
generated/graphql-types.ts
```

Detectar padrões conhecidos:

```text
generated
dist
build
*.lock
snapshots
codegen
```

Generated LOC recebe peso menor no Scope Score.

Exemplo:

```text
total LOC = 1700
generated = 1200
behavioral LOC = 500
```

Não tratar como uma task de 1700 LOC.

---

# 21. Structural Diff Features

Adicionar sinais:

```text
number of source directories
number of packages
number of languages
number of test files
implementation/test ratio
```

Uma mudança:

```text
900 LOC
2 source files
3 test files
1 subsystem
```

pode ser metodologicamente melhor que:

```text
180 LOC
12 files
5 packages
4 subsystems
```

---

# 22. Isolation Confidence

Avaliar se a task parece suficientemente isolada.

Sinais negativos:

```text
many packages
many unrelated root directories
migration + runtime + infra simultaneously
large generated changes
cross-language implementation
```

Sinais positivos:

```text
single subsystem
coherent paths
single package
tests colocated
```

---

# 23. Environment Confidence

Barato, antes da validation real.

Sinais:

```text
supported package manager
lockfile exists
known test runner
known install command
project.cwd known
```

Exemplo:

```text
pnpm-lock.yaml
package.json
vitest
→ high
```

---

# 24. Candidate Classes

Além do score numérico, expor classes humanas.

```text
HIGH_CONFIDENCE
MEDIUM_CONFIDENCE
EXPERIMENTAL
```

### High

```text
score ≥0.80
```

### Medium

```text
0.60–0.79
```

### Experimental

```text
<0.60
```

Thresholds configuráveis e sujeitos a calibração.

---

# 25. Validation Strategy

Ao executar:

```bash
repobench benchmark build
```

validar na ordem:

```text
HIGH
↓
MEDIUM
↓
EXPERIMENTAL
```

Dentro de cada grupo:

```text
candidate_score descending
```

Isso reduz Time to First Benchmark.

---

# 26. Stop When Sufficient

Já existe comportamento de:

```text
--stop-when-sufficient
```

Essa feature deve virar parte central da estratégia adaptativa.

Fluxo:

```text
Need 24 tasks
    ↓
validate highest-confidence candidates
    ↓
24 VALID achieved
    ↓
stop
```

Em vez de:

```text
validate 180 candidates
before knowing whether enough exist
```

---

# 27. Porém representativeness importa

Não podemos simplesmente escolher os 24 maiores scores.

Senão podemos gerar:

```text
20 bugfixes
4 features
0 integrations
```

Portanto validation prioritization deve considerar:

```text
candidate quality
+
benchmark coverage need
```

---

# 28. Adaptive Validation Priority

Prioridade dinâmica:

```text
ValidationPriority =

0.65 × CandidateScore
+
0.35 × CoverageNeed
```

CoverageNeed representa o quanto aquela candidate ajuda a preencher dimensões ainda sub-representadas.

Exemplo:

Benchmark atual:

```text
bugfix       enough
frontend     enough
integration  missing
```

Candidate integration recebe boost.

---

# 29. Coverage-Aware Queue

Exemplo:

```text
Candidate A
score 0.93
bugfix
payments

Candidate B
score 0.86
integration
platform
```

Se bugfix/payments já estiver representado:

```text
B may validate before A.
```

Isso reduz custo sem sacrificar representatividade.

---

# 30. Candidate Funnel

`repobench analyze` deve mostrar o funil completo.

Exemplo:

```text
RepoBench Repository Analysis

Merged PRs
1,842

Relevant human changes
1,104

With implementation changes
987

With verifier evidence
423

With usable instructions
361

Candidate classes

High confidence       126
Medium confidence     147
Experimental           88

Estimated benchmarkability

HIGH

Target benchmark:
24 tasks
```

---

# 31. Zero-Candidate Experience

Não terminar apenas com:

```text
0 candidates.
```

Isso é um dead end.

Exemplo:

```text
No high-confidence candidates found.

Primary blockers:

No test evidence            61%
Instruction unavailable     24%
Unsupported history          9%
Other                        6%

What to try:

1. Increase lookback window
2. Enable Tier C instructions
3. Configure custom test paths

Estimated opportunity:

37 PRs could become candidates if
Tier C instructions are enabled.
```

O erro precisa ensinar como desbloquear o benchmark.

---

# 32. Cap Pressure Diagnostic

Precisamos mostrar explicitamente quanto o modelo antigo de threshold estaria afetando o repository.

Exemplo:

```text
Repository change profile

Median implementation LOC     420
P75                            910
P90                           2,140

Previous default cap          400

73% of otherwise eligible PRs
would exceed the old cap.
```

Isso ajuda a calibrar o novo sistema.

---

# 33. Configuração

Novo default:

```yaml
task_mining:

  require_test_change: true

  size_policy: adaptive

  complexity:
    method: repository_percentile

  candidate_score:
    high_confidence: 0.80
    medium_confidence: 0.60

  validation:
    include:
      - high
      - medium
```

---

# 34. Backward Compatibility

Permitir:

```yaml
size_policy: hard_caps

min_implementation_loc: 20
max_implementation_loc: 400
max_implementation_files: 8
```

por uma versão de transição.

Mas:

```text
adaptive
```

deve virar default.

---

# 35. CLI — Candidate View

```bash
repobench candidates
```

Novo output:

```text
PR      SCORE    CLASS     TYPE        SIZE      STATUS

#482    0.94     HIGH      bugfix      medium    discovered
#501    0.91     HIGH      feature     large     discovered
#487    0.84     HIGH      bugfix      large     discovered
#533    0.72     MEDIUM    integration xlarge    discovered
```

---

# 36. Candidate Inspect

```bash
repobench candidate inspect 501
```

Output:

```text
PR #501

Candidate Score
0.91

Instruction
0.96
Tier A
Linked issue #488

Verifier Evidence
0.92
3 tests changed
74 test LOC

Reconstructability
1.00

Isolation
0.87
single subsystem

Environment
0.96
pnpm + vitest

Scope
0.58

Change size
1,184 implementation LOC
12 files
P82 for this repository

Classification
LARGE

Recommendation
Strong candidate despite large diff.
```

Isso torna a decisão explicável.

---

# 37. Rejection Model

Separar:

```text
FILTERED
```

de:

```text
LOW_PRIORITY
```

Hoje uma large task pode ser FILTERED.

Novo modelo:

```text
DISCOVERED
LOW_PRIORITY
VALIDATING
VALID
REJECTED
```

Low priority não significa inválida.

Significa:

> provavelmente não vale validar antes de candidates melhores.

---

# 38. Stable Failure Codes

Hard methodological failures continuam:

```text
NO_INSTRUCTION
NO_TEST_CHANGE
HISTORY_UNSUPPORTED
BASELINE_BROKEN
NOOP_PASSES
GOLD_FAILS
FLAKY_VERIFIER
ENVIRONMENT_UNSUPPORTED
LEAKAGE_HIGH
```

Remover como default hard rejection:

```text
TASK_TOO_LARGE
```

Manter:

```text
TASK_EXTREME_SCOPE
```

apenas para safety/performance extremos.

---

# 39. Extreme Scope Guard

Ainda precisamos evitar:

```text
75k LOC
800 files
monorepo-wide rewrite
```

Default:

não usar número absoluto pequeno.

Usar guard muito alto, por exemplo:

```text
absolute emergency cap
```

destinado a impedir comportamento patológico.

Esse limite deve ser:

* muito acima do workload normal;
* configurável;
* apresentado claramente.

Nunca confundir com complexity threshold.

---

# 40. Benchmark Sampling

Depois das tasks VALID:

sampling continua tentando aproximar:

```text
task type
subsystem
complexity
```

Mas agora terá acesso a:

```text
large
xlarge
```

que anteriormente eram frequentemente eliminadas.

Isso pode aumentar a fidelidade do benchmark.

---

# 41. Novo Benchmark Health Signal

Adicionar:

# Candidate Coverage

Pergunta:

> Qual fração do Workload Universe possui pelo menos tasks válidas similares?

Exemplo:

```text
Workload coverage

Bugfix          94%
Feature         87%
Integration     61%
Migration       12%
```

Isso é diferente de:

```text
benchmark sampling distribution
```

porque revela gaps do pipeline.

---

# 42. Benchmarkability

Novo conceito:

```text
Benchmarkability
```

Score de repository antes do benchmark.

Componentes:

```text
Verifier availability
Instruction availability
Reconstructability
Environment support
Candidate diversity
```

Exemplo:

```text
Repository Benchmarkability

82 / 100

Verifier evidence       91
Instructions            86
History                 97
Environment             94
Candidate diversity     44
```

Isso entrega valor mesmo antes do agent run.

---

# 43. Product Insight

Esse score pode futuramente se tornar extremamente interessante.

Porque diz:

> “quão preparada está esta engenharia para avaliar coding agents?”

Isso pode revelar problemas organizacionais:

```text
few tests
poor issue descriptions
huge PRs
non-reproducible builds
```

RepoBench pode começar a medir não apenas agents, mas:

# AI-evaluability of engineering organizations

Não faz parte do scope comercial da V0.9, mas o schema deve permitir essa evolução.

---

# 44. Leakage False Positives

A mesma onda deve corrigir o problema observado em leakage substring matching.

Hoje referências comuns podem gerar:

```text
LEAKAGE_HIGH
```

indevidamente.

O detector deve distinguir:

```text
#7
```

como referência genérica de issue/PR

de:

```text
exact gold identifier
new function/class/symbol
future filename
```

---

# 45. Leakage Risk Levels

Em vez de binário:

```text
leakage / no leakage
```

usar:

```text
LOW
MEDIUM
HIGH
```

Somente HIGH gera hard reject.

Medium:

```text
candidate warning
```

---

# 46. Leakage Evidence

Toda classificação precisa dizer por quê.

Exemplo:

```text
Leakage risk: MEDIUM

Evidence:

Instruction mentions identifier
"IdempotencyRegistry"

Identifier:
absent in BASE
introduced in GOLD
```

Isso é muito melhor que score opaco.

---

# 47. Field Validation Wave

A feature não deve ser considerada pronta somente porque unit tests passam.

Precisamos executar em uma matrix real.

Target:

```text
30 repositories
```

---

# 48. Repository Matrix

Distribuição recomendada:

```text
5 Python libraries
5 Python applications
5 TypeScript libraries
5 TypeScript applications
5 monorepos
5 mixed/less-curated projects
```

---

# 49. Repositories devem variar

Dimensões:

```text
small vs large
old vs new
squash vs merge
few vs many tests
monorepo vs single package
library vs app
high vs low PR volume
```

---

# 50. Field Test Protocol

Para cada repository:

```text
1. clone
2. install RepoBench
3. repobench init
4. repobench analyze
5. repobench benchmark build
```

Primeira passada:

```text
ZERO config tuning
```

Segunda passada:

```text
≤1 config modification
```

---

# 51. Dados coletados

Para cada repo:

```text
merged PRs
relevant PRs
high candidates
medium candidates
valid tasks
validation failures
benchmark size
benchmark health
elapsed build time
manual interventions
```

---

# 52. Comparação Old vs New

Rodar:

```text
hard_caps
```

e:

```text
adaptive
```

no mesmo repository.

Medir:

```text
Candidate recall
Valid yield
Benchmark health
Validation cost
False-positive task rate
```

---

# 53. Critério de sucesso principal

Adaptive mining deve produzir:

```text
significant increase in VALID tasks
```

sem queda relevante em:

```text
human-reviewed task validity
```

---

# 54. Manual Audit

Amostra:

```text
100 VALID tasks
```

revistas manualmente.

Para cada uma avaliar:

```text
Is instruction legitimate?
Is verifier meaningful?
Is task coherent?
Is historical reconstruction faithful?
Would this be a reasonable coding-agent task?
```

---

# 55. Precision Target

Meta inicial:

```text
≥90% das VALID tasks
```

devem ser consideradas metodologicamente aceitáveis por revisão humana.

Se aumentar recall reduzir precision abaixo disso:

recalibrar score.

---

# 56. Large Task Audit

Auditar separadamente:

```text
small
medium
large
xlarge
```

Hipótese:

large tasks anteriormente rejeitadas podem ser perfeitamente válidas.

Precisamos provar isso empiricamente.

---

# 57. Success Criteria

Feature pronta para default quando:

### Activation

```text
≥70% dos supported repositories
geram ≥10 VALID tasks
```

ou meta revisada após baseline.

### Candidate recall

Melhora material em relação ao hard-cap baseline.

### Quality

```text
≥90% manually-audited precision
```

### Zero-result rate

Redução de pelo menos:

```text
50%
```

vs hard caps.

### Cost

Tempo de build permanece aceitável através de adaptive prioritization.

---

# 58. Telemetry / Observability

Mesmo local-first, logs estruturados devem permitir gerar:

```json
{
  "repo": "...",
  "relevant_prs": 219,
  "candidates_high": 31,
  "candidates_medium": 48,
  "validated": 24,

  "rejections": {
    "NO_TEST_CHANGE": 71,
    "NO_INSTRUCTION": 28,
    "GOLD_FAILS": 6
  }
}
```

Nome do repository não precisa sair da máquina.

---

# 59. New Analyze Summary

Desired output:

```text
Repository Analysis
────────────────────────────────────

Merged PRs                         842
Relevant human changes             517

Candidate opportunity

High confidence                     41
Medium confidence                   76
Experimental                        29

Large changes are common here.

Median implementation LOC          384
P75                                811
P90                              1,742

Benchmarkability

HIGH

Estimated VALID tasks
25–48

Requested benchmark size
24

Primary risks

Integration tasks have weak test evidence.
Migration work may be underrepresented.
```

---

# 60. Benchmark Build Experience

```text
Building benchmark

Need: 24 VALID tasks

[01] PR #481 · score .95 · bugfix       ✓ VALID
[02] PR #510 · score .93 · feature      ✓ VALID
[03] PR #499 · score .91 · integration  ✗ NOOP_PASSES
...

Validated
12 / 24

Coverage need:
integration ↑

Prioritizing integration candidates...
```

Isso torna visível o adaptive behavior.

---

# 61. Stop Condition

Build termina quando:

```text
requested benchmark size reached
AND
minimum representativeness threshold reached
```

Exemplo:

```text
24 tasks reached
but subsystem coverage = 61
```

AgentFit pode validar algumas candidates extras para tentar melhorar coverage.

---

# 62. Config

```yaml
benchmark:

  size: 24

  min_representativeness: 75

  validation_budget:
    max_candidates: 60
```

Assim temos bounded work.

---

# 63. Validation Budget

Nova entidade importante.

Em vez de validar indefinidamente:

```text
max_candidates
```

ou futuramente:

```text
max_minutes
```

Exemplo:

```text
Need 24 tasks

Validated candidates:
39

VALID:
24

Stopped.
```

---

# 64. Graceful Insufficiency

Se budget terminar com:

```text
17 valid tasks
```

não falhar necessariamente.

Mostrar:

```text
Requested:
24

Available:
17

Benchmark created with 17 tasks.

Health:
71

Warnings:
Low sample size.
Integration work underrepresented.
```

Melhor uma resposta honesta que nenhum benchmark.

---

# 65. Minimum Benchmark Size

Default:

```text
min_size: 10
```

Abaixo:

```text
INSUFFICIENT_BENCHMARK
```

Não gerar recommendation forte.

---

# 66. Statistical Implication

Benchmark pequeno aumenta uncertainty.

Isso já é tratado pelo reporting.

Portanto não precisamos:

> rejeitar benchmark pequeno

quando podemos:

> reportar intervalos largos.

---

# 67. Backwards Migration

Existing repositories com:

```yaml
max_implementation_loc
```

continuam funcionando.

Mostrar warning:

```text
Legacy hard-cap mining configuration detected.

This mode is supported but no longer recommended.

Run:
repobench config migrate
```

P1, se necessário.

---

# 68. Storage Changes

Adicionar no candidate:

```text
candidate_score
candidate_class

instruction_score
verifier_score
reconstructability_score
scope_score
isolation_score
environment_score

scope_percentile
risk_flags
```

---

# 69. Assessment Versioning

Score deve registrar:

```text
assessment_version
```

porque o algoritmo evoluirá.

Exemplo:

```text
adaptive-v1
```

Isso permite reproduzir benchmark histórico.

---

# 70. Benchmark Manifest

Adicionar:

```json
{
  "candidate_methodology": {
    "version": "adaptive-v1"
  }
}
```

---

# 71. Explainability Requirement

Todo score precisa ser decomponível.

Nunca:

```text
Candidate score: 0.74
```

somente.

Sempre:

```text
Candidate score: 0.74

Instruction         0.91
Verifier            0.82
History             1.00
Isolation           0.61
Environment         0.78
Scope               0.40
```

---

# 72. No ML Model na V0.9

Não treinar classifier.

Heurísticas determinísticas são melhores inicialmente porque:

* auditáveis;
* debuggable;
* reproducíveis;
* community-friendly;
* fáceis de calibrar.

Depois do field dataset:

podemos considerar learned ranking.

---

# 73. Dataset potencial

A Field Validation Wave cria, incidentalmente:

```text
PR
+
features
+
candidate score
+
validation outcome
+
human audit
```

Isso pode virar dataset para:

```text
candidate ranking model
```

futuramente.

Mas só depois.

---

# 74. Future Opportunity — Learned Candidate Ranking

V2:

```text
P(VALID | PR features)
```

Pode substituir parte das heurísticas.

Mas hard gates continuam.

---

# 75. Future Opportunity — PR Decomposition

Large PRs podem conter várias logical tasks.

Futuro:

```text
PR
 ↓
semantic clusters
 ↓
task A
task B
task C
```

Isso poderia aumentar muito eval yield.

Mas não pertence a esta versão.

---

# 76. Future Opportunity — Agent Session Mining

Outro caminho para aumentar yield:

```text
Codex / Claude historical sessions
```

→ task + original instruction.

Também fora da V0.9.

---

# 77. Rollout Plan

## Phase A — Instrumentation

Primeiro implementar:

* candidate score fields;
* scope percentile;
* funnel metrics;
* cap-pressure report.

Ainda sem mudar default.

---

## Phase B — Shadow Mode

Executar:

```text
hard_caps decision
+
adaptive decision
```

simultaneamente.

Só hard caps controlam benchmark.

Registrar diferenças.

---

## Phase C — Field Validation

30 repos.

Comparar.

---

## Phase D — Adaptive opt-in

```yaml
size_policy: adaptive
```

---

## Phase E — Adaptive default

Depois dos success criteria.

---

# 78. Engineering Breakdown

### Epic 1 — Repository-relative scope

* distribution calculator;
* percentile computation;
* generated-file detection;
* structural scope features.

### Epic 2 — Candidate assessment

* component scores;
* aggregation;
* classes;
* explanation.

### Epic 3 — Adaptive prioritization

* candidate queue;
* coverage need;
* validation priority;
* validation budget.

### Epic 4 — UX

* funnel;
* benchmarkability;
* candidate inspect;
* graceful insufficiency.

### Epic 5 — Leakage improvements

* remove naive substring matching;
* evidence-based risk;
* LOW/MEDIUM/HIGH.

### Epic 6 — Field validation

* test matrix;
* result artifact;
* manual audit.

---

# 79. Acceptance Criteria — Mining

Dado um repository onde:

```text
median LOC > current 400 cap
```

RepoBench não pode rejeitar automaticamente a maioria dos candidates exclusivamente por tamanho.

---

# 80. Acceptance Criteria — Large Candidate

Dado um PR com:

```text
1200 LOC
12 files
```

mas:

```text
valid history
valid instruction
test changes
supported environment
```

ele deve poder seguir para validation.

---

# 81. Acceptance Criteria — Hard Validation

Independentemente do candidate score:

se:

```text
NOOP passes
```

task é rejeitada.

Se:

```text
GOLD fails
```

task é rejeitada.

Score nunca altera esse comportamento.

---

# 82. Acceptance Criteria — Explainability

Para toda discovered candidate:

CLI deve explicar:

* score;
* class;
* scope;
* principais fatores positivos;
* principais riscos.

---

# 83. Acceptance Criteria — Benchmark Build

Build deve priorizar:

```text
high-quality
+
coverage-improving
```

candidates.

---

# 84. Acceptance Criteria — Zero Results

Quando benchmark não puder ser criado:

RepoBench deve explicar:

```text
why
+
how many PRs failed at each stage
+
what user action could materially improve yield
```

---

# 85. Acceptance Criteria — Field Test

Antes de adaptive virar default:

executar ao menos:

```text
30 repositories
```

e publicar internamente relatório old vs new.

---

# 86. Risks

## Risk A — More large tasks → expensive validation

Mitigação:

adaptive ordering + budget.

---

## Risk B — Large tasks reduce agent solve rate

Isso não é necessariamente um problema.

Pode ser precisamente realidade do workload.

Benchmark não deve ser fácil.

Deve ser representativo.

---

## Risk C — Large task verifier less specific

Mitigação:

Verifier Evidence Score + hard no-op/oracle.

---

## Risk D — Score becomes arbitrary

Mitigação:

field calibration + explainability + versioning.

---

## Risk E — More complexity in mining

Real.

Mas essa complexidade está no core differentiator do produto.

Vale investir aqui mais do que em HTML/dashboard.

---

# 87. Strategic Rationale

O moat potencial do RepoBench não está em:

```text
run Codex
run Claude
```

Esses CLIs já existem.

Também não está principalmente em:

```text
report pass rate
```

Isso é fácil de replicar.

Está em:

# historical engineering work

→ trustworthy eval candidates
→ representative benchmark

Portanto melhorar Candidate Yield sem sacrificar methodology não é uma melhoria incremental.

É uma melhoria no principal ativo estratégico do produto.

---

# 88. Definition of Done

A feature estará concluída quando um developer puder rodar:

```bash
repobench analyze
```

em um repository moderno, com PRs reais e heterogêneos, e o RepoBench consiga responder:

```text
Here is the real shape of your workload.

Here are the historical changes that
can plausibly become trustworthy evals.

Here is how confident we are in each one.

Here is why.

And here is the shortest path to a
representative benchmark.
```

Sem depender de um cap arbitrário como:

```text
400 LOC
```

para decidir previamente o que constitui ou não uma boa coding-agent task.

O resultado desejado é:

```text
MORE RECALL
+
SAME VALIDATION RIGOR
+
BETTER REPRESENTATIVENESS
+
LOWER TIME TO FIRST BENCHMARK
```

e não simplesmente:

```text
MORE TASKS.
```
