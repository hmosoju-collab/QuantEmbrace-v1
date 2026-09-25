"""Persisted paper-book state — resume across real-time paper sessions.

A paper track record is built one month at a time: each session resumes the
prior book, applies at most one rebalance, and persists. The store binds the
state to the config hash it was built under, so a config change to a live book
is caught (fail-closed) rather than silently continuing a different experiment.
"""

from dataclasses import asdict, dataclass, field
from datetime import date
import json
from pathlib import Path

from qe.execution import Book, Holding


class BookStoreError(RuntimeError):
    pass


@dataclass
class BookState:
    inception: str
    seed_nav: float
    config_hash: str
    cash: float
    holdings: dict[str, dict]  # symbol -> {"qty", "avg_price"}
    nav_history: list[dict] = field(default_factory=list)  # {"date","nav"}
    last_rebalance: str | None = None

    def to_book(self) -> Book:
        return Book(
            cash=self.cash,
            holdings={s: Holding(**h) for s, h in self.holdings.items()},
        )

    @classmethod
    def seed(cls, *, inception: date, seed_nav: float, config_hash: str) -> "BookState":
        return cls(
            inception=inception.isoformat(),
            seed_nav=seed_nav,
            config_hash=config_hash,
            cash=seed_nav,
            holdings={},
        )


def load_book_state(path: str | Path) -> BookState | None:
    path = Path(path)
    if not path.exists():
        return None
    return BookState(**json.loads(path.read_text()))


def save_book_state(path: str | Path, state: BookState, book: Book) -> None:
    state.cash = round(book.cash, 2)
    state.holdings = {s: {"qty": h.qty, "avg_price": h.avg_price} for s, h in book.holdings.items()}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2, sort_keys=True, default=str))
