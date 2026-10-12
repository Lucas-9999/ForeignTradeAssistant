#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把仓库根目录的 海关数据.xlsx(孚盟数据)分别导入:

1. FTA 邮箱库      Foreign Trade Assistant/data/fta.db  → leads(source='孚盟数据')
2. WhatsApp 联系人库 WhatsApp Assistant/data/wa.db       → leads(source='孚盟数据')

字段映射(海关数据列 → 两库字段):
  联系人         → contact_name
  职位           → notes 开头("职位: …"),并尽力从 "At X" 片段提取公司名
  邮箱           → email  (FTA 主键去重,小写归一)
  电话           → phone  (FTA 原始值) / whatsapp_e164 (WA, 经 normalize_phone 规范化)
  Facebook/Twitter/LinkedIn → notes(仅非空项)
  国家/地区       → country(ISO 代码 → 英文全名,与库内既有英文名一致)

去重:
  - FTA: 按 email 小写去重(先查库内既有 email,跳过已存在)。
  - WA:  按 whatsapp_e164 去重(repo.WaRepo.upsert_lead_many 的 INSERT OR IGNORE + merge),
         同一号码多联系人只保留一条(号码即通道)。

回滚: 全部新增行都有 source='孚盟数据',
      DELETE FROM leads WHERE source='孚盟数据' 即可精确回滚。
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parents[2]          # MoMa Project/
CUSTOMS_XLSX = ROOT / "海关数据.xlsx"
FTA_DB = ROOT / "Foreign Trade Assistant" / "data" / "fta.db"
WA_DB = ROOT / "WhatsApp Assistant" / "data" / "wa.db"
WA_PKG = ROOT / "WhatsApp Assistant"

# ISO 代码 → 英文全名(与 FTA / WA 库内 country 列既有英文名一致)
COUNTRY_NAME = {
    "US": "United States",
    "CA": "Canada",
    "IN": "India",
    "UK": "United Kingdom",
    "MX": "Mexico",
}

# 职位里 "At X" 片段 → 公司名(尽力而为,仅当片段明显是公司名时)
_AT_COMPANY_RE = re.compile(r"\b[Aa]t\s+(.+)$")


def extract_company(position: str | None) -> str | None:
    """从职位文本提取公司名,失败返回 None。"""
    if not position:
        return None
    m = _AT_COMPANY_RE.search(position.strip())
    if not m:
        return None
    comp = m.group(1).strip()
    # 去掉括号注释与 "Nasdaq …" 之类尾缀,保留干净的公司名
    comp = re.sub(r"\s*\([^)]*\)", "", comp)
    comp = re.sub(r"\s+[Nn]asdaq.*$", "", comp).strip()
    # 明显非公司名的常见误提取(如 "Stay At Home Mom")
    if not comp or len(comp.split()) > 6 or any(
        w in comp.lower() for w in ("home mom", "stay at home", "the moment")
    ):
        return None
    return comp


def build_notes(position, facebook, twitter, linkedin) -> str | None:
    """职位 + 社交媒体 → notes(仅非空项,以 ' | ' 拼接)。"""
    parts = []
    if position:
        parts.append(f"职位: {position}")
    if facebook:
        parts.append(f"Facebook: {facebook}")
    if twitter:
        parts.append(f"Twitter: {twitter}")
    if linkedin:
        parts.append(f"LinkedIn: {linkedin}")
    return " | ".join(parts) if parts else None


def read_customs():
    """读 Excel,返回清洗后的 dict 列表。"""
    wb = openpyxl.load_workbook(CUSTOMS_XLSX, read_only=True)
    rows = list(wb.worksheets[0].iter_rows(values_only=True))
    wb.close()

    records = []
    for r in rows[1:]:
        if not any(r):
            continue
        name = str(r[0]).strip() if r[0] else None
        position = str(r[1]).strip() if r[1] else None
        email = str(r[2]).strip().lower() if r[2] else None
        phone = str(r[3]).strip() if r[3] else None
        facebook = str(r[4]).strip() if r[4] else None
        twitter = str(r[5]).strip() if r[5] else None
        linkedin = str(r[6]).strip() if r[6] else None
        country_raw = str(r[7]).strip().upper() if r[7] else None
        records.append({
            "name": name, "position": position, "email": email, "phone": phone,
            "facebook": facebook, "twitter": twitter, "linkedin": linkedin,
            "country": COUNTRY_NAME.get(country_raw) if country_raw else None,
        })
    return records


