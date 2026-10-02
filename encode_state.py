#!/usr/bin/env python3
"""
encode_state.py — Encode state.json to base64 for GitHub Secrets.

After running login_once.py, run this script to get the base64 string
that you paste into your GitHub repository secret (STATE_JSON_B64).

Usage:
    python encode_state.py
"""

import base64
import sys
from pathlib import Path

STATE_FILE = Path(__file__).parent / "state.json"


def main() -> None:
    if not STATE_FILE.exists():
        print("ERROR: state.json not found! Run login_once.py first.")
        sys.exit(1)

    data = STATE_FILE.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")

    print("=" * 60)
    print("  STATE_JSON_B64 — Copy this to GitHub Secrets")
    print("=" * 60)
    print()
    print(b64)
    print()
    print(f"({len(data)} bytes -> {len(b64)} chars base64)")
    print()
    print("Steps:")
    print("  1. Go to your GitHub repo -> Settings -> Secrets -> Actions")
    print("  2. Click 'New repository secret'")
    print("  3. Name: STATE_JSON_B64")
    print("  4. Value: paste the base64 string above")
    print("  5. Click 'Add secret'")


if __name__ == "__main__":
    main()
