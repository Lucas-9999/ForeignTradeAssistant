"""
CRM Mailboxes write API tests (M5, schema V8).

Covers: reserved-name rejection, Fernet password roundtrip (write-only),
missing-key 503, DB-first resolution vs env fallback, env seeding idempotency,
delete-409 when the mailbox sent today, the SMTP test endpoint (network
mocked), and AntiSpamGuard reading its daily limit from the mailboxes table.

Uses the shared `api_client` fixture (conftest.py) whose temp DB is built via
build_database → migrations incl. V8 + env seeding run.
"""

import sqlite3

import pytest

import config
from crm import mailbox_manager
from crm.db import _seed_env_mailboxes
from emailer.rotation import AntiSpamGuard


@pytest.fixture
def enc_key(monkeypatch):
    """A valid (non-production) Fernet key so password roundtrips work."""
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("FTA_MAILBOX_ENC_KEY", key)
    return key


# ── List + reserved names ─────────────────────────────────────────────────


def test_mailboxes_write_reserved_names_409(api_client):
    """default/zoho are env-managed — creating or deleting them is refused."""
    r = api_client.post("/api/v1/mailboxes", json={
        "name": "default", "smtp_host": "x", "username": "y",
    })
    assert r.status_code == 409
    assert api_client.delete("/api/v1/mailboxes/default").status_code == 409
    assert api_client.delete("/api/v1/mailboxes/zoho").status_code == 409


def test_mailboxes_create_and_list_no_password(api_client, enc_key):
    r = api_client.post("/api/v1/mailboxes", json={
        "name": "sales",
        "display_name": "销售组",
        "smtp_host": "smtp.sales.example.com",
        "smtp_port": 587,
        "use_tls": True,
        "username": "sales@example.com",
        "password": "s3cret",
        "from_email": "sales@example.com",
        "from_name": "Sales Team",
        "daily_limit": 50,
    })
    assert r.status_code == 200, r.text
    box = r.json()
    assert box["name"] == "sales"
    assert box["has_smtp_password"] is True
    assert box["daily_limit"] == 50
    assert "password" not in box, "must never serialize the SMTP password"

    # Duplicate name → 409
    dup = api_client.post("/api/v1/mailboxes", json={
        "name": "sales", "smtp_host": "x", "username": "y",
    })
    assert dup.status_code == 409

    # Listed — still no password, only the flag
    body = api_client.get("/api/v1/mailboxes").json()
    by_name = {m["name"]: m for m in body["mailboxes"]}
    assert "sales" in by_name
    assert by_name["sales"]["has_smtp_password"] is True
    assert "password" not in by_name["sales"]


def test_mailboxes_password_roundtrip_and_clear(api_client, enc_key, monkeypatch):
    """Fernet roundtrip: password stored encrypted, resolved back identically."""
    monkeypatch.setattr(config, "SMTP_PASSWORD", "")  # env fallback blank → deterministic
    api_client.post("/api/v1/mailboxes", json={
        "name": "roundtrip", "smtp_host": "smtp.rt.example.com",
        "username": "rt@example.com", "password": "hunter2",
    })

    # Resolver returns the plaintext; the DB only holds the Fernet token
    assert mailbox_manager.resolve_smtp_settings("roundtrip")["password"] == "hunter2"
    conn = sqlite3.connect(str(config.DB_PATH))
    try:
        token = conn.execute(
            "SELECT password_enc FROM mailboxes WHERE name='roundtrip'"
        ).fetchone()[0]
        assert token and token != "hunter2"
        assert mailbox_manager.decrypt_password(token) == "hunter2"
    finally:
        conn.close()

    # Empty-string password clears the stored token → env fallback (custom name
    # has no env → password resolves to "")
    r = api_client.put("/api/v1/mailboxes/roundtrip", json={"password": ""})
    assert r.status_code == 200, r.text
    assert r.json()["has_smtp_password"] is False
    assert mailbox_manager.resolve_smtp_settings("roundtrip")["password"] == ""


