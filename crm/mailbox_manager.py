"""
CRM Mailbox Manager — env-backed (pre-V8) → DB-backed (V8) mailbox resolution.

After schema V8:
  - `mailboxes` table holds every SMTP account. `default`/`zoho` are seeded
    from env at migration time with password_enc=NULL, so resolution returns
    byte-identical credentials to the pre-V8 env behavior (zero change).
  - Passwords are Fernet-encrypted under FTA_MAILBOX_ENC_KEY. When the key is
    missing, encrypt/decrypt return None → admin password endpoints return 503
    (Chinese message) and sending falls back to the env password — never a
    crash. New accounts added without a key simply can't store a password.
  - Resolver is DB-first; the PASSWORD env fallback applies ONLY to the
    reserved `default`/`zoho` names (CRM-002). A custom mailbox missing a
    decryptable token resolves an EMPTY password — it never inherits the
    NetEase/Zoho env credentials. Field env-fill for custom boxes uses an
    empty profile (host/port/use_tls keep sane defaults, no credentials).
  - `credential_source` (OPT-005) reports where the password came from:
    `encrypted` (Fernet token) / `env` (reserved + env) / `missing`.

Blueprint 约定: 已发数量以 quota 表为准 (canonical anti-spam counters),
与发信强一致。密码永不序列化。
"""

from __future__ import annotations

import logging
import os
import smtplib
import socket
import sqlite3
import ssl
from typing import Optional

import config
from crm.models import MailboxInfo

logger = logging.getLogger(__name__)

# 内置邮箱名：env 管理 + 禁止删除。其余名字对用户开放。
RESERVED_NAMES = frozenset({"default", "zoho"})


# ═══════════════════════════════════════════════════════════════════════════
# Fernet 加解密（FTA_MAILBOX_ENC_KEY）
# ═══════════════════════════════════════════════════════════════════════════


def _fernet():
    key = os.getenv("FTA_MAILBOX_ENC_KEY")
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet

        return Fernet(key.encode("utf-8"))
    except Exception as e:
        logger.warning("FTA_MAILBOX_ENC_KEY 无效: %s", e)
        return None


def encryption_available() -> bool:
    return _fernet() is not None


def encrypt_password(plaintext: Optional[str]) -> Optional[str]:
    """Fernet-encrypt a password. Returns None if no key or empty input."""
    f = _fernet()
    if f is None or not plaintext:
        return None
    try:
        return f.encrypt(plaintext.encode("utf-8")).decode("utf-8")
    except Exception:
        logger.exception("密码加密失败")
        return None


def decrypt_password(token: Optional[str]) -> Optional[str]:
    """Fernet-decrypt a stored token. Returns None on missing key / failure."""
    f = _fernet()
    if f is None or not token:
        return None
    try:
        return f.decrypt(token.encode("utf-8")).decode("utf-8")
    except Exception:
        logger.warning("密码解密失败（密钥不匹配？）")
        return None


# ═══════════════════════════════════════════════════════════════════════════
# 解析器（DB-first，env 回退）
# ═══════════════════════════════════════════════════════════════════════════


def _empty_settings() -> dict:
    """Empty profile for custom mailboxes — must NEVER fall back to env (CRM-002)."""
    return {
        "display_name": "",
        "host": "",
        "port": 587,
        "use_tls": True,
        "username": "",
        "password": "",
        "from_email": "",
        "from_name": "",
        "daily_limit": 0,
    }


