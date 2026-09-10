"""`izy config` — show where the config lives and whether it is current."""
from __future__ import annotations

import sys

from .. import config, paths
from ..config_upgrade import plan_for, upgrade_file


def cmd_config(args) -> int:
    path = paths.config_path()
    if not path.exists():
        config.load(path)          # writes the commented defaults
        print(f"wrote a fresh config to {path}")
        return 0

    plan = plan_for(path.read_text())
    print(f"config: {path}")
    print(f"status: {plan.describe()}")

    if plan.empty:
        return 0
    if not args.upgrade:
        print("\nThese exist as defaults but are not in your file, so you cannot")
        print("see or tune them. Add them (your edits are preserved) with:")
        print("  izy config --upgrade")
        return 1

    applied = upgrade_file(path)
    print(f"\nupgraded; a copy of the previous file is at {path}.bak")
    for name in applied.added_sections:
        print(f"  + [{name}]")
    for key in applied.added_keys:
        print(f"  + {key}")
    print("\nRestart to pick it up:  systemctl --user restart izy")
    return 0
