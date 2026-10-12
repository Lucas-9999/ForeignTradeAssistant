"""P11 operator authorization CLI only manipulates a temporary database."""

from datetime import datetime, timedelta, timezone

from crm.db import build_database


def test_cli_creates_lists_and_revokes_one_exact_authorization(tmp_path, monkeypatch, capsys):
    import config
    from tools.authorizations import main

    db_path = tmp_path / "authorizations-tool.db"
    conn = build_database(db_path)
    conn.execute(
        "INSERT INTO leads (id, company_name, email, status) VALUES (7, 'Buyer', 'buyer@example.com', 'new')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr("getpass.getuser", lambda: "test-operator")
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()

    assert main([
        "create", "--lead-id", "7", "--email", "Buyer@Example.com", "--mailbox", "default",
        "--purpose", "controlled_test", "--expires-at", expires_at,
        "--approval-note", "temporary fake-provider check",
    ]) == 0
    created = capsys.readouterr().out
    assert "created authorization #1" in created

    assert main(["list"]) == 0
    assert "active" in capsys.readouterr().out

    assert main(["revoke", "--id", "1"]) == 0
    assert "revoked authorization #1" in capsys.readouterr().out


def test_cli_refuses_email_not_owned_by_lead(tmp_path, monkeypatch, capsys):
    import config
    from tools.authorizations import main

    db_path = tmp_path / "authorizations-tool.db"
    conn = build_database(db_path)
    conn.execute(
        "INSERT INTO leads (id, company_name, email, status) VALUES (7, 'Buyer', 'buyer@example.com', 'new')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", db_path)

    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    assert main([
        "create", "--lead-id", "7", "--email", "other@example.com", "--mailbox", "default",
        "--purpose", "controlled_test", "--expires-at", expires_at,
        "--approval-note", "must not authorize a different recipient",
    ]) == 2
    assert "does not match" in capsys.readouterr().err
