"""DynamoDB collector — table reachability, strictly read-only.

Confirms each configured table is present and ``ACTIVE`` using the shared
``get_dynamodb_resource()`` factory (resource API — this also keeps us clear of
the repo-wide ``boto3.client`` ban in ruff). Operations used:

    * ``Table(name).table_status`` → an internal ``DescribeTable`` (read-only).
    * optional ``Table.get_item(Key=probe_key)`` when a probe key is configured
      in rules — a single read, never a write.

It NEVER calls ``put_item`` / ``update_item`` / ``delete_item`` / ``batch_writer``.
Table suffixes from rules are joined with ``config.dynamodb_table_prefix`` so the
same policy works across dev/staging/prod.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from monitoring_agent.collectors.base import Collector
from monitoring_agent.snapshot import CollectorResult, Status, worst

logger = logging.getLogger("monitoring_agent.collectors.dynamodb")


def decide_table_status(table_status: Optional[str]) -> Status:
    """Map a DynamoDB TableStatus string to a coarse status."""
    state = (table_status or "").upper()
    if state == "ACTIVE":
        return Status.OK
    if state in ("CREATING", "UPDATING"):
        return Status.DEGRADED
    if state == "":
        return Status.UNKNOWN
    # DELETING / ARCHIVING / ARCHIVED / INACCESSIBLE_ENCRYPTION_CREDENTIALS
    return Status.DOWN


class DynamoDBCollector(Collector):
    """Observe DynamoDB table presence/status read-only."""

    name = "dynamodb"

    def _collect_sync(self) -> CollectorResult:
        table_rules = self.rules.dynamodb_tables
        if not table_rules:
            return self._result(Status.UNKNOWN, "dynamodb: no tables configured in rules.yaml")

        try:
            from botocore.exceptions import ClientError
            from shared.aws.clients import get_dynamodb_resource
        except Exception as exc:  # noqa: BLE001
            return self._result(Status.UNKNOWN, "dynamodb: boto3/shared client unavailable", error=repr(exc))

        try:
            resource = get_dynamodb_resource()
        except Exception as exc:  # noqa: BLE001
            return self._result(Status.UNKNOWN, "dynamodb: could not create resource", error=repr(exc))

        per_table: list[dict[str, Any]] = []
        effective: list[Status] = []

        for rule in table_rules:
            table_name = self.config.table(rule.suffix)
            entry: dict[str, Any] = {"suffix": rule.suffix, "table": table_name, "critical": rule.critical}
            try:
                table = resource.Table(table_name)
                status_str = table.table_status  # DescribeTable (read-only)
                status = decide_table_status(status_str)
                entry["table_status"] = status_str
                if rule.probe_key:
                    table.get_item(Key=rule.probe_key)  # read-only point read
                    entry["probe"] = "ok"
                if rule.kill_switch_probe and status == Status.OK:
                    ks_resp = table.get_item(
                        Key={"PK": "KILLSWITCH", "SK": "GLOBAL"},
                        ProjectionExpression="#a, #r",
                        ExpressionAttributeNames={"#a": "active", "#r": "reason"},
                    )
                    ks_item = ks_resp.get("Item", {})
                    if ks_item.get("active") is True:
                        status = Status.DOWN
                        entry["kill_switch_active"] = True
                        entry["kill_switch_reason"] = ks_item.get("reason", "unknown")
                    else:
                        entry["kill_switch_active"] = False
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                entry["error_code"] = code
                if code == "ResourceNotFoundException":
                    entry["table_status"] = "MISSING"
                    status = Status.DOWN if rule.critical else Status.DEGRADED
                else:
                    entry["error"] = repr(exc)
                    status = Status.UNKNOWN
            except Exception as exc:  # noqa: BLE001
                entry["error"] = repr(exc)
                status = Status.UNKNOWN

            entry["status"] = status.value
            per_table.append(entry)

            eff = status
            if not rule.critical and status == Status.DOWN:
                eff = Status.DEGRADED
            effective.append(eff)

        overall = worst(effective)
        missing = [e["table"] for e in per_table if e.get("table_status") == "MISSING"]
        impaired = [
            e["table"]
            for e in per_table
            if e["status"] != Status.OK.value and e.get("table_status") != "MISSING"
        ]
        summary = f"dynamodb: {len(table_rules)} table(s)"
        if missing:
            summary += f"; missing={missing}"
        if impaired:
            summary += f"; impaired={impaired}"
        if not missing and not impaired:
            summary += "; all ACTIVE"
        return self._result(overall, summary, details={"tables": per_table})
