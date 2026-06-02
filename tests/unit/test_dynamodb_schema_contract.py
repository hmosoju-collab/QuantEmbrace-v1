from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
DYNAMODB_TF = ROOT / "infra" / "terraform" / "modules" / "dynamodb" / "main.tf"
LOCAL_TABLES = ROOT / "scripts" / "setup_local_tables.py"


def _terraform_resource_block(resource_name: str) -> str:
    lines = DYNAMODB_TF.read_text().splitlines()
    start = next(
        index
        for index, line in enumerate(lines)
        if f'resource "aws_dynamodb_table" "{resource_name}"' in line
    )

    block: list[str] = []
    depth = 0
    for line in lines[start:]:
        block.append(line)
        depth += line.count("{") - line.count("}")
        if block and depth == 0:
            break

    return "\n".join(block)


def _quoted_assignment(block: str, name: str) -> str:
    match = re.search(rf'^\s*{name}\s*=\s*"([^"]+)"', block, flags=re.MULTILINE)
    assert match is not None, f"missing Terraform assignment: {name}"
    return match.group(1)


def _attribute_names(block: str) -> set[str]:
    return set(
        re.findall(
            r'attribute\s*\{[^}]*?name\s*=\s*"([^"]+)"',
            block,
            flags=re.DOTALL,
        )
    )


def _ttl_attribute(block: str) -> str:
    match = re.search(
        r'ttl\s*\{[^}]*?attribute_name\s*=\s*"([^"]+)"',
        block,
        flags=re.DOTALL,
    )
    assert match is not None, "missing Terraform ttl block"
    return match.group(1)


def _gsi_block(block: str, index_name: str) -> str:
    match = re.search(
        rf'global_secondary_index\s*\{{(?=[^}}]*name\s*=\s*"{index_name}").*?\n\s*\}}',
        block,
        flags=re.DOTALL,
    )
    assert match is not None, f"missing Terraform GSI: {index_name}"
    return match.group(0)


def test_production_hot_path_tables_use_canonical_pk_sk_schema() -> None:
    for resource_name in ("positions", "latest_prices", "risk_state"):
        block = _terraform_resource_block(resource_name)

        assert _quoted_assignment(block, "hash_key") == "PK"
        assert _quoted_assignment(block, "range_key") == "SK"
        assert {"PK", "SK"}.issubset(_attribute_names(block))


def test_localstack_schema_matches_production_pk_sk_contract() -> None:
    local_schema = LOCAL_TABLES.read_text()

    assert 'for suffix in ("positions", "risk-state", "sessions")' in local_schema
    assert 'name=f"{prefix}-latest-prices"' in local_schema
    assert "key_schema=pk_sk_key" in local_schema
    assert "attributes=pk_sk_attrs" in local_schema


def test_candle_cache_uses_queryable_ttl_protected_schema() -> None:
    block = _terraform_resource_block("candle_cache")
    index = _gsi_block(block, "candle-open-time-index")

    assert _quoted_assignment(block, "hash_key") == "PK"
    assert {"PK", "cache_bucket", "candle_open_time"}.issubset(_attribute_names(block))
    assert _quoted_assignment(index, "hash_key") == "cache_bucket"
    assert _quoted_assignment(index, "range_key") == "candle_open_time"
    assert _ttl_attribute(block) == "expires_at"


def test_strategy_state_requires_strategy_name_and_symbol_keys() -> None:
    block = _terraform_resource_block("strategy_state")

    assert _quoted_assignment(block, "hash_key") == "strategy_name"
    assert _quoted_assignment(block, "range_key") == "symbol"
    assert {"strategy_name", "symbol"}.issubset(_attribute_names(block))


def test_localstack_strategy_state_matches_production_key_contract() -> None:
    local_schema = LOCAL_TABLES.read_text()

    assert 'name=f"{prefix}-strategy-state"' in local_schema
    assert '"AttributeName": "strategy_name", "KeyType": "HASH"' in local_schema
    assert '"AttributeName": "symbol", "KeyType": "RANGE"' in local_schema
    assert 'ttl_attribute="expires_at"' in local_schema


def test_localstack_candle_cache_matches_production_index_and_ttl() -> None:
    local_schema = LOCAL_TABLES.read_text()

    assert '"AttributeName": "cache_bucket", "AttributeType": "S"' in local_schema
    assert '"AttributeName": "cache_bucket", "KeyType": "HASH"' in local_schema
    assert '"AttributeName": "candle_open_time", "KeyType": "RANGE"' in local_schema
    assert 'ttl_attribute="expires_at"' in local_schema
