"""
Tests for UniverseMode — enum behavior and mode properties.
"""

import pytest

from shared.universe.modes import UniverseMode


class TestUniverseMode:
    def test_all_three_modes_exist(self) -> None:
        assert UniverseMode.PAPER_SAFE_START
        assert UniverseMode.PAPER_EXPAND
        assert UniverseMode.LIVE_ADVANCED

    def test_paper_modes_are_paper(self) -> None:
        assert UniverseMode.PAPER_SAFE_START.is_paper is True
        assert UniverseMode.PAPER_EXPAND.is_paper is True

    def test_live_mode_is_not_paper(self) -> None:
        assert UniverseMode.LIVE_ADVANCED.is_paper is False

    def test_live_mode_is_live(self) -> None:
        assert UniverseMode.LIVE_ADVANCED.is_live is True

    def test_paper_modes_are_not_live(self) -> None:
        assert UniverseMode.PAPER_SAFE_START.is_live is False
        assert UniverseMode.PAPER_EXPAND.is_live is False

    def test_dynamo_namespace_paper(self) -> None:
        assert UniverseMode.PAPER_SAFE_START.dynamo_namespace == "PAPER"
        assert UniverseMode.PAPER_EXPAND.dynamo_namespace == "PAPER"

    def test_dynamo_namespace_live(self) -> None:
        assert UniverseMode.LIVE_ADVANCED.dynamo_namespace == "LIVE"

    def test_paper_modes_have_different_namespaces_from_live(self) -> None:
        assert UniverseMode.PAPER_SAFE_START.dynamo_namespace != UniverseMode.LIVE_ADVANCED.dynamo_namespace
        assert UniverseMode.PAPER_EXPAND.dynamo_namespace != UniverseMode.LIVE_ADVANCED.dynamo_namespace

    def test_promotion_path_safe_start_to_expand(self) -> None:
        assert UniverseMode.PAPER_SAFE_START.can_promote_to == UniverseMode.PAPER_EXPAND

    def test_promotion_path_expand_to_live(self) -> None:
        assert UniverseMode.PAPER_EXPAND.can_promote_to == UniverseMode.LIVE_ADVANCED

    def test_live_has_no_promotion_target(self) -> None:
        assert UniverseMode.LIVE_ADVANCED.can_promote_to is None

    def test_from_string_case_insensitive(self) -> None:
        assert UniverseMode.from_string("paper_safe_start") == UniverseMode.PAPER_SAFE_START
        assert UniverseMode.from_string("PAPER_EXPAND") == UniverseMode.PAPER_EXPAND
        assert UniverseMode.from_string("live_advanced") == UniverseMode.LIVE_ADVANCED

    def test_from_string_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown UniverseMode"):
            UniverseMode.from_string("INVALID_MODE")

    def test_string_value_matches_name(self) -> None:
        assert UniverseMode.PAPER_SAFE_START.value == "PAPER_SAFE_START"
        assert UniverseMode.PAPER_EXPAND.value == "PAPER_EXPAND"
        assert UniverseMode.LIVE_ADVANCED.value == "LIVE_ADVANCED"

    def test_modes_are_distinct(self) -> None:
        modes = [UniverseMode.PAPER_SAFE_START, UniverseMode.PAPER_EXPAND, UniverseMode.LIVE_ADVANCED]
        assert len(set(modes)) == 3

    def test_paper_live_isolation_at_namespace_level(self) -> None:
        """Paper and live must never share a namespace — snapshot isolation guarantee."""
        paper_ns = {UniverseMode.PAPER_SAFE_START.dynamo_namespace, UniverseMode.PAPER_EXPAND.dynamo_namespace}
        live_ns = {UniverseMode.LIVE_ADVANCED.dynamo_namespace}
        assert paper_ns.isdisjoint(live_ns), "Paper and live namespaces must never overlap"
