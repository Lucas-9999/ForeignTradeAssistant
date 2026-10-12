"""
IMAP Diagnostics — verify mailbox connectivity for warm follow-up.

Usage:
    python -m engine.diagnostics              # Full diagnostics
    python -m engine.diagnostics --sent-only  # Only check Sent folder
    python -m engine.diagnostics --inbox-only # Only check INBOX
    python -m engine.diagnostics --preview 3  # Show last 3 messages from INBOX

This is a read-only diagnostic tool. It does NOT modify any data.
"""

from __future__ import annotations

import email
import imaplib
import logging
import ssl
import sys
from datetime import datetime, timedelta
from email.header import decode_header
from typing import Optional

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("engine.diagnostics")

# ═══════════════════════════════════════════════════════════════════════════
# Sent folder name candidates (ordered by likelihood)
# ═══════════════════════════════════════════════════════════════════════════

SENT_FOLDER_CANDIDATES = [
    "Sent",
    "Sent Items",
    "Sent Messages",
    "已发送",
    "Sent Mail",
    "INBOX.Sent",
    "INBOX.Sent Items",
    "[Gmail]/Sent Mail",
    '"Sent"',
    '"Sent Items"',
    '"Sent Messages"',
]


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _decode_header_value(header_value: Optional[str]) -> str:
    """Decode RFC 2047 encoded header value."""
    if not header_value:
        return ""
    try:
        parts = decode_header(header_value)
        decoded = []
        for part, charset in parts:
            if isinstance(part, bytes):
                decoded.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                decoded.append(part)
        return " ".join(decoded)
    except Exception:
        return str(header_value)


def _extract_body_preview(msg, max_chars: int = 200) -> str:
    """Extract a short plain-text preview from an email message."""
    body_parts = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        body_parts.append(payload.decode(charset, errors="replace"))
                except Exception:
                    pass
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                body_parts.append(payload.decode(charset, errors="replace"))
        except Exception:
            pass

    text = "\n".join(body_parts).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "..."
    return text


# ═══════════════════════════════════════════════════════════════════════════
# Diagnostics
# ═══════════════════════════════════════════════════════════════════════════


def check_imap_connectivity() -> dict:
    """Test basic IMAP connectivity and return folder listing.

    Returns:
        dict with keys: ok (bool), error (str|None),
        folders (list of {name, attributes, message_count}),
        host, port, username.
    """
    result = {
        "ok": False,
        "error": None,
        "folders": [],
        "host": config.IMAP_HOST,
        "port": config.IMAP_PORT,
        "username": config.IMAP_USERNAME,
    }

    if not config.IMAP_USERNAME or not config.IMAP_PASSWORD:
        result["error"] = "IMAP credentials not configured (FTA_IMAP_USERNAME / FTA_IMAP_PASSWORD)"
        return result

    conn = None
    try:
        from emailer.inbox import create_imap_connection

        conn = create_imap_connection(
            host=config.IMAP_HOST,
            port=config.IMAP_PORT,
            username=config.IMAP_USERNAME,
            password=config.IMAP_PASSWORD,
        )

        # List all folders
        status, folder_list = conn.list()
        if status == "OK":
            for line in folder_list:
                decoded = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
                # Parse: (attributes) separator "name"
                parts = decoded.split('" "')
                if len(parts) >= 1:
                    attrs_part = parts[0].strip()
                    # Extract name from last quoted section
                    name = ""
                    if len(parts) >= 2:
                        name = parts[-1].replace('"', "").strip()
                    else:
                        # Try to extract from the raw line
                        import re
                        m = re.search(r'"([^"]+)"\s*$', decoded)
                        if m:
                            name = m.group(1)

                    folder_info = {
                        "name": name or attrs_part,
                        "attributes": attrs_part,
                        "message_count": 0,
                        "selectable": True,
                    }

                    # Try to select and count
                    try:
                        s, data = conn.select(f'"{name}"', readonly=True)
                        if s == "OK":
                            folder_info["message_count"] = len(data[0].split()) if data[0] else 0
                        else:
                            folder_info["selectable"] = False
                    except Exception:
                        folder_info["selectable"] = False

                    result["folders"].append(folder_info)

        result["ok"] = True

    except imaplib.IMAP4.error as e:
        result["error"] = f"IMAP error: {e}"
        logger.error("IMAP connection failed: %s", e)
    except ssl.SSLError as e:
        result["error"] = f"SSL error: {e}"
        logger.error("SSL error: %s", e)
    except Exception as e:
        result["error"] = f"Unexpected error: {type(e).__name__}: {e}"
        logger.exception("IMAP diagnostics exception")
    finally:
        if conn:
            try:
                conn.close()
                conn.logout()
            except Exception:
                pass

    return result


