"""Diagnose missing native packages, especially useful on Termux/Android.

Tries to import every Python package Cero depends on. For each failure,
prints a likely fix (the `pkg install ...` command for Termux, or the
equivalent apt command for Debian/Ubuntu).

Usage:
    uv run python scripts/check_dependencies.py
"""
from __future__ import annotations

import importlib
import platform
import sys


CHECKS = [
    # (import_name, why_we_need_it, termux_fix, apt_fix)
    ("yaml",        "config.yaml parsing",       "pkg install python",                   "apt install python3-yaml"),
    ("pydantic",    "typed config + signals",    "uv sync",                              "uv sync"),
    ("sqlalchemy",  "DB ORM",                    "uv sync",                              "uv sync"),
    ("aiosqlite",   "async SQLite driver",       "uv sync",                              "uv sync"),
    ("aiohttp",     "async HTTP for feeds",      "pkg install python-cryptography",      "apt install libssl-dev"),
    ("ccxt",        "exchange wrapper",          "pkg install rust libffi openssl-tool", "apt install libssl-dev libffi-dev"),
    ("aiogram",     "Telegram bot",              "uv sync",                              "uv sync"),
    ("fastapi",     "web dashboard",             "uv sync",                              "uv sync"),
    ("uvicorn",     "web server",                "uv sync",                              "uv sync"),
    ("loguru",      "structured logging",        "uv sync",                              "uv sync"),
    ("numpy",       "indicator math",            "pkg install python-numpy",             "apt install python3-numpy"),
    ("pandas",      "candle helpers (optional)", "pkg install python-pandas",            "apt install python3-pandas"),
]


def main() -> None:
    is_termux = "ANDROID_ROOT" in __import__("os").environ or "com.termux" in sys.executable
    fix_col = 2 if is_termux else 3
    fix_label = "Termux fix" if is_termux else "Debian/Ubuntu fix"

    print(f"=== Cero dependency check ===")
    print(f"python:   {sys.version.split()[0]}")
    print(f"platform: {platform.machine()} {platform.system()}")
    print(f"on Termux: {is_termux}")
    print()

    failures: list[tuple[str, str, str]] = []
    for name, why, termux_fix, apt_fix in CHECKS:
        try:
            importlib.import_module(name)
            print(f"  OK   {name:<14} ({why})")
        except ImportError as e:
            print(f"  FAIL {name:<14} — {e}")
            fix = termux_fix if is_termux else apt_fix
            failures.append((name, why, fix))

    print()
    if not failures:
        print("All dependencies importable. Cero should boot.")
        sys.exit(0)

    print(f"=== {len(failures)} dependency issue(s) ===")
    print()
    print(f"Likely fixes ({fix_label}):")
    for name, why, fix in failures:
        print(f"  {name}: {fix}")
    print()
    print("After installing native packages, re-run `uv sync` to rebuild")
    print("Python packages against them, then re-run this script.")
    sys.exit(1)


if __name__ == "__main__":
    main()
