"""Formatting and pagination helpers (clean)."""


def format_amount(cents: int) -> str:
    """Cents -> a plain decimal string, e.g. 12345 -> '123.45'."""
    return f"{cents / 100:.2f}"


def paginate(items, page: int = 1, size: int = 10):
    """Slice `items` into a page; returns (page_items, total)."""
    start = max(page - 1, 0) * size
    return items[start:start + size], len(items)
