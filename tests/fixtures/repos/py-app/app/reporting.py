"""Summary statistics for reports (clean)."""
from app.service import format_amount


def summarize(entries) -> str:
    """One-line summary over (name, cents) entries."""
    total = sum(cents for _, cents in entries)
    top = max(entries, key=lambda e: e[1], default=None)
    top_line = f"top: {top[0]} {format_amount(top[1])}" if top else "top: -"
    return f"total {format_amount(total)} | {top_line}"
