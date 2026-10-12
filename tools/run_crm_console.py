"""Run the CRM Web Console locally without starting any background scheduler.

This entry keeps P10 EMAIL_DRY_RUN enabled. It does not collect leads, run
batch email jobs, check inboxes, or trigger follow-ups on startup.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import app as fta_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FTA CRM 控制台（不启动后台任务）")
    parser.add_argument("--host", default="127.0.0.1", help="默认仅本机访问")
    parser.add_argument("--port", type=int, default=8001, help="默认 8001，避免占用 app.py 的 8000")
    args = parser.parse_args(argv)

    fta_app.app.state.scheduler_enabled = False
    uvicorn.run(fta_app.app, host=args.host, port=args.port, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
