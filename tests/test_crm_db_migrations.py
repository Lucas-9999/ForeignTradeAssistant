"""
CRM-004: 显式 schema migration 测试.

- get_connection() 不再隐式迁移（不创建缺失表、不盖章版本）
- _migrate_schema 版本门控：v8 库跳过；旧库升级并盖章 user_version=8
- get_schema_version / ensure_schema_current（落后 → RuntimeError）
- tools/migrate_db.py 备份前置检查 + 幂等迁移

写 API 一律临时 SQLite（conftest._isolate_production_db 已重定向 DB_PATH）。
"""

import sqlite3
import sys

import pytest

from crm.db import (
    DB_SCHEMA,
    SCHEMA_VERSION,
    _migrate_schema,
    build_database,
    ensure_schema_current,
    get_connection,
    get_schema_version,
)


def _build_base_db(db_path, stamp: int = 0):
    """建一个只有 DB_SCHEMA 基础表（无 tags/mailboxes）的库，可选盖章."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(DB_SCHEMA)
    conn.execute(f"PRAGMA user_version = {stamp}")
    conn.commit()
    conn.close()


# ── get_connection 不再隐式迁移 ────────────────────────────────────────────


def test_get_connection_does_not_create_missing_tables(tmp_path):
    """CRM-004: get_connection() 只设 pragma，绝不执行 DDL."""
    db_path = tmp_path / "base.db"
    _build_base_db(db_path)

    conn = get_connection(db_path)
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert "tags" not in tables
    assert "mailboxes" not in tables
    assert get_schema_version(db_path) == 0  # 从未盖章


def test_get_connection_raises_when_db_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        get_connection(tmp_path / "nope.db")


# ── _migrate_schema 版本门控 ───────────────────────────────────────────────


def test_migrate_upgrades_v0_to_v8_and_stamps(tmp_path):
    """旧库（无 tags/mailboxes，user_version=0）→ 升级到 V8 并盖章."""
    db_path = tmp_path / "v0.db"
    _build_base_db(db_path)

    conn = sqlite3.connect(str(db_path))
    _migrate_schema(conn)
    conn.close()

    assert get_schema_version(db_path) == SCHEMA_VERSION
    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "tags" in tables and "mailboxes" in tables
    finally:
        conn.close()


def test_migrate_idempotent(tmp_path):
    db_path = tmp_path / "idem.db"
    _build_base_db(db_path)

    conn = sqlite3.connect(str(db_path))
    _migrate_schema(conn)
    conn.close()
    v1 = get_schema_version(db_path)

    conn = sqlite3.connect(str(db_path))
    _migrate_schema(conn)  # 再跑 → 版本门控直接跳过
    conn.close()
    assert get_schema_version(db_path) == v1 == SCHEMA_VERSION


def test_migrate_skips_when_already_current(tmp_path):
    db_path = tmp_path / "cur.db"
    _build_base_db(db_path, stamp=SCHEMA_VERSION)

    conn = sqlite3.connect(str(db_path))
    _migrate_schema(conn)
    conn.close()
    assert get_schema_version(db_path) == SCHEMA_VERSION
    # tags 表仍未创建（当前版本跳过全部迁移，包括 CREATE TABLE）——证明被跳过
    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert "tags" not in tables


# ── ensure_schema_current ──────────────────────────────────────────────────


def test_ensure_schema_current_ok(tmp_path):
    db_path = tmp_path / "ok.db"
    _build_base_db(db_path, stamp=SCHEMA_VERSION)
    assert ensure_schema_current(db_path) == SCHEMA_VERSION


def test_ensure_schema_current_refuses_stale(tmp_path):
    db_path = tmp_path / "stale.db"
    _build_base_db(db_path)
    with pytest.raises(RuntimeError, match="schema 版本落后"):
        ensure_schema_current(db_path)


# ── tools/migrate_db.py 前置检查与幂等 ─────────────────────────────────────


def _run_migrate_tool(argv, monkeypatch, backups_dir=None) -> int:
    """以指定 argv 调用 tools/migrate_db.main()，返回其退出码.

    BACKUP_DIR 由调用方用 backups_dir 显式指定（隔离到临时目录，
    避免污染/依赖真实 data/backups）。
    """
    import tools.migrate_db as migrate_db

    if backups_dir is None:
        raise ValueError("backups_dir must be set to isolate the tool's backup glob")
    monkeypatch.setattr(migrate_db, "BACKUP_DIR", backups_dir)
    monkeypatch.setattr(sys, "argv", ["migrate_db.py", *argv])
    return migrate_db.main()


def test_migrate_tool_refuses_without_backup(tmp_path, monkeypatch):
    """CRM-004: 无指定 label 备份 → 中止，数据库保持原版本."""
    import config

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "prod.db")
    _build_base_db(config.DB_PATH)

    code = _run_migrate_tool(
        ["--target", f"v{SCHEMA_VERSION}", "--label", "pre_x"],
        monkeypatch,
        backups_dir=tmp_path / "backups",
    )
    assert code == 1
    assert get_schema_version(config.DB_PATH) == 0  # 未迁移


def test_migrate_tool_succeeds_with_backup(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "prod.db")
    _build_base_db(config.DB_PATH)
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / f"fta.db.pre_pre_x_{'20260817'}_000000").write_bytes(b"snap")

    code = _run_migrate_tool(
        ["--target", f"v{SCHEMA_VERSION}", "--label", "pre_x"],
        monkeypatch,
        backups_dir=tmp_path / "backups",
    )
    assert code == 0
    assert get_schema_version(config.DB_PATH) == SCHEMA_VERSION

    # 幂等：再跑一次 → 已最新，退出 0 且版本不变
    code = _run_migrate_tool(
        ["--target", f"v{SCHEMA_VERSION}", "--label", "pre_x"],
        monkeypatch,
        backups_dir=tmp_path / "backups",
    )
    assert code == 0
    assert get_schema_version(config.DB_PATH) == SCHEMA_VERSION


def test_migrate_tool_no_backup_check_escape_hatch(tmp_path, monkeypatch):
    """开发逃生门 --no-backup-check 可跳过备份要求（不推荐生产）。"""
    import config

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "dev.db")
    _build_base_db(config.DB_PATH)

    code = _run_migrate_tool(
        ["--target", f"v{SCHEMA_VERSION}", "--no-backup-check"],
        monkeypatch,
        backups_dir=tmp_path / "backups",
    )
    assert code == 0
    assert get_schema_version(config.DB_PATH) == SCHEMA_VERSION


def test_migrate_tool_rejects_non_latest_target(tmp_path, monkeypatch):
    """The cumulative migrator must not silently overshoot an older target."""
    import config

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "v8.db")
    _build_base_db(config.DB_PATH, stamp=8)

    code = _run_migrate_tool(
        ["--target", "v9", "--no-backup-check"],
        monkeypatch,
        backups_dir=tmp_path / "backups",
    )

    assert code == 1
    assert get_schema_version(config.DB_PATH) == 8


# ── V8 → V9: batch_run_guards（08-17 双批次互斥） ───────────────────────────


def test_migrate_v9_creates_batch_run_guards_and_stamps(tmp_path):
    """V8 库（无 batch_run_guards，user_version=8）→ 升级到 V9 建表 + 盖章."""
    db_path = tmp_path / "v8.db"
    _build_base_db(db_path, stamp=8)
    conn = sqlite3.connect(str(db_path))
    # 模拟 V8 时代：batch_run_guards 尚不存在（从 DB_SCHEMA 去掉）
    conn.execute("DROP TABLE IF EXISTS batch_run_guards")
    conn.commit()
    _migrate_schema(conn)
    conn.close()

    assert get_schema_version(db_path) == SCHEMA_VERSION
    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "batch_run_guards" in tables
        assert "mailboxes" in tables  # 存量 V6→V8 迁移仍按序执行
    finally:
        conn.close()


def test_migrate_v9_base_schema_contains_guard_table(tmp_path):
    """DB_SCHEMA 本身已含 batch_run_guards（全新库直接可用）。"""
    db_path = tmp_path / "base.db"
    _build_base_db(db_path, stamp=0)
    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "batch_run_guards" in tables
    finally:
        conn.close()


# ── V9 → V10: email reputation data layer ────────────────────────────────


REPUTATION_TABLES = {
    "email_suppressions",
    "reputation_events",
    "mailbox_health",
    "domain_health",
    "recipient_domain_metrics",
}


DELIVERY_AUTHORIZATION_TABLE = "delivery_authorizations"


def test_migrate_v12_creates_delivery_authorizations_and_stamps(tmp_path):
    """V12 库升级后补齐 P11 授权表、唯一索引与不可变范围触发器。"""
    db_path = tmp_path / "v12.db"
    _build_base_db(db_path, stamp=12)
    conn = sqlite3.connect(str(db_path))
    conn.execute("DROP TABLE IF EXISTS delivery_authorizations")
    conn.execute("DROP INDEX IF EXISTS uq_delivery_authorizations_active_scope")
    conn.execute("PRAGMA user_version = 12")
    conn.commit()

    _migrate_schema(conn)
    names = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
        ).fetchall()
    }
    conn.close()

    assert get_schema_version(db_path) == SCHEMA_VERSION == 14
    assert DELIVERY_AUTHORIZATION_TABLE in names
    assert "uq_delivery_authorizations_active_scope" in names


def test_fresh_database_contains_delivery_authorization_table(tmp_path):
    conn = build_database(tmp_path / "fresh.db")
    try:
        names = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index', 'trigger')"
            ).fetchall()
        }
    finally:
        conn.close()
    assert DELIVERY_AUTHORIZATION_TABLE in names
    assert "uq_delivery_authorizations_active_scope" in names
    assert "trg_delivery_authorizations_scope_immutable" in names


def test_migrate_v13_adds_delivery_authorization_scope_immutability(tmp_path):
    db_path = tmp_path / "v13.db"
    _build_base_db(db_path, stamp=13)
    conn = sqlite3.connect(str(db_path))
    conn.execute("DROP TRIGGER IF EXISTS trg_delivery_authorizations_scope_immutable")
    conn.execute("PRAGMA user_version = 13")
    conn.commit()
    _migrate_schema(conn)
    triggers = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }
    conn.close()
    assert get_schema_version(db_path) == SCHEMA_VERSION == 14
    assert "trg_delivery_authorizations_scope_immutable" in triggers


def test_migrate_v10_creates_only_phase1_reputation_tables_and_stamps(tmp_path):
    """V9 → V10 adds the five approved P1 tables without later-phase tables."""
    db_path = tmp_path / "v9.db"
    _build_base_db(db_path, stamp=9)
    conn = sqlite3.connect(str(db_path))
    for table in REPUTATION_TABLES:
        conn.execute(f'DROP TABLE IF EXISTS "{table}"')
    conn.commit()

    _migrate_schema(conn)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()

    assert get_schema_version(db_path) == SCHEMA_VERSION
    assert REPUTATION_TABLES <= tables
    assert "smtp_events" not in tables
    assert "ip_health" not in tables


def test_reputation_invariants_reject_invalid_direct_sql(tmp_path):
    """P1 persistence invariants also protect callers that use raw SQL."""
    db_path = tmp_path / "reputation-invariants.db"
    conn = build_database(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="temporary suppression"):
            conn.execute(
                """INSERT INTO email_suppressions
                   (email, domain, reason, source, is_permanent)
                   VALUES ('buyer@example.com', 'example.com', 'invalid_email',
                           'raw-sql-test', 0)"""
            )

        with pytest.raises(sqlite3.IntegrityError, match="suppression reason"):
            conn.execute(
                """INSERT INTO email_suppressions
                   (email, domain, reason, source, is_permanent)
                   VALUES ('other@example.com', 'example.com', 'hard_buonse',
                           'raw-sql-test', 1)"""
            )

        with pytest.raises(sqlite3.IntegrityError, match="event type"):
            conn.execute(
                """INSERT INTO reputation_events
                   (entity_type, entity_id, event_type, severity, reason)
                   VALUES ('mailbox', 'default', 'MADE_UP_EVENT', 'warning', 'test')"""
            )
    finally:
        conn.close()


def test_migrate_v12_adds_reputation_invariant_triggers_to_v10(tmp_path):
    db_path = tmp_path / "v10.db"
    conn = build_database(db_path)
    trigger_names = {
        "trg_email_suppressions_validate_insert",
        "trg_email_suppressions_validate_update",
        "trg_email_suppressions_reason_insert",
        "trg_email_suppressions_reason_update",
        "trg_reputation_events_validate_insert",
        "trg_reputation_events_immutable_update",
        "trg_reputation_events_immutable_delete",
    }
    for trigger in trigger_names:
        conn.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
    conn.execute("PRAGMA user_version = 10")
    conn.commit()

    _migrate_schema(conn)
    installed = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
    }
    conn.close()

    assert get_schema_version(db_path) == SCHEMA_VERSION == 14
    assert trigger_names <= installed
