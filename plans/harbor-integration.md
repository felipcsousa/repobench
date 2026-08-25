# Plano: Integração Harbor Completa (Mínimo)

## Contexto

O RepoBench tem pipeline completo de discovery→mining→validation→benchmark, mas a execução via Harbor é stub. O objetivo é implementar: export correto no formato Harbor + execução via subprocess + parse de result.json.

## Decisões do usuário

- **Ambiente**: Configurável via `repobench.yml` (`execution.environment: docker | local`)
- **Execução**: Subprocesso `harbor run` (sem SDK)
- **Concorrência**: Configurável via `execution.concurrency`
- **Escopo**: Mínimo — export + run + parse
- **Test script**: Auto-detect (pytest/go test/mvn test) + wrapper RepoBench verifier

## Arquivos a modificar

| Arquivo | Mudanças |
|---|---|
| `repobench/harbor/exporter.py` | Reescrever: task.toml real (schema Harbor), test.sh auto-detect, Dockerfile stub |
| `repobench/harbor/runner.py` | **Novo**: executar `harbor run` via subprocess, streaming output, parse result.json |
| `repobench/harbor/parser.py` | **Novo**: parse result.json do Harbor em trials RepoBench |
| `repobench/cli/run.py` | Integrar exporter + runner + parser (substituir stub atual) |
| `repobench/models.py` | Adicionar campos de resultado Harbor em Trial |
| `repobench/config.py` | Adicionar `HarborConfig` ao schema (ja existe `ExecutionConfig`) |

## Código existente a reutilizar

- `harbor/exporter.py:_build_task_toml()` — base para task.toml (precisa reformatar)
- `cli/run.py:run_benchmark()` — estrutura de loop trials (manter, integrar runner)
- `storage/database.py:insert_trial()` — ja aceita resultados
- `analysis/metrics.py:compute_config_metrics()` — ja calcula pass@1, CI, custo
- `utils.py:run_cmd_safe()` — subprocess helper
- `config.py:load_config()` — ja carrega ExecutionConfig

---

## Steps

### Step 1: Task.toml real (formato Harbor)

Reescrever `harbor/exporter.py` para gerar task.toml válido:

```toml
schema_version = "1.4"

[task]
name = "repobench/<benchmark_id>/<task_id>"
version = "1.0.0"
description = "<pr_title>"

[metadata]
pr_number = 483
candidate_id = "af_c_..."
subsystem = "payments"
complexity = "medium"
task_type = "bugfix"

[verifier]
timeout_sec = 120.0

[agent]
timeout_sec = 300.0

[environment]
network_mode = "no-network"  # ou "public" baseado em config
```

### Step 2: Test script auto-detect

Gerar `tests/test.sh` que:
1. Detecta linguagem do projeto (Go/Java/Python/JS)
2. Roda o comando de teste apropriado
3. Escreve reward.txt (1=pass, 0=fail)

```bash
#!/bin/bash
set -e

# Detect and run tests
if [ -f "go.mod" ]; then
    go test ./... 2>&1 | tee /tmp/test_output.txt
elif [ -f "pom.xml" ]; then
    mvn test -q 2>&1 | tee /tmp/test_output.txt
elif [ -f "build.gradle" ] || [ -f "build.gradle.kts" ]; then
    ./gradlew test --quiet 2>&1 | tee /tmp/test_output.txt
elif [ -f "package.json" ]; then
    npm test 2>&1 | tee /tmp/test_output.txt
elif [ -f "pyproject.toml" ] || [ -f "pytest.ini" ]; then
    python -m pytest 2>&1 | tee /tmp/test_output.txt
else
    echo "No test framework detected"
    echo 0 > /logs/verifier/reward.txt
    exit 0
fi

# Write reward
if [ $? -eq 0 ]; then
    echo 1 > /logs/verifier/reward.txt
else
    echo 0 > /logs/verifier/reward.txt
fi
```

### Step 3: Runner (harbor/runner.py)

Novo módulo que:
1. Verifica `harbor --version`
2. Para cada trial: monta comando `harbor run -p <task_dir> -a <agent> -m <model> --n-concurrent <N>`
3. Executa via subprocess com streaming
4. Captura stdout/stderr
5. Encontra result.json no job directory do Harbor
6. Retorna resultado estruturado

```python
def run_harbor_trial(
    task_dir: Path,
    agent: str,
    model: str,
    timeout: int = 600,
) -> HarborTrialResult:
    """Execute one trial via Harbor CLI."""
    cmd = ["harbor", "run", "-p", str(task_dir), "-a", agent, "-m", model]
    # ... subprocess execution, parse result.json
```

### Step 4: Parser (harbor/parser.py)

Módulo que lê `result.json` do Harbor e converte para modelo RepoBench:

```python
def parse_harbor_result(result_path: Path) -> HarborTrialResult:
    """Parse Harbor result.json into RepoBench trial data."""
    data = json.loads(result_path.read_text())
    return HarborTrialResult(
        solved=data.get("reward", 0) >= 1.0,
        duration_ms=data.get("duration_ms"),
        prompt_tokens=data.get("prompt_tokens"),
        completion_tokens=data.get("completion_tokens"),
        cost_usd=data.get("cost_usd"),
    )
```

### Step 5: Integrar em cli/run.py

Substituir o stub atual pelo fluxo real:

```python
# Para cada config + task:
for cfg in configs:
    for task in tasks:
        # 1. Export task para Harbor format
        task_dir = export_single_task(task, benchmark_dir)
        
        # 2. Run via Harbor
        result = run_harbor_trial(task_dir, cfg.agent, cfg.model)
        
        # 3. Parse result
        trial = build_trial_from_result(result, task, cfg)
        
        # 4. Store in DB
        db.insert_trial(trial)
```

### Step 6: Config de ambiente

Adicionar ao `repobench.yml`:

```yaml
execution:
  environment: docker  # ou "local"
  concurrency: 4
  harbor_timeout: 600
  network_mode: "no-network"  # ou "public"
```

Atualizar `ExecutionConfig` em models.py.

---

## Verificação

1. `repobench benchmark build` — gera benchmark com tasks
2. Verificar que `.repobench/benchmarks/<id>/harbor/task-001/` tem:
   - `task.toml` (schema_version = "1.4")
   - `instruction.md`
   - `tests/test.sh` (executável, auto-detect)
3. `harbor run -p .repobench/benchmarks/<id>/harbor/task-001 -a oracle -m test` — executa
4. `repobench report` — mostra resultados
