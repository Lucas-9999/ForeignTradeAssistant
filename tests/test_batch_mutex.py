"""
V9 批次互斥测试 — batch_run_guards 原子 claim + 每日单批次。

背景：08-17 双批次并发重复发信（采集 finally 链 + cron + 启动 catch-up 三个
入口各自触发一次，且无互斥）。本测试覆盖：
- _claim_batch_run / _finish_batch_run（collector.scheduler）原子语义
- 卡死残留回收（>3h running → 允许重跑；刚启动 → 不回收）
- _run_scheduled_email_batch 同一天第二次触发直接跳过（不重复发信）

config.DB_PATH 由 conftest._isolate_production_db 自动重定向到临时库。
"""

import sqlite3

import pytest

from collector.scheduler import (
    _claim_batch_run,
    _finish_batch_run,
    _run_scheduled_email_batch,
)


def _open_guard_conn(db_path):
    """建一个只含 batch_run_guards 的最小库（等价于生产 V9 结构）。"""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS batch_run_guards (
            kind TEXT NOT NULL,
            date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at TIMESTAMP,
            PRIMARY KEY (kind, date)
        )"""
    )
    conn.commit()
    return conn


def test_claim_first_true_second_false(tmp_path, monkeypatch):
    """首次 claim 成功；同日同 kind 再 claim 拒绝（互斥核心语义）。"""
    import config

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "m.db")
    _open_guard_conn(config.DB_PATH).close()

    assert _claim_batch_run("email", "2026-08-17") is True
    assert _claim_batch_run("email", "2026-08-17") is False


def test_finish_done_blocks_same_day(tmp_path, monkeypatch):
    """跑完持久化为 done → 同日再触发仍拒绝（一天至多一批次）。"""
    import config

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "m.db")
    _open_guard_conn(config.DB_PATH).close()

    assert _claim_batch_run("email", "2026-08-17") is True
    _finish_batch_run("email", "2026-08-17")
    assert _claim_batch_run("email", "2026-08-17") is False


def test_different_kind_and_date_independent(tmp_path, monkeypatch):
    """不同通道（email/zoho）与不同日期互不阻塞（两通道按设计可并行）。"""
    import config

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "m.db")
    _open_guard_conn(config.DB_PATH).close()

    assert _claim_batch_run("email", "2026-08-17") is True
    assert _claim_batch_run("zoho", "2026-08-17") is True
    assert _claim_batch_run("email", "2026-08-18") is True


def test_stale_running_reclaimed(tmp_path, monkeypatch):
    """>3h 未 finish 的 running（崩溃残留）→ 允许重跑。"""
    import config

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "m.db")
    conn = _open_guard_conn(config.DB_PATH)
    conn.execute(
        "INSERT INTO batch_run_guards (kind, date, status, started_at) "
        "VALUES ('email', '2026-08-17', 'running', datetime('now','-5 hours'))"
    )
    conn.commit()
    conn.close()

    assert _claim_batch_run("email", "2026-08-17") is True


def test_fresh_running_not_reclaimed(tmp_path, monkeypatch):
    """刚启动的 running（<3h）→ 不回收，拒绝重跑。"""
    import config

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "m.db")
    conn = _open_guard_conn(config.DB_PATH)
    conn.execute(
        "INSERT INTO batch_run_guards (kind, date, status, started_at) "
        "VALUES ('email', '2026-08-17', 'running', CURRENT_TIMESTAMP)"
    )
    conn.commit()
    conn.close()

    assert _claim_batch_run("email", "2026-08-17") is False


def test_entry_point_skips_duplicate_trigger(tmp_path, monkeypatch):
    """同一天两次触发 _run_scheduled_email_batch → 实际只发信一次。

    2026-08-17 是周一（工作日），is_weekday 打桩放行；发信体打桩计数。
    第二次触发应被 batch_run_guards 挡住（skip），不重复发信。
    """
    import config

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "m.db")
    _open_guard_conn(config.DB_PATH).close()

    calls: list = []
    monkeypatch.setattr(
        "collector.scheduler._do_scheduled_email_batch", lambda: calls.append("run")
    )
    monkeypatch.setattr("collector.scheduler._is_weekday", lambda: True)

    _run_scheduled_email_batch()
    _run_scheduled_email_batch()
    assert calls == ["run"]  # 恰好一次
