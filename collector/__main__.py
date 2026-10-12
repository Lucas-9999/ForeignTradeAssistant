"""
Entry point for `python -m collector`.

Usage:
    python -m collector run --source=industry
    python -m collector list-sources
"""

from collector.cli import main

if __name__ == "__main__":
    main()