def _env_settings(name: str) -> dict:
    """Env profile settings for the RESERVED mailboxes only (pre-V8 behavior).

    Any other name returns an empty profile: a custom mailbox must never
    inherit the default NetEase or Zoho credentials (CRM-002).
    """
    if name == "default":
        return {
            "display_name": "NetEase 163",
            "host": config.SMTP_HOST,
            "port": config.SMTP_PORT,
            "use_tls": bool(config.SMTP_USE_TLS),
            "username": config.SMTP_USERNAME,
            "password": config.SMTP_PASSWORD,
            "from_email": config.SMTP_FROM_EMAIL,
            "from_name": config.SMTP_FROM_NAME,
            "daily_limit": config.DAILY_EMAIL_LIMIT,
        }
    if name == "zoho":
        return {
            "display_name": "Zoho 个人邮箱",
            "host": config.ZOHO_SMTP_HOST,
            "port": config.ZOHO_SMTP_PORT,
            "use_tls": bool(config.ZOHO_SMTP_USE_TLS),
            "username": config.ZOHO_SMTP_USERNAME,
            "password": config.ZOHO_SMTP_PASSWORD,
            "from_email": config.ZOHO_SMTP_FROM_EMAIL,
            "from_name": config.ZOHO_SMTP_FROM_NAME,
            "daily_limit": config.ZOHO_DAILY_LIMIT,
        }
    return _empty_settings()


def _row(conn: sqlite3.Connection, name: str) -> Optional[dict]:
    try:
        row = conn.execute("SELECT * FROM mailboxes WHERE name = ?", (name,)).fetchone()
    except sqlite3.Error:
        return None  # 表不存在（V8 前）→ 视为无行
    return dict(row) if row else None


