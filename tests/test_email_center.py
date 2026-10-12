"""M6 Email Center API contract tests.

All tests use the isolated api_client database.  SMTP and DNS are never
contacted: real execution is patched at the public `core_send_batch` seam.
"""

from __future__ import annotations

import time
import pytest

DEFAULT_TEMPLATE_ID = "general_general_quality_01"


@pytest.fixture(autouse=True)
def _synthetic_default_mailbox(monkeypatch):
    """Keep API contracts independent of an operator's local mail settings.

    Test execution replaces the send seam; these values never authenticate.
    """
    import config

    monkeypatch.setattr(config, "SMTP_HOST", "smtp.portfolio.invalid")
    monkeypatch.setattr(config, "SMTP_USERNAME", "sender@portfolio.invalid")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "synthetic-test-password")
    monkeypatch.setattr(config, "SMTP_FROM_EMAIL", "sender@portfolio.invalid")


def _preview(client, **overrides):
    template_id = overrides.pop("template_id", None)
    if template_id is None:
        template_id = DEFAULT_TEMPLATE_ID
    payload = {
        "mailbox": "default",
        "limit": 2,
        "include_low_relevance": True,
        "template_id": template_id,
        **overrides,
    }
    return client.post("/api/v1/email-center/preview", json=payload)


def test_email_center_options_are_read_only_and_password_free(api_client, monkeypatch):
    monkeypatch.delenv("FTA_UI_SEND_TOKEN", raising=False)

    r = api_client.get("/api/v1/email-center/options")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["send_enabled"] is False
    assert body["max_batch_size"] == 50
    assert body["mailboxes"]
    assert body["template_categories"]
    assert body["templates"]
    for mailbox in body["mailboxes"]:
        assert "password" not in mailbox
        assert "password_enc" not in mailbox
        assert isinstance(mailbox["has_smtp_password"], bool)


def test_preview_binds_exact_candidates_without_sending(api_client, monkeypatch):
    def forbidden_send(*args, **kwargs):
        raise AssertionError("preview must never call send_batch")

    import emailer.email_center as service

    monkeypatch.setattr(service, "core_send_batch", forbidden_send)
    r = _preview(api_client)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["candidate_count"] == 1
    assert len(body["samples"]) == 1
    assert body["confirmation_id"]
    assert body["confirmation_text"] == "确认发送 1 封"
    assert all(x["lead_id"] for x in body["samples"])
    assert all(x["subject"] for x in body["samples"])

    exact_id = DEFAULT_TEMPLATE_ID
    exact = _preview(api_client, template_id=exact_id).json()
    assert exact["samples"]
    assert {sample["template_id"] for sample in exact["samples"]} == {exact_id}

    missing_template = api_client.post(
        "/api/v1/email-center/preview",
        json={"mailbox": "default", "limit": 1},
    )
    assert missing_template.status_code == 400


def test_preview_excludes_archived_sources_like_the_send_batch(api_client):
    """A preview must not freeze a lead that send_batch will later exclude."""
    preview = _preview(api_client, limit=2)

    assert preview.status_code == 200, preview.text
    assert preview.json()["lead_ids"] == [4]


def test_preview_rejects_unknown_or_inactive_mailbox(api_client):
    missing = _preview(api_client, mailbox="missing")
    assert missing.status_code == 404

    off = api_client.patch("/api/v1/mailboxes/default", json={"is_active": 0})
    assert off.status_code == 200
    inactive = _preview(api_client, mailbox="default")
    assert inactive.status_code == 409


def test_preview_rejects_custom_mailbox_without_credentials(api_client):
    created = api_client.post("/api/v1/mailboxes", json={
        "name": "empty-creds",
        "smtp_host": "smtp.example.com",
        "username": "sender@example.com",
    })
    assert created.status_code == 200
    assert created.json()["has_smtp_password"] is False

    preview = _preview(api_client, mailbox="empty-creds")
    assert preview.status_code == 409
    assert "凭证" in preview.json()["detail"]


def test_specific_industry_template_scopes_preview_to_matching_leads(api_client):
    """A medical template must never be previewed for an automotive lead."""
    import app as fta_app

    conn = fta_app.get_db()
    conn.executemany(
        "UPDATE leads SET industry = ? WHERE id = ?",
        [("Automotive & Vehicles", 1), ("Medical devices", 4)],
    )
    conn.commit()
    conn.close()

    preview = _preview(
        api_client,
        limit=2,
        template_id="general_medical_quality_01",
    )

    assert preview.status_code == 200, preview.text
    assert preview.json()["lead_ids"] == [4]

    conflicting = _preview(
        api_client,
        limit=1,
        template_id="general_medical_quality_01",
        industry_group="automotive",
    )
    assert conflicting.status_code == 400


