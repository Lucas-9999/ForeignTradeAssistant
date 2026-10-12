"""
FTA 显式数据库迁移器 (CRM-004) — 取代 get_connection 的隐式自动迁移.

生产守则 (AGENTS.md): 任何 DDL 前必须备份 + integrity_check. 本工具强制:
  1. 数据库存在
  2. PRAGMA integrity_check = ok
  3. 指定 label 的备份已存在 (data/backups/fta.db.pre_<label>_*)
  4. 打印 当前版本 → 目标版本
  5. 执行幂等迁移并盖章 PRAGMA user_version = SCHEMA_VERSION
  6. 校验结果

幂等: 已是最新版本时直接退出 0, 不重复跑 DDL.

Usage:
    python tools/backup_db.py --label pre_xxx --check
    python tools/migrate_db.py --target v12 --label pre_xxx --check
    python tools/migrate_db.py --no-backup-check   # 开发逃生门 (不推荐生产)
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# Windows 控制台默认 cp936, 中文输出会乱码 — 强制 UTF-8.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import config  # noqa: E402
from crm.db import SCHEMA_VERSION, _migrate_schema, get_schema_version  # noqa: E402

BACKUP_DIR = config.DATA_DIR / "backups"


def _integrity(db_path: Path) -> str:
    """只读: PRAGMA integrity_check, 返回 'ok' 或错误文本."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute("PRAGMA integrity_check").fetchone()
            return row[0] if row else "ok"
        finally:
            con.close()
    except sqlite3.Error as e:
        return f"ERROR: {e}"


def _backup_exists(label: str) -> bool:
    """指定 label 的备份快照是否已存在 (data/backups/fta.db.pre_<label>_*)."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
    return bool(list(BACKUP_DIR.glob(f"fta.db.pre_{safe}_*")))


def main() -> int:
    ap = argparse.ArgumentParser(description="FTA 显式数据库迁移 (CRM-004)")
    ap.add_argument(
        "--target", default=f"v{SCHEMA_VERSION}",
        help=f"目标版本, 如 v{SCHEMA_VERSION} (默认当前 SCHEMA_VERSION)",
    )
    ap.add_argument(
        "--label", default="pre_migration",
        help="要求存在的备份标签 (必须先运行 tools/backup_db.py --label <label> --check)",
    )
    ap.add_argument(
        "--no-backup-check", action="store_true",
        help="跳过备份前置检查 (仅开发用, 不推荐生产)",
    )
    args = ap.parse_args()

    target = args.target.lower().lstrip("v")
    try:
        target_v = int(target)
    except ValueError:
        print(f"[error] 非法 target: {args.target} (应为 vN, 如 v{SCHEMA_VERSION})", file=sys.stderr)
        return 1
    if target_v != SCHEMA_VERSION:
        print(
            f"[error] 累积迁移器仅支持最新目标 v{SCHEMA_VERSION}；"
            f"收到 v{target_v}",
            file=sys.stderr,
        )
        return 1

    if not config.DB_PATH.exists():
        print(f"[error] 找不到数据库: {config.DB_PATH}", file=sys.stderr)
        return 1

    # ── 前置检查 ─────────────────────────────────────────────────────────
    current = get_schema_version(config.DB_PATH)
    print(f"[info] 当前版本 v{current} → 目标 v{target_v}")

    if current >= target_v:
        print(f"[ok] 已是最新版本 v{current}（≥ v{target_v}），无需迁移。")
        return 0

    if not args.no_backup_check:
        if _integrity(config.DB_PATH) != "ok":
            print("[error] 数据库 integrity_check 失败，中止迁移", file=sys.stderr)
            return 1
        if not _backup_exists(args.label):
            print(
                f"[error] 未找到标签 {args.label!r} 的备份。请先运行:\n"
                f"  python tools/backup_db.py --label {args.label} --check",
                file=sys.stderr,
            )
            return 1

    # ── 执行幂等迁移并盖章 ──────────────────────────────────────────────
    try:
        conn = sqlite3.connect(str(config.DB_PATH))
        try:
            _migrate_schema(conn)
            actual = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(f"[error] 迁移失败: {e}", file=sys.stderr)
        return 1

    if actual < target_v:
        print(
            f"[error] 迁移后版本 v{actual} 仍未达到目标 v{target_v}，请检查迁移逻辑",
            file=sys.stderr,
        )
        return 1
    print(f"[ok] 迁移完成：v{current} → v{actual}（{config.DB_PATH}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
