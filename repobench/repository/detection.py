"""Project detection: languages, package managers, test frameworks, etc."""

from __future__ import annotations

import json
from pathlib import Path

from repobench.logging import get_logger

log = get_logger("repository.detection")


def detect_languages(project_root: Path) -> list[str]:
    """Detect programming languages in the project.

    Uses file extensions and configuration files for detection.
    """
    lang_indicators: dict[str, list[str]] = {
        "python": [
            "pyproject.toml",
            "setup.py",
            "setup.cfg",
            "requirements.txt",
            "poetry.lock",
            "uv.lock",
            "Pipfile",
        ],
        "typescript": ["tsconfig.json", "tsconfig.*.json"],
        "javascript": ["package.json"],
        "go": ["go.mod", "go.sum"],
        "rust": ["Cargo.toml", "Cargo.lock"],
        "ruby": ["Gemfile", "Gemfile.lock", "*.gemspec"],
        "java": ["pom.xml", "build.gradle", "build.gradle.kts"],
    }

    # Check config files first
    detected = set()
    for lang, indicators in lang_indicators.items():
        for pattern in indicators:
            matches = list(project_root.glob(pattern))
            if matches:
                detected.add(lang)
                break

    # Check source file extensions
    ext_lang_map = {
        ".py": "python",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".js": "javascript",
        ".jsx": "javascript",
        ".go": "go",
        ".rs": "rust",
        ".rb": "ruby",
        ".java": "java",
    }

    source_dirs = ["src", "lib", "app", "pkg"]
    for ext, lang in ext_lang_map.items():
        for src_dir in source_dirs:
            count = len(
                list((project_root / src_dir).glob(f"**/*{ext}"))
                if (project_root / src_dir).exists()
                else []
            )
            if count > 0:
                detected.add(lang)
        # Also check root-level source files
        count = len(list(project_root.glob(f"*{ext}")))
        if count > 2:  # threshold to avoid noise
            detected.add(lang)

    result = sorted(detected)
    log.info("Detected languages: %s", result or ["unknown"])
    return result if result else ["unknown"]


def detect_package_manager(project_root: Path) -> str | None:
    """Detect the primary package manager."""
    # Python
    if (project_root / "uv.lock").exists():
        return "uv"
    if (project_root / "poetry.lock").exists():
        return "poetry"
    if (project_root / "Pipfile.lock").exists():
        return "pipenv"
    if (project_root / "requirements.txt").exists():
        return "pip"
    if (project_root / "pyproject.toml").exists():
        # Check if it uses uv or pip
        try:
            content = (project_root / "pyproject.toml").read_text()
            if "uv" in content.lower() or "[tool.uv]" in content:
                return "uv"
        except Exception:
            pass
        return "pip"

    # JavaScript/Node
    if (project_root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (project_root / "yarn.lock").exists():
        return "yarn"
    if (project_root / "package-lock.json").exists():
        return "npm"
    if (project_root / "bun.lockb").exists():
        return "bun"

    # Rust
    if (project_root / "Cargo.toml").exists():
        return "cargo"
    # Go
    if (project_root / "go.mod").exists():
        return "go"
    # Ruby
    if (project_root / "Gemfile.lock").exists():
        return "bundler"

    # Java — Maven
    if (project_root / "pom.xml").exists():
        return "maven"
    # Java — Gradle
    if (project_root / "build.gradle").exists() or (project_root / "build.gradle.kts").exists():
        return "gradle"

    return None


def detect_test_framework(project_root: Path) -> str | None:
    """Detect the test framework in use."""
    # Python test frameworks
    if (project_root / "pytest.ini").exists():
        return "pytest"
    if (project_root / "setup.cfg").exists():
        try:
            content = (project_root / "setup.cfg").read_text()
            if "[tool:pytest]" in content or "[pytest]" in content:
                return "pytest"
        except Exception:
            pass

    # Check pyproject.toml for pytest
    pyproject = project_root / "pyproject.toml"
    if pyproject.exists():
        try:
            content = pyproject.read_text()
            if "[tool.pytest" in content:
                return "pytest"
            if "[tool.pytest.ini_options]" in content:
                return "pytest"
        except Exception:
            pass

    # JavaScript test frameworks
    pkg_json = project_root / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text())
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "vitest" in deps:
                return "vitest"
            if "jest" in deps:
                return "jest"
            if "@playwright/test" in deps:
                return "playwright"
            if "mocha" in deps:
                return "mocha"
        except Exception:
            pass

    # Check config files
    vitest_configs = list(project_root.glob("vitest.config.*"))
    if vitest_configs:
        return "vitest"
    jest_configs = list(project_root.glob("jest.config.*"))
    if jest_configs:
        return "jest"

    # Go — native test runner
    if (project_root / "go.mod").exists():
        return "go-test"

    # Java — detect JUnit via pom.xml or build.gradle
    if (project_root / "pom.xml").exists():
        try:
            content = (project_root / "pom.xml").read_text()
            if "junit" in content.lower() or "surefire" in content.lower():
                return "junit-maven"
            if "testng" in content.lower():
                return "testng-maven"
        except Exception:
            pass
        return "maven-test"  # fallback: Maven project likely has tests

    if (project_root / "build.gradle").exists() or (project_root / "build.gradle.kts").exists():
        gradle_file = (
            project_root / "build.gradle"
            if (project_root / "build.gradle").exists()
            else project_root / "build.gradle.kts"
        )
        try:
            content = gradle_file.read_text()
            if "junit" in content.lower():
                return "junit-gradle"
            if "testng" in content.lower():
                return "testng-gradle"
        except Exception:
            pass
        return "gradle-test"  # fallback

    return None