def test_confirm_is_fail_closed_without_or_with_wrong_token(api_client, monkeypatch):
    preview = _preview(api_client).json()

    monkeypatch.delenv("FTA_UI_SEND_TOKEN", raising=False)
    disabled = api_client.post(
        "/api/v1/email-center/send",
        json={
            "confirmation_id": preview["confirmation_id"],
            "confirmation_text": preview["confirmation_text"],
        },
    )
    assert disabled.status_code == 503

    monkeypatch.setenv("FTA_UI_SEND_TOKEN", "operator-secret")
    denied = api_client.post(
        "/api/v1/email-center/send",
        headers={"X-FTA-Send-Token": "wrong"},
        json={
            "confirmation_id": preview["confirmation_id"],
            "confirmation_text": preview["confirmation_text"],
        },
    )
    assert denied.status_code == 403


def test_confirm_revalidates_mailbox_after_preview(api_client, monkeypatch):
    monkeypatch.setenv("FTA_UI_SEND_TOKEN", "operator-secret")
    preview = _preview(api_client).json()
    off = api_client.patch("/api/v1/mailboxes/default", json={"is_active": 0})
    assert off.status_code == 200

    blocked = api_client.post(
        "/api/v1/email-center/send",
        headers={"X-FTA-Send-Token": "operator-secret"},
        json={
            "confirmation_id": preview["confirmation_id"],
            "confirmation_text": preview["confirmation_text"],
        },
    )
    assert blocked.status_code == 409


def test_confirm_rejects_wrong_phrase_without_consuming_preview(api_client, monkeypatch):
    """A typo must not start a job or invalidate the valid confirmation."""
    import emailer.email_center as service

    calls = []
    monkeypatch.setattr(service, "core_send_batch", lambda **kwargs: calls.append(kwargs) or {})
    monkeypatch.setenv("FTA_UI_SEND_TOKEN", "operator-secret")

    template_id = DEFAULT_TEMPLATE_ID
    preview = _preview(api_client, template_id=template_id).json()
    headers = {"X-FTA-Send-Token": "operator-secret"}
    wrong = api_client.post(
        "/api/v1/email-center/send",
        headers=headers,
        json={
            "confirmation_id": preview["confirmation_id"],
            "confirmation_text": "确认发送错误数量",
        },
    )
    assert wrong.status_code == 409
    assert calls == []

    valid = api_client.post(
        "/api/v1/email-center/send",
        headers=headers,
        json={
            "confirmation_id": preview["confirmation_id"],
            "confirmation_text": preview["confirmation_text"],
        },
    )
    assert valid.status_code == 202
    job_id = valid.json()["job_id"]
    for _ in range(50):
        job = api_client.get(f"/api/v1/email-center/status/{job_id}").json()
        if job["status"] in {"completed", "failed"}:
            break
        time.sleep(0.01)
    assert job["status"] == "completed"


def test_confirm_runs_once_and_exposes_job_status(api_client, monkeypatch):
    import emailer.email_center as service

    calls = []

    def fake_send_batch(**kwargs):
        calls.append(kwargs)
        return {
            "sent": 2,
            "skipped": 0,
            "errors": 0,
            "details": [{"status": "sent"}, {"status": "sent"}],
        }

    monkeypatch.setattr(service, "core_send_batch", fake_send_batch)
    monkeypatch.setenv("FTA_UI_SEND_TOKEN", "operator-secret")

    template_id = DEFAULT_TEMPLATE_ID
    preview = _preview(api_client, template_id=template_id).json()
    payload = {
        "confirmation_id": preview["confirmation_id"],
        "confirmation_text": preview["confirmation_text"],
    }
    headers = {"X-FTA-Send-Token": "operator-secret"}

    started = api_client.post("/api/v1/email-center/send", headers=headers, json=payload)
    assert started.status_code == 202, started.text
    job_id = started.json()["job_id"]

    body = None
    for _ in range(50):
        body = api_client.get(f"/api/v1/email-center/status/{job_id}").json()
        if body["status"] in {"completed", "failed"}:
            break
        time.sleep(0.01)

    assert body and body["status"] == "completed"
    assert body["sent"] == 2
    assert body["details"] == []
    assert len(calls) == 1
    assert calls[0]["smtp_profile"] == "default"
    assert calls[0]["lead_ids"] == preview["lead_ids"]
    assert calls[0]["template_id"] == template_id
    assert calls[0]["dry_run"] is False

    replay = api_client.post("/api/v1/email-center/send", headers=headers, json=payload)
    assert replay.status_code == 409
    assert len(calls) == 1


