NOTE — The provisional product name "AgentFit" in this PRD was renamed to "RepoBench" (official project name). Read every occurrence of AgentFit / agentfit / .agentfit/ / agentfit.yml as RepoBench / repobench / .repobench/ / repobench.yml.

# PRD — AgentFit V1

## Repository-native evals for coding agents, with simple local execution

**Status:** Draft para implementação
**Versão:** 0.2
**Produto:** AgentFit — nome provisório
**Modelo:** Open Source / local-first
**Licença recomendada:** Apache 2.0
**Interface V1:** CLI
**Execution:** local subprocess runner
**Source control V1:** Git + GitHub
**Linguagens V1:** Python + JavaScript/TypeScript
**Harnesses oficiais V1:** Claude Code, Codex CLI, OpenCode, Gemini CLI
**Extensibilidade:** custom command adapter
**Backend AgentFit:** nenhum
**Data:** agosto de 2026

---

# 1. Resumo executivo

AgentFit transforma o histórico real de engenharia de um repositório em uma suíte privada, reproduzível e representativa de avaliações para coding agents.

A pergunta central é:

> **Qual stack de coding agent funciona melhor para o trabalho que realmente acontece neste codebase?**

A unidade comparada não é apenas o modelo.

É um:

# Execution Target

```text
Harness
+
Model
+
Provider/configuração local
+
Harness configuration
+
Repository instructions
```

Exemplos:

```text
Claude Code + Opus + Anthropic
Codex + GPT-X + OpenAI
OpenCode + GLM + Z.AI
OpenCode + MiniMax + OpenRouter
OpenCode + modelo local + Ollama
Gemini CLI + Gemini Pro
custom-harness + modelo interno
```

O AgentFit não gerencia API keys, não implementa um agent loop, não implementa ferramentas de coding agent e não faz chamadas de inferência diretamente.

Ele utiliza os CLIs que o usuário já possui e já configurou localmente.

O fluxo é:

```text
Engineering history
        │
        ▼
     DISCOVER
Quais PRs podem virar evals?
        │
        ▼
     VALIDATE
Esses evals realmente medem algo?
        │
        ▼
    REPRESENT
O benchmark representa o workload?
        │
        ▼
   PREPARE TASK
Cria workspace histórico isolado
        │
        ▼
 LOCAL EXECUTION
claude / codex / opencode / gemini
        │
        ▼
     VERIFY
Hidden tests + regression
        │
        ▼
     MEASURE
Quality / cost / time / tokens
        │
        ▼
      DECIDE
Qual target faz mais sentido?
```

A V1 deve parecer:

```bash
agentfit init
agentfit benchmark build
agentfit run codex claude opencode-glm
agentfit report
```

e não:

> “configure uma infraestrutura de benchmark distribuída.”

---

# 2. Decisões centrais da V1

| Decisão                            | V1                          |
| ---------------------------------- | --------------------------- |
| Execution framework externo        | Não                         |
| Docker obrigatório                 | Não                         |
| Backend                            | Não                         |
| API keys gerenciadas pelo AgentFit | Não                         |
| Agent loop próprio                 | Não                         |
| Local subprocess                   | **Sim**                     |
| Harnesses instalados pelo usuário  | **Sim**                     |
| Configuração/provider existente    | **Reutilizada**             |
| Modelo override                    | Quando suportado            |
| Custom harness                     | **Sim**                     |
| Hidden verifier                    | **Sim**                     |
| Synthetic Git repository           | **Sim**                     |
| Network isolation                  | Não garantido               |
| Future Git history isolation       | **Sim**                     |
| GitHub credential sanitization     | **Sim**                     |
| LLM judge                          | Não                         |
| Generated tests                    | Não                         |
| Benchmark Health                   | **Sim**                     |
| Statistical uncertainty            | **Sim**                     |
| Cost                               | Reportado quando observável |
| Execution concurrency              | Local, configurável         |
| SaaS                               | Não                         |

---

# 3. Problema

Benchmarks públicos respondem:

> “qual modelo ou agente performa melhor em determinado benchmark?”

Uma equipe precisa responder:

> “qual configuração performa melhor no trabalho que nós realmente fazemos?”

Essas perguntas são diferentes.

Exemplo:

```text
Public benchmark:

Claude       71%
Codex        68%
Model C      63%
```

Isso não responde necessariamente:

```text
Nosso repository:

Bugfix           28%
Feature          24%
Refactor         17%
Integration      13%
Migration         7%
Performance       6%
Infra             5%
```

Nem responde:

```text
Claude Code + Opus
vs
Codex + GPT-X
vs
OpenCode + GLM
vs
OpenCode + MiniMax
```

Também não mede:

* configuração de reasoning;
* harness;
* provider;
* repository instructions;
* MCPs;
* skills;
* tools;
* custo real;
* latência real.

Na prática, o objeto relevante é a stack inteira.

---

# 4. Product Thesis

Coding intelligence está ficando abundante.

Measurement continua escasso.

Conforme uma organização passa de:

```text
1 coding model
```

para:

```text
5 harnesses
×
15 models
×
8 providers
×
3 reasoning modes
×
N instructions/tools
```

escolher a configuração ótima empiricamente se torna difícil.

AgentFit transforma o próprio histórico da equipe em infrastructure de decisão.

---

# 5. Hipóteses

## H1 — Engineering history contém evals

Uma fração dos PRs históricos contém:

```text
estado inicial
+
intenção original
+
mudança correta aceita
+
verificação automatizada
```

suficiente para construir uma tarefa retrospectiva.

---

## H2 — Qualidade > quantidade

É preferível produzir:

```text
24 evals altamente confiáveis
```

a:

```text
400 evals duvidosos.
```

---

## H3 — Benchmark validity e representativeness são diferentes

Um benchmark pode possuir tarefas perfeitamente válidas e representar pessimamente o workload.

Portanto teremos:

```text
Task Validity
≠
Benchmark Representativeness
```

---

## H4 — Não precisamos possuir o agent loop

Claude Code, Codex, OpenCode, Gemini CLI e outros harnesses já resolvem:

```text
prompt
→
reasoning
→
tools
→
file editing
→
testing
```

AgentFit precisa somente executar o processo.

---

## H5 — Local execution é suficiente para PMF inicial

Não precisamos de:

```text
distributed runners
cloud sandboxes
multi-tenant orchestration
```

para responder:

> “qual desses três targets funciona melhor no meu repo?”

---

# 6. Princípio fundamental

> **AgentFit mede agentes. Não tenta ser um agente.**

A execution layer deve permanecer pequena.

Ela possui somente:

1. preparar workspace;
2. preparar environment;
3. iniciar processo;
4. observar stdout/stderr;
5. impor timeout;
6. terminar process tree;
7. capturar mudanças;
8. executar verifier;
9. persistir métricas.

Qualquer funcionalidade além disso exige justificativa forte.

---

# 7. Usuários

## Persona A — AI Engineering / Developer Productivity

Responsável por decidir:

* coding harness;
* modelo;
* provider;
* reasoning;
* configurations;
* agent instructions;
* tools.

JTBD:

> Quando preciso padronizar coding agents para meu time, quero testá-los contra nosso próprio workload para selecionar uma stack baseada em qualidade, custo e velocidade.

