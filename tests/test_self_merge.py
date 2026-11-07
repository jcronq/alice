"""Tests for ``alice_forge.self_merge`` — the code-review self-merge gate.

Issue #784 Part A. The policy decides whether an open-CI-green PR is
safe to auto-merge, after a ``/code-review`` agent pass. Tests cover
the decision tree (no findings → merge, findings + non-hot-path →
merge_after_fix, findings + hot-path → block, etc.), the config
loader's normalisation, the hot-path classifier, and the CLI exit
codes with mocked review runners.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from alice_forge import self_merge as sm


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def test_config_from_empty_dict_uses_defaults() -> None:
    cfg = sm.SelfMergeConfig.from_dict({})
    assert cfg.require_agent_review is True
    assert cfg.review_effort == "low"
    assert cfg.auto_apply_findings is True
    assert cfg.review_timeout_seconds == 300


def test_config_from_none_uses_defaults() -> None:
    cfg = sm.SelfMergeConfig.from_dict(None)
    assert cfg.require_agent_review is True
    assert cfg.review_effort == "low"


def test_config_overrides_apply() -> None:
    cfg = sm.SelfMergeConfig.from_dict(
        {
            "require_agent_review": False,
            "review_effort": "medium",
            "auto_apply_findings": False,
            "review_timeout_seconds": 60,
        }
    )
    assert cfg.require_agent_review is False
    assert cfg.review_effort == "medium"
    assert cfg.auto_apply_findings is False
    assert cfg.review_timeout_seconds == 60


def test_config_invalid_effort_clamps_to_low() -> None:
    cfg = sm.SelfMergeConfig.from_dict({"review_effort": "nuclear"})
    assert cfg.review_effort == "low"


def test_config_bogus_timeout_falls_back_to_default() -> None:
    assert sm.SelfMergeConfig.from_dict({"review_timeout_seconds": "nope"}).review_timeout_seconds == 300
    assert sm.SelfMergeConfig.from_dict({"review_timeout_seconds": -5}).review_timeout_seconds == 300
    assert sm.SelfMergeConfig.from_dict({"review_timeout_seconds": 0}).review_timeout_seconds == 300


# ---------------------------------------------------------------------------
# Hot-path classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "src/auth/login.py",
        "src/alice_speaking/auth.py",
        "migrations/0012_add_column.sql",
        "src/alice_forge/migration/0001_init.py",
        "alembic/versions/abc.py",
        "src/schema/user.py",
        "schema.sql",
        "db/schema.sql",
        "credentials.json",
        "src/secrets/kms.py",
        "src/secret/token.py",  # "secret" singular matches
    ],
)
def test_is_hot_path_detects_known_patterns(path: str) -> None:
    assert sm.is_hot_path([path]) is True


@pytest.mark.parametrize(
    "path",
    [
        "src/alice_speaking/transports/signal.py",
        "tests/test_something.py",
        "README.md",
        "src/status_site/server.py",
        "docs/designs/foo.md",
        "",
    ],
)
def test_is_hot_path_ignores_safe_paths(path: str) -> None:
    assert sm.is_hot_path([path]) is False


def test_is_hot_path_mixed_bag_returns_true_on_any_match() -> None:
    assert sm.is_hot_path(["README.md", "src/auth/jwt.py", "test.py"]) is True


def test_is_hot_path_empty_list_is_false() -> None:
    assert sm.is_hot_path([]) is False


# ---------------------------------------------------------------------------
# /code-review stdout parsing
# ---------------------------------------------------------------------------


def test_parse_findings_returns_empty_when_no_json() -> None:
    assert sm._parse_findings("No issues found. Checked for bugs.") == []


def test_parse_findings_extracts_report_findings_payload() -> None:
    stdout = """
Running /code-review...

ReportFindings input = {"findings": [
  {"file": "src/foo.py", "line": 12, "summary": "off-by-one", "short_summary": "off-by-one", "category": "correctness"}
]}

Done.
"""
    out = sm._parse_findings(stdout)
    assert len(out) == 1
    assert out[0].file == "src/foo.py"
    assert out[0].line == 12
    assert out[0].category == "correctness"


def test_parse_findings_picks_last_matching_payload() -> None:
    """When multiple JSON-looking blobs appear (e.g. a scratch pass + a
    final report), the parser returns the last one."""
    stdout = """
Scratch pad: {"findings": []}

