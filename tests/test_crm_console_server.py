"""CRM-only server entry must never start collection or mail schedulers."""

from fastapi.testclient import TestClient


def test_crm_only_lifespan_keeps_scheduler_stopped(monkeypatch):
    import app as fta_app
    import collector.scheduler as scheduler

    started: list[bool] = []
    stopped: list[bool] = []
    monkeypatch.setattr(scheduler, "start_scheduler", lambda: started.append(True))
    monkeypatch.setattr(scheduler, "stop_scheduler", lambda wait=False: stopped.append(wait))
    monkeypatch.setattr(fta_app, "ensure_schema_current", lambda _path: 14)
    previous = getattr(fta_app.app.state, "scheduler_enabled", True)
    fta_app.app.state.scheduler_enabled = False
    try:
        with TestClient(fta_app.app) as client:
            assert client.get("/api/v1/health").status_code == 200
        assert started == []
        assert stopped == []
    finally:
        fta_app.app.state.scheduler_enabled = previous


def test_lifespan_starts_imap_scheduler_without_collector_sources(monkeypatch):
    """P13 bounce monitoring must not depend on an enabled collection source."""
    import app as fta_app
    import collector.cli as cli
    import collector.scheduler as scheduler
    import config

    started: list[bool] = []
    stopped: list[bool] = []
    monkeypatch.setattr(cli, "SOURCE_REGISTRY", {})
    monkeypatch.setattr(config, "IMAP_CHECK_ENABLED", True)
    monkeypatch.setattr(scheduler, "start_scheduler", lambda: started.append(True))
    monkeypatch.setattr(scheduler, "stop_scheduler", lambda wait=False: stopped.append(wait))
    monkeypatch.setattr(fta_app, "ensure_schema_current", lambda _path: 14)
    previous = getattr(fta_app.app.state, "scheduler_enabled", True)
    fta_app.app.state.scheduler_enabled = True
    try:
        with TestClient(fta_app.app) as client:
            assert client.get("/api/v1/health").status_code == 200
        assert started == [True]
        assert stopped == [False]
    finally:
        fta_app.app.state.scheduler_enabled = previous


def test_crm_console_runner_binds_locally_and_disables_scheduler(monkeypatch):
    import app as fta_app
    import tools.run_crm_console as runner

    captured = {}
    monkeypatch.setattr(runner.uvicorn, "run", lambda app, **kwargs: captured.update(app=app, **kwargs))
    previous = getattr(fta_app.app.state, "scheduler_enabled", True)
    try:
        assert runner.main([]) == 0
        assert fta_app.app.state.scheduler_enabled is False
        assert captured["app"] is fta_app.app
        assert captured["host"] == "127.0.0.1"
        assert captured["port"] == 8001
        assert captured["reload"] is False
    finally:
        fta_app.app.state.scheduler_enabled = previous