---

## Persona B — Staff / Principal Engineer

Usa vários coding agents e possui uma percepção subjetiva de que:

> “Claude parece melhor nisso, mas Codex parece melhor naquilo.”

Quer substituir percepção por evidência.

---

## Persona C — OSS / Local AI Power User

Experimenta:

* OpenRouter;
* Z.AI;
* MiniMax;
* modelos locais;
* novos harnesses;
* OpenCode;
* Ollama;
* LM Studio.

Quer saber:

> “quanto eu realmente perco em qualidade usando esse modelo 8× mais barato?”

---

# 8. Não usuários da V1

AgentFit V1 não será otimizado para:

* consumidor não técnico;
* repositories sem testes;
* GitLab;
* Bitbucket;
* Windows nativo;
* mobile builds pesados;
* visual-only evals;
* cross-repository tasks;
* remote agents;
* browser coding environments;
* fully managed enterprise evaluation.

---

# 9. Proposta de valor

## Headline

> **Know which coding agent actually works for your codebase.**

## Subheadline

> Turn work your team already shipped into a private benchmark, then run the coding agents already configured on your machine.

---

# 10. O primeiro aha moment

Antes de executar qualquer modelo:

```bash
agentfit analyze
```

resultado:

```text
AgentFit analyzed your repository

Merged PRs                   1,842
Potential task candidates      187
Validated eval candidates       43

Workload

Bugfix                         28%
Feature                        24%
Refactor                       17%
Integration                    13%
Migration                       7%
Other                          11%

Suggested benchmark

Tasks                           24
Estimated coverage              87%
```

Nenhum token de inference foi consumido.

---

# 11. O segundo aha moment

```bash
agentfit run \
  codex \
  claude \
  opencode-glm
```

resultado:

```text
                       Solve       Time       Cost*
Codex                   82%        8m42       $0.71
Claude                  86%        9m18       $1.49
OpenCode + GLM          75%       11m03       $0.18

* when provider/harness exposes usage
```

E:

```text
Claude observed quality advantage:
+4 percentage points

95% CI:
-7pp → +14pp

No conclusive quality difference.

Recommended cost-effective target:
Codex
```

---

# 12. Core product architecture

```text
┌──────────────────────────────────────────┐
│                AgentFit                  │
│                                          │
│ Repository Intelligence                  │
│ Candidate Mining                         │
│ Historical Reconstruction                │
│ Task Validation                          │
│ Benchmark Sampling                       │
│                                          │
│ Local Execution Layer                    │
│   ├─ Workspace Manager                   │
│   ├─ Process Runner                      │
│   ├─ Harness Adapters                    │
│   └─ Usage Parsers                       │
│                                          │
│ Verification Engine                      │
│ Analysis / Statistics                    │
│ Reporting                                │
└──────────────────────────────────────────┘
                    │
        local subprocess execution
                    │
       ┌────────────┼─────────────┐
       ▼            ▼             ▼
    claude        codex       opencode
                                    │
                                   etc.
```

---

# 13. O que significa “execution local”

AgentFit não chama diretamente:

```text
Anthropic API
OpenAI API
Gemini API
OpenRouter API
Z.AI API
```

Ele chama:

```text
claude
codex
opencode
gemini
custom command
```

O harness decide como chegar ao modelo.

Portanto:

```text
AgentFit
  ↓
Harness
  ↓
Configured Provider
  ↓
Model
```

Isso é deliberado.

---

# 14. Por que isso é importante

O que queremos medir não é somente:

```text
GPT-X vs Claude-Y
```

Queremos medir:

```text
Codex + GPT-X
vs
Claude Code + Claude-Y
vs
OpenCode + GLM
```

Harness faz parte da performance.

Ele determina:

* context strategy;
* repository exploration;
* tools;
* tool prompting;
* compaction;
* patch strategy;
* test loops;
* instruction handling;
* token usage.

Portanto tentar abstraí-lo demais destruiria justamente o fenômeno que queremos medir.

---

# 15. Execution Target

A principal entidade de execução será:

```text
ExecutionTarget
```

Schema:

```yaml
targets:

  claude:
    harness: claude
    model: opus

  codex:
    harness: codex
    model: gpt-x

  opencode-glm:
    harness: opencode
    model: zhipuai/glm-x

  opencode-minimax:
    harness: opencode
    model: openrouter/minimax-x
```

Model pode ser omitido:

```yaml
claude-default:
  harness: claude
```

Nesse caso:

> utilizar configuração default do harness.

---

# 16. Provider

AgentFit não possuirá uma abstraction obrigatória de provider.

Campo opcional:

```yaml
provider: openrouter
```

serve principalmente para:

* metadata;
* reporting;
* grouping;
* pricing override.

O controle real do provider continua pertencendo ao harness.

Exemplo OpenCode:

```yaml
model: openrouter/minimax-x
```

já é suficiente.

Para harnesses onde provider é definido em config local:

```yaml
provider: inherit
```

---

# 17. Filosofia de credenciais

AgentFit nunca deve pedir:

```text
Paste your API key:
```

Nem armazenar:

```text
OPENAI_API_KEY
ANTHROPIC_API_KEY
OPENROUTER_API_KEY
```

Se:

```bash
claude
```

funciona para o usuário,

AgentFit espera conseguir executar:

```bash
claude ...
```

Se:

```bash
opencode run ...
```

funciona,

AgentFit utiliza a configuração existente.

---

# 18. Harnesses oficiais da V1

Support level:

```text
Tier 1
Claude Code
Codex CLI
OpenCode
Gemini CLI
```

E:

```text
Tier 2
Generic Command Adapter
```

Com isso cobrimos simultaneamente:

* principais harnesses;
* vários providers;
* modelos cloud;
* modelos locais;
* futuros harnesses.

---

# 19. Harness Adapter Contract

Interface conceitual:

```python
class HarnessAdapter:

    def detect() -> HarnessDetection:
        ...

    def version() -> str:
        ...

    def validate_target(target) -> ValidationResult:
        ...

    def build_command(
        target,
        prompt,
        workspace
    ) -> CommandSpec:
        ...

    def parse_output(
        stdout,
        stderr
    ) -> HarnessResult:
        ...

    def cleanup(process):
        ...
```

O adapter não executa a task.

Ele apenas traduz:

```text
ExecutionTarget
+
Task
```

para:

```text
local command
```

---

# 20. Common CommandSpec

```python
CommandSpec:
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    stdin: str | None

    timeout_seconds: int

    output_mode:
        text
        json
        jsonl
```

Nenhum `shell=True` para adapters oficiais.

Isso reduz:

* quoting bugs;
* command injection;
* platform inconsistencies.

---

# 21. Claude Code Adapter

AgentFit utilizará o modo não interativo do Claude Code.

Claude Code oferece modo `-p/--print`, formatos estruturados e seleção explícita de modelo, permitindo sua utilização via subprocess para automação.

Conceitualmente:

```text
claude
  -p
  <prompt>
  --model <model>
  --output-format <structured>
```

Argumentos específicos de permission/autonomy serão controlados pelo adapter + configuração do target.

Exemplo:

```yaml
claude-opus:
  harness: claude
  model: opus

  args:
    - ...
```

