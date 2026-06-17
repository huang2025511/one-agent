#!/usr/bin/env python3
"""One-Agent CLI entry point.

Run as: one-agent
Or:     python -m one_agent
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from one_agent import main  # noqa: E402


def entry():
    """Entry point for console_scripts."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print()
        sys.exit(0)


if __name__ == "__main__":
    entry()
