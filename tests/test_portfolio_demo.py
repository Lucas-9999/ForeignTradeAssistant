"""The portfolio view must not inherit credentials, write, or start providers."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def demo(tmp_path, monkeypatch):
    import app as original_app
    import config
    from tools import run_portfolio_demo as runner

    # Preserve shared globals used by the wider API test suite.
    previous_env = os.environ.copy()
    for name, value in list(vars(config).items()):
        if name.isupper():
            monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(config, "LLM_API_KEY", "synthetic-test-value")
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://model.portfolio.invalid")
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "synthetic-test-value")
    monkeypatch.setattr(original_app.app.state, "scheduler_enabled", True, raising=False)
    application, counts = runner.create_demo_app(tmp_path / "demo.sqlite3")
    yield application, counts
    os.environ.clear()
    os.environ.update(previous_env)


def test_demo_has_synthetic_data_and_no_scheduler(demo, monkeypatch):
    import config
    import collector.scheduler as scheduler

    monkeypatch.setattr(scheduler, "start_scheduler", lambda: pytest.fail("demo started a scheduler"))
    application, counts = demo
    assert counts == {"leads": 12, "tags": 1, "groups": 1}
    assert config.EMAIL_DRY_RUN is True
    assert config.OUTBOUND_DELIVERY_ENABLED is False
    assert config.SMTP_PASSWORD == config.IMAP_PASSWORD == ""
    assert config.LLM_API_KEY == config.LLM_BASE_URL == config.LLM_MODEL == ""
    assert config.TWILIO_AUTH_TOKEN == ""
    import urllib.request
    from utils.sanitize import _llm_translate

    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("demo called a model"))
    _llm_translate.cache_clear()
    assert _llm_translate("合成测试行业") is None
    with TestClient(application) as client:
        assert client.get("/api/v1/health").status_code == 200
        overview = client.get("/api/v1/dashboard/overview").json()
        assert overview["kpis"]["total_leads"] == 12
        data = client.get("/api/v1/leads", params={"page": 1, "page_size": 20}).json()
        assert len(data["items"]) == 12
        assert all(item["email"].endswith("@portfolio.invalid") for item in data["items"])


@pytest.mark.parametrize("path", [
    "/api/v1/emails/send", "/api/v1/emails/send-batch",
    "/api/v1/mailboxes/default/test", "/api/v1/collector/run", "/api/v1/inbox/check",
])
def test_demo_blocks_writes_before_business_handlers(demo, path, monkeypatch):
    import app as original_app

    monkeypatch.setattr(original_app, "get_db", lambda: pytest.fail("write reached the DB handler"))
    with TestClient(demo[0]) as client:
        response = client.post(path, json={})
        assert response.status_code == 403
        assert response.headers["X-Portfolio-Mode"] == "synthetic-read-only"


def test_demo_blocks_legacy_diagnostic_page(demo):
    with TestClient(demo[0]) as client:
        assert client.get("/dashboard/health").status_code == 403


def test_existing_database_is_never_opened_or_replaced(tmp_path):
    from tools.run_portfolio_demo import create_demo_app

    existing = tmp_path / "existing.sqlite3"
    marker = b"do-not-replace-existing-database"
    existing.write_bytes(marker)
    with pytest.raises(ValueError, match="fresh database"):
        create_demo_app(existing)
    assert existing.read_bytes() == marker


def test_inspect_cli_ignores_activation_and_cleans_runtime(tmp_path):
    root = Path(__file__).resolve().parents[1]
    existing = tmp_path / "existing.sqlite3"
    existing.write_bytes(b"do-not-open")
    environment = os.environ.copy()
    environment.update({
        "FTA_DB_PATH": str(existing), "FTA_OUTBOUND_DELIVERY_ENABLED": "true",
        "FTA_SMTP_PASSWORD": "synthetic-test-value", "FTA_IMAP_CHECK_ENABLED": "true",
        "LLM_API_KEY": "synthetic-test-value", "LLM_BASE_URL": "https://model.portfolio.invalid",
        "TWILIO_AUTH_TOKEN": "synthetic-test-value",
    })
    runtime = root / "data" / "demo-runtime"
    before = set(runtime.iterdir()) if runtime.exists() else set()
    result = subprocess.run(
        [sys.executable, "tools/run_portfolio_demo.py", "--inspect"], cwd=root,
        env=environment, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["mode"] == "synthetic-read-only"
    assert data["smtp_enabled"] is False
    assert data["schedulers_enabled"] is False
    assert existing.read_bytes() == b"do-not-open"
    assert set(runtime.iterdir()) == before
