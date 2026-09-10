"""`izy doctor` and `izy install-extension` — the environment commands.\n\nEverything here answers 'is tracking actually working on this machine',\nwhich is the first question whenever titles stop appearing."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .. import config

EXT_UUID = "izy@local"

def cmd_doctor(args) -> int:
    """Re-check the Step 0 conditions on demand. Cheap to run, and the first
    thing to try when titles stop showing up."""
    from ..watchers import pick_watcher
    from ..watchers.native import IZY_DEST
    from .. import dbus

    cfg = config.load()
    print(f"session type   : {_env('XDG_SESSION_TYPE')}")
    print(f"desktop        : {_env('XDG_CURRENT_DESKTOP')}")
    ext = dbus.service_available(IZY_DEST)
    print(f"izy extension  : {'present on the bus' if ext else 'NOT RUNNING'}")
    if ext:
        print(f"  focus sample : {dbus.call_string(IZY_DEST, '/org/izy/Focus', IZY_DEST, 'GetFocused')}")
    installed = (Path.home() / ".local/share/gnome-shell/extensions" / EXT_UUID).exists()
    print(f"  installed    : {installed}")
    if installed and not ext:
        print("  -> installed but not loaded. Enable it and log out and back in:")
        print(f"     gnome-extensions enable {EXT_UUID}")
    if not installed:
        print("  -> run: izy install-extension")

    # A missing dateparser degrades every reminder to "I could not read that",
    # which looks like a parsing bug rather than a missing dependency.
    try:
        import dateparser  # noqa: F401
        print("dateparser     : installed")
    except ImportError:
        print("dateparser     : MISSING — reminders cannot parse times.")
        print("  -> reinstall: ./packaging/install.sh")
    key = "set" if os.environ.get("ANTHROPIC_API_KEY") else "not set"
    print(f"ANTHROPIC_API_KEY: {key} (llm enabled={cfg.llm.enabled})")

    from ..capture import describe as describe_capture
    print(f"screen capture : {describe_capture(cfg)}")

    from ..config_upgrade import plan_for
    from .. import paths
    config_path = paths.config_path()
    if config_path.exists():
        plan = plan_for(config_path.read_text())
        print(f"config         : {plan.describe()}")
        if not plan.empty:
            print("  -> run: izy config --upgrade")

    watcher = pick_watcher(cfg.watcher)
    describe = getattr(watcher, "describe", None)
    print(f"chosen watcher : {describe() if describe else watcher.name}")
    snap = watcher.poll()
    print(f"poll now       : {snap}")
    watcher.close()
    return 0 if snap is not None else 1


def cmd_install_extension(args) -> int:
    src = Path(__file__).resolve().parent.parent / "gnome-extension" / EXT_UUID
    if not src.exists():
        print(f"extension source not found at {src}", file=sys.stderr)
        return 1
    dest = Path.home() / ".local/share/gnome-shell/extensions" / EXT_UUID
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    print(f"installed -> {dest}")

    if shutil.which("gnome-extensions"):
        subprocess.run(["gnome-extensions", "enable", EXT_UUID],
                       capture_output=True, text=True)
    print(
        "\nGNOME Shell only scans for new extensions at startup, and a Wayland\n"
        "session cannot restart the shell in place. Log out and back in once,\n"
        "then confirm with:  izy doctor\n"
        f"If it is still not loaded after that:  gnome-extensions enable {EXT_UUID}"
    )
    return 0

def _env(name: str) -> str:
    return os.environ.get(name) or "(unset)"
