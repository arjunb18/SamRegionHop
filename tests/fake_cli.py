# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Arjun Bhagavatula
"""Stand-in for samregionhop.py used by test_ui: same prompts and exit codes, no phone."""

import sys
import time

args = sys.argv[1:]
try:
    if args == ["info"]:
        print("Model                SM-S928B\nActive CSC           THL\n"
              "CSCs in bundle       EUX INS THL WWD XME ZZZ")
    elif args == ["targets"]:
        print("Checking which regions SM-S928B (on THL — Thailand) can reliably switch to...\n\n"
              "  EUX  Europe (open market)\n  INS  India (open market)\n\nReliable targets: EUX INS")
    elif args == ["report"]:
        print("Report saved: /tmp/fake-report.md")
    elif args[:1] == ["fota"]:
        print("fota " + " ".join(args[1:]))
    elif args == ["probe"]:
        print("probing...", flush=True)
        while True:  # runs until stopped
            time.sleep(0.05)
    elif args[:1] == ["change"] and args[2:] == ["--dry-run"]:
        if args[1] == "BAD":
            print("error: refused", file=sys.stderr)
            sys.exit(1)
        print("Dry run: every check passed. Nothing was changed.")
    elif args[:1] == ["change"] and len(args) == 2:
        target = args[1]
        typed = input(f"\nType {target} to change the CSC (anything else aborts): ")
        if typed.strip().upper() != target:
            print("error: aborted. Nothing was changed.", file=sys.stderr)
            sys.exit(1)
        print(f"CHANGED to {target}")
    else:
        sys.exit(f"fake_cli: unexpected args {args}")
except KeyboardInterrupt:
    sys.exit("\ninterrupted.")
