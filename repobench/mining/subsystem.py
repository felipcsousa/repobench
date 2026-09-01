"""Subsystem inference (PRD §68).

Priority: CODEOWNERS prefix match → workspace/package directory → stable first path
segment → unknown. Deterministic: CODEOWNERS resolves on the first changed file,
package dirs scan until the first matching file.
"""

from __future__ import annotations

from pathlib import Path

# GitHub's documented locations, in precedence order (root wins).
CODEOWNERS_LOCATIONS: tuple[str, ...] = (
    "CODEOWNERS",
    ".github/CODEOWNERS",
    "docs/CODEOWNERS",
)


def _normalize(path: str) -> str:
    segments = [segment for segment in path.replace("\\", "/").split("/") if segment and segment != "."]
    return "/".join(segments)


def _codeowners_owner(path: str, codeowners: dict[str, str]) -> str | None:
    """Longest matching path prefix wins; "*" matches everything with length 0."""
    best_owner: str | None = None
    best_length = -1
    for prefix, owner in codeowners.items():
        normalized = prefix.strip("/")
        if normalized == "*":
            matched, length = True, 0
        else:
            matched = path == normalized or path.startswith(normalized + "/")
            length = len(normalized)
        if matched and length > best_length:
            best_owner, best_length = owner, length
    return best_owner


def _package_name(paths: list[str], package_dirs: dict[str, str]) -> str | None:
    """First changed file whose top segment(s) match a package dir, most specific first."""
    for path in paths:
        segments = path.split("/")
        for length in range(len(segments) - 1, 0, -1):
            prefix = "/".join(segments[:length])
            name = package_dirs.get(prefix)
            if name:
                return name
    return None


def infer_subsystem(
    changed_files: list[str],
    *,
    codeowners: dict[str, str] | None = None,
    package_dirs: dict[str, str] | None = None,
) -> str:
    if not changed_files:
        return "unknown"
    paths = [normalized for path in changed_files if (normalized := _normalize(path))]
    if not paths:
        return "unknown"

    if codeowners:
        owner = _codeowners_owner(paths[0], codeowners)
        if owner:
            return owner

    if package_dirs:
        name = _package_name(paths, package_dirs)
        if name:
            return name

    segments = paths[0].split("/")
    # Top-level files (no directory) belong to no subsystem — call them "root".
    return segments[0] if len(segments) >= 2 else "root"


def parse_codeowners(text: str) -> dict[str, str]:
    """Parse a CODEOWNERS file into {path prefix: first owner}.

    GitHub semantics kept where the dict allows: blank/# lines are skipped and a
    later rule for the same prefix wins. Only the FIRST owner is kept — the
    subsystem dimension needs one label per prefix, not a team roster.
    """
    owners: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        prefix, owner = parts[0], parts[1]
        owners[prefix.strip("/")] = owner.lstrip("@")
    return owners


def load_codeowners(root: Path) -> dict[str, str]:
    """CODEOWNERS map from the first existing GitHub location; {} when absent."""
    for relative in CODEOWNERS_LOCATIONS:
        path = Path(root) / relative
        if path.is_file():
            try:
                return parse_codeowners(path.read_text(errors="replace"))
            except OSError:
                return {}
    return {}


def detect_package_dirs(root: Path) -> dict[str, str]:
    """Heuristic {directory: package name} map for package-resolution (PRD §68).

    Recognizes plain Python packages (dir with __init__.py), the src/ layout,
    and JS monorepo packages/ trees. Bounded to two levels — no full walk.
    """
    root = Path(root)
    packages: dict[str, str] = {}
    top_level = [d for d in sorted(root.iterdir()) if d.is_dir() and not d.name.startswith(".")]
    for directory in top_level:
        if directory.name in {"node_modules", ".repobench", "venv", ".venv"}:
            continue
        if (directory / "__init__.py").is_file():
            packages[directory.name] = directory.name
        if directory.name == "src":
            for child in sorted(directory.iterdir()):
                if child.is_dir() and (child / "__init__.py").is_file():
                    packages[f"src/{child.name}"] = child.name
        if directory.name == "packages":
            for child in sorted(directory.iterdir()):
                if child.is_dir() and (child / "package.json").is_file():
                    packages[f"packages/{child.name}"] = child.name
    return packages