AgentFit não deve assumir que todos os usuários desejam o mesmo permission mode.

---

# 22. Codex Adapter

O Codex fornece `codex exec` especificamente para execução não interativa/automação.

Conceitualmente:

```text
codex exec
  --json
  --model <model>
  <prompt>
```

Configuração adicional existente do usuário permanece válida.

Target:

```yaml
codex-high:
  harness: codex
  model: gpt-x

  args:
    - ...
```

---

# 23. OpenCode Adapter

OpenCode expõe:

```text
opencode run
```

em modo não interativo, permite selecionar modelos como `provider/model` e oferece output estruturado.

Isso o torna particularmente útil para AgentFit.

Exemplo:

```yaml
glm:
  harness: opencode
  model: zai/glm-x

minimax:
  harness: opencode
  model: openrouter/minimax-x

local-qwen:
  harness: opencode
  model: ollama/qwen-x
```

O mesmo harness permite comparar providers diferentes sem nova integração AgentFit.

---

# 24. Gemini CLI Adapter

Gemini CLI possui execução headless, escolha de modelo e JSON contendo usage/tool statistics.

Target:

```yaml
gemini:
  harness: gemini
  model: gemini-x
```

---

# 25. Generic Command Adapter

A V1 deve oferecer um escape hatch extremamente simples.

Exemplo:

```yaml
my-agent:
  harness: command

  command:
    - my-agent
    - run
    - --model
    - my-model
    - --prompt-file
    - "{prompt_file}"

  output: text
```

Placeholders suportados:

```text
{workspace}
{prompt}
{prompt_file}
{task_id}
{target_id}
```

Isso permite executar praticamente qualquer harness local.

---

# 26. Custom Command e segurança

Generic Command é código local explicitamente configurado pelo usuário.

AgentFit deve:

* exibir o command antes da primeira execução;
* não usar shell interpolation;
* usar argv array;
* exigir `--trust-custom-command` ou configuração persistida;
* registrar command template no run manifest.

---

# 27. Native execution mode

A V1 será:

# Native Mode

Ou seja:

> medir o harness de forma semelhante a como o usuário normalmente o utiliza.

Isso significa preservar:

* harness configuration;
* global instructions;
* repository instructions;
* provider configuration;
* model configuration.

Isso é feature.

Não bug.

Estamos medindo:

```text
real execution stack
```

---

# 28. Normalized Mode

Fica fora da V1.

No futuro pode existir:

```text
agentfit run --normalized
```

onde tentamos padronizar:

* prompt;
* tools;
* system instructions;
* context;
* permissions.

Mas isso responde uma pergunta diferente:

> “qual modelo é melhor sob condições controladas?”

V1 responde:

> “qual stack funciona melhor para mim?”

---

# 29. Target fingerprint

Como Native Mode depende da configuração local, precisamos registrar seu fingerprint.

Para cada target:

```text
Harness:
opencode

Harness version:
x.y.z

Model:
zai/glm-x

Provider:
zai

Configuration fingerprint:
sha256:...

Repository instruction fingerprint:
sha256:...
```

Nunca salvar conteúdo de credential files.

---

# 30. Reproducibility contract

AgentFit registra:

```text
AgentFit version
OS
architecture
harness
harness version
model
provider metadata
target arguments
target config hash
repository instruction hashes
task version
benchmark version
timestamp
```

Isso não garante bit-level reproducibility.

Modelos cloud são não determinísticos.

Mas garante:

> descrição suficiente da configuração testada.

---

# 31. Repository instructions

A stack pode incluir automaticamente:

```text
AGENTS.md
CLAUDE.md
GEMINI.md
.opencode/*
etc.
```

Se determinado harness normalmente lê esse arquivo:

> ele deve continuar lendo.

AgentFit registra hashes dos instruction files conhecidos.

Isso é importante porque:

```text
Claude Code + CLAUDE.md
```

não é equivalente a:

```text
Codex + AGENTS.md
```

E esse efeito faz parte do benchmark Native.

---

# 32. Execution pipeline

Para cada:

```text
Task × ExecutionTarget
```

AgentFit executa:

```text
1. CREATE WORKSPACE
2. MATERIALIZE BASE
3. SYNTHETIC GIT INIT
4. PREPARE ENVIRONMENT
5. START HARNESS
6. WAIT
7. TERMINATE CHILDREN
8. CAPTURE PATCH
9. APPLY HIDDEN VERIFIER
10. RUN TASK VERIFIER
11. RUN REGRESSION VERIFIER
12. COLLECT METRICS
13. DESTROY WORKSPACE
```

Esse pipeline é o coração da execution layer.

---

# 33. Workspace isolation

Não usar:

```text
git worktree
```

para trials.

Um worktree continua conectado ao histórico Git original.

O agente poderia executar:

```bash
git log
git show <future-commit>
```

e descobrir a resposta.

Portanto:

```text
original repository
        │
        ▼
git archive BASE
        │
        ▼
temporary directory
```

---

# 34. Synthetic Git repository

Depois de extrair `BASE`:

```bash
git init
git add .
git commit -m "AgentFit benchmark base"
```

O agent recebe:

```text
single synthetic commit
```

Sem:

```text
past history
future history
remote
PR references
gold commit
```

Isso mantém ferramentas Git funcionais sem expor a solução.

---

# 35. Synthetic repository invariants

Dentro do trial:

```bash
git log
```

deve mostrar apenas:

```text
AgentFit benchmark base
```

`git remote -v`:

```text
<empty>
```

`git reflog`:

não pode revelar histórico original.

`git branch -a`:

não pode revelar branches originais.

---

# 36. Hidden verifier

O agente **não deve receber os testes que definem a resposta correta** quando esses testes foram adicionados pelo PR histórico.

Estrutura:

```text
Task Package

base.tar
instruction.md
gold.patch
verifier.patch
metadata.json
```

Durante execução:

```text
workspace
=
BASE
+
instruction
```

Somente depois que o harness termina:

```text
workspace
+
verifier.patch
```

O agente nunca vê o verifier.

---

# 37. Por que esconder o verifier

Se fornecermos:

```text
new test
```

ao agente, podemos transformar a tarefa de:

> compreender o problema

em:

> implementar exatamente o comportamento explicitado pelos assertions.

Queremos reproduzir melhor o contexto original disponível antes da solução.

---

# 38. Baseline tests

Os testes que já existiam em `BASE` permanecem acessíveis.

O agente pode executá-los normalmente.

Somente:

```text
tests introduzidos/alterados pelo GOLD
```

são mantidos fora do workspace até a fase de verification.

---

# 39. Process Runner

Componente:

```text
LocalProcessRunner
```

Responsabilidades:

```text
spawn subprocess
stream stdout
stream stderr
capture events
measure wall time
apply timeout
terminate process group
return exit status
```

Implementação Python:

```text
asyncio.create_subprocess_exec
```

ou equivalente.

---

# 40. Process groups

Harnesses podem iniciar:

* shell;
* language servers;
* MCP servers;
* test runners;
* subprocesses.

Portanto cada trial deve executar em process group próprio.

Após conclusão ou timeout:

```text
terminate group
      ↓
grace period
      ↓
kill remaining children
```

Objetivo:

não deixar processos órfãos entre trials.

---