And then the real one:
{"findings": [{"file": "a.py", "summary": "bug"}]}
"""
    out = sm._parse_findings(stdout)
    assert [f.file for f in out] == ["a.py"]


def test_parse_findings_filters_items_missing_required_fields() -> None:
    stdout = (
        '{"findings": ['
        '{"file": "ok.py", "summary": "found"}, '
        '{"summary": "no file"}, '
        '{"file": "no_summary.py"}, '
        '{"not_a": "finding"}'
        "]}"
    )
    out = sm._parse_findings(stdout)
    assert len(out) == 1
    assert out[0].file == "ok.py"


# ---------------------------------------------------------------------------
# run_code_review_agent — subprocess driver
# ---------------------------------------------------------------------------


class _FakeCompleted:
    """Minimal CompletedProcess-alike for the runner fixture."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_code_review_agent_returns_timed_out_on_timeout() -> None:
    def runner(_cmd: list[str], _timeout: int) -> _FakeCompleted:
        raise subprocess.TimeoutExpired(cmd=_cmd, timeout=_timeout)

    result = sm.run_code_review_agent(
        "jcronq/alice", 42, timeout_seconds=1, runner=runner  # type: ignore[arg-type]
    )
    assert result.timed_out is True
    assert result.findings == ()


def test_run_code_review_agent_nonzero_exit_becomes_error() -> None:
    def runner(_cmd: list[str], _timeout: int) -> _FakeCompleted:
        return _FakeCompleted(returncode=1, stderr="boom")

    result = sm.run_code_review_agent(
        "jcronq/alice", 42, runner=runner  # type: ignore[arg-type]
    )
    assert result.error and "boom" in result.error
    assert result.findings == ()
    assert result.timed_out is False


def test_run_code_review_agent_parses_findings_on_success() -> None:
    stdout = '{"findings": [{"file": "a.py", "summary": "x"}]}'

    def runner(_cmd: list[str], _timeout: int) -> _FakeCompleted:
        return _FakeCompleted(returncode=0, stdout=stdout)

    result = sm.run_code_review_agent(
        "jcronq/alice", 42, runner=runner  # type: ignore[arg-type]
    )
    assert result.findings == (sm.ReviewFinding(file="a.py", summary="x"),)
    assert result.error is None


def test_run_code_review_agent_unexpected_exception_becomes_error() -> None:
    def runner(_cmd: list[str], _timeout: int) -> _FakeCompleted:
        raise RuntimeError("socket died")

    result = sm.run_code_review_agent(
        "jcronq/alice", 42, runner=runner  # type: ignore[arg-type]
    )
    assert result.error and "socket died" in result.error


# ---------------------------------------------------------------------------
# decide_self_merge — the policy tree
# ---------------------------------------------------------------------------


def _fake_review(result: sm.ReviewResult):
    """Build a ``run_review`` stub that returns ``result``."""

    def inner(
        _repo: str, _pr: int, *, effort: str = "low", timeout_seconds: int = 300
    ) -> sm.ReviewResult:
        return result

    return inner


def test_decide_review_disabled_short_circuits_to_merge() -> None:
    cfg = sm.SelfMergeConfig(require_agent_review=False)
    decision = sm.decide_self_merge(
        repo="jcronq/alice",
        pr_number=1,
        pr_files=["src/auth/x.py"],  # hot-path; irrelevant since gate is off
        config=cfg,
        run_review=_fake_review(sm.ReviewResult()),
    )
    assert decision.action == "merge_review_disabled"
    assert decision.hot_path_hit is False


def test_decide_zero_findings_is_merge() -> None:
    cfg = sm.SelfMergeConfig()
    decision = sm.decide_self_merge(
        repo="jcronq/alice",
        pr_number=1,
        pr_files=["README.md"],
        config=cfg,
        run_review=_fake_review(sm.ReviewResult(findings=())),
    )
    assert decision.action == "merge"
    assert decision.findings == ()


def test_decide_timed_out_proceeds_with_merge() -> None:
    cfg = sm.SelfMergeConfig()
    decision = sm.decide_self_merge(
        repo="jcronq/alice",
        pr_number=1,
        pr_files=["README.md"],
        config=cfg,
        run_review=_fake_review(sm.ReviewResult(timed_out=True)),
    )
    assert decision.action == "merge"
    assert decision.timed_out is True


def test_decide_review_error_blocks() -> None:
    cfg = sm.SelfMergeConfig()
    decision = sm.decide_self_merge(
        repo="jcronq/alice",
        pr_number=1,
        pr_files=["README.md"],
        config=cfg,
        run_review=_fake_review(sm.ReviewResult(error="claude crashed")),
    )
    assert decision.action == "block"
    assert "claude crashed" in decision.reason


