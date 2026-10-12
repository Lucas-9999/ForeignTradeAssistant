"""Read-only workbook contract for forthcoming Fumeng lead exports.

This module deliberately validates and normalizes data only.  Importing a
workbook into CRM remains a separately confirmed operation, so a new export
cannot create leads merely because someone opens or previews it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import openpyxl


class FumengContractError(ValueError):
    """The supplied workbook cannot be safely interpreted as a Fumeng export."""


_FIELD_ALIASES: dict[str, frozenset[str]] = {
    "company_name": frozenset({"公司名称", "公司", "企业名称", "company", "company name"}),
    "contact_name": frozenset({"联系人", "联系人姓名", "姓名", "contact", "contact name", "name"}),
    "position": frozenset({"职位", "职务", "position", "title", "job title"}),
    "email": frozenset({"邮箱", "电子邮箱", "email", "e-mail", "email address"}),
    "phone": frozenset({"电话", "手机", "手机号", "phone", "mobile", "telephone"}),
    "country": frozenset({"国家", "国家/地区", "地区", "country", "country/region", "region"}),
    "website": frozenset({"网站", "官网", "website", "web site", "url"}),
}


def _header_key(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _cell_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _resolve_columns(headers: tuple[Any, ...]) -> dict[str, int]:
    normalized = {_header_key(value): index for index, value in enumerate(headers)}
    columns: dict[str, int] = {}
    for field, aliases in _FIELD_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                columns[field] = normalized[alias]
                break
    if "email" not in columns:
        raise FumengContractError(
            "Fumeng workbook must contain an email column (邮箱 / 电子邮箱 / Email)."
        )
    return columns


@dataclass(frozen=True)
class FumengPreview:
    """Safe preview of a workbook; ``records`` contains only usable unique rows."""

    path: Path
    columns: dict[str, int]
    total_rows: int
    valid_rows: int
    missing_email_rows: int
    duplicate_email_rows: int
    records: tuple[dict[str, str | None], ...]


def preview_workbook(path: Path) -> FumengPreview:
    """Read and validate a Fumeng export without touching the database."""
    path = Path(path)
    if not path.is_file():
        raise FumengContractError(f"Workbook does not exist: {path}")

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        iterator = sheet.iter_rows(values_only=True)
        try:
            headers = next(iterator)
        except StopIteration as exc:
            raise FumengContractError("Workbook is empty.") from exc
        columns = _resolve_columns(headers)

        total_rows = missing_email_rows = duplicate_email_rows = 0
        seen_emails: set[str] = set()
        records: list[dict[str, str | None]] = []
        for row in iterator:
            if not any(value is not None and str(value).strip() for value in row):
                continue
            total_rows += 1
            record = {
                field: _cell_text(row[index]) if index < len(row) else None
                for field, index in columns.items()
            }
            email = (record.get("email") or "").lower()
            if not email:
                missing_email_rows += 1
                continue
            if email in seen_emails:
                duplicate_email_rows += 1
                continue
            seen_emails.add(email)
            record["email"] = email
            records.append(record)
    finally:
        workbook.close()

    return FumengPreview(
        path=path,
        columns=columns,
        total_rows=total_rows,
        valid_rows=len(records),
        missing_email_rows=missing_email_rows,
        duplicate_email_rows=duplicate_email_rows,
        records=tuple(records),
    )