def test_mailboxes_key_missing_503(api_client, monkeypatch):
    """Without FTA_MAILBOX_ENC_KEY, storing a password → 503; no-password
    creates still work (sending is always safe — env fallback)."""
    monkeypatch.delenv("FTA_MAILBOX_ENC_KEY", raising=False)
    monkeypatch.setattr(config, "SMTP_PASSWORD", "")  # no env password to leak through
    r = api_client.post("/api/v1/mailboxes", json={
        "name": "nokey", "smtp_host": "x", "username": "y", "password": "z",
    })
    assert r.status_code == 503

    ok = api_client.post("/api/v1/mailboxes", json={
        "name": "nokey", "smtp_host": "smtp.x.com", "username": "y",
    })
    assert ok.status_code == 200
    assert ok.json()["has_smtp_password"] is False


def test_mailboxes_update_and_404(api_client):
    api_client.post("/api/v1/mailboxes", json={
        "name": "editable", "smtp_host": "smtp.e.com", "username": "e@e.com",
    })
    r = api_client.put("/api/v1/mailboxes/editable", json={"display_name": "改名"})
    assert r.status_code == 200 and r.json()["display_name"] == "改名"

    # PATCH alias works too
    r = api_client.patch("/api/v1/mailboxes/editable", json={"daily_limit": 99})
    assert r.status_code == 200 and r.json()["daily_limit"] == 99

    assert api_client.put("/api/v1/mailboxes/nope", json={"display_name": "x"}).status_code == 404
    assert api_client.patch("/api/v1/mailboxes/nope", json={"display_name": "x"}).status_code == 404


# ── Delete protection ─────────────────────────────────────────────────────


def test_mailboxes_delete_refused_if_sent_today(api_client):
    """Deleting a mailbox that already sent today → 409."""
    api_client.post("/api/v1/mailboxes", json={
        "name": "busy", "smtp_host": "smtp.busy.com", "username": "b@b.com",
    })
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.execute(
        "INSERT INTO mailbox_quotas (date, mailbox, email_count) VALUES (date('now'), 'busy', 5)"
    )
    conn.commit()
    conn.close()

    assert api_client.delete("/api/v1/mailboxes/busy").status_code == 409
    # Missing mailbox → 404
    assert api_client.delete("/api/v1/mailboxes/nope").status_code == 404


def test_mailboxes_delete_ok_when_no_quota(api_client):
    api_client.post("/api/v1/mailboxes", json={
        "name": "quiet", "smtp_host": "smtp.q.com", "username": "q@q.com",
    })
    assert api_client.delete("/api/v1/mailboxes/quiet").status_code == 200
    body = api_client.get("/api/v1/mailboxes").json()
    assert "quiet" not in {m["name"] for m in body["mailboxes"]}


# ── Resolver: DB-first / env fallback / seeding idempotency ───────────────


def test_db_row_overrides_env(api_client, monkeypatch):
    """A DB row's non-null fields beat env; a custom mailbox NEVER inherits
    the env password (CRM-002) — it resolves empty instead."""
    monkeypatch.setattr(config, "SMTP_HOST", "env.example.com")
    monkeypatch.setattr(config, "SMTP_USERNAME", "env@example.com")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "env-pw")

    api_client.post("/api/v1/mailboxes", json={
        "name": "custom", "smtp_host": "db.example.com",
        "username": "db@example.com", "daily_limit": 77,
    })
    s = mailbox_manager.resolve_smtp_settings("custom")
    assert s["host"] == "db.example.com"       # DB wins
    assert s["username"] == "db@example.com"   # DB wins
    assert s["password"] == ""                 # custom never falls back to env-pw
    assert s["daily_limit"] == 77              # DB limit wins