def test_decide_findings_on_cold_path_with_auto_apply_is_merge_after_fix() -> None:
    cfg = sm.SelfMergeConfig(auto_apply_findings=True)
    findings = (sm.ReviewFinding(file="src/status_site/server.py", summary="off-by-one"),)
    decision = sm.decide_self_merge(
        repo="jcronq/alice",
        pr_number=1,
        pr_files=["src/status_site/server.py"],
        config=cfg,
        run_review=_fake_review(sm.ReviewResult(findings=findings)),
    )
    assert decision.action == "merge_after_fix"
    assert decision.findings == findings
    assert decision.hot_path_hit is False


def test_decide_findings_on_hot_path_file_blocks() -> None:
    cfg = sm.SelfMergeConfig(auto_apply_findings=True)
    findings = (sm.ReviewFinding(file="src/auth/login.py", summary="token leak"),)
    decision = sm.decide_self_merge(
        repo="jcronq/alice",
        pr_number=1,
        pr_files=["src/auth/login.py"],
        config=cfg,
        run_review=_fake_review(sm.ReviewResult(findings=findings)),
    )
    assert decision.action == "block"
    assert decision.hot_path_hit is True


def test_decide_findings_with_pr_files_hot_path_blocks() -> None:
    """Even when the finding is on a non-hot-path file, a hot-path file
    in the PR's changed set triggers escalation. The policy is 'if the
    PR touches hot-path AND review found anything, block'."""
    cfg = sm.SelfMergeConfig(auto_apply_findings=True)
    findings = (sm.ReviewFinding(file="src/status_site/x.py", summary="nit"),)
    decision = sm.decide_self_merge(
        repo="jcronq/alice",
        pr_number=1,
        pr_files=["migrations/0042.sql"],  # hot-path, but review didn't flag it
        config=cfg,
        run_review=_fake_review(sm.ReviewResult(findings=findings)),
    )
    assert decision.action == "block"
    assert decision.hot_path_hit is True


def test_decide_findings_with_auto_apply_disabled_blocks() -> None:
    cfg = sm.SelfMergeConfig(auto_apply_findings=False)
    findings = (sm.ReviewFinding(file="README.md", summary="typo"),)
    decision = sm.decide_self_merge(
        repo="jcronq/alice",
        pr_number=1,
        pr_files=["README.md"],
        config=cfg,
        run_review=_fake_review(sm.ReviewResult(findings=findings)),
    )
    assert decision.action == "block"
    assert decision.hot_path_hit is False


def test_decide_uses_configured_effort_and_timeout() -> None:
    captured: dict[str, object] = {}

    def inner(
        _repo: str, _pr: int, *, effort: str = "low", timeout_seconds: int = 300
    ) -> sm.ReviewResult:
        captured["effort"] = effort
        captured["timeout"] = timeout_seconds
        return sm.ReviewResult()

    cfg = sm.SelfMergeConfig(review_effort="high", review_timeout_seconds=42)
    sm.decide_self_merge(
        repo="jcronq/alice",
        pr_number=1,
        pr_files=[],
        config=cfg,
        run_review=inner,
    )
    assert captured["effort"] == "high"
    assert captured["timeout"] == 42


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _patch_decide(monkeypatch: pytest.MonkeyPatch, decision: sm.MergeDecision) -> None:
    monkeypatch.setattr(sm, "decide_self_merge", lambda **_kw: decision)


def test_cli_merge_decision_exits_zero(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_decide(monkeypatch, sm.MergeDecision(action="merge", reason="clean"))
    rc = sm.main(["--repo", "jcronq/alice", "--pr", "1"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["action"] == "merge"


def test_cli_block_decision_exits_two(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_decide(
        monkeypatch,
        sm.MergeDecision(
            action="block",
            reason="hot path",
            findings=(sm.ReviewFinding(file="src/auth/x.py", summary="leak"),),
            hot_path_hit=True,
        ),
    )
    rc = sm.main(["--repo", "jcronq/alice", "--pr", "1"])
    assert rc == 2
    out = json.loads(capsys.readouterr().out.strip())
    assert out["action"] == "block"
    assert out["hot_path_hit"] is True
    assert out["findings"] == [
        {
            "file": "src/auth/x.py",
            "summary": "leak",
            "short_summary": "",
            "line": None,
            "category": "",
        }
    ]


def test_cli_merge_review_disabled_exits_zero(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_decide(
        monkeypatch,
        sm.MergeDecision(action="merge_review_disabled", reason="off"),
    )
    rc = sm.main(["--repo", "jcronq/alice", "--pr", "1", "--no-review"])
    assert rc == 0


def test_cli_unexpected_exception_exits_one(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_it(**_kw: object) -> sm.MergeDecision:
        raise RuntimeError("explode")

    monkeypatch.setattr(sm, "decide_self_merge", raise_it)
    rc = sm.main(["--repo", "jcronq/alice", "--pr", "1"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out.strip())
    assert out["action"] == "error"
    assert "explode" in out["reason"]
