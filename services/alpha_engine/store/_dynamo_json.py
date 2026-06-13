"""Float<->Decimal helpers for the DynamoDB resource API.

The boto3 resource API rejects ``float`` and returns ``Decimal``. These helpers
round-trip plain dicts (as produced by the alpha dataclasses' ``to_dict``) through
JSON so every float becomes a ``Decimal`` on write and every ``Decimal`` becomes a
plain ``int``/``float`` on read. Kept local to the alpha store to avoid touching
shared infra in v1.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
import json
from typing import Any

# India Standard Time — NSE trade_date is the IST calendar date of the decision.
_IST = timezone(timedelta(hours=5, minutes=30))


def to_dynamo(value: Any) -> Any:
    """Recursively convert floats to ``Decimal`` for a DynamoDB ``put_item``."""
    return json.loads(json.dumps(value, default=str), parse_float=Decimal)


def from_dynamo(value: Any) -> Any:
    """Recursively convert ``Decimal`` back to ``int``/``float`` for app use."""
    if isinstance(value, Decimal):
        # Preserve integers as int, everything else as float
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, list):
        return [from_dynamo(v) for v in value]
    if isinstance(value, dict):
        return {k: from_dynamo(v) for k, v in value.items()}
    return value


def ist_trade_date(decision_ts: datetime) -> str:
    """Return the IST calendar date (YYYY-MM-DD) of a decision timestamp."""
    if decision_ts.tzinfo is None:
        decision_ts = decision_ts.replace(tzinfo=UTC)
    return decision_ts.astimezone(_IST).date().isoformat()