# 41. Timeout

Config global:

```yaml
execution:
  timeout_minutes: 20
```

Override:

```yaml
targets:
  local-model:
    timeout_minutes: 40
```

Resultado:

```text
TIMEOUT
```

não:

```text
FAILED
```

Esses estados devem ser separados.

---

# 42. Exit code não define correctness

Um harness pode terminar:

```text
exit code = 0
```

e implementação estar errada.

Ou:

```text
exit code != 0
```

depois de já alterar corretamente o código.

Correctness é definido pelo verifier.

Portanto:

```text
Harness exit status
≠
Task result
```

---

# 43. Trial outcome

Estados:

```text
SOLVED
UNSOLVED
HARNESS_ERROR
TIMEOUT
SETUP_ERROR
VERIFIER_ERROR
```

`UNSOLVED` significa:

> harness terminou normalmente, mas solução não passou.

---

# 44. Environment

A V1 não utiliza container obrigatório.

O harness executa usando:

```text
host machine
+
temporary working directory
```

Isso maximiza:

* simplicidade;
* compatibilidade;
* uso de modelos locais;
* uso da configuração real;
* velocidade de onboarding.

---

# 45. Consequência da execução host-native

AgentFit V1 **não é uma sandbox de segurança**.

Um coding harness com permissão ampla potencialmente pode:

* acessar outros paths;
* acessar network;
* executar processos;
* acessar environment variables.

Isso precisa estar explicitamente documentado.

---

# 46. Security model da V1

AgentFit reduz leakage.

Não promete isolamento hostil.

Proteções P0:

```text
✓ synthetic Git repository
✓ no git remote
✓ gold outside workspace
✓ verifier outside workspace
✓ temporary GitHub config
✓ scrub GH_TOKEN
✓ scrub GITHUB_TOKEN
✓ scrub SSH_AUTH_SOCK quando possível
✓ disable Git credential prompts
✓ no original repository path in prompt
```

Não garantimos:

```text
✗ network sandbox
✗ filesystem sandbox
✗ malicious harness containment
```

---

# 47. GitHub credential sanitization

Trial environment deve sobrescrever:

```text
GH_CONFIG_DIR=<empty-temp-dir>
GIT_TERMINAL_PROMPT=0
```

E remover:

```text
GH_TOKEN
GITHUB_TOKEN
```

Quando compatível, remover também:

```text
SSH_AUTH_SOCK
```

Objetivo:

dificultar que um agente simplesmente execute:

```bash
gh pr view 483
```

e recupere a solução.

---

# 48. Provider credentials

Aqui existe uma tensão deliberada.

O harness precisa de suas credenciais de inference.

Portanto AgentFit não pode simplesmente limpar todo o environment.

Política V1:

```yaml
execution:
  environment: inherit
```

com denylist de credenciais relacionadas à fonte do benchmark.

Provider auth continua disponível.

---

# 49. Environment policies futuras

Possíveis modos:

```text
inherit
minimal
allowlist
container
```

Mas V1 começa com:

```text
inherit + benchmark credential scrubbing
```

por simplicidade.

---

# 50. Network leakage

Sem sandbox de rede, não podemos garantir que um agente não procure:

```text
public GitHub
search engine
documentation
```

Portanto Benchmark Health inclui:

```text
Network Isolation:
NONE
```

Isso não invalida automaticamente o benchmark.

Mas reduz seu leakage confidence.

---

# 51. Public repository warning

Repositories públicos recebem:

```text
⚠ PUBLIC REPOSITORY

This benchmark cannot guarantee that
the tested model or agent has never seen
the repository, issue or solution.

Results measure practical performance,
not contamination-free capability.
```

Repositories privados são o cenário metodologicamente mais forte.

---

# 52. Future local sandbox

Fora da V1.

Possíveis implementações futuras:

* Docker;
* Apple sandbox;
* bubblewrap;
* firejail;
* lightweight VM.

Importante:

> execution architecture não deve impedir adicionar isso depois.

Portanto:

```text
Workspace
+
Runner
```

devem ser abstrações separadas.

---

# 53. Usage collection

Cada harness adapter tenta produzir:

```python
UsageRecord:
    input_tokens
    cached_input_tokens
    output_tokens
    reasoning_tokens
    requests
    tool_calls
    reported_cost_usd
```

Todos os campos são opcionais.

---

# 54. Missing usage

Nunca inventar.

Se Codex fornece tokens mas não custo:

```text
tokens: 48,320
cost: unavailable
```

Se generic command fornece apenas exit code:

```text
tokens: unavailable
cost: unavailable
```

Quality continua mensurável.

---

# 55. Pricing overrides

Opcional:

```yaml
pricing:
  glm:
    input_per_million: 0.40
    cached_input_per_million: 0.10
    output_per_million: 1.20
```

Quando AgentFit calcular custo:

```text
cost_source:
USER_PROVIDED_PRICING
```

Quando harness informar custo:

```text
cost_source:
HARNESS_REPORTED
```

Nunca misturar sem identificação.

---

# 56. Subscription-based usage

Alguns coding agents podem ser utilizados via:

* assinatura;
* token plan;
* bundled credits.

Nesses casos custo marginal por trial pode ser impossível de medir.

Report:

```text
Cost:
N/A — subscription-backed target
```

O produto não deve converter arbitrariamente assinatura mensal em custo/task.

---

# 57. Concurrency

Default:

```yaml
execution:
  jobs: 1
```

Porque execução local concorre por:

* CPU;
* RAM;
* disk;
* rate limits;
* local model GPU;
* provider quota.

Usuário pode:

```bash
agentfit run --jobs 3
```

---

# 58. Fairness e concurrency

Para comparações de tempo:

```text
jobs = 1
```

deve ser recomendado.

Execução concorrente pode distorcer:

* wall clock;
* local model latency;
* package installs.

O report registra concurrency.

---

# 59. Trial workspace lifecycle

Por default:

```text
temporary
```

Depois do trial:

```text
deleted
```

Em debug:

```bash
agentfit run --keep-workspaces
```

Preserva:

```text
.agentfit/workspaces/<trial-id>/
```

---

# 60. Captura de solução

Quando o harness termina:

```bash
git status
git diff
```

AgentFit cria:

```text
agent.patch
```

Antes de aplicar verifier.

Também registra:

```text
changed files
LOC added
LOC removed
```

---

# 61. Agent commits

Se harness fizer:

```bash
git commit
```

AgentFit não pode depender somente de `git diff`.

Portanto solution capture compara:

```text
working tree final
```

com:

```text
synthetic BASE tree
```

independentemente de commits criados pelo agente.

---

# 62. Verification workspace

Para reduzir interferência:

idealmente verification acontece em uma cópia da árvore final.

```text
agent workspace
       │
       ▼
final tree snapshot
       │
       ▼
verification workspace
       │
       + verifier patch
       │
       ▼
tests
```

Assim verifier não altera o artifact original da execução.

---

# 63. Correctness

Definition:

```text
SOLVED =
hidden task verifier passes
AND
regression verifier passes
```

Nenhum LLM judge é necessário.

Nenhuma similaridade com GOLD é necessária.

---

# 64. Gold solution

GOLD serve para:

```text
oracle validation
```

antes de inserir a task no benchmark.

