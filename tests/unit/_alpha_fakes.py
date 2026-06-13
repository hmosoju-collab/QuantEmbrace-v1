"""In-memory DynamoDB ``Table`` fakes for Alpha Engine unit tests.

Not collected by pytest (underscore prefix). Emulates only the operations the
alpha stores/gate use: ``put_item`` (with ``attribute_not_exists`` condition),
``get_item``, and a minimal ``SET`` ``update_item``. LocalStack covers the rest
in the integration tests.
"""

from __future__ import annotations

from typing import Any


class FakeConditionalCheckFailed(Exception):
    """Mimics botocore's ConditionalCheckFailedException shape."""

    def __init__(self) -> None:
        super().__init__("ConditionalCheckFailedException")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeTable:
    """A dict-backed stand-in for a boto3 DynamoDB ``Table`` resource."""

    def __init__(self, *, raise_on_get: bool = False) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}
        self._raise_on_get = raise_on_get

    # ── boto3-compatible surface ──────────────────────────────────────────────

    def put_item(self, *, Item: dict, ConditionExpression: str | None = None, **_: Any) -> dict:
        key = (Item["PK"], Item["SK"])
        if (
            ConditionExpression
            and "attribute_not_exists" in str(ConditionExpression)
            and key in self._items
        ):
            raise FakeConditionalCheckFailed()
        self._items[key] = dict(Item)
        return {}

    def get_item(self, *, Key: dict, **_: Any) -> dict:
        if self._raise_on_get:
            raise RuntimeError("simulated dynamodb get_item failure")
        item = self._items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(item)} if item is not None else {}

    def update_item(
        self,
        *,
        Key: dict,
        UpdateExpression: str,
        ExpressionAttributeValues: dict,
        ExpressionAttributeNames: dict | None = None,
        **_: Any,
    ) -> dict:
        key = (Key["PK"], Key["SK"])
        item = self._items.setdefault(key, {"PK": Key["PK"], "SK": Key["SK"]})
        names = ExpressionAttributeNames or {}
        expr = UpdateExpression.strip()
        assert expr.upper().startswith("SET "), f"unsupported UpdateExpression: {expr!r}"
        for assignment in expr[4:].split(","):
            lhs, rhs = (part.strip() for part in assignment.split("=", 1))
            field = names.get(lhs, lhs)
            item[field] = ExpressionAttributeValues[rhs]
        return {}

    # ── test helpers ──────────────────────────────────────────────────────────

    def raw(self, pk: str, sk: str) -> dict | None:
        return self._items.get((pk, sk))

    def __len__(self) -> int:
        return len(self._items)
