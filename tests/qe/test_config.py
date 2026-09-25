from datetime import date

import pydantic
import pytest

from qe.config import DataConfig, RunConfig, UniverseConfig


def _config(**overrides) -> RunConfig:
    kwargs = {
        "name": "t",
        "start_date": date(2024, 1, 1),
        "end_date": date(2024, 6, 30),
        "universe": UniverseConfig(symbols=("RELIANCE", "OIL")),
    }
    kwargs.update(overrides)
    return RunConfig(**kwargs)


def test_hash_is_deterministic_and_full_sha256():
    a, b = _config(), _config()
    assert a.config_hash() == b.config_hash()
    assert len(a.config_hash()) == 64
    assert a.short_hash == a.config_hash()[:12]


def test_hash_changes_when_any_field_changes():
    base = _config()
    assert base.config_hash() != _config(end_date=date(2024, 7, 1)).config_hash()
    assert (
        base.config_hash() != _config(universe=UniverseConfig(symbols=("RELIANCE",))).config_hash()
    )
    assert base.config_hash() != _config(data=DataConfig(interval="1m")).config_hash()


def test_config_is_frozen():
    cfg = _config()
    with pytest.raises(pydantic.ValidationError):
        cfg.name = "mutated"


def test_validation_fail_closed():
    with pytest.raises(pydantic.ValidationError):
        _config(universe=UniverseConfig(symbols=()))  # empty universe
    with pytest.raises(pydantic.ValidationError):
        _config(universe=UniverseConfig(symbols=("A", "A")))  # duplicates
    with pytest.raises(pydantic.ValidationError):
        _config(end_date=date(2023, 1, 1))  # end before start
    with pytest.raises(pydantic.ValidationError):
        RunConfig.model_validate(
            {**_config().model_dump(mode="json"), "unknown_key": 1}
        )  # extra keys forbidden


def test_yaml_round_trip(tmp_path):
    cfg = _config()
    path = tmp_path / "run.yaml"
    import yaml

    path.write_text(yaml.safe_dump(cfg.model_dump(mode="json")))
    assert RunConfig.from_yaml(path).config_hash() == cfg.config_hash()