Não serve como expected patch.

Um agente pode encontrar solução diferente e ainda:

```text
SOLVED
```

---

# 65. Candidate discovery

A execution redesign não altera o princípio de mining.

Para cada merged PR:

coletar:

```text
base SHA
gold SHA
diff
issue
PR title/body
labels
test changes
implementation changes
timestamp
```

---

# 66. Workload Universe

Todos os PRs relevantes no período formam:

```text
Workload Universe
```

Para cada um estimar:

```text
task type
subsystem
complexity
language
date
```

O benchmark será uma amostra desse universo.

---

# 67. Task type

V1:

```text
bugfix
feature
refactor
integration
migration
performance
infrastructure
unknown
```

Classificação preferencialmente determinística:

1. labels;
2. conventional commit;
3. title patterns;
4. diff patterns.

LLM não é necessário.

---

# 68. Subsystem

Prioridade:

```text
CODEOWNERS
→ workspace/package
→ stable directory
→ unknown
```

---

# 69. Complexity

Relative repository complexity.

Sinais:

```text
implementation LOC
implementation files
packages touched
test LOC
```

Buckets:

```text
small
medium
large
```

Grandes tarefas podem ser excluídas automaticamente da V1.

---

# 70. Candidate hard filters

P0:

```text
merged PR
human-origin change
implementation change exists
test change exists
supported repository history
base reconstructable
gold reconstructable
instruction provenance exists
task size supported
environment supported
```

---

# 71. Instruction provenance

Prioridade:

```text
1. linked issue existing before implementation
2. original task metadata
3. PR problem statement
4. PR title
```

Nunca:

```text
gold diff
→
LLM reverse-engineered prompt
```

na V1.

---

# 72. Instruction confidence

```text
A — pre-existing issue/task
B — strong PR problem description
C — potentially solution-contaminated description
```

Default benchmark:

```text
A + B
```

C somente via opt-in.

---

# 73. Test separation

PR diff é dividido em:

```text
implementation patch
verifier patch
```

Verifier:

```text
test files
snapshots
configured fixtures
test assets
```

Se separação não for segura:

```text
REJECT
```

---

# 74. Environment detection

V1 suporta:

## Node

```text
npm
pnpm
yarn
```

## Python

```text
uv
pip
poetry
```

Config:

```yaml
project:
  install_command: pnpm install --frozen-lockfile
  build_command: pnpm build
  test_command: pnpm test --run
```

Auto-detection gera sugestão.

Usuário pode editar.

---

# 75. Setup command

Cada trial pode executar:

```text
install_command
```

antes do harness.

Problema:

isso pode ser caro.

V1 aceita o custo operacional em troca de simplicidade.

Otimizações entram depois.

---

# 76. Package caches

Os package managers continuam usando caches normais do usuário.

Portanto:

```text
pnpm cache
npm cache
uv cache
pip cache
```

reduzem bastante reinstalações.

AgentFit não implementará seu próprio dependency cache na V1.

---

# 77. Preflight baseline

Antes de validar uma historical task:

```text
BASE
+
dependencies
```

precisa passar no baseline configurado.

Se base já estiver quebrado:

```text
BASELINE_BROKEN
```

---

# 78. No-op validation

```text
BASE
+
hidden verifier
```

deve:

```text
FAIL
```

Se passar:

```text
NOOP_PASSES
```

task rejeitada.

---

# 79. Oracle validation

```text
BASE
+
GOLD implementation
+
hidden verifier
```

deve:

```text
PASS
```

Caso contrário:

```text
GOLD_FAILS
```

---

# 80. Regression validation

GOLD também precisa passar:

```text
regression command
```

Se falhar:

```text
GOLD_REGRESSION
```

task rejeitada.

---

# 81. Determinism

Oracle verifier executado:

```text
3×
```

Precisa retornar:

```text
PASS
PASS
PASS
```

Caso contrário:

```text
FLAKY_VERIFIER
```

---

# 82. Task status

```text
DISCOVERED
FILTERED
PREPARING
VALIDATING
VALID
REJECTED
```

Stable rejection codes:

```text
NO_TEST_CHANGE
NO_INSTRUCTION
TASK_TOO_SMALL
TASK_TOO_LARGE
HISTORY_UNSUPPORTED
BASELINE_BROKEN
ENVIRONMENT_UNSUPPORTED
NOOP_PASSES
GOLD_FAILS
GOLD_REGRESSION
FLAKY_VERIFIER
LEAKAGE_HIGH
```

---

# 83. Benchmark sampling

Depois de gerar `VALID tasks`, AgentFit escolhe sample representativo.

Dimensões P0:

```text
task_type
subsystem
complexity
```

---

# 84. Sampling

Greedy stratified selection.

A cada task selecionada:

> escolher aquela que mais aproxima a distribuição do benchmark da distribuição do Workload Universe.

Objetivo:

```text
minimize:

w_type × distance(type)
+
w_subsystem × distance(subsystem)
+
w_complexity × distance(complexity)
```

---

# 85. Benchmark Coverage

Exemplo:

```text
Benchmark Coverage

Task type                 94
Subsystem                 84
Complexity                91

Overall representativeness
89
```

---

# 86. Benchmark Health

Componentes:

```text
Representativeness       40%
Validation confidence    25%
Leakage resistance       15%
Recency                  10%
Diversity                10%
```

Health não substitui hard gates.

---

# 87. Leakage score no Local Mode

Como não há network sandbox:

máximo possível do componente Leakage pode ser reduzido.

Exemplo:

```text
History isolation          ✓
Gold isolation             ✓
Verifier isolation         ✓
GitHub credentials         ✓
Network isolation          ✗

Leakage Resistance:
78/100
```

Isso é preferível a fingir segurança inexistente.

---

# 88. Benchmark build

```bash
agentfit benchmark build
```

Output:

```text
Valid candidates             43
Requested benchmark size     24

Sampling...

Benchmark
af_b_20260831_c73f

Tasks                        24

Health                       86

Representativeness           90
Validation                   97
Leakage                      78
Recency                      88
Diversity                    82

Warnings

⚠ No network isolation
⚠ Migrations underrepresented
```

---

# 89. Benchmark versioning

Benchmarks são imutáveis.

ID depende de:

```text
task versions
sampling configuration
workload window
AgentFit methodology version
```

Rebuild:

```text
new benchmark
```

Nunca modificar silenciosamente benchmark existente.

---

# 90. CLI

Golden path:

```bash
agentfit doctor
agentfit init
agentfit analyze
agentfit candidates
agentfit benchmark build
agentfit targets
agentfit run
agentfit report
```

---

# 91. `agentfit doctor`

Verifica:

```text
Git
GitHub CLI
Python
Node
package manager
test framework

Harnesses:
Claude Code
Codex
OpenCode
Gemini CLI
```

Exemplo:

```text
AgentFit Doctor

Repository
✓ Git
✓ GitHub remote
✓ GitHub CLI

Project
✓ TypeScript
✓ pnpm
✓ Vitest

Harnesses

✓ claude       2.x
✓ codex        x.x
✓ opencode     x.x
✗ gemini       not installed

3 execution harnesses available.
```

---

# 92. Doctor não deve gastar tokens

