"""Portfolio construction: net strategy targets, size to integer shares.

The layer v1 never had (RA-1 finding F-5): strategies emit weights, the
portfolio nets them into one book-level target, and sizing happens here —
so cross-strategy direction conflicts are netted by construction.
"""


def net_targets(target_sets: list[dict[str, float]]) -> dict[str, float]:
    """Sum target weights across strategies into one book-level target."""
    book: dict[str, float] = {}
    for targets in target_sets:
        for symbol, w in targets.items():
            book[symbol] = book.get(symbol, 0.0) + w
    return {s: w for s, w in book.items() if w != 0.0}


def size_targets(weights: dict[str, float], nav: float, prices: dict[str, float]) -> dict[str, int]:
    """Convert weights to integer share targets at current prices.

    Floor division (never round up — capital protection over fill count);
    symbols without a valid price are skipped, matching the v1 book.
    """
    sized: dict[str, int] = {}
    for symbol, w in weights.items():
        px = prices.get(symbol)
        if px is None or px <= 0:
            continue
        sized[symbol] = int((nav * w) // px)
    return sized