def test_env_seeding_idempotent_and_byte_identical(tmp_path, monkeypatch):
    """Seeding runs once (idempotent) and seeded rows resolve to env values."""
    monkeypatch.setattr(config, "SMTP_USERNAME", "neta@163.com")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "pw-163")
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.163.com")
    monkeypatch.setattr(config, "SMTP_PORT", 465)
    monkeypatch.setattr(config, "SMTP_USE_TLS", False)
    monkeypatch.setattr(config, "DAILY_EMAIL_LIMIT", 123)
    monkeypatch.setattr(config, "ZOHO_SMTP_USERNAME", "")  # isolate: only default seeds

    db_path = tmp_path / "seed.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE mailboxes (
            name TEXT PRIMARY KEY, display_name TEXT, smtp_host TEXT, smtp_port INTEGER,
            use_tls INTEGER, username TEXT, password_enc TEXT, from_email TEXT,
            from_name TEXT, imap_host TEXT, imap_port INTEGER, daily_limit INTEGER,
            is_active INTEGER NOT NULL DEFAULT 1, source TEXT DEFAULT 'db',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""
    )
    conn.commit()

    assert _seed_env_mailboxes(conn) == 1
    assert _seed_env_mailboxes(conn) == 0  # idempotent
    conn.close()

    conn2 = sqlite3.connect(db_path)
    conn2.row_factory = sqlite3.Row
    try:
        s = mailbox_manager.resolve_smtp_settings("default", conn2)
        assert s["username"] == "neta@163.com"
        assert s["password"] == "pw-163"
        assert s["host"] == "smtp.163.com"
        assert s["port"] == 465
        assert s["use_tls"] is False
        assert s["daily_limit"] == 123
        # password_enc is NULL for seeded rows (env-fallback marker)
        row = conn2.execute(
            "SELECT password_enc FROM mailboxes WHERE name='default'"
        ).fetchone()
        assert row["password_enc"] is None
    finally:
        conn2.close()


def test_antispam_guard_limit_from_mailboxes(api_client):
    """AntiSpamGuard._daily_limit resolves once from the mailboxes table (V8)."""
    api_client.post("/api/v1/mailboxes", json={
        "name": "small", "smtp_host": "smtp.s.com", "username": "s@s.com",
        "daily_limit": 42,
    })
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        guard = AntiSpamGuard(conn, mailbox="small")
        assert guard._daily_limit() == 42
        guard_default = AntiSpamGuard(conn, mailbox="default")
        assert guard_default._daily_limit() == config.DAILY_EMAIL_LIMIT
    finally:
        conn.close()


# ── SMTP test endpoint (network mocked) ───────────────────────────────────


