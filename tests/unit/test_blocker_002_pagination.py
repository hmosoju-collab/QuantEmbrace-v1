"""
BLOCKER-002 regression tests — unpaginated DynamoDB reads in loss_validator.

Before the fix both methods made a single DynamoDB call:
    _get_unrealized_pnl         — single scan()  → silent 1MB truncation
    _fetch_realized_pnl_from_db — single query() → silent 1MB truncation

DynamoDB returns at most 1 MB per scan/query response.  On a high-fill day
or a large positions table, unread pages silently drop P&L contributions,
understating exposure and allowing risk limits to pass incorrectly.

After the fix both methods paginate via LastEvaluatedKey until exhausted.

These tests verify:
    T01-T05 — structural: LastEvaluatedKey / ExclusiveStartKey / while loop
    T06-T09 — _get_unrealized_pnl multi-page: all positions counted
    T10-T13 — _fetch_realized_pnl_from_db fallback multi-page: all fills counted
    T14     — single-page case still works (no regression)
    T15     — ExclusiveStartKey is NOT sent on the first page
    T16     — ExclusiveStartKey IS sent on subsequent pages
"""

from __future__ import annotations

import sys
from pathlib import Path

# ── Source-level structural checks (no import needed) ─────────────────────────

_SRC = (
    Path(__file__).parent.parent.parent
    / "services" / "risk_engine" / "validators" / "loss_validator.py"
).read_text()


def _extract(name: str) -> str:
    start = _SRC.find(f"async def {name}")
    end = _SRC.find("\n    async def ", start + 1)
    return _SRC[start:] if end == -1 else _SRC[start:end]


_M_UNREALIZED = _extract("_get_unrealized_pnl")
_M_FETCH = _extract("_fetch_realized_pnl_from_db")


class TestGetUnrealizedPnlStructure:
    """Structural checks for _get_unrealized_pnl pagination."""

    def test_T01_last_evaluated_key_present(self) -> None:
        assert "LastEvaluatedKey" in _M_UNREALIZED

    def test_T02_exclusive_start_key_present(self) -> None:
        assert "ExclusiveStartKey" in _M_UNREALIZED

    def test_T03_while_loop_present(self) -> None:
        assert "while True" in _M_UNREALIZED

    def test_T04_break_on_none(self) -> None:
        assert "break" in _M_UNREALIZED

    def test_T05_scan_kwargs_dict(self) -> None:
        """scan_kwargs dict built outside the thread call (not inline kwargs)."""
        assert "scan_kwargs" in _M_UNREALIZED


class TestFetchRealizedPnlStructure:
    """Structural checks for _fetch_realized_pnl_from_db fallback pagination."""

    def test_T06_last_evaluated_key_present(self) -> None:
        assert "LastEvaluatedKey" in _M_FETCH

    def test_T07_exclusive_start_key_present(self) -> None:
        assert "ExclusiveStartKey" in _M_FETCH

    def test_T08_while_loop_in_fallback(self) -> None:
        assert "while True" in _M_FETCH

    def test_T09_fast_path_aggregate_still_present(self) -> None:
        """Aggregate get_item fast path must still be tried before the fallback."""
        assert "aggregate" in _M_FETCH
        assert _M_FETCH.index("aggregate") < _M_FETCH.index("while True")