def detect_sent_folder(conn: imaplib.IMAP4_SSL = None, folders: list[dict] = None) -> Optional[dict]:
    """Detect the Sent folder from available folders.

    Args:
        conn: Active IMAP connection (optional — used if folders not provided).
        folders: Pre-fetched folder list from check_imap_connectivity().

    Returns:
        dict with name, message_count, or None if not found.
    """
    if folders is None:
        if conn is None:
            return None
        folders = []
        status, listing = conn.list()
        if status == "OK":
            for line in listing:
                decoded = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
                import re
                m = re.search(r'"([^"]+)"\s*$', decoded)
                name = m.group(1) if m else decoded
                folders.append({"name": name, "message_count": 0})

    # Try candidates in order
    for candidate in SENT_FOLDER_CANDIDATES:
        for f in folders:
            if f["name"] == candidate or f["name"].lower() == candidate.lower():
                # Try to select and get count
                count = f.get("message_count", 0)
                if conn and count == 0:
                    try:
                        s, data = conn.select(f'"{f["name"]}"', readonly=True)
                        if s == "OK":
                            count = len(data[0].split()) if data[0] else 0
                    except Exception:
                        pass
                return {"name": f["name"], "message_count": count}

    # Fallback: fuzzy match on "sent" in folder name
    for f in folders:
        name_lower = f["name"].lower()
        if "sent" in name_lower and "sent" == name_lower.split("/")[-1].strip('"'):
            count = f.get("message_count", 0)
            return {"name": f["name"], "message_count": count}

    return None


def preview_inbox(count: int = 5) -> list[dict]:
    """Fetch the most recent messages from INBOX for preview.

    Args:
        count: Number of recent messages to fetch.

    Returns:
        List of dicts with from, subject, date, body_preview.
    """
    results = []

    if not config.IMAP_USERNAME or not config.IMAP_PASSWORD:
        return results

    conn = None
    try:
        from emailer.inbox import create_imap_connection

        conn = create_imap_connection(
            host=config.IMAP_HOST,
            port=config.IMAP_PORT,
            username=config.IMAP_USERNAME,
            password=config.IMAP_PASSWORD,
        )
        conn.select("INBOX", readonly=True)

        status, message_ids = conn.search(None, "ALL")
        if status != "OK":
            return results

        msg_ids = message_ids[0].split()
        # Get last N messages (newest first)
        recent_ids = msg_ids[-count:] if len(msg_ids) > count else msg_ids
        recent_ids = list(reversed(recent_ids))

        for mid in recent_ids:
            try:
                status, msg_data = conn.fetch(mid, "(RFC822)")
                if status != "OK":
                    continue
                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                results.append({
                    "from": _decode_header_value(msg.get("From", "")),
                    "subject": _decode_header_value(msg.get("Subject", "")),
                    "date": str(msg.get("Date", "")),
                    "body_preview": _extract_body_preview(msg, 200),
                })
            except Exception as e:
                logger.warning("Error previewing message %s: %s", mid, e)

    except Exception as e:
        logger.error("Preview failed: %s", e)
    finally:
        if conn:
            try:
                conn.close()
                conn.logout()
            except Exception:
                pass

    return results