def detect_build_commands(project_root: Path) -> dict[str, str | None]:
    """Detect build, test, and install commands."""
    commands: dict[str, str | None] = {
        "install": None,
        "build": None,
        "test": None,
    }

    pkg_manager = detect_package_manager(project_root)

    if pkg_manager in ("uv", "pip", "poetry"):
        # Python projects
        pyproject = project_root / "pyproject.toml"
        if pyproject.exists():
            try:
                content = pyproject.read_text()
                if "[tool.poetry" in content:
                    commands["build"] = "poetry build"
                    commands["test"] = "poetry run pytest"
                    commands["install"] = "poetry install --no-interaction"
                elif "[tool.uv]" in content or "uv" in content.lower():
                    commands["test"] = "uv run pytest"
                    commands["install"] = "uv sync"
                else:
                    commands["test"] = "python -m pytest"
                    commands["install"] = "pip install -e '.[dev]'"
            except Exception:
                pass

        if pkg_manager == "uv":
            commands["install"] = commands["install"] or "uv sync"
        elif pkg_manager == "pip":
            if (project_root / "requirements.txt").exists():
                commands["install"] = "pip install -r requirements.txt"
        elif pkg_manager == "poetry":
            commands["install"] = commands["install"] or "poetry install --no-interaction"

    elif pkg_manager in ("npm", "pnpm", "yarn", "bun"):
        pkg_json = project_root / "package.json"
        if pkg_json.exists():
            try:
                pkg = json.loads(pkg_json.read_text())
                scripts = pkg.get("scripts", {})
                commands["build"] = scripts.get("build")
                commands["test"] = scripts.get("test")
                commands["install"] = {
                    "npm": "npm ci",
                    "pnpm": "pnpm install --frozen-lockfile",
                    "yarn": "yarn install --frozen-lockfile",
                    "bun": "bun install --frozen-lockfile",
                }.get(pkg_manager, f"{pkg_manager} install")
            except Exception:
                pass

    elif pkg_manager == "cargo":
        commands["build"] = "cargo build"
        commands["test"] = "cargo test"
        commands["install"] = "cargo fetch"

    elif pkg_manager == "go":
        commands["build"] = "go build ./..."
        commands["test"] = "go test ./..."
        commands["install"] = "go mod download"

    elif pkg_manager == "maven":
        commands["build"] = "mvn compile -q"
        commands["test"] = "mvn test -q"
        commands["install"] = "mvn dependency:resolve -q"

    elif pkg_manager == "gradle":
        # Check for Gradle wrapper
        if (project_root / "gradlew").exists():
            commands["build"] = "./gradlew compileJava --quiet"
            commands["test"] = "./gradlew test --quiet"
            commands["install"] = "./gradlew dependencies --quiet"
        else:
            commands["build"] = "gradle compileJava --quiet"
            commands["test"] = "gradle test --quiet"
            commands["install"] = "gradle dependencies --quiet"

    elif pkg_manager == "bundler":
        commands["build"] = None
        commands["test"] = "bundle exec rspec"
        commands["install"] = "bundle install"

    log.info(
        "Detected commands: install=%s, build=%s, test=%s",
        commands["install"],
        commands["build"],
        commands["test"],
    )
    return commands