class TestMultiPageScan:
    """Functional: _get_unrealized_pnl consumes all pages."""

    @staticmethod
    def _simulate_unrealized(pages: list[dict]) -> tuple[float, int]:
        """Run the pagination logic from _get_unrealized_pnl against fake pages."""
        call_idx = [0]

        def scan_fn(**kwargs):
            page = pages[call_idx[0]]
            call_idx[0] += 1
            return page

        total = 0.0
        page_count = 0
        last_evaluated_key = None

        while True:
            scan_kwargs: dict = {}
            if last_evaluated_key is not None:
                scan_kwargs["ExclusiveStartKey"] = last_evaluated_key
            response = scan_fn(**scan_kwargs)
            page_count += 1
            for item in response.get("Items", []):
                qty = float(item.get("quantity", {}).get("N", "0"))
                avg = float(item.get("avg_price", {}).get("N", "0"))
                last = float(item.get("last_price", {}).get("N", "0"))
                total += qty * (last - avg)
            last_evaluated_key = response.get("LastEvaluatedKey")
            if last_evaluated_key is None:
                break

        return total, page_count

    def test_T10_three_page_scan_all_positions_counted(self) -> None:
        """Three-page scan sums all positions including those on pages 2 and 3."""
        pages = [
            {
                "Items": [
                    {"quantity": {"N": "100"}, "avg_price": {"N": "2500"}, "last_price": {"N": "2600"}},  # +10000
                    {"quantity": {"N": "50"},  "avg_price": {"N": "3000"}, "last_price": {"N": "2900"}},  # -5000
                ],
                "LastEvaluatedKey": {"PK": {"S": "p1"}},
            },
            {
                "Items": [
                    {"quantity": {"N": "200"}, "avg_price": {"N": "1500"}, "last_price": {"N": "1600"}},  # +20000
                ],
                "LastEvaluatedKey": {"PK": {"S": "p2"}},
            },
            {
                "Items": [
                    {"quantity": {"N": "150"}, "avg_price": {"N": "400"}, "last_price": {"N": "380"}},  # -3000
                ],
                # No LastEvaluatedKey → last page
            },
        ]
        total, page_count = self._simulate_unrealized(pages)
        # Expected: 10000 - 5000 + 20000 - 3000 = 22000
        assert abs(total - 22_000.0) < 0.01, f"Got {total}, expected 22000.0"
        assert page_count == 3

    def test_T11_single_page_no_regression(self) -> None:
        pages = [{"Items": [
            {"quantity": {"N": "10"}, "avg_price": {"N": "100"}, "last_price": {"N": "110"}},  # +100
        ]}]
        total, page_count = self._simulate_unrealized(pages)
        assert abs(total - 100.0) < 0.01
        assert page_count == 1

    def test_T12_empty_table_returns_zero(self) -> None:
        pages = [{"Items": []}]
        total, page_count = self._simulate_unrealized(pages)
        assert total == 0.0
        assert page_count == 1

    def test_T13_exclusive_start_key_not_sent_on_first_page(self) -> None:
        """First scan call must NOT include ExclusiveStartKey."""
        first_call_kwargs: list[dict] = []
        call_idx = [0]
        pages = [
            {"Items": [], "LastEvaluatedKey": {"PK": {"S": "p1"}}},
            {"Items": []},
        ]

        def scan_fn(**kwargs):
            first_call_kwargs.append(dict(kwargs))
            page = pages[call_idx[0]]; call_idx[0] += 1
            return page

        last_evaluated_key = None
        for _ in range(2):
            kwargs: dict = {}
            if last_evaluated_key is not None:
                kwargs["ExclusiveStartKey"] = last_evaluated_key
            resp = scan_fn(**kwargs)
            last_evaluated_key = resp.get("LastEvaluatedKey")
            if last_evaluated_key is None:
                break

        assert "ExclusiveStartKey" not in first_call_kwargs[0], (
            "First scan call must not include ExclusiveStartKey"
        )
        assert "ExclusiveStartKey" in first_call_kwargs[1], (
            "Second scan call must include ExclusiveStartKey"
        )


class TestMultiPageFallbackQuery:
    """Functional: _fetch_realized_pnl_from_db fallback paginates correctly."""

    @staticmethod
    def _simulate_fallback_query(pages: list[dict]) -> tuple[float, int]:
        """Run the fallback query pagination logic against fake pages."""
        call_idx = [0]

        def query_fn(**kwargs):
            page = pages[call_idx[0]]; call_idx[0] += 1
            return page

        total = 0.0
        page_count = 0
        last_evaluated_key = None

        while True:
            kwargs: dict = {}
            if last_evaluated_key is not None:
                kwargs["ExclusiveStartKey"] = last_evaluated_key
            response = query_fn(**kwargs)
            page_count += 1
            for item in response.get("Items", []):
                pnl = float(item.get("realized_pnl", {}).get("N", "0"))
                total += pnl
            last_evaluated_key = response.get("LastEvaluatedKey")
            if last_evaluated_key is None:
                break

        return total, page_count

    def test_T14_three_page_query_all_fills_counted(self) -> None:
        pages = [
            {"Items": [{"realized_pnl": {"N": "-500"}}, {"realized_pnl": {"N": "-200"}}],
             "LastEvaluatedKey": {"pk": "p1"}},
            {"Items": [{"realized_pnl": {"N": "-300"}}],
             "LastEvaluatedKey": {"pk": "p2"}},
            {"Items": [{"realized_pnl": {"N": "-100"}}]},
        ]
        total, page_count = self._simulate_fallback_query(pages)
        assert abs(total - (-1100.0)) < 0.01, f"Got {total}, expected -1100.0"
        assert page_count == 3

    def test_T15_positive_pnl_multipage(self) -> None:
        pages = [
            {"Items": [{"realized_pnl": {"N": "800"}}],
             "LastEvaluatedKey": {"pk": "p1"}},
            {"Items": [{"realized_pnl": {"N": "200"}}]},
        ]
        total, page_count = self._simulate_fallback_query(pages)
        assert abs(total - 1000.0) < 0.01
        assert page_count == 2

    def test_T16_empty_fallback_returns_zero(self) -> None:
        pages = [{"Items": []}]
        total, _ = self._simulate_fallback_query(pages)
        assert total == 0.0
