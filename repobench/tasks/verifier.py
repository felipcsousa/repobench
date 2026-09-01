"""Implementation / verifier patch split (PRD §73).

The PR diff is divided into per-file chunks; test-classified files (deterministic
`is_test_path` classification) become the hidden verifier patch, everything else
becomes the gold (implementation) patch.
"""

from __future__ import annotations

import pydantic

from repobench.core.testpaths import is_test_path


class SplitResult(pydantic.BaseModel):
    implementation_patch: str
    verifier_patch: str
    implementation_files: list[str]
    verifier_files: list[str]
    unsafe_reason: str | None = None


def _iter_chunks(diff_text: str):
    """Yield per-file diff chunks (each starts with a 'diff --git ' line), preserving order."""
    chunk: list[str] = []
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if chunk:
                yield "".join(chunk)
            chunk = [line]
        elif chunk:
            chunk.append(line)
    if chunk:
        yield "".join(chunk)


def _strip_path_operand(operand: str) -> str:
    """Clean one diff path operand: drop quotes, timestamps and the a/ b/ prefix."""
    operand = operand.strip()
    if operand.startswith('"') and operand.endswith('"') and len(operand) >= 2:
        operand = operand[1:-1]
    operand = operand.split("\t", 1)[0].strip()
    if len(operand) > 2 and operand[1] == "/" and operand[0] in "ab":
        return operand[2:]
    return operand


def _chunk_path(chunk: str) -> str | None:
    """Best-effort b-side path of a chunk; falls back to the a-side (deletions) then
    to the 'diff --git a/x b/y' header line."""
    git_header: str | None = None
    a_path: str | None = None
    b_path: str | None = None
    for line in chunk.splitlines():
        if line.startswith("diff --git ") and git_header is None:
            git_header = line
        elif line.startswith("--- ") and a_path is None:
            a_path = _strip_path_operand(line[4:])
        elif line.startswith("+++ ") and b_path is None:
            b_path = _strip_path_operand(line[4:])

    if b_path and b_path != "/dev/null":
        return b_path
    if a_path and a_path != "/dev/null":
        return a_path
    if git_header:
        parts = git_header.split()
        if len(parts) >= 4:
            return _strip_path_operand(parts[-1])
    return None


def split_diff(diff_text: str) -> SplitResult:
    """Split a unified diff into the implementation patch and the hidden verifier patch."""
    impl_chunks: list[str] = []
    verifier_chunks: list[str] = []
    impl_files: list[str] = []
    verifier_files: list[str] = []

    for chunk in _iter_chunks(diff_text):
        path = _chunk_path(chunk)
        if path is not None and is_test_path(path):
            verifier_chunks.append(chunk)
            verifier_files.append(path)
        else:
            impl_chunks.append(chunk)
            if path is not None:
                impl_files.append(path)

    implementation_patch = "".join(impl_chunks)
    verifier_patch = "".join(verifier_chunks)

    unsafe_reason: str | None = None
    if not implementation_patch.strip():
        unsafe_reason = "empty_implementation"
    elif not verifier_patch.strip():
        unsafe_reason = "empty_verifier"

    return SplitResult(
        implementation_patch=implementation_patch,
        verifier_patch=verifier_patch,
        implementation_files=impl_files,
        verifier_files=verifier_files,
        unsafe_reason=unsafe_reason,
    )