def detect_monorepo(project_root: Path) -> dict[str, Any]:
    """Detect monorepo structure.

    Returns a dict with 'is_monorepo', 'workspaces', and 'package_manager'.
    """
    result: dict[str, Any] = {
        "is_monorepo": False,
        "workspaces": [],
        "type": None,
    }

    # Check for workspace patterns in package.json (JS monorepos)
    pkg_json = project_root / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text())
            workspaces = pkg.get("workspaces")
            if workspaces:
                result["is_monorepo"] = True
                result["type"] = "npm-workspaces"
                if isinstance(workspaces, list):
                    result["workspaces"] = workspaces
                elif isinstance(workspaces, dict):
                    result["workspaces"] = workspaces.get("packages", [])
        except Exception:
            pass

    # Check for pnpm-workspace.yaml
    if (project_root / "pnpm-workspace.yaml").exists():
        result["is_monorepo"] = True
        result["type"] = "pnpm-workspaces"

    # Check for lerna.json
    if (project_root / "lerna.json").exists():
        result["is_monorepo"] = True
        result["type"] = "lerna"

    # Check for nx.json
    if (project_root / "nx.json").exists():
        result["is_monorepo"] = True
        result["type"] = "nx"

    # Check for turbo.json
    if (project_root / "turbo.json").exists():
        result["is_monorepo"] = True
        result["type"] = "turborepo"

    # Check for Python workspace patterns
    pyproject = project_root / "pyproject.toml"
    if pyproject.exists():
        try:
            content = pyproject.read_text()
            if "[tool.hatch" in content or "hatch" in content.lower():
                # Hatch multi-project
                import re

                if re.search(r"\[tool\.hatch\.build\.targets\.wheel\]", content):
                    pass  # Not necessarily monorepo
        except Exception:
            pass

    # Check for Maven multi-module (pom.xml with <modules>)
    pom = project_root / "pom.xml"
    if pom.exists():
        try:
            content = pom.read_text()
            if "<modules>" in content:
                result["is_monorepo"] = True
                result["type"] = "maven-multi-module"
        except Exception:
            pass

    # Check for Gradle multi-project (settings.gradle with include)
    for settings_file in ["settings.gradle", "settings.gradle.kts"]:
        sf = project_root / settings_file
        if sf.exists():
            try:
                content = sf.read_text()
                if "include" in content:
                    result["is_monorepo"] = True
                    result["type"] = "gradle-multi-project"
            except Exception:
                pass

    # Generic: check for common monorepo directory patterns
    common_workspace_dirs = ["packages", "apps", "services", "libs", "modules"]
    workspace_dirs = []
    for d in common_workspace_dirs:
        dp = project_root / d
        if dp.is_dir():
            sub_items = [item for item in dp.iterdir() if item.is_dir()]
            if len(sub_items) >= 2:
                workspace_dirs.append(d)

    if workspace_dirs and not result["is_monorepo"]:
        result["is_monorepo"] = True
        result["type"] = "directory-convention"
        result["workspaces"] = workspace_dirs

    return result


def detect_codeowners(project_root: Path) -> dict[str, list[str]] | None:
    """Parse CODEOWNERS file if it exists."""
    codeowners_paths = [
        project_root / "CODEOWNERS",
        project_root / ".github" / "CODEOWNERS",
        project_root / "docs" / "CODEOWNERS",
    ]

    for path in codeowners_paths:
        if path.exists():
            return _parse_codeowners(path)

    return None


def _parse_codeowners(path: Path) -> dict[str, list[str]]:
    """Parse a CODEOWNERS file into a path -> owners mapping."""
    result: dict[str, list[str]] = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                pattern = parts[0]
                owners = [o.lstrip("@") for o in parts[1:]]
                result[pattern] = owners
    except Exception as e:
        log.warning("Failed to parse CODEOWNERS: %s", e)
    return result


from typing import Any
