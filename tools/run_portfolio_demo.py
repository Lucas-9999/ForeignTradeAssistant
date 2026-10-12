"""Start an isolated, synthetic, read-only CRM demo on the loopback interface."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))


def _configure_demo_environment(db_path: Path) -> None:
    # Do not inherit credentials, provider activation, or a business DB path.
    for key in list(os.environ):
        if key.startswith(("FTA_", "LLM_", "TWILIO_")):
            os.environ.pop(key)
    os.environ.update({
        "FTA_DEMO_MODE": "true",
        "PYTHON_DOTENV_DISABLED": "1",
        "FTA_DB_PATH": str(db_path),
        "FTA_LOG_DIR": str(db_path.parent / "logs"),
        "FTA_OUTBOUND_DELIVERY_ENABLED": "false",
        "FTA_EMAIL_SCHEDULE_ENABLED": "false",
        "FTA_IMAP_CHECK_ENABLED": "false",
        "FTA_ZOHO_BATCH_ENABLED": "false",
        "FTA_ZOHO_SENT_ENABLED": "false",
        "FTA_FOLLOWUP_SCHEDULE_ENABLED": "false",
        "FTA_INDUSTRY_COLLECTION_ENABLED": "false",
        "FTA_XUNKEXING_COLLECTION_ENABLED": "false",
        "FTA_LINKEDIN_COLLECTION_ENABLED": "false",
        "FTA_FB_COLLECTION_ENABLED": "false",
        "FTA_SELF_HOSTED_SMTP_ENABLED": "false",
    })


def _seed_synthetic_data(db_path: Path) -> dict[str, int]:
    from crm.db import build_database

    fixture = json.loads((BASE_DIR / "data" / "portfolio-demo.json").read_text(encoding="utf-8"))
    conn = build_database(db_path)
    try:
        lead_ids = []
        for index, lead in enumerate(fixture["leads"]):
            cursor = conn.execute(
                """INSERT INTO leads
                (company_name, contact_name, email, country, industry, source, status,
                 notes, discovered_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now', ?), datetime('now', ?))""",
                (lead["company_name"], lead["contact_name"], lead["email"],
                 lead["country"], lead["industry"], "synthetic_import", lead["status"],
                 "Synthetic portfolio fixture; no real customer or delivery.",
                 f"-{index % 6} days", f"-{index % 6} days"),
            )
            lead_ids.append(cursor.lastrowid)
            if lead["status"] in {"contacted", "replied", "dead"}:
                conn.execute(
                    """INSERT INTO communications
                    (lead_id, channel, direction, subject, body, status, sent_at)
                    VALUES (?, 'email', 'outbound', 'Simulated preview',
                            'Synthetic timeline record; no message was sent.',
                            'sent', datetime('now', '-2 days'))""",
                    (cursor.lastrowid,),
                )
            if lead["status"] == "replied":
                conn.execute(
                    """INSERT INTO communications
                    (lead_id, channel, direction, subject, body, status, sent_at)
                    VALUES (?, 'email', 'inbound', 'Simulated reply',
                            'Synthetic inbound record.', 'received', datetime('now'))""",
                    (cursor.lastrowid,),
                )
        tag = conn.execute(
            "INSERT INTO tags (name, category, color) VALUES ('Demo review', 'demo', '#2563eb')"
        ).lastrowid
        group = conn.execute(
            """INSERT INTO groups (name, description, color)
            VALUES ('Synthetic pipeline', 'Fictional records for local review', '#059669')"""
        ).lastrowid
        for lead_id in lead_ids[:4]:
            conn.execute("INSERT INTO lead_tags (lead_id, tag_id) VALUES (?, ?)", (lead_id, tag))
            conn.execute("INSERT INTO group_members (group_id, lead_id) VALUES (?, ?)", (group, lead_id))
        conn.execute(
            """INSERT INTO mailboxes
            (name, display_name, smtp_host, smtp_port, use_tls, username, from_email,
             from_name, daily_limit, is_active)
            VALUES ('default', 'Synthetic mailbox — no credentials', 'smtp.portfolio.invalid',
                    587, 1, 'sender@portfolio.invalid', 'sender@portfolio.invalid',
                    'Example Team', 200, 1)"""
        )
        conn.commit()
        return {"leads": len(lead_ids), "tags": 1, "groups": 1}
    finally:
        conn.close()


