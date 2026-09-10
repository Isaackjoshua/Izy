"""The optional screen-capture tier. Off by default.

SPEC.md is unusually specific about this one, and the wording decides the
design: screen capture is "opt-in, off by default, and gated behind a per-app
capture blocklist that is enforced *before* the capture happens, not after".

So this module is built around `allowed()`, not around capturing. Nothing calls
a screenshot API until the gate has said yes, which means a blocked app's pixels
are never read into this process at all — not read then discarded, not captured
then filtered. That distinction is the whole point: a screenshot of your
password manager that is deleted a millisecond later was still taken.

Three independent things must all be true before a single pixel is read:

  1. `capture.enabled` is true in the config. It ships false.
  2. The focused app is not on the blocklist, which ships populated with the
     categories most people would regret: password managers, banking, private
     browsing, messaging.
  3. A capture backend actually exists on this machine.

**On GNOME Wayland there is no silent backend, and that is by design.** Measured
on this system: `org.gnome.Shell.Screenshot` returns AccessDenied, there are no
screenshot CLI tools, and the only route is the XDG desktop portal — which asks
the user. A background daemon cannot quietly photograph the screen, which for a
feature like this is the correct default rather than a limitation to work
around. See `docs/STEP0-ENVIRONMENT.md` for the same measurement discipline.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: Shipped blocklist. These are the windows whose contents would be most
#: regretted, and the list is deliberately broad — the cost of missing a capture
#: is one extra tier-4 question; the cost of taking a wrong one is much higher.
DEFAULT_BLOCKLIST = (
    "1password", "bitwarden", "keepass", "lastpass", "dashlane", "seahorse",
    "gnome-keyring", "polkit", "pinentry", "gnupg", "authenticator",
    "bank", "paypal", "stripe", "revolut", "monzo",
    "signal", "whatsapp", "telegram", "element", "protonmail", "thunderbird",
    "private browsing", "incognito", "inprivate",
    "izy",           # never photograph our own prompts
)


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str


def _matches(patterns, value: str | None) -> str | None:
    if not value:
        return None
    haystack = value.lower()
    for raw in patterns or ():
        pattern = str(raw)
        if pattern.startswith("re:"):
            try:
                if re.search(pattern[3:], value, re.I):
                    return pattern
            except re.error:
                continue          # a bad regex must fail closed, not crash
        elif pattern.lower() in haystack:
            return pattern
    return None


class CaptureGate:
    """Decides whether a capture may be attempted. Never captures anything."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg

    @property
    def blocklist(self) -> tuple:
        configured = tuple(self.cfg.capture.blocklist or ())
        return configured if configured else DEFAULT_BLOCKLIST

    def allowed(self, app: str | None, title: str | None) -> Verdict:
        """The gate. Called before anything touches a screenshot API.

        Fails closed: an unknown app with no title is refused rather than
        allowed, because 'we could not tell what this window was' is not a
        reason to photograph it.
        """
        if not self.cfg.capture.enabled:
            return Verdict(False, "capture is disabled")
        if not app and not title:
            return Verdict(False, "unidentified window")
        if hit := _matches(self.blocklist, app):
            return Verdict(False, f"app is blocklisted: {hit}")
        if hit := _matches(self.blocklist, title):
            return Verdict(False, f"title is blocklisted: {hit}")
        return Verdict(True, "allowed")


class PortalBackend:
    """Capture via the XDG desktop portal.

    Deliberately the only backend. The portal asks the user, and on GNOME
    Wayland nothing else works anyway — `Shell.Screenshot` is AccessDenied and
    no CLI tool is installed. An interactive prompt makes this unsuitable for
    continuous background use, which is a real constraint on the feature rather
    than a bug in this file.
    """

    DEST = "org.freedesktop.portal.Desktop"
    PATH = "/org/freedesktop/portal/desktop"
    IFACE = "org.freedesktop.portal.Screenshot"

    def available(self) -> bool:
        from . import dbus
        return dbus.service_available(self.DEST)

    def capture(self, out_path):
        """Not implemented: the portal's Screenshot call is an async
        request/response over a Request object and prompts the user.

        Left unimplemented rather than half-implemented, because the honest
        status of this tier on this machine is 'no silent backend exists'. If
        you want it, the portal flow is the route, and it will ask you every
        time until you grant it persistently.
        """
        raise NotImplementedError(
            "the XDG portal prompts for every screenshot; see izy/capture.py")


def backend_for(cfg):
    """The capture backend, or None when this machine has no usable one."""
    portal = PortalBackend()
    return portal if portal.available() else None


def describe(cfg) -> str:
    """One line for `izy doctor`."""
    if not cfg.capture.enabled:
        return "disabled (the default)"
    backend = backend_for(cfg)
    if backend is None:
        return "enabled, but no capture backend on this machine"
    return "enabled; portal backend present (prompts on every capture)"
