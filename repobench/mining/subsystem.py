"""Subsystem inference (PRD §68).

Priority: CODEOWNERS prefix match → workspace/package directory → stable first path
segment → unknown. Deterministic: CODEOWNERS resolves on the first changed file,
package dirs scan until the first matching file.
"""

from __future__ import annotations


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