class ReadOnlyDemo:
    """Reject writes and legacy diagnostic routes before the underlying API."""

    def __init__(self, application):
        self.application = application

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            from starlette.responses import JSONResponse

            path = scope.get("path", "")
            allowed_path = (
                path.startswith(("/ui", "/api/v1/", "/docs", "/redoc"))
                or path == "/openapi.json"
            )
            if scope["method"] not in {"GET", "HEAD", "OPTIONS"} or not allowed_path:
                await JSONResponse(
                    {"detail": "Synthetic read-only demo: writes, provider tests and collection are disabled."},
                    status_code=403,
                    headers={"X-Portfolio-Mode": "synthetic-read-only"},
                )(scope, receive, send)
                return
        await self.application(scope, receive, send)


def create_demo_app(db_path: Path):
    """Create a new synthetic DB; never read or replace an existing database."""
    db_path = db_path.resolve()
    if db_path.exists():
        raise ValueError("Demo requires a fresh database path; existing files are never opened.")
    _configure_demo_environment(db_path)
    import config

    # Also set process-local values when used by tests after config was imported.
    config.DB_PATH = db_path
    config.LOG_DIR = db_path.parent / "logs"
    for name in (
        "SMTP_USERNAME", "SMTP_PASSWORD", "IMAP_USERNAME", "IMAP_PASSWORD",
        "ZOHO_SMTP_USERNAME", "ZOHO_SMTP_PASSWORD", "ZOHO_IMAP_USERNAME",
        "ZOHO_IMAP_PASSWORD", "MAILBOX_ENC_KEY", "UI_SEND_TOKEN",
        "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM",
    ):
        setattr(config, name, "")
    for name in (
        "OUTBOUND_DELIVERY_ENABLED", "EMAIL_SCHEDULE_ENABLED", "IMAP_CHECK_ENABLED",
        "ZOHO_BATCH_ENABLED", "ZOHO_SENT_ENABLED", "FOLLOWUP_SCHEDULE_ENABLED",
        "COLLECTION_INDUSTRY_ENABLED", "COLLECTION_XUNKEXING_ENABLED",
        "COLLECTION_LINKEDIN_ENABLED", "FB_COLLECTION_ENABLED", "SELF_HOSTED_SMTP_ENABLED",
    ):
        setattr(config, name, False)
    config.EMAIL_DRY_RUN = True
    counts = _seed_synthetic_data(db_path)
    import app as fta_app

    fta_app.app.state.scheduler_enabled = False
    return ReadOnlyDemo(fta_app.app), counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--inspect", action="store_true", help="Create, inspect and discard the synthetic DB without serving HTTP")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    runtime = BASE_DIR / "data" / "demo-runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="demo-", dir=runtime) as directory:
        try:
            application, counts = create_demo_app(Path(directory) / "demo.sqlite3")
            if args.inspect:
                print(json.dumps({"mode": "synthetic-read-only", "smtp_enabled": False,
                                  "schedulers_enabled": False, **counts}))
                return 0
            print(f"Synthetic, read-only portfolio demo: http://127.0.0.1:{args.port}/ui/")
            print("All displayed messages and customers are fictional. No external messages are sent.")
            import uvicorn

            uvicorn.run(application, host="127.0.0.1", port=args.port, reload=False)
        finally:
            # Windows holds the rotated log file open until its handler closes.
            # Close it before TemporaryDirectory removes this demo's own files.
            logging.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