def import_fta(records):
    """导入 FTA 邮箱库。按 email 去重,返回 (created, skipped)。"""
    conn = sqlite3.connect(FTA_DB)
    conn.execute("PRAGMA journal_mode=WAL")

    # 库内既有 email(小写) — 跳过已存在
    emails = [r["email"] for r in records if r["email"]]
    existing = set()
    for i in range(0, len(emails), 500):
        chunk = emails[i:i + 500]
        ph = ",".join("?" for _ in chunk)
        existing.update(
            row[0] for row in conn.execute(
                f"SELECT LOWER(email) FROM leads WHERE LOWER(email) IN ({ph})", chunk
            )
        )

    rows = []
    skipped = 0
    seen = set()
    for r in records:
        email = r["email"]
        if not email:
            skipped += 1          # 无邮箱 → 不进邮箱库
            continue
        if email in existing or email in seen:
            skipped += 1
            continue
        seen.add(email)
        rows.append((
            extract_company(r["position"]),
            r["name"],
            email,
            r["phone"],
            None,                   # whatsapp 不填(邮箱通道)
            "孚盟数据",
            build_notes(r["position"], r["facebook"], r["twitter"], r["linkedin"]),
            r["country"],
            "new",
        ))

    created = 0
    if rows:
        cur = conn.executemany(
            """INSERT INTO leads
               (company_name, contact_name, email, phone, whatsapp, source,
                notes, country, status, discovered_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            rows,
        )
        conn.commit()
        created = cur.rowcount
    conn.close()
    return created, skipped


def import_wa(records):
    """导入 WhatsApp 联系人库。经 normalize_phone 规范化,按 e164 去重。"""
    sys.path.insert(0, str(WA_PKG))
    from whatsapp.models import LeadRecord
    from whatsapp.normalize import normalize_phone
    from whatsapp.repo import WaRepo

    conn = sqlite3.connect(WA_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    leads = []
    skipped = 0
    for r in records:
        if not r["phone"]:
            skipped += 1
            continue
        n = normalize_phone(r["phone"])
        if not n.e164:
            skipped += 1          # 号码无法规范化 → 不进发送池
            continue
        leads.append(LeadRecord(
            whatsapp_e164=n.e164,
            raw_number=r["phone"],
            country_code=n.cc,
            country=r["country"] or n.country,
            company_name=extract_company(r["position"]),
            contact_name=r["name"],
            email=r["email"],
            source="孚盟数据",
            phone_type=n.phone_type,
            notes=build_notes(r["position"], r["facebook"], r["twitter"], r["linkedin"]),
            status="ready" if n.phone_type == "mobile" else "review",
        ))

    repo = WaRepo(conn)
    created, updated = repo.upsert_lead_many(leads)
    conn.close()
    return created, updated, skipped


def main():
    if not CUSTOMS_XLSX.exists():
        sys.exit(f"未找到海关数据文件: {CUSTOMS_XLSX}")

    records = read_customs()
    print(f"海关数据读取: 共 {len(records)} 条")
    with_email = sum(1 for r in records if r["email"])
    with_phone = sum(1 for r in records if r["phone"])
    print(f"  含邮箱: {with_email} | 含电话: {with_phone}")

    print("\n── 导入 FTA 邮箱库 ──")
    fa, fs = import_fta(records)
    print(f"  新增 {fa} 条,跳过 {fs} 条(无邮箱/重复)")

    print("\n── 导入 WhatsApp 联系人库 ──")
    wc, wu, ws = import_wa(records)
    print(f"  新增 {wc} 条,合并 {wu} 条(同号码多联系人),跳过 {ws} 条(无号/无法规范化)")


if __name__ == "__main__":
    main()