`doctor` nunca deve rodar uma inferência somente para validar autenticação.

Detecta:

```text
binary
version
known configuration
```

Se auth só puder ser validada fazendo request:

```text
Auth: unverified
```

---

# 93. `agentfit targets`

```bash
agentfit targets list
```

Output:

```text
TARGET              HARNESS      MODEL              PROVIDER
claude              claude       opus               inherited
codex               codex        gpt-x              inherited
glm                  opencode     zai/glm-x          zai
minimax              opencode     openrouter/mm-x    openrouter
```

---

# 94. Target validation

```bash
agentfit targets validate glm
```

Valida somente configuração estrutural.

Quando harness permite descobrir modelos localmente, adapter pode confirmar existência.

Mas nenhuma inference é necessária.

---

# 95. Configuration

`agentfit.yml`:

```yaml
version: 1

repository:
  provider: github
  lookback_days: 180

project:
  install_command: pnpm install --frozen-lockfile
  build_command: pnpm build
  regression_command: pnpm test --run

task_mining:
  require_test_change: true

  min_implementation_loc: 20
  max_implementation_loc: 400
  max_implementation_files: 8

benchmark:
  size: 24

  dimensions:
    task_type: 0.30
    subsystem: 0.40
    complexity: 0.30

execution:
  jobs: 1
  timeout_minutes: 20
  keep_workspaces: false

targets:

  claude:
    harness: claude
    model: opus

  codex:
    harness: codex
    model: gpt-x

  glm:
    harness: opencode
    model: zai/glm-x

  minimax:
    harness: opencode
    model: openrouter/minimax-x
```

---

# 96. Running

```bash
agentfit run claude codex glm
```

ou:

```bash
agentfit run --all
```

Preview:

```text
Benchmark
af_b_20260831_c73f

Tasks
24

Targets
3

Trials
72

Execution
local

Concurrency
1

Timeout
20m / trial

Network isolation
none

Continue? [y/N]
```

---

# 97. Non-interactive

```bash
agentfit run claude codex --yes
```

Para scripts.

---

# 98. Progress

```text
Running benchmark

Task 07/24

claude
SOLVED
08:41

codex
RUNNING
04:12

glm
PENDING
```

Não precisa de TUI sofisticada.

Rich live output é suficiente.

---

# 99. Failure resilience

Cada trial é independente.

Se:

```text
trial 17 crashes
```

benchmark continua.

Run state persistido em SQLite.

Depois:

```bash
agentfit run --resume
```

continua pending/failed-infrastructure trials.

---

# 100. Trial manifest

Cada trial registra:

```json
{
  "trial_id": "...",
  "benchmark_id": "...",
  "task_id": "...",

  "target": {
    "id": "glm",
    "harness": "opencode",
    "harness_version": "...",
    "model": "zai/glm-x",
    "provider": "zai"
  },

  "execution": {
    "started_at": "...",
    "duration_ms": 82111,
    "exit_code": 0,
    "timeout": false
  },

  "verification": {
    "task": true,
    "regression": true
  },

  "usage": {
    "input_tokens": 48122,
    "output_tokens": 9277,
    "reported_cost_usd": null
  }
}
```

---

# 101. Metrics

## Correctness

```text
Solved
Pass@1
```

## Economics

quando disponível:

```text
total cost
cost/task
cost/verified solve
```

## Token efficiency

quando disponível:

```text
tokens/task
tokens/verified solve
```

## Speed

```text
p50
p90
```

## Behavior

quando disponível:

```text
tool calls
turns
files changed
LOC changed
```

---

# 102. Pass@1

V1 executa:

```text
1 trial
per
Task × Target
```

Então:

```text
Pass@1
```

Não prometemos medir stochastic reliability.

---

# 103. V1.5 — Multiple rollouts

Posteriormente:

```bash
agentfit run --rollouts 5
```

Permitirá:

```text
pass@1
pass@5
variance
reliability
```

Mas isso multiplica custo.

Não necessário para validar a tese inicial.

---

# 104. Statistics

Para pass rates:

```text
Wilson 95% CI
```

Para comparar targets nas mesmas tasks:

```text
paired bootstrap
```

com seed armazenada.

---

# 105. Não declarar falso vencedor

Exemplo:

```text
Claude
86%

Codex
82%

Difference
+4pp

95% CI:
-7pp to +14pp
```

Resultado:

```text
No conclusive quality difference.
```

Não:

```text
Claude wins.
```

---

# 106. Pareto frontier

Relatório:

```text
QUALITY
  ↑
  │                ● Claude
  │
  │          ● Codex
  │
  │   ● GLM
  │
  └────────────────────────→ COST
```

Quando custo não estiver disponível:

usar gráfico:

```text
Quality × Time
```

ou report separado.

---

# 107. Cost-effective target

Policy default:

1. identificar best observed quality;
2. encontrar targets sem diferença estatística conclusiva;
3. dentro desse conjunto, escolher menor cost/verified solve;
4. se custo indisponível, não recomendar por custo.

---

# 108. Subscription targets

Exemplo:

```text
Claude subscription
Codex subscription
```

Se custo/task não for observável:

```text
Quality comparison:
available

Economic recommendation:
unavailable
```

Nada de fingir precisão.

---

# 109. Segment report

Breakdown:

```text
task type
subsystem
complexity
```

Exemplo:

| Segment  | Codex | Claude | GLM |
| -------- | ----: | -----: | --: |
| Bugfix   |   91% |    88% | 82% |
| Feature  |   76% |    90% | 68% |
| Payments |   82% |    94% | 71% |
| Frontend |   86% |    81% | 79% |

---

# 110. Small-n warning

Se:

```text
n < 5
```

segment é apenas descritivo.

Mostrar:

```text
LOW SAMPLE — do not route decisions from this segment.
```

---

# 111. Report

```bash
agentfit report
```

Exemplo:

```text
AGENTFIT

Repository
acme/payments

Benchmark
af_b_20260831_c73f

Tasks
24

Benchmark Health
86/100

                       Solve       Time       $/Solve

Claude                  86%        9m18        $1.49
Codex                   82%        8m42        $0.71
GLM                     75%       11m03        $0.18

Claude vs Codex

Observed difference:
+4pp

95% CI:
-7pp → +14pp

No conclusive quality difference.

Cost-effective recommendation:
Codex

Benchmark warnings:

⚠ No network isolation
⚠ Migration work underrepresented
```

---

# 112. Machine-readable report

```bash
agentfit report --format json
```

P0.

Isso permitirá posteriormente:

* CI;
* dashboards;
* routing;
* regression tracking;
* third-party tooling.

---

# 113. HTML

P1:

```bash
agentfit report --format html
```

Single static file.

Sem backend.

---

# 114. Storage

SQLite:

```text
.agentfit/state.db
```

Principais entidades:

```text
repositories
pull_requests
candidates
tasks
task_validations
benchmarks
benchmark_tasks

execution_targets
runs
trials
usage_records
```

---

# 115. Filesystem

```text
.agentfit/

  state.db

  cache/

  tasks/
    <task-id>/
      base.tar
      gold.patch
      verifier.patch
      instruction.md
      metadata.json

  benchmarks/
    <benchmark-id>/
      manifest.json

  runs/
    <run-id>/
      manifest.json
      trials/

  workspaces/
```

