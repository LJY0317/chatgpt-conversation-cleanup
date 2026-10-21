#!/usr/bin/env python3
from pathlib import Path
import sys

if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11+ is required. Try python3.14 run.py on macOS.")

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from chatgpt_cleanup.__main__ import entrypoint


if __name__ == "__main__":
    raise SystemExit(entrypoint())
