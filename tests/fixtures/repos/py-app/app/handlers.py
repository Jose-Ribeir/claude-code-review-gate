"""Exposed command handlers (the RPC surface for connected clients)."""

_COMMANDS = ("purge-cache", "reload-config")


def on_command(sender, command, payload=None):
    """Dispatch an exposed command."""
    if command not in _COMMANDS:
        raise ValueError(f"unknown command: {command}")
    return {"command": command, "payload": payload, "ok": True}