`.agentfit/` deve entrar no `.gitignore`.

---

# 116. Stack

Core:

```text
Python 3.12+
```

Package management:

```text
uv
```

CLI:

```text
Typer
```

Terminal:

```text
Rich
```

Schemas:

```text
Pydantic v2
```

Database:

```text
SQLite
```

Git:

```text
git subprocess
```

GitHub:

```text
gh
```

Process execution:

```text
asyncio subprocess
```

Tests:

```text
pytest
```

---

# 117. Estrutura de módulos

```text
agentfit/
│
├── cli/
│
├── repository/
│   ├── git.py
│   ├── github.py
│   └── workload.py
│
├── mining/
│   ├── candidates.py
│   ├── classification.py
│   ├── complexity.py
│   └── subsystem.py
│
├── tasks/
│   ├── reconstruction.py
│   ├── instruction.py
│   ├── verifier.py
│   ├── package.py
│   └── leakage.py
│
├── validation/
│   ├── baseline.py
│   ├── noop.py
│   ├── oracle.py
│   ├── regression.py
│   └── determinism.py
│
├── benchmark/
│   ├── sampling.py
│   ├── coverage.py
│   ├── health.py
│   └── manifest.py
│
├── execution/
│   ├── workspace.py
│   ├── runner.py
│   ├── process.py
│   ├── environment.py
│   ├── usage.py
│   │
│   └── adapters/
│       ├── base.py
│       ├── claude.py
│       ├── codex.py
│       ├── opencode.py
│       ├── gemini.py
│       └── command.py
│
├── verification/
│   ├── verifier.py
│   └── result.py
│
├── analysis/
│   ├── metrics.py
│   ├── stats.py
│   ├── pareto.py
│   └── recommendation.py
│
├── reporting/
│   ├── terminal.py
│   ├── json.py
│   └── html.py
│
└── storage/
```

---

# 118. Adapter development ergonomics

Adicionar um novo harness deve exigir aproximadamente:

```text
detect binary
+
construct argv
+
parse usage/output
```

Nada mais.

Ideal:

```python
class FooAdapter(HarnessAdapter):

    binary = "foo"

    def build_command(...):
        return [...]

    def parse_output(...):
        return ...
```

Target: adapter simples em poucas centenas de linhas, não milhares.

---

# 119. Adapter capabilities

Cada adapter declara:

```python
HarnessCapabilities:
    model_override: bool
    structured_output: bool
    token_usage: bool
    cost_usage: bool
    auto_approval: bool
    custom_provider: bool
```

Isso evita assumir paridade falsa.

---

# 120. Capability report

```bash
agentfit doctor --harnesses
```

Exemplo:

```text
             MODEL   JSON   TOKENS   COST   PROVIDER

Claude         ✓       ✓      ✓       ?       limited
Codex          ✓       ✓      ✓       ?       config
OpenCode       ✓       ✓      ✓       ✓*      broad
Gemini         ✓       ✓      ✓       ?       google

* depending on provider
```

---

# 121. P0 Acceptance — Local Runner

Dada uma valid task e um executable local:

AgentFit deve:

* criar workspace isolado;
* materializar BASE;
* criar synthetic Git repo;
* executar harness no cwd correto;
* aplicar timeout;
* capturar stdout;
* capturar stderr;
* capturar exit code;
* terminar children;
* produzir final patch;
* aplicar hidden verifier;
* executar tests;
* produzir TrialResult;
* destruir workspace.

---

# 122. P0 Acceptance — Harness adapters

Pelo menos:

```text
Claude Code
Codex
OpenCode
Gemini CLI
```

devem executar end-to-end.

Generic Command também deve funcionar.

---

# 123. P0 Acceptance — Provider flexibility

AgentFit deve conseguir executar:

```text
dois models diferentes
no mesmo harness
```

e:

```text
dois providers diferentes
quando harness suporta provider selection localmente.
```

Particularmente:

```text
OpenCode provider/model
```

deve funcionar sem nova integração AgentFit.

---

# 124. P0 Acceptance — No credentials

Nenhum fluxo do AgentFit deve:

* pedir API key;
* salvar API key;
* mandar API key para database;
* serializar environment inteiro.

---

# 125. P0 Acceptance — Leakage

Agent workspace:

* não possui original Git history;
* não possui gold patch;
* não possui verifier patch;
* não possui Git remote;
* não recebe GitHub token por environment;
* utiliza GitHub config temporário vazio.

---

# 126. P0 Acceptance — Benchmark

Benchmark deve:

* usar somente VALID tasks;
* ter versionamento;
* medir representativeness;
* mostrar Health;
* mostrar leakage limitations;
* gerar 15–30 tasks por default quando disponíveis.

---

# 127. P0 Acceptance — Result

Depois de executar ≥2 targets:

AgentFit deve mostrar:

* pass rate;
* confidence;
* time;
* usage quando disponível;
* cost quando disponível;
* segment breakdown;
* methodology warnings;
* Pareto/recommendation quando possível.

---

# 128. Milestone 0 — CLI foundation

Construir:

```text
package
Typer app
config
SQLite
logging
doctor
```

Done:

```bash
agentfit doctor
```

---

# 129. Milestone 1 — Harness spike

Antes de repository mining completo, provar:

```text
AgentFit
→
temporary repository
→
Claude
→
edit file
→
capture patch
```

Depois repetir:

```text
Codex
OpenCode
Gemini
```

Esse vertical slice deve acontecer cedo.

---

# 130. Milestone 2 — Local Execution Engine

Construir:

```text
WorkspaceManager
SyntheticGit
LocalProcessRunner
Timeout
ProcessGroup
PatchCapture
TrialResult
```

Teste com tasks artificiais.

---

# 131. Milestone 3 — Generic Command

Criar custom adapter.

Isso serve simultaneamente como:

* escape hatch;
* test harness;
* proof de extensibilidade.

---

# 132. Milestone 4 — Repository Intelligence

Implementar:

```text
Git ingestion
GitHub ingestion
PRs
workload classification
subsystem
complexity
```

Done:

```bash
agentfit analyze
```

---

# 133. Milestone 5 — Candidate Mining

Implementar:

```text
base/gold reconstruction
instruction provenance
test detection
implementation/verifier split
candidate filters
```

---

# 134. Milestone 6 — Historical Validation

Implementar:

```text
baseline
no-op
oracle
regression
determinism
```

Essa é a etapa mais importante para credibilidade metodológica.

---

# 135. Milestone 7 — Benchmark Builder

Implementar:

```text
sampling
coverage
benchmark health
versioning
```

---

# 136. Milestone 8 — Full Matrix Execution

Agora conectar:

```text
Benchmark
×
ExecutionTargets
```

ao runner.

Done:

```bash
agentfit run claude codex glm
```

---

# 137. Milestone 9 — Decision Layer

Implementar:

```text
metrics
Wilson CI
paired bootstrap
cost/solve
Pareto
segment analysis
recommendation
```

---

# 138. Milestone 10 — OSS Beta

Publicar:

* README;
* installation;
* quickstart;
* methodology;
* security model;
* sample repository;
* custom adapter guide;
* troubleshooting;
* contributor guide.

---

# 139. Test strategy