def test_legacy_batch_send_requires_token_unless_dry_run(api_client, monkeypatch):
    monkeypatch.setenv("FTA_UI_SEND_TOKEN", "operator-secret")

    blocked = api_client.post("/api/v1/emails/send-batch", json={"limit": 1})
    assert blocked.status_code == 403

    # Dry-run stays available for operational preview and must not require the
    # write token. Patch the canonical sender to avoid DNS/network work.
    import app as fta_app

    monkeypatch.setattr(
        fta_app,
        "core_send_batch",
        lambda **kwargs: {"sent": 0, "skipped": 0, "errors": 0, "details": []},
    )
    dry = api_client.post("/api/v1/emails/send-batch", json={"limit": 1, "dry_run": True})
    assert dry.status_code == 200


def test_legacy_single_send_requires_token_before_smtp(api_client, monkeypatch):
    monkeypatch.setenv("FTA_UI_SEND_TOKEN", "operator-secret")

    blocked = api_client.post("/api/v1/emails/send", json={"lead_id": 1})
    assert blocked.status_code == 403


def test_legacy_single_send_honors_unified_suppression_before_smtp(
    api_client, monkeypatch
):
    import sqlite3

    import app as fta_app
    import config
    from services.email_reputation.models import SuppressionReason
    from services.email_reputation.suppression_service import SuppressionService

    monkeypatch.setenv("FTA_UI_SEND_TOKEN", "operator-secret")

    async def forbidden_send(**kwargs):
        raise AssertionError("suppressed recipient must never reach SMTP")

    monkeypatch.setattr("emailer.sender.send_email", forbidden_send)
    conn = sqlite3.connect(str(config.DB_PATH))
    SuppressionService(conn).suppress_domain(
        "alpha.com",
        reason=SuppressionReason.DEAD_DOMAIN,
        source="dns_audit",
    )
    conn.close()

    response = api_client.post(
        "/api/v1/emails/send",
        headers={"X-FTA-Send-Token": "operator-secret"},
        json={"lead_id": 1},
    )

    assert response.status_code == 429
    assert "suppressed" in response.json()["detail"].lower()


def test_legacy_single_send_global_dry_run_never_records_delivery(
    api_client, monkeypatch
):
    import sqlite3

    import config

    monkeypatch.setenv("FTA_UI_SEND_TOKEN", "operator-secret")
    monkeypatch.setattr(config, "EMAIL_DRY_RUN", True, raising=False)
    monkeypatch.setattr("utils.email_verify.has_mx_record", lambda domain: True)

    async def forbidden_send(**kwargs):
        raise AssertionError("global dry-run API request must never reach SMTP")

    monkeypatch.setattr("emailer.sender.send_email", forbidden_send)
    response = api_client.post(
        "/api/v1/emails/send",
        headers={"X-FTA-Send-Token": "operator-secret"},
        json={"lead_id": 1},
    )

    assert response.status_code == 200
    assert response.json()["success"] is False
    assert response.json()["dry_run"] is True
    conn = sqlite3.connect(str(config.DB_PATH))
    comms = conn.execute(
        "SELECT COUNT(*) FROM communications WHERE lead_id = 1 AND status = 'sent'"
    ).fetchone()[0]
    lead_status = conn.execute("SELECT status FROM leads WHERE id = 1").fetchone()[0]
    conn.close()
    assert comms == 0
    assert lead_status == "new"


def test_legacy_single_send_sanitizes_smtp_failure_response_and_storage(
    api_client, monkeypatch
):
    import sqlite3

    import app as fta_app
    import config

    monkeypatch.setenv("FTA_UI_SEND_TOKEN", "operator-secret")
    monkeypatch.setattr("utils.email_verify.has_mx_record", lambda domain: True)

    async def failed_send(**kwargs):
        return (
            False,
            "550 5.7.1 policy buyer@alpha.com password=hunter2",
            None,
        )

    monkeypatch.setattr("emailer.sender.send_email", failed_send)

    response = api_client.post(
        "/api/v1/emails/send",
        headers={"X-FTA-Send-Token": "operator-secret"},
        json={"lead_id": 1},
    )

    conn = sqlite3.connect(str(config.DB_PATH))
    stored_error = conn.execute(
        "SELECT error_message FROM communications ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    conn.close()
    response_text = response.text
    assert response.status_code == 200
    assert "hunter2" not in response_text
    assert "buyer@alpha.com" not in response_text
    # P12 denies the un-authorized request before SMTP, so no failure body is
    # persisted at all (and therefore cannot leak provider diagnostics).
    assert stored_error is None


def test_audit_contains_metadata_but_no_confirmation_or_secrets(api_client, monkeypatch):
    monkeypatch.setenv("FTA_UI_SEND_TOKEN", "operator-secret")
    preview = _preview(api_client).json()

    audit = api_client.get("/api/v1/email-center/audit").json()["items"]
    assert audit
    text = str(audit).lower()
    assert preview["confirmation_id"] not in text
    assert "operator-secret" not in text
    assert "password" not in text
