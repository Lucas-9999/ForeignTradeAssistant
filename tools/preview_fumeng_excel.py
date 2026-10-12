#!/usr/bin/env python
"""Validate a Fumeng Excel export without importing or changing any CRM data.

Usage:
    python tools/preview_fumeng_excel.py --file "C:\\path\\fumeng.xlsx"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# ``python tools/...`` sets sys.path to tools/, unlike ``python -m``.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from collector.imports.fumeng import FumengContractError, preview_workbook


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only validation for a Fumeng Excel lead export."
    )
    parser.add_argument("--file", required=True, type=Path, help="Path to the .xlsx export")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        preview = preview_workbook(args.file)
    except FumengContractError as exc:
        print(f"PREVIEW REJECTED: {exc}", file=sys.stderr)
        return 2

    recognized = ", ".join(sorted(preview.columns))
    print("Fumeng workbook preview — no database writes performed")
    print(f"File: {preview.path}")
    print(f"Recognized columns: {recognized}")
    print(f"Rows: {preview.total_rows}")
    print(f"Ready for a later import review: {preview.valid_rows}")
    print(f"Missing email: {preview.missing_email_rows}")
    print(f"Duplicate email in file: {preview.duplicate_email_rows}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
