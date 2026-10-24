"""Unit tests for shared environment helpers and the routing CLI (no AWS)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.agents.routing_agent import cli
from backend.shared import config
from backend.shared.config import load_default_env, load_env_file, resolve_region
from backend.tools import deliveries, escalation


@pytest.fixture(autouse=True)
def clean_region_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AWS_REGION", "AWS_DEFAULT_REGION", "BEDROCK_REGION"):
        monkeypatch.delenv(name, raising=False)


def test_resolve_region_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_region(None, "BEDROCK_REGION") == "us-east-1"

    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    assert resolve_region(None, "BEDROCK_REGION") == "eu-west-1"
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    assert resolve_region(None, "BEDROCK_REGION") == "us-west-2"
    monkeypatch.setenv("BEDROCK_REGION", "us-east-2")
    assert resolve_region(None, "BEDROCK_REGION") == "us-east-2"
    assert resolve_region("ap-south-1", "BEDROCK_REGION") == "ap-south-1"


def test_load_env_file_sets_only_missing_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n\nNEW_KEY=\"quoted value\"\nexport OTHER=1\nKEEP=from-file\nbad line\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("NEW_KEY", raising=False)
    monkeypatch.delenv("OTHER", raising=False)
    monkeypatch.setenv("KEEP", "from-shell")

    loaded = load_env_file(env)

    assert loaded == ["NEW_KEY", "OTHER"]
    assert config.os.environ["NEW_KEY"] == "quoted value"
    assert config.os.environ["KEEP"] == "from-shell"
    monkeypatch.delenv("NEW_KEY")
    monkeypatch.delenv("OTHER")


def test_load_default_env_prefers_wardline_over_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inner, outer = tmp_path / "wardline.env", tmp_path / "root.env"
    inner.write_text("WL_TEST_INNER=1\n", encoding="utf-8")
    outer.write_text("WL_TEST_OUTER=1\n", encoding="utf-8")
    monkeypatch.delenv("WL_TEST_INNER", raising=False)
    monkeypatch.delenv("WL_TEST_OUTER", raising=False)
    monkeypatch.setattr(config, "ENV_SEARCH_PATHS", (inner, outer))

    path, loaded = load_default_env()

    assert path == inner and loaded == ["WL_TEST_INNER"]
    assert "WL_TEST_OUTER" not in config.os.environ
    monkeypatch.delenv("WL_TEST_INNER")

    monkeypatch.setattr(config, "ENV_SEARCH_PATHS", (tmp_path / "missing.env",))
    assert load_default_env() == (None, [])


def test_cli_fake_run_routes_insulin_in_dry_run(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "ENV_SEARCH_PATHS", ())
    monkeypatch.delenv("NOTIFY_DRY_RUN", raising=False)
    monkeypatch.delenv("DRY_RUN", raising=False)

    assert cli.main([]) == 0

    out = capsys.readouterr().out
    summary = json.loads(out[out.index('{\n  "agent"') :])
    assert summary["event_type"] == "MedRequested"
    assert [d["from_location"] for d in summary["deliveries"]] == ["Unit 6 cabinet"]
    assert summary["escalations"] == []
    assert '"event": "email"' in out  # printed, not sent
    deliveries.reset_deliveries()
    escalation.reset_escalations()
    monkeypatch.delenv("NOTIFY_DRY_RUN", raising=False)
    monkeypatch.delenv("DRY_RUN", raising=False)


def test_cli_rejects_live_without_model_id_and_fake_non_insulin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "ENV_SEARCH_PATHS", ())
    monkeypatch.delenv("MODEL_ID", raising=False)

    with pytest.raises(SystemExit):
        cli.main(["--live"])
    with pytest.raises(SystemExit):
        cli.main(["--case", "heparin"])
    monkeypatch.delenv("NOTIFY_DRY_RUN", raising=False)
    monkeypatch.delenv("DRY_RUN", raising=False)
