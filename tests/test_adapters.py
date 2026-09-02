"""Adapter tests: exact argv construction, defensive usage parsing, placeholder
substitution for the generic command adapter, the registry, and cost resolution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repobench.config import PricingRule
from repobench.core.errors import UsageError
from repobench.core.types import ExecutionTarget, OutputMode, UsageRecord
from repobench.execution.adapters.base import HarnessResult
from repobench.execution.adapters.claude import ClaudeAdapter
from repobench.execution.adapters.codex import CodexAdapter
from repobench.execution.adapters.command import ALLOWED_PLACEHOLDERS, CommandAdapter
from repobench.execution.adapters.gemini import GeminiAdapter
from repobench.execution.adapters.opencode import OpenCodeAdapter
from repobench.execution.adapters.registry import KNOWN_HARNESSES, all_adapters, get_adapter
from repobench.execution.usage import resolve_cost, total_tokens

PROMPT = "Fix the bug in calculator.py"
WORKSPACE = Path("/tmp/does-not-exist-ws")


# --------------------------------------------------------------------- claude


def test_claude_argv_basic() -> None:
    spec = ClaudeAdapter().build_command(
        ExecutionTarget(harness="claude"), PROMPT, WORKSPACE, timeout_seconds=77
    )
    assert spec.argv == ["claude", "-p", PROMPT, "--output-format", "json"]
    assert spec.cwd == WORKSPACE
    assert spec.env == {}
    assert spec.timeout_seconds == 77
    assert spec.output_mode == OutputMode.JSON


def test_claude_argv_model_and_extra_args() -> None:
    target = ExecutionTarget(
        harness="claude",
        model="opus",
        args=["--dangerously-skip-permissions", "--verbose"],
    )
    spec = ClaudeAdapter().build_command(target, PROMPT, WORKSPACE, timeout_seconds=10)
    assert spec.argv == [
        "claude",
        "-p",
        PROMPT,
        "--model",
        "opus",
        "--output-format",
        "json",
        "--dangerously-skip-permissions",
        "--verbose",
    ]


def test_claude_parse_output_usage() -> None:
    stdout = (
        "some stream noise\n"
        '{"type":"system","subtype":"init"}\n'
        '{"type":"result","usage":{"input_tokens":120,"cache_read_input_tokens":80,'
        '"output_tokens":45}}\n'
    )
    result = ClaudeAdapter().parse_output(stdout, "")
    assert result.usage == UsageRecord(
        input_tokens=120, cached_input_tokens=80, output_tokens=45
    )
    assert total_tokens(result.usage) == 165


@pytest.mark.parametrize("stdout", ["", "no json here\nat all", "{ not json", '{"usage": "junk"}'])
def test_claude_parse_output_garbage(stdout: str) -> None:
    result = ClaudeAdapter().parse_output(stdout, "stderr noise")
    assert result == HarnessResult()


def test_claude_parse_output_cost_and_counts() -> None:
    # issue #17: the result JSON's top level carries total_cost_usd, num_turns
    # and tool_use_count — all lifted when present.
    stdout = (
        '{"type":"result","total_cost_usd":0.42,"num_turns":7,"tool_use_count":3,'
        '"usage":{"input_tokens":120,"cache_read_input_tokens":80,"output_tokens":45}}\n'
    )
    result = ClaudeAdapter().parse_output(stdout, "")
    assert result.usage == UsageRecord(
        input_tokens=120,
        cached_input_tokens=80,
        output_tokens=45,
        requests=7,
        tool_calls=3,
        reported_cost_usd=0.42,
    )


def test_claude_parse_output_cost_without_usage_dict() -> None:
    result = ClaudeAdapter().parse_output('{"type":"result","total_cost_usd":1.5}\n', "")
    assert result.usage == UsageRecord(reported_cost_usd=1.5)


def test_claude_parse_output_never_coerces_malformed_extras() -> None:
    # Wrong-typed cost/turns/tool fields are ignored, never coerced (PRD §54).
    stdout = (
        '{"type":"result","total_cost_usd":"0.42","num_turns":true,'
        '"tool_use_count":null,"usage":{"input_tokens":10,"output_tokens":5}}\n'
    )
    result = ClaudeAdapter().parse_output(stdout, "")
    assert result.usage == UsageRecord(input_tokens=10, output_tokens=5)


def test_claude_validate_target() -> None:
    # A model-less official target yields a warning, never an error (PRD §94).
    checked = ClaudeAdapter().validate_target(ExecutionTarget(harness="claude"))
    assert checked.valid
    assert checked.errors == []
    assert any("default model" in w for w in checked.warnings)
    explicit = ClaudeAdapter().validate_target(ExecutionTarget(harness="claude", model="sonnet"))
    assert explicit.valid and explicit.warnings == []


# ---------------------------------------------------------------------- codex


def test_codex_argv_basic() -> None:
    spec = CodexAdapter().build_command(
        ExecutionTarget(harness="codex"), PROMPT, WORKSPACE, timeout_seconds=88
    )
    assert spec.argv == ["codex", "exec", "--json", PROMPT]
    assert spec.cwd == WORKSPACE and spec.env == {} and spec.timeout_seconds == 88
    assert spec.output_mode == OutputMode.JSONL


def test_codex_argv_model_and_extra_args() -> None:
    target = ExecutionTarget(harness="codex", model="gpt-x", args=["--full-auto"])
    spec = CodexAdapter().build_command(target, PROMPT, WORKSPACE, timeout_seconds=10)
    assert spec.argv == ["codex", "exec", "--json", "--model", "gpt-x", "--full-auto", PROMPT]


def test_codex_parse_output_token_count_event() -> None:
    stdout = (
        "\n"
        '{"type":"item.started","item":{"type":"reasoning","text":"thinking"}}\n'
        "total bytes 1024\n"
        '{"type":"token_count","token_count":{"input":10,"cached_input":5,"output":3}}\n'
    )
    result = CodexAdapter().parse_output(stdout, "")
    assert result.usage == UsageRecord(input_tokens=10, cached_input_tokens=5, output_tokens=3)


def test_codex_parse_output_usage_dict_and_last_wins() -> None:
    stdout = (
        '{"type":"token_count","usage":{"input_tokens":1,"output_tokens":2}}\n'
        '{"type":"token_count","usage":{"input_tokens":100,"output_tokens":200}}\n'
    )
    result = CodexAdapter().parse_output(stdout, "")
    assert result.usage == UsageRecord(input_tokens=100, output_tokens=200)


@pytest.mark.parametrize("stdout", ["", "garbage \n lines {broken", '{"type":"other"}', "{}"])
def test_codex_parse_output_garbage(stdout: str) -> None:
    assert CodexAdapter().parse_output(stdout, "err").usage is None


def test_codex_validate_target() -> None:
    checked = CodexAdapter().validate_target(ExecutionTarget(harness="codex", model="gpt-x"))
    assert checked.valid
    assert checked.errors == [] and checked.warnings == []


# ------------------------------------------------------------------- opencode


def test_opencode_argv_basic() -> None:
    spec = OpenCodeAdapter().build_command(
        ExecutionTarget(harness="opencode"), PROMPT, WORKSPACE, timeout_seconds=66
    )
    assert spec.argv == ["opencode", "run", PROMPT]
    assert spec.cwd == WORKSPACE and spec.env == {} and spec.timeout_seconds == 66
    assert spec.output_mode == OutputMode.TEXT


def test_opencode_argv_model_and_extra_args() -> None:
    target = ExecutionTarget(harness="opencode", model="openrouter/minimax-x", args=["--share"])
    spec = OpenCodeAdapter().build_command(target, PROMPT, WORKSPACE, timeout_seconds=10)
    assert spec.argv == [
        "opencode",
        "run",
        "--model",
        "openrouter/minimax-x",
        "--share",
        PROMPT,
    ]


def test_opencode_parse_output_usage_blob_in_text() -> None:
    stdout = (
        "writing file...\n"
        "done. here is the report:\n"
        'some text {"session":"abc","usage":{"input_tokens":9,"output_tokens":4}} '
        "trailing prose\n"
    )
    result = OpenCodeAdapter().parse_output(stdout, "")
    assert result.usage == UsageRecord(input_tokens=9, output_tokens=4)


def test_opencode_parse_output_provider_style_keys() -> None:
    stdout = 'finished {"tokens":{"promptTokens":11,"completionTokens":2,"cached":7}}\n'
    result = OpenCodeAdapter().parse_output(stdout, "")
    assert result.usage == UsageRecord(
        input_tokens=11, output_tokens=2, cached_input_tokens=7
    )


@pytest.mark.parametrize("stdout", ["", "plain text only", '{"usage":{"bogus":1}}', "}}garbage{{"])
def test_opencode_parse_output_garbage(stdout: str) -> None:
    assert OpenCodeAdapter().parse_output(stdout, "err").usage is None


def test_opencode_parse_output_cost_and_counts() -> None:
    # issue #17: cost/request/tool-call keys inside the usage object are lifted.
    stdout = (
        'done {"usage":{"input_tokens":9,"output_tokens":4,"cost":0.0123,'
        '"requests":2,"tool_calls":1}}\n'
    )
    result = OpenCodeAdapter().parse_output(stdout, "")
    assert result.usage == UsageRecord(
        input_tokens=9,
        output_tokens=4,
        reported_cost_usd=0.0123,
        requests=2,
        tool_calls=1,
    )


def test_opencode_parse_output_cost_beside_tokens_container() -> None:
    # OpenCode message info shape: a `tokens` block next to a top-level `cost`.
    stdout = 'ok {"tokens":{"promptTokens":11,"completionTokens":2},"cost":0.5}\n'
    result = OpenCodeAdapter().parse_output(stdout, "")
    assert result.usage == UsageRecord(
        input_tokens=11, output_tokens=2, reported_cost_usd=0.5
    )


def test_opencode_parse_output_never_coerces_malformed_cost() -> None:
    # Wrong-typed cost/counts are ignored, never coerced (PRD §54)…
    assert OpenCodeAdapter().parse_output('{"cost":"free","requests":null}', "err").usage is None
    # …but a valid cost survives alongside malformed counts.
    result = OpenCodeAdapter().parse_output('{"cost":0.5,"num_turns":"seven"}', "err")
    assert result.usage == UsageRecord(reported_cost_usd=0.5)


def test_opencode_validate_target() -> None:
    checked = OpenCodeAdapter().validate_target(ExecutionTarget(harness="opencode"))
    assert checked.valid
    assert checked.errors == [] and checked.warnings == []


# --------------------------------------------------------------------- gemini


def test_gemini_argv_basic() -> None:
    spec = GeminiAdapter().build_command(
        ExecutionTarget(harness="gemini"), PROMPT, WORKSPACE, timeout_seconds=55
    )
    assert spec.argv == ["gemini", "-o", "json", "-p", PROMPT]
    assert spec.cwd == WORKSPACE and spec.env == {} and spec.timeout_seconds == 55
    assert spec.output_mode == OutputMode.JSON


def test_gemini_argv_model_and_extra_args() -> None:
    target = ExecutionTarget(harness="gemini", model="gemini-x", args=["--yolo"])
    spec = GeminiAdapter().build_command(target, PROMPT, WORKSPACE, timeout_seconds=10)
    assert spec.argv == ["gemini", "--model", "gemini-x", "-o", "json", "--yolo", "-p", PROMPT]


def test_gemini_parse_output_usage_metadata() -> None:
    stdout = json.dumps(
        {
            "response": "fixed it",
            "usageMetadata": {
                "promptTokenCount": 7,
                "candidatesTokenCount": 3,
                "thoughtsTokenCount": 2,
            },
        }
    )
    result = GeminiAdapter().parse_output(stdout, "")
    assert result.usage == UsageRecord(
        input_tokens=7, output_tokens=3, reasoning_tokens=2
    )


@pytest.mark.parametrize("stdout", ["", "not json", '{"no_usage": true}', "[1, 2, 3]"])
def test_gemini_parse_output_garbage(stdout: str) -> None:
    assert GeminiAdapter().parse_output(stdout, "err").usage is None


def test_gemini_validate_target() -> None:
    checked = GeminiAdapter().validate_target(ExecutionTarget(harness="gemini"))
    assert checked.valid
    assert checked.errors == [] and checked.warnings == []


# -------------------------------------------------------------------- command


def test_command_placeholder_substitution(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = ExecutionTarget(
        id="my-agent",
        harness="command",
        command=[
            "my-agent",
            "run",
            "--workspace",
            "{workspace}",
            "--prompt-file",
            "{prompt_file}",
            "--task",
            "{task_id}",
            "--target",
            "{target_id}",
            "{prompt}",
        ],
    )
    spec = CommandAdapter().build_command(
        target, "please fix", repo, task_id="task_1", target_id="my-agent", timeout_seconds=9
    )
    prompt_file = repo.parent / "prompt.md"
    assert spec.argv == [
        "my-agent",
        "run",
        "--workspace",
        str(repo),
        "--prompt-file",
        str(prompt_file),
        "--task",
        "task_1",
        "--target",
        "my-agent",
        "please fix",
    ]
    # prompt file written OUTSIDE the repo (trial dir), with the full prompt text
    assert prompt_file.read_text() == "please fix"
    assert not (repo / "prompt.md").exists()
    assert spec.cwd == repo
    assert spec.env == {}
    assert spec.timeout_seconds == 9
    assert spec.output_mode == OutputMode.TEXT  # from target.output


def test_command_prompt_file_never_inside_repo(tmp_path: Path) -> None:
    trial = tmp_path / "trial_x"
    repo = trial / "repo"
    repo.mkdir(parents=True)
    target = ExecutionTarget(harness="command", command=["agent", "{prompt_file}"])
    CommandAdapter().build_command(target, "the prompt", repo, task_id="t", target_id="c")
    assert (trial / "prompt.md").read_text() == "the prompt"
    assert not (repo / "prompt.md").exists()


def test_command_output_mode_from_target() -> None:
    from repobench.core.types import OutputMode as OM

    target = ExecutionTarget(harness="command", command=["a"], output=OM.JSONL)
    spec = CommandAdapter().build_command(target, "p", Path("/tmp/w"))
    assert spec.output_mode == OM.JSONL


def test_command_unknown_placeholder_is_invalid() -> None:
    target = ExecutionTarget(harness="command", command=["agent", "--key", "{api_key}"])
    checked = CommandAdapter().validate_target(target)
    assert not checked.valid
    assert any("{api_key}" in e for e in checked.errors)


def test_command_empty_or_missing_command_is_invalid() -> None:
    for target in (
        ExecutionTarget(harness="command", command=[]),
        ExecutionTarget(harness="command", command=None),
    ):
        checked = CommandAdapter().validate_target(target)
        assert not checked.valid
        assert checked.errors


def test_command_all_placeholders_valid() -> None:
    target = ExecutionTarget(
        harness="command",
        command=["a", "{workspace}", "{prompt}", "{prompt_file}", "{task_id}", "{target_id}"],
    )
    checked = CommandAdapter().validate_target(target)
    assert checked.valid and not checked.errors
    assert ALLOWED_PLACEHOLDERS == {"workspace", "prompt", "prompt_file", "task_id", "target_id"}


def test_command_detect_reports_available() -> None:
    detection = CommandAdapter().detect()
    assert detection.installed


def test_command_parse_output_never_invents_usage() -> None:
    assert CommandAdapter().parse_output('{"usage":{"input_tokens":5}}', "").usage is None


# ------------------------------------------------------------------- registry


# Fixture stdout per harness used to prove the capability table tells the truth
# (issue #17): each fixture carries the cost/token shapes that harness actually
# emits — plus a cost-looking key the harness does NOT report, to prove nothing
# is invented for the adapters that cannot produce a cost.
_CAPABILITY_FIXTURES: dict[str, str] = {
    "claude": (
        '{"type":"result","total_cost_usd":0.5,'
        '"usage":{"input_tokens":1,"output_tokens":1}}'
    ),
    "codex": '{"token_count":{"input":1,"output":1},"cost_usd":9.99}',
    "opencode": '{"tokens":{"input_tokens":1,"output_tokens":1},"cost":0.25}',
    "gemini": '{"usageMetadata":{"promptTokenCount":1,"candidatesTokenCount":1},"cost_usd":9.99}',
    "command": '{"usage":{"input_tokens":1,"output_tokens":1},"cost_usd":9.99}',
}


@pytest.mark.parametrize("harness", KNOWN_HARNESSES)
def test_capability_cost_flag_matches_parsed_output(harness: str) -> None:
    """cost_usage must equal whether parse_output can actually produce a
    reported_cost_usd (issue #17) — `doctor --harnesses` renders this claim."""
    adapter = get_adapter(harness)
    result = adapter.parse_output(_CAPABILITY_FIXTURES[harness], "")
    produces_cost = result.usage is not None and result.usage.reported_cost_usd is not None
    assert produces_cost == adapter.capabilities.cost_usage


@pytest.mark.parametrize("harness", KNOWN_HARNESSES)
def test_capability_token_flag_matches_parsed_output(harness: str) -> None:
    """token_usage must equal whether parse_output can actually produce token
    counts (issue #17)."""
    adapter = get_adapter(harness)
    result = adapter.parse_output(_CAPABILITY_FIXTURES[harness], "")
    usage = result.usage
    produces_tokens = usage is not None and (
        usage.input_tokens is not None or usage.output_tokens is not None
    )
    assert produces_tokens == adapter.capabilities.token_usage


def test_registry_known_harnesses() -> None:
    assert KNOWN_HARNESSES == ("claude", "codex", "opencode", "gemini", "command")
    assert set(all_adapters()) == set(KNOWN_HARNESSES)


def test_registry_get_adapter() -> None:
    assert isinstance(get_adapter("claude"), ClaudeAdapter)
    assert isinstance(get_adapter("codex"), CodexAdapter)
    assert isinstance(get_adapter("opencode"), OpenCodeAdapter)
    assert isinstance(get_adapter("gemini"), GeminiAdapter)
    assert isinstance(get_adapter("command"), CommandAdapter)


def test_registry_unknown_harness_raises_usage_error() -> None:
    with pytest.raises(UsageError) as excinfo:
        get_adapter("aider")
    message = str(excinfo.value)
    for harness in KNOWN_HARNESSES:
        assert harness in message


@pytest.mark.parametrize("harness", KNOWN_HARNESSES)
def test_validate_target_accepts_minimal_own_harness_target(harness: str) -> None:
    # The registry only ever pairs an adapter with its own harness name, so a
    # minimal well-formed target must always pass structural validation (PRD §94).
    target = ExecutionTarget(harness=harness)
    if harness == "command":
        target = target.model_copy(update={"command": ["agent", "{workspace}"]})
    checked = get_adapter(harness).validate_target(target)
    assert checked.valid and checked.errors == []


# ---------------------------------------------------------------------- usage


def test_resolve_cost_prefers_harness_reported() -> None:
    usage = UsageRecord(input_tokens=999, reported_cost_usd=0.5)
    pricing = PricingRule(input_per_million=1.0, output_per_million=2.0)
    assert resolve_cost(usage, pricing) == (0.5, "HARNESS_REPORTED")


def test_resolve_cost_computes_from_user_pricing() -> None:
    usage = UsageRecord(input_tokens=1_000_000, cached_input_tokens=1_000_000, output_tokens=500_000)
    pricing = PricingRule(
        input_per_million=0.40, cached_input_per_million=0.10, output_per_million=1.20
    )
    cost, source = resolve_cost(usage, pricing)
    assert source == "USER_PROVIDED_PRICING"
    assert cost == pytest.approx(0.40 + 0.10 + 0.60)


def test_resolve_cost_cached_tokens_free_when_rule_omitted() -> None:
    usage = UsageRecord(input_tokens=1_000_000, cached_input_tokens=1_000_000)
    pricing = PricingRule(input_per_million=0.40, output_per_million=1.20)
    cost, source = resolve_cost(usage, pricing)
    assert source == "USER_PROVIDED_PRICING"
    assert cost == pytest.approx(0.40)


def test_resolve_cost_unknown_cases() -> None:
    assert resolve_cost(None, None) == (None, None)
    assert resolve_cost(None, PricingRule(input_per_million=1, output_per_million=1)) == (
        None,
        None,
    )
    assert resolve_cost(UsageRecord(input_tokens=10), None) == (None, None)
    # no token data at all: never invent a 0.0 cost
    assert resolve_cost(UsageRecord(), PricingRule(input_per_million=1, output_per_million=1)) == (
        None,
        None,
    )


def test_total_tokens() -> None:
    assert total_tokens(None) is None
    assert total_tokens(UsageRecord(input_tokens=3)) is None
    assert total_tokens(UsageRecord(output_tokens=3)) is None
    assert total_tokens(UsageRecord(input_tokens=3, output_tokens=4)) == 7