def get_mailbox_row(name: str, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    """DB row for a mailbox name, or None. Opens a short read connection when
    no conn is given (sender/test endpoints don't hold one)."""
    if conn is not None:
        return _row(conn, name)
    try:
        db_conn = sqlite3.connect(str(config.DB_PATH))
        db_conn.row_factory = sqlite3.Row
        try:
            return _row(db_conn, name)
        finally:
            db_conn.close()
    except sqlite3.Error as e:
        logger.warning("get_mailbox_row(%s) failed: %s", name, e)
        return None


def resolve_smtp_settings(name: str, conn: Optional[sqlite3.Connection] = None) -> dict:
    """Resolve a mailbox's SMTP settings — DB-first, env fallback.

    Returns {display_name, host, port, use_tls, username, password, from_email,
    from_name, daily_limit}. No row → pure env (pre-V8 behavior).
    """
    env = _env_settings(name)
    row = get_mailbox_row(name, conn)
    if not row:
        return env

    def _or(field: str, env_val):
        v = row.get(field)
        return v if v not in (None, "") else env_val

    use_tls = row.get("use_tls")
    port = row.get("smtp_port")
    limit = row.get("daily_limit")

    password = decrypt_password(row.get("password_enc")) if row.get("password_enc") else None
    # 密码 env 回退仅限保留邮箱：播种行 password_enc=NULL → 与 env 字节一致。
    # 自定义邮箱缺密码/密钥失效 → 密码保持空，绝不读 default/zoho 的 env 密码（CRM-002）。
    if not password and name in RESERVED_NAMES:
        password = env["password"]

    return {
        "display_name": row.get("display_name") or env["display_name"],
        "host": _or("smtp_host", env["host"]),
        "port": int(port) if port else env["port"],
        "use_tls": bool(use_tls) if use_tls is not None else env["use_tls"],
        "username": _or("username", env["username"]),
        "password": password or "",
        "from_email": _or("from_email", env["from_email"]),
        "from_name": _or("from_name", env["from_name"]),
        "daily_limit": int(limit) if limit else env["daily_limit"],
    }


# ═══════════════════════════════════════════════════════════════════════════
# 配额查询
# ═══════════════════════════════════════════════════════════════════════════


def _sent_today(conn: sqlite3.Connection, table: str, mailbox: Optional[str] = None) -> int:
    if table == "sending_quotas":
        row = conn.execute(
            "SELECT email_count FROM sending_quotas WHERE date=date('now')"
        ).fetchone()
        return row["email_count"] if row else 0
    row = conn.execute(
        "SELECT email_count FROM mailbox_quotas WHERE date=date('now') AND mailbox=?",
        (mailbox,),
    ).fetchone()
    return row["email_count"] if row else 0


def _quota_table_for(name: str) -> str:
    """'default' 走 sending_quotas（网易批量），其余走 mailbox_quotas."""
    return "sending_quotas" if name == "default" else "mailbox_quotas"


# ═══════════════════════════════════════════════════════════════════════════
# 读取
# ═══════════════════════════════════════════════════════════════════════════


def _build_info(conn: sqlite3.Connection, name: str, row: Optional[dict]) -> MailboxInfo:
    settings = resolve_smtp_settings(name, conn) if row else _env_settings(name)
    quota_table = _quota_table_for(name)
    sent = _sent_today(conn, quota_table, mailbox=name)
    # 自定义邮箱未填 display_name 时显示自身 name，而不是继承 env 默认邮箱的显示名。
    display_name = settings["display_name"]
    if row and name not in RESERVED_NAMES and not row.get("display_name"):
        display_name = name
    # 凭证来源枚举（OPT-005）：可解密 token → encrypted；保留邮箱走 env 密码 → env；其余 → missing。
    if row and row.get("password_enc") and decrypt_password(row.get("password_enc")):
        credential_source = "encrypted"
    elif settings["password"] and name in RESERVED_NAMES:
        credential_source = "env"
    else:
        credential_source = "missing"
    return MailboxInfo(
        name=name,
        display_name=display_name,
        smtp_host=settings["host"],
        smtp_port=settings["port"],
        use_tls=settings["use_tls"],
        username=settings["username"],
        from_email=settings["from_email"],
        from_name=settings["from_name"],
        has_smtp_password=bool(settings["password"]),
        daily_limit=settings["daily_limit"],
        sent_today=sent,
        remaining=max(0, settings["daily_limit"] - sent),
        quota_table=quota_table,
        is_active=int(row["is_active"]) if row else 1,
        source="db" if row else "env",
        credential_source=credential_source,
    )


def get_mailbox(conn: sqlite3.Connection, name: str) -> Optional[MailboxInfo]:
    row = get_mailbox_row(name, conn)
    if not row:
        return None
    return _build_info(conn, name, row)


def list_mailboxes(conn: sqlite3.Connection) -> list[MailboxInfo]:
    """All configured mailboxes: DB rows first, then env profiles (default/zoho)
    that have no DB row yet (empty-env installs → placeholder shells, matching
    the pre-V8 contract that default/zoho always appear)."""
    boxes: list[MailboxInfo] = []
    seen: set[str] = set()
    try:
        rows = conn.execute("SELECT name FROM mailboxes ORDER BY name").fetchall()
    except sqlite3.Error:
        rows = []
    for r in rows:
        boxes.append(_build_info(conn, r["name"], get_mailbox_row(r["name"], conn)))
        seen.add(r["name"])
    for name in ("default", "zoho"):
        if name not in seen:
            boxes.append(_build_info(conn, name, None))
            seen.add(name)
    return boxes


# ═══════════════════════════════════════════════════════════════════════════
# 写路径（M5）
# ═══════════════════════════════════════════════════════════════════════════
# 约定同 tags：用户错误抛 ValueError（router 映射 409/404），单事务提交。
# 密码只在写请求里出现（write-only），读取永远只有 has_smtp_password。


def create_mailbox(
    conn: sqlite3.Connection,
    *,
    name: str,
    smtp_host: str,
    smtp_port: int = 587,
    use_tls: bool = True,
    username: str,
    password: Optional[str] = None,
    display_name: Optional[str] = None,
    from_email: Optional[str] = None,
    from_name: Optional[str] = None,
    daily_limit: Optional[int] = None,
) -> MailboxInfo:
    name = (name or "").strip()
    if not name:
        raise ValueError("邮箱名不能为空")
    if name in RESERVED_NAMES:
        raise ValueError(f"「{name}」为内置保留邮箱，请直接编辑该行或新建其他邮箱")
    if get_mailbox_row(name, conn):
        raise ValueError(f"邮箱「{name}」已存在")
    if not smtp_host or not username:
        raise ValueError("SMTP 服务器与用户名必填")

    enc = encrypt_password(password) if password else None
    conn.execute(
        """INSERT INTO mailboxes
           (name, display_name, smtp_host, smtp_port, use_tls, username,
            password_enc, from_email, from_name, daily_limit, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'db')""",
        (name, display_name, smtp_host, int(smtp_port), int(bool(use_tls)),
         username, enc, from_email or username, from_name, daily_limit),
    )
    conn.commit()
    return get_mailbox(conn, name)


def update_mailbox(
    conn: sqlite3.Connection,
    name: str,
    *,
    display_name: Optional[str] = None,
    smtp_host: Optional[str] = None,
    smtp_port: Optional[int] = None,
    use_tls: Optional[bool] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    from_email: Optional[str] = None,
    from_name: Optional[str] = None,
    daily_limit: Optional[int] = None,
    is_active: Optional[int] = None,
) -> Optional[MailboxInfo]:
    """Update a mailbox's fields (only provided ones). Returns None if missing."""
    if not get_mailbox_row(name, conn):
        return None
    sets, params = [], []
    field_map = {
        "display_name": display_name, "smtp_host": smtp_host,
        "smtp_port": smtp_port, "use_tls": use_tls, "username": username,
        "from_email": from_email, "from_name": from_name,
        "daily_limit": daily_limit, "is_active": is_active,
    }
    for col, val in field_map.items():
        if val is not None:
            sets.append(f"{col} = ?")
            params.append(val)
    if password is not None:
        enc = encrypt_password(password) if password else None
        sets.append("password_enc = ?")
        params.append(enc)
    if sets:
        sets.append("updated_at = datetime('now')")
        params.append(name)
        conn.execute(f"UPDATE mailboxes SET {', '.join(sets)} WHERE name = ?", params)
        conn.commit()
    return get_mailbox(conn, name)


def delete_mailbox(conn: sqlite3.Connection, name: str) -> bool:
    """Delete a mailbox. Reserved names and same-day-sent accounts are refused
    (ValueError). Returns True when a row was deleted."""
    if name in RESERVED_NAMES:
        raise ValueError(f"「{name}」为内置保留邮箱，不能删除")
    if _sent_today(conn, _quota_table_for(name), mailbox=name) > 0:
        raise ValueError(f"「{name}」今日已发送邮件，不能删除")
    cur = conn.execute("DELETE FROM mailboxes WHERE name = ?", (name,))
    conn.commit()
    if cur.rowcount == 0:
        raise ValueError("邮箱不存在")
    return True


# ═══════════════════════════════════════════════════════════════════════════
# 测试连接（stdlib smtplib，只连接不发送）
# ═══════════════════════════════════════════════════════════════════════════


def test_mailbox(conn: sqlite3.Connection, name: str) -> dict:
    """Connect + login to the mailbox's SMTP server. Sends NOTHING."""
    settings = resolve_smtp_settings(name, conn)
    host, port, use_tls = settings["host"], settings["port"], settings["use_tls"]
    if not host or not settings["username"] or not settings["password"]:
        return {"ok": False, "message": "SMTP 凭证未配置（邮箱未填密码，或未设置 FTA_MAILBOX_ENC_KEY）"}
    try:
        context = ssl.create_default_context() if use_tls else None
        if use_tls:  # STARTTLS on 587
            server = smtplib.SMTP(host, int(port), timeout=15)
            server.starttls(context=context)
        else:  # direct SSL on 465
            server = smtplib.SMTP_SSL(host, int(port), context=context, timeout=15)
        server.login(settings["username"], settings["password"])
        server.quit()
        return {"ok": True, "message": f"{host}:{port} 认证成功"}
    except smtplib.SMTPAuthenticationError:
        return {"ok": False, "message": "认证失败：用户名或密码错误"}
    except (smtplib.SMTPException, socket.error, OSError) as e:
        return {"ok": False, "message": f"连接失败：{e}"}
