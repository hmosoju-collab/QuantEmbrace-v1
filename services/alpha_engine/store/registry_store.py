"""AlphaRegistryStore — institutional memory for alpha models (ADR-031).

Table ``{prefix}-alpha-registry`` (no TTL — this is permanent research memory):

  META record   PK=MODEL#{model_id}  SK=META
      champion_model_version, challenger_model_version, change_history[]

  VERSION record PK=MODEL#{model_id}  SK=VERSION#{model_version}
      status (SHADOW|RESEARCH|APPROVED|RETIRED|REJECTED),
      status_history[] (append-only: {status, reason, decided_by, decided_at}),
      lineage: parent_model_id, parent_model_version, change_summary,
               hypothesis, experiment_id,
      stats: pbo_score, deflated_sharpe, adjusted_p_value, stats_computed_at,
      ic_summary, health_state, notes, created_at.

Promotion is ALWAYS a human decision (research recommends; the operator approves).
A version cannot be registered without lineage (hypothesis + experiment_id +
change_summary) — that is what makes the registry a research memory rather than a
pile of model ids.
"""

from __future__ import annotations

from typing import Any

from alpha_engine.store._dynamo_json import from_dynamo, to_dynamo
from shared.logging.logger import get_logger
from shared.utils.helpers import utc_now

logger = get_logger(__name__, service_name="alpha_engine")

REGISTRY_STATUSES: frozenset[str] = frozenset(
    {"SHADOW", "RESEARCH", "APPROVED", "RETIRED", "REJECTED"}
)
_META_SK = "META"


class RegistryError(RuntimeError):
    """Raised on invalid registry operations (missing lineage, bad status, …)."""


def model_pk(model_id: str) -> str:
    return f"MODEL#{model_id}"


def version_sk(model_version: str) -> str:
    return f"VERSION#{model_version}"