def test_smtp_test_endpoint_success_mocked(api_client, monkeypatch, enc_key):
    class _FakeSMTP:
        def __init__(self, *args, **kwargs):
            self.calls = []

        def starttls(self, context=None):
            self.calls.append("starttls")
            return (220, b"ready")

        def login(self, user, pw):
            self.calls.append(("login", user, pw))
            return (235, b"ok")

        def quit(self):
            self.calls.append("quit")
            return (221, b"bye")

    monkeypatch.setattr(mailbox_manager.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(mailbox_manager.smtplib, "SMTP_SSL", _FakeSMTP)

    api_client.post("/api/v1/mailboxes", json={
        "name": "tester", "smtp_host": "smtp.t.com", "username": "t@t.com",
        "password": "tpw", "use_tls": True,
    })
    r = api_client.post("/api/v1/mailboxes/tester/test")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert "认证成功" in r.json()["message"]


def test_smtp_test_missing_password_no_network(api_client, monkeypatch):
    """Blank env password → immediate '未配置' failure, no SMTP call."""
    monkeypatch.setattr(config, "SMTP_PASSWORD", "")
    r = api_client.post("/api/v1/mailboxes/default/test")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "凭证未配置" in body["message"]


def test_smtp_test_missing_mailbox_404_no_network(api_client, monkeypatch):
    """CRM-002: testing a nonexistent mailbox → 404 and smtplib is never reached."""
    def _boom(*args, **kwargs):
        raise AssertionError("smtplib must not be reached for a nonexistent mailbox")

    monkeypatch.setattr(mailbox_manager.smtplib, "SMTP", _boom)
    monkeypatch.setattr(mailbox_manager.smtplib, "SMTP_SSL", _boom)
    r = api_client.post("/api/v1/mailboxes/ghost/test")
    assert r.status_code == 404, r.text
    assert "不存在" in r.json()["detail"]


def test_smtp_test_custom_no_password_no_network(api_client, monkeypatch):
    """CRM-002: custom mailbox without a password → '凭证未配置', no SMTP call."""
    def _boom(*args, **kwargs):
        raise AssertionError("smtplib must not be reached")

    monkeypatch.setattr(mailbox_manager.smtplib, "SMTP", _boom)
    monkeypatch.setattr(mailbox_manager.smtplib, "SMTP_SSL", _boom)
    api_client.post("/api/v1/mailboxes", json={
        "name": "nopass", "smtp_host": "smtp.n.com", "username": "n@n.com",
    })
    r = api_client.post("/api/v1/mailboxes/nopass/test")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False
    assert "凭证未配置" in r.json()["message"]


def test_custom_never_falls_back_on_bad_fernet_key(api_client, enc_key, monkeypatch):
    """CRM-002: an undecryptable token → empty password, never the default env password."""
    from cryptography.fernet import Fernet

    monkeypatch.setattr(config, "SMTP_PASSWORD", "default-pw")
    api_client.post("/api/v1/mailboxes", json={
        "name": "badkey", "smtp_host": "smtp.b.com", "username": "b@b.com", "password": "secret1",
    })

    # Rotate the key → the stored token no longer decrypts.
    monkeypatch.setenv("FTA_MAILBOX_ENC_KEY", Fernet.generate_key().decode("utf-8"))

    s = mailbox_manager.resolve_smtp_settings("badkey")
    assert s["password"] == ""              # 绝不回退到 default-pw
    assert s["username"] == "b@b.com"       # 其它字段照常来自 DB

    body = api_client.get("/api/v1/mailboxes").json()
    by_name = {m["name"]: m for m in body["mailboxes"]}
    assert by_name["badkey"]["has_smtp_password"] is False
    assert by_name["badkey"]["credential_source"] == "missing"


def test_mailboxes_credential_source_enum(api_client, enc_key, monkeypatch):
    """OPT-005: credential_source reports env / encrypted / missing, never the password."""
    monkeypatch.setattr(config, "SMTP_PASSWORD", "env-pw")  # default 由 env 提供密码

    # 保留邮箱（播种行 password_enc=NULL）→ env
    body = api_client.get("/api/v1/mailboxes").json()
    by_name = {m["name"]: m for m in body["mailboxes"]}
    assert by_name["default"]["credential_source"] == "env"
    assert by_name["default"]["has_smtp_password"] is True

    # 自定义邮箱带密码 → encrypted
    api_client.post("/api/v1/mailboxes", json={
        "name": "src-enc", "smtp_host": "smtp.s.com", "username": "s@s.com", "password": "sp",
    })
    # 自定义邮箱无密码 → missing
    api_client.post("/api/v1/mailboxes", json={
        "name": "src-none", "smtp_host": "smtp.n.com", "username": "n@n.com",
    })

    body = api_client.get("/api/v1/mailboxes").json()
    by_name = {m["name"]: m for m in body["mailboxes"]}
    assert by_name["src-enc"]["credential_source"] == "encrypted"
    assert by_name["src-enc"]["has_smtp_password"] is True
    assert by_name["src-none"]["credential_source"] == "missing"
    assert by_name["src-none"]["has_smtp_password"] is False
    # 永不序列化密码
    assert "password" not in by_name["src-enc"]
    assert "password_enc" not in by_name["src-enc"]