def run_diagnostics(
    check_inbox: bool = True,
    check_sent: bool = True,
    preview_count: int = 0,
) -> dict:
    """Run full IMAP diagnostics and return structured results.

    Args:
        check_inbox: Check INBOX folder.
        check_sent: Detect and check Sent folder.
        preview_count: If > 0, fetch this many recent INBOX messages.

    Returns:
        Full diagnostics dict suitable for JSON serialization or printing.
    """
    report = {
        "timestamp": datetime.now().isoformat(),
        "host": config.IMAP_HOST,
        "port": config.IMAP_PORT,
        "username": config.IMAP_USERNAME,
        "connection": {"ok": False, "error": None, "folder_count": 0},
        "inbox": None,
        "sent": None,
        "previews": [],
        "verdict": "NOT TESTED",
    }

    # ── Step 1: Connection + folder listing ──────────────────────────────
    conn_result = check_imap_connectivity()
    report["connection"] = {
        "ok": conn_result["ok"],
        "error": conn_result["error"],
        "folder_count": len(conn_result["folders"]),
        "folders": conn_result["folders"],
    }

    if not conn_result["ok"]:
        report["verdict"] = f"FAILED: {conn_result['error']}"
        return report

    # ── Step 2: INBOX check ─────────────────────────────────────────────
    if check_inbox:
        inbox_folder = None
        for f in conn_result["folders"]:
            if f["name"].upper() == "INBOX":
                inbox_folder = f
                break

        if inbox_folder:
            report["inbox"] = {
                "found": True,
                "name": inbox_folder["name"],
                "message_count": inbox_folder.get("message_count", 0),
            }
        else:
            report["inbox"] = {"found": False}

    # ── Step 3: Sent folder detection ──────────────────────────────────
    if check_sent:
        # Try to connect if we have credentials
        sent_result = None
        if config.IMAP_USERNAME and config.IMAP_PASSWORD:
            conn = None
            try:
                from emailer.inbox import create_imap_connection

                conn = create_imap_connection(
                    host=config.IMAP_HOST,
                    port=config.IMAP_PORT,
                    username=config.IMAP_USERNAME,
                    password=config.IMAP_PASSWORD,
                )
                sent_result = detect_sent_folder(
                    conn=conn, folders=conn_result["folders"]
                )
            except Exception as e:
                logger.warning("Sent folder detection failed: %s", e)
            finally:
                if conn:
                    try:
                        conn.close()
                        conn.logout()
                    except Exception:
                        pass
        else:
            # Try with folder list only (no message count)
            sent_result = detect_sent_folder(folders=conn_result["folders"])

        report["sent"] = (
            {"found": True, "name": sent_result["name"], "message_count": sent_result["message_count"]}
            if sent_result
            else {"found": False}
        )

    # ── Step 4: Preview (optional) ─────────────────────────────────────
    if preview_count > 0:
        report["previews"] = preview_inbox(count=preview_count)

    # ── Step 5: Verdict ────────────────────────────────────────────────
    issues = []
    if check_inbox and (not report.get("inbox") or not report["inbox"]["found"]):
        issues.append("INBOX not accessible")
    if check_sent and (not report.get("sent") or not report["sent"]["found"]):
        issues.append("Sent folder not detected")

    if not issues:
        report["verdict"] = "OK — IMAP fully functional for warm follow-up"
    elif len(issues) == 1 and "Sent" in issues[0]:
        report["verdict"] = f"WARNING: {issues[0]} (warm follow-up can use DB instead)"
    else:
        report["verdict"] = f"ISSUES: {'; '.join(issues)}"

    return report


def print_diagnostics(report: dict):
    """Pretty-print the diagnostics report to stdout."""
    print()
    print("=" * 60)
    print("  IMAP Diagnostics Report")
    print("=" * 60)

    conn = report["connection"]
    print(f"  Server:     {report['host']}:{report['port']}")
    print(f"  Username:   {report['username']}")
    print(f"  Connection: {'✓ OK' if conn['ok'] else '✗ FAILED'}")
    if conn["error"]:
        print(f"  Error:      {conn['error']}")

    if conn["ok"]:
        print(f"  Folders:    {conn['folder_count']} found")

        # INBOX
        if report.get("inbox"):
            ib = report["inbox"]
            if ib["found"]:
                print(f"\n  INBOX:      ✓ {ib['message_count']} messages")
            else:
                print(f"\n  INBOX:      ✗ NOT FOUND")

        # Sent
        if report.get("sent"):
            st = report["sent"]
            if st["found"]:
                print(f"  Sent:       ✓ \"{st['name']}\" — {st['message_count']} messages")
            else:
                print(f"  Sent:       ✗ Not detected")
                print(f"  Candidates tried: {', '.join(SENT_FOLDER_CANDIDATES[:5])}...")

        # Folder listing
        if conn["folders"] and len(conn["folders"]) <= 20:
            print(f"\n  Folder listing:")
            for f in conn["folders"]:
                sel = "✓" if f.get("selectable", True) else "✗"
                print(f"    [{sel}] {f['name']} ({f.get('message_count', '?')} msgs)")

        # Previews
        if report.get("previews"):
            print(f"\n  Recent INBOX previews ({len(report['previews'])}):")
            for i, p in enumerate(report["previews"], 1):
                print(f"    [{i}] From: {p['from'][:60]}")
                print(f"        Subj: {p['subject'][:80]}")
                if p["body_preview"]:
                    print(f"        Body: {p['body_preview'][:100]}")

    print(f"\n  Verdict: {report['verdict']}")
    print("=" * 60)
    print()


# ═══════════════════════════════════════════════════════════════════════════
# CLI entry point
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="IMAP Diagnostics — verify mailbox connectivity for warm follow-up"
    )
    parser.add_argument(
        "--inbox-only", action="store_true",
        help="Only check INBOX (skip Sent folder detection)",
    )
    parser.add_argument(
        "--sent-only", action="store_true",
        help="Only check Sent folder (skip INBOX)",
    )
    parser.add_argument(
        "--preview", type=int, default=0, metavar="N",
        help="Show last N messages from INBOX (requires IMAP access)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output as JSON (machine-readable)",
    )

    args = parser.parse_args()

    check_inbox = not args.sent_only
    check_sent = not args.inbox_only

    report = run_diagnostics(
        check_inbox=check_inbox,
        check_sent=check_sent,
        preview_count=args.preview,
    )

    if args.json:
        import json
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print_diagnostics(report)