class AlphaRegistryStore:
    def __init__(self, *, table: Any) -> None:
        self._table = table

    # ── Reads ─────────────────────────────────────────────────────────────────

    def get_meta(self, model_id: str) -> dict | None:
        resp = self._table.get_item(Key={"PK": model_pk(model_id), "SK": _META_SK})
        item = resp.get("Item")
        return from_dynamo(item) if item else None

    def get_version(self, model_id: str, model_version: str) -> dict | None:
        resp = self._table.get_item(
            Key={"PK": model_pk(model_id), "SK": version_sk(model_version)}
        )
        item = resp.get("Item")
        return from_dynamo(item) if item else None

    def list_versions(self, model_id: str) -> list[dict]:
        from boto3.dynamodb.conditions import Key

        resp = self._table.query(
            KeyConditionExpression=Key("PK").eq(model_pk(model_id))
            & Key("SK").begins_with("VERSION#")
        )
        return [from_dynamo(i) for i in resp.get("Items", [])]

    # ── Writes ────────────────────────────────────────────────────────────────

    def register_version(
        self,
        *,
        model_id: str,
        model_version: str,
        alpha_family: str,
        hypothesis: str,
        experiment_id: str,
        change_summary: str,
        parent_model_id: str | None = None,
        parent_model_version: str | None = None,
        notes: str = "",
        make_champion_if_first: bool = True,
    ) -> dict:
        """Register a new model version in SHADOW status with mandatory lineage."""
        if not model_version:
            raise RegistryError("model_version is mandatory")
        for field_name, field_value in (
            ("hypothesis", hypothesis),
            ("experiment_id", experiment_id),
            ("change_summary", change_summary),
        ):
            if not field_value or not str(field_value).strip():
                raise RegistryError(
                    f"{field_name} is mandatory when registering a model version "
                    "(ADR-031 lineage). Root models pass parent_model_version=None "
                    "but must still state a hypothesis."
                )
        if self.get_version(model_id, model_version) is not None:
            raise RegistryError(
                f"{model_id}@{model_version} already registered — versions are immutable"
            )

        now = utc_now().isoformat()
        record = {
            "PK": model_pk(model_id),
            "SK": version_sk(model_version),
            "record_type": "VERSION",
            "model_id": model_id,
            "model_version": model_version,
            "alpha_family": alpha_family,
            "status": "SHADOW",
            "status_history": [
                {
                    "status": "SHADOW",
                    "reason": "registered",
                    "decided_by": "system",
                    "decided_at": now,
                }
            ],
            "parent_model_id": parent_model_id,
            "parent_model_version": parent_model_version,
            "change_summary": change_summary,
            "hypothesis": hypothesis,
            "experiment_id": experiment_id,
            "pbo_score": None,
            "deflated_sharpe": None,
            "adjusted_p_value": None,
            "stats_computed_at": None,
            "ic_summary": {},
            "health_state": "HEALTHY",
            "notes": notes,
            "created_at": now,
        }
        self._table.put_item(
            Item=to_dynamo(record),
            ConditionExpression="attribute_not_exists(SK)",
        )
        self._ensure_meta(model_id, model_version, make_champion_if_first)
        logger.info("alpha_engine.registry_version_registered %s@%s", model_id, model_version)
        return record

    def set_status(
        self,
        *,
        model_id: str,
        model_version: str,
        status: str,
        reason: str,
        decided_by: str,
    ) -> dict:
        """Append a status transition. Promotion gating is enforced by the CLI."""
        status = status.upper()
        if status not in REGISTRY_STATUSES:
            raise RegistryError(f"invalid status {status!r}; must be one of {sorted(REGISTRY_STATUSES)}")
        record = self.get_version(model_id, model_version)
        if record is None:
            raise RegistryError(f"{model_id}@{model_version} is not registered")

        entry = {
            "status": status,
            "reason": reason,
            "decided_by": decided_by,
            "decided_at": utc_now().isoformat(),
        }
        history = list(record.get("status_history", []))
        history.append(entry)
        self._table.update_item(
            Key={"PK": model_pk(model_id), "SK": version_sk(model_version)},
            UpdateExpression="SET #s = :s, status_history = :h",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=to_dynamo({":s": status, ":h": history}),
        )
        logger.info(
            "alpha_engine.registry_status_changed %s@%s -> %s by %s",
            model_id, model_version, status, decided_by,
        )
        record["status"] = status
        record["status_history"] = history
        return record

    def set_champion(self, *, model_id: str, model_version: str, reason: str, decided_by: str) -> None:
        self._set_role(model_id, model_version, "champion_model_version", reason, decided_by)

    def set_challenger(self, *, model_id: str, model_version: str, reason: str, decided_by: str) -> None:
        self._set_role(model_id, model_version, "challenger_model_version", reason, decided_by)

    def update_stats(self, *, model_id: str, model_version: str, stats: dict[str, Any]) -> None:
        """Write deflated_sharpe / pbo_score / adjusted_p_value (P5)."""
        allowed = {"pbo_score", "deflated_sharpe", "adjusted_p_value"}
        payload = {k: stats[k] for k in allowed if k in stats}
        payload["stats_computed_at"] = utc_now().isoformat()
        self._patch_version(model_id, model_version, payload)

    def update_ic_summary(self, *, model_id: str, model_version: str, ic_summary: dict[str, Any]) -> None:
        self._patch_version(model_id, model_version, {"ic_summary": ic_summary})

    def update_health(self, *, model_id: str, model_version: str, health_state: str) -> None:
        self._patch_version(model_id, model_version, {"health_state": health_state})

    # ── Internals ─────────────────────────────────────────────────────────────

    def _ensure_meta(self, model_id: str, model_version: str, make_champion_if_first: bool) -> None:
        meta = self.get_meta(model_id)
        if meta is None:
            self._table.put_item(
                Item=to_dynamo(
                    {
                        "PK": model_pk(model_id),
                        "SK": _META_SK,
                        "record_type": "META",
                        "model_id": model_id,
                        "champion_model_version": model_version if make_champion_if_first else None,
                        "challenger_model_version": None,
                        "change_history": [],
                    }
                )
            )

    def _set_role(
        self, model_id: str, model_version: str, role_attr: str, reason: str, decided_by: str
    ) -> None:
        if self.get_version(model_id, model_version) is None:
            raise RegistryError(f"{model_id}@{model_version} is not registered")
        meta = self.get_meta(model_id) or {
            "PK": model_pk(model_id),
            "SK": _META_SK,
            "record_type": "META",
            "model_id": model_id,
            "champion_model_version": None,
            "challenger_model_version": None,
            "change_history": [],
        }
        change_history = list(meta.get("change_history", []))
        change_history.append(
            {
                "role": role_attr,
                "model_version": model_version,
                "reason": reason,
                "decided_by": decided_by,
                "decided_at": utc_now().isoformat(),
            }
        )
        meta[role_attr] = model_version
        meta["change_history"] = change_history
        self._table.put_item(Item=to_dynamo(meta))
        logger.info(
            "alpha_engine.registry_%s_set %s@%s by %s", role_attr, model_id, model_version, decided_by
        )

    def _patch_version(self, model_id: str, model_version: str, payload: dict[str, Any]) -> None:
        if self.get_version(model_id, model_version) is None:
            raise RegistryError(f"{model_id}@{model_version} is not registered")
        names = {f"#{i}": k for i, k in enumerate(payload)}
        values = {f":{i}": v for i, (_, v) in enumerate(payload.items())}
        set_expr = ", ".join(f"{n} = :{i}" for i, n in enumerate(names))
        self._table.update_item(
            Key={"PK": model_pk(model_id), "SK": version_sk(model_version)},
            UpdateExpression=f"SET {set_expr}",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=to_dynamo(values),
        )
