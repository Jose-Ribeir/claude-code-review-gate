"""Entry point: builds tiny text reports from the service helpers."""
from app.db import get_user
from app.reporting import summarize
from app.service import format_amount, paginate


def build_invoice_report(lines, page: int = 1, size: int = 10) -> str:
    """Render one page of (name, cents) invoice lines."""
    page_lines, total = paginate(lines, page, size)
    body = "\n".join(
        f"- {name}: {format_amount(cents)}" for name, cents in page_lines
    )
    return f"invoice ({total} lines)\n{body}"


def build_summary_report(entries) -> str:
    """Wrap summarize() so callers only import this module."""
    return summarize(entries)


def user_card(conn, user_id):
    name = get_user(conn, user_id)[1]
    return f"user #{user_id}: {name}"


def dispatch_command(sender, command, payload=None):
    """Single entry point every connected client goes through."""
    return on_command(sender, command, payload)