## Unit

* classifications;
* patch splitting;
* sampling;
* stats;
* adapter command construction;
* adapter parsers.

## Integration

Cada official harness com tiny fixture.

## End-to-end

```text
historical PR
→
task
→
target execution
→
verification
→
report
```

## Adversarial

Agent tenta:

```text
git log
git reflog
git remote
gh pr view
```

e não encontra solução histórica.

---

# 140. Fixture repository

Criar:

```text
agentfit-fixtures
```

Com histórico Git intencional contendo:

```text
good bugfix
good feature
no test
weak test
gold failure
flaky test
broken base
solution leakage
oversized task
```

E tasks artificiais para cada adapter.

---

# 141. Execution fixture

Uma task simples:

```text
Bug:
sum_even incorrectly sums odd numbers.
```

BASE contém bug.

Hidden verifier testa comportamento.

Cada harness deve conseguir modificar o mesmo workspace.

Isso permite verificar runner sem depender do mining.

---

# 142. Principal risco da nova arquitetura

## Host execution

Prós:

* extremamente simples;
* funciona com subscriptions;
* funciona com local auth;
* funciona com modelos locais;
* pouca infraestrutura;
* onboarding rápido;
* reproduz uso real.

Contras:

* isolamento de segurança menor;
* network leakage possível;
* host differences;
* dependency contamination possível;
* comparação de tempo afetada pela máquina;
* configuração global do harness influencia resultado.

Minha avaliação:

> **é a escolha correta para V1.**

Porque todos esses contras podem ser medidos ou documentados.

Já a complexidade de uma execution platform completa atrasaria justamente o wedge do produto.

---

# 143. Segundo risco: adapter churn

Harness CLI muda.

Mitigação:

```text
adapter contract pequeno
version detection
integration tests
supported version ranges
generic command fallback
```

---

# 144. Terceiro risco: custo incompleto

Nem todos os harnesses/providers entregam:

```text
token + exact dollar cost
```

Mitigação:

quality funciona independentemente.

Nunca tornar economics requisito para benchmark.

---

# 145. Quarto risco: Native Mode não é perfeitamente controlado

Claude e Codex podem receber instructions distintas.

Esse é um risco somente se a pergunta for:

> “qual modelo puro é melhor?”

Não se a pergunta for:

> “qual configuração eu deveria efetivamente utilizar?”

A V1 deliberadamente responde a segunda.

---

# 146. Product positioning

Evitar:

> “SWE-bench for your repo.”

Mais preciso:

# **Benchmark the coding agents you actually use on the code you actually work on.**

Ou:

> **Your repo. Your agents. Your benchmark.**

---

# 147. README aha moment

```text
$ agentfit init

1,842 PRs analyzed
43 high-confidence eval candidates

$ agentfit benchmark build

24 representative tasks
Benchmark Health: 86/100

$ agentfit run claude codex glm

Running 72 local trials...

$ agentfit report

                       SOLVE    TIME     $/SOLVE

Claude                  86%     9m18      $1.49
Codex                   82%     8m42      $0.71
GLM                     75%    11m03      $0.18

Claude's observed +4pp over Codex
is not statistically conclusive.

Recommended default:
Codex
```

Isso comunica o produto inteiro em ~20 linhas.

---

# 148. V1.5 — Continuous benchmark

Depois de V1:

```text
new PRs
   ↓
new candidate tasks
   ↓
benchmark drift
```

AgentFit identifica:

```text
Your current benchmark no longer
represents your workload well.

Coverage:
87 → 74

Reason:
integration work increased significantly.
```

---

# 149. V1.5 — Target regression

```bash
agentfit compare codex-new --against codex-old
```

Resultado:

```text
Overall
+3pp

Payments
-11pp

Frontend
+8pp

Cost
-19%
```

---

# 150. V1.5 — Repeated rollouts

```bash
agentfit run --rollouts 3
```

Agora podemos medir:

```text
reliability
variance
cost per reliable solve
```

---

# 151. V2 — Container execution opcional

Não substituir Local Mode.

Adicionar:

```text
runner:
  local
  container
```

Usuário escolhe.

Local continua melhor para:

* subscriptions;
* local models;
* complex harness config.

Container melhora:

* reproducibility;
* isolation;
* network control.

---

# 152. V2 — Routing

Quando houver dados suficientes:

```text
task
  ↓
classify
  ↓
historical segment
  ↓
best target
```

Exemplo:

```text
Bugfix
→ Codex

Large integration
→ Claude

Simple refactor
→ GLM
```

Mas routing só deve existir depois de medição confiável.

---

# 153. Non-goals definitivos da V1

Não construir:

* agent loop;
* model gateway;
* provider gateway;
* API proxy;
* cloud runner;
* distributed scheduler;
* multi-tenant service;
* LLM judge;
* generated test engine;
* auto-router;
* IDE;
* mission control;
* model leaderboard global.

---

# 154. North Star

A principal métrica conceitual não é:

```text
number of agent runs
```

É:

# **Decisions backed by representative repository evidence**

No produto OSS inicial podemos aproximar isso por:

```text
repository
→
healthy benchmark
→
≥2 execution targets
→
completed comparison
```

---

# 155. Definition of Done — V1

AgentFit V1 está pronta quando um desenvolvedor desconhecido consegue:

```bash
cd my-repository

agentfit init
agentfit benchmark build

agentfit run \
  claude \
  codex \
  opencode-glm

agentfit report
```

usando somente:

* seu repository;
* GitHub;
* seus CLIs já instalados;
* suas configurações locais;
* seus providers existentes.

E recebe uma resposta metodologicamente defensável para:

> **“Qual dessas stacks de coding agent apresenta a melhor relação entre capacidade, custo e tempo no tipo de trabalho representado pelo meu próprio repositório?”**

Sem:

* configurar Harbor;
* levantar servidor;
* configurar backend;
* criar Docker image;
* cadastrar API keys no AgentFit;
* criar manualmente dezenas de evals;
* confiar em LLM-as-a-judge.

---

# 156. A arquitetura que queremos preservar

A fronteira precisa permanecer muito clara:

```text
              AgentFit

  Owns                         Does not own

  history mining              inference
  task generation             model APIs
  benchmark quality           agent loop
  workspace creation          tool implementations
  process execution           provider auth
  verification                provider routing
  measurement                 coding behavior
  statistics
```

Isso é o que mantém o produto pequeno.

---

# 157. A tese final

A primeira versão não precisa ser uma infraestrutura universal de AI evaluation.

Ela precisa executar excepcionalmente bem uma única transformação:

```text
MY ENGINEERING HISTORY

        ↓

TRUSTWORTHY REPRESENTATIVE TASKS

        ↓

MY LOCALLY CONFIGURED CODING AGENTS

        ↓

VERIFIED RESULTS

        ↓

A DECISION
```

O moat potencial permanece em:

```text
engineering history
→
trustworthy representative evals
```

A execution layer existe somente para fechar o loop end-to-end.

Ela deve ser simples o suficiente para que, se amanhã surgir um novo harness relevante, suportá-lo signifique:

```text
build argv
+
parse output
```

e não:

> integrar um novo runtime inteiro.

Esse é o princípio arquitetural que deve proteger a V1 contra overengineering.
