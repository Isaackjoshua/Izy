"""The control plane — a FastAPI app on a Unix domain socket (izy-v2.md §1).

One API serves the dashboard, the widget and the CLI. It reads state from the
`StateBus` the tick publishes to, and it mutates *only* by submitting commands
to the `CommandQueue` the tick drains. It never writes to the DB. See server.py.
"""
from .server import ApiServer, build_app

__all__ = ["ApiServer", "build_app"]
