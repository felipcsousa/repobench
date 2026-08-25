# Plano: Suporte a Go e Java no RepoBench

## Contexto

O RepoBench atualmente tem suporte P0 a Python e JavaScript/TypeScript. O PRD prevê expansão de linguagens no V2. Go e Java foram selecionados por:
- **Go**: agentes já funcionam bem, testes nativos (`go test`), ambiente reproduzível (`go.mod`), crescimento em backend/infra
- **Java**: enterprise massivo, Maven/Gradle padronizados, JUnit como padrão de facto

Parte da detecção já existe (Go tem detecção parcial em `detection.py`). O trabalho principal é completar os padrões de teste, comandos de ambiente, e integração no pipeline de verificação.

---

## Arquivos a modificar

| Arquivo | Mudanças |
|---|---|
| `repobench/repository/detection.py` | Test framework Go/Java, build commands Java, monorepo Gradle |
| `repobench/repository/workload.py` | Test file patterns Go/Java em `_is_test_file()` |
| `repobench/tasks/verifier.py` | `DEFAULT_TEST_GLOBS` Go/Java, `get_test_command_hint()` |
| `repobench/cli/init.py` | `_detect_languages()` Java, `_detect_pkg_manager()` Java, `_detect_test_command()` Java |
| `repobench/validation/environment.py` | `_detect_install_command()` Java, `_detect_test_command()` Java |
| `repobench/cli/doctor.py` | Checks para Go/Java toolchains |

## Código existente a reutilizar

- `detection.py:detect_languages()` — já tem `go.mod` e `.java` ext mapping
- `detection.py:detect_package_manager()` — já tem `go.mod` → `"go"`
- `detection.py:detect_build_commands()` — já tem Go commands, precisa Java
- `workload.py:_detect_languages_from_files()` — já tem `.go` e `.java`
- `environment.py:_detect_install_command()` — já tem Go, precisa Java
- `verifier.py:_matches_any_glob()` — infraestrutura de glob matching

---

## Steps

### Step 1: Go — completar test detection

**`repobench/repository/detection.py`**:
- Adicionar em `detect_test_framework()`: se `go.mod` existe, retornar `"go-test"`
- Adicionar em `detect_build_commands()`: blocos para `go.mod` com `go build ./...`, `go test ./...`, `go mod download`

**`repobench/repository/workload.py`** — `_is_test_file()`:
- Adicionar padrão Go: `name.endswith("_test.go")` e `"testdata"` in parts

**`repobench/tasks/verifier.py`**:
- Adicionar `"*_test.go"` e `"**/testdata/**"` a `DEFAULT_TEST_GLOBS`
- Atualizar `get_test_command_hint()` para detectar Go test files

**`repobench/cli/init.py`**:
- `_detect_test_command()`: adicionar detecção `go.mod` → `"go test ./..."`
- `_detect_pkg_manager()`: adicionar bloco Go com install/build/test

**`repobench/validation/environment.py`**:
- `_detect_test_command()`: adicionar `"go-test"` → `"go test ./..."`

### Step 2: Java — detecção completa

**`repobench/repository/detection.py`**:
- `detect_languages()`: já tem `pom.xml` e `build.gradle` ✅
- `detect_package_manager()`: adicionar `pom.xml` → `"maven"`, `build.gradle`/`build.gradle.kts` → `"gradle"`
- `detect_test_framework()`: adicionar detecção JUnit/TestNG via pom.xml ou build.gradle
- `detect_build_commands()`: adicionar blocos Maven e Gradle
- `detect_monorepo()`: adicionar detecção multi-module Maven/Gradle

**`repobench/repository/workload.py`** — `_is_test_file()`:
- Adicionar: `name.endswith("Test.java")` ou `name.endswith("Tests.java")`
- Adicionar: `"test"` directory + `.java` extension
- Adicionar: `name.endswith("IT.java")` (integration tests Maven)

**`repobench/tasks/verifier.py`**:
- Adicionar a `DEFAULT_TEST_GLOBS`: `"**/*Test.java"`, `"**/*Tests.java"`, `"**/*IT.java"`, `"**/src/test/**"`
- Adicionar a `DEFAULT_VERIFIER_ASSET_GLOBS`: `"**/src/test/resources/**"`
- Atualizar `get_test_command_hint()` para Java

**`repobench/cli/init.py`**:
- `_detect_languages()`: adicionar `.java` file counting
- `_detect_pkg_manager()`: adicionar Maven/Gradle com install/build/test
- `_detect_test_command()`: adicionar detecção JUnit

**`repobench/validation/environment.py`**:
- `_detect_install_command()`: adicionar Maven (`mvn dependency:resolve -q`) e Gradle (`gradle dependencies --quiet` ou `./gradlew dependencies`)
- `_detect_test_command()`: adicionar Maven/Gradle test

**`repobench/cli/doctor.py`**:
- Adicionar checks para `java -version` e `mvn --version` / `gradle --version`

### Step 3: Config defaults

**`repobench/models.py`**:
- Nenhuma mudança necessária — `languages` é `list[str]` livre

**`repobench/repository/detection.py`**:
- `detect_package_manager()`: ordem de prioridade precisa considerar monorepos com múltiplos ecosystems (ex: um repo com `go.mod` e `pom.xml`)

### Step 4: Verificação e testes

- [ ] `repobench init` em repo Go detecta `go` e `go test ./...`
- [ ] `repobench init` em repo Java detecta `java`, `maven`/`gradle` e comandos corretos
- [ ] `repobench analyze` classifica PRs Go/Java corretamente
- [ ] `repobench candidates` mostra candidatos Go/Java
- [ ] Testes unitários passam: `pytest tests/`
- [ ] Imports continuam limpos

---

## Notas de implementação

### Go
- Go test files seguem padrão `<name>_test.go` (sem diretório `tests/` separado)
- `testdata/` directory é ignored pelo Go toolchain automaticamente
- `go test -race ./...` é o comando recomendado com race detection
- Multi-module repos são comuns (cada `go.mod` é um módulo)

### Java
- Maven: `pom.xml`, testes em `src/test/java/`, comando `mvn test`
- Gradle: `build.gradle` ou `build.gradle.kts`, testes em `src/test/java/`, comando `./gradlew test`
- JUnit 4: `*Test.java`, JUnit 5: mesmo padrão
- Integration tests Maven: `*IT.java` (failsafe plugin)
- Multi-module Maven/Gradle: `detect_monorepo()` precisa detectar `<modules>` no pom.xml ou `includeBuild` no settings.gradle
- Java precisa de JDK instalado — doctor deve verificar
