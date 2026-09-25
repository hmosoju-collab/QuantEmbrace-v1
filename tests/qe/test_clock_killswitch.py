from datetime import date, datetime

from qe.clock import IST, SimClock, WallClock
from qe.killswitch import KillSwitch, drawdown_trigger, staleness_trigger


def test_sim_clock_is_deterministic():
    clk = SimClock(datetime(2026, 3, 31, 15, 30, tzinfo=IST))
    assert clk.kind == "sim"
    assert clk.today() == date(2026, 3, 31)
    clk.advance_to(datetime(2026, 4, 30, 15, 30, tzinfo=IST))
    assert clk.today() == date(2026, 4, 30)


def test_wall_clock_is_ist():
    clk = WallClock()
    assert clk.kind == "wall"
    assert clk.now().tzinfo is not None
    assert clk.today() == clk.now().date()


def test_kill_switch_activation_is_idempotent_no_refire(tmp_path):
    ks = KillSwitch(tmp_path / "kill.json")
    assert not ks.is_active()

    first = ks.activate("loss limit", by="auto-trigger")
    assert first.active and first.reason == "loss limit"
    stamp = first.activated_at

    # Re-activating (the exact self-refire scenario) must NOT overwrite or re-fire:
    # the ORIGINAL reason/time/actor is preserved.
    again = ks.activate("some other reason", by="operator")
    assert again.reason == "loss limit"
    assert again.activated_at == stamp
    assert again.activated_by == "auto-trigger"


def test_kill_switch_deactivate_round_trip(tmp_path):
    ks = KillSwitch(tmp_path / "kill.json")
    ks.activate("halt", by="operator")
    assert ks.is_active()
    ks.deactivate(by="operator")
    assert not ks.is_active()
    # Deactivating an already-inactive switch is a harmless no-op.
    ks.deactivate(by="operator")
    assert not ks.is_active()


def test_drawdown_trigger():
    assert drawdown_trigger(70.0, 100.0, 0.25) is not None  # -30% breaches -25%
    assert drawdown_trigger(80.0, 100.0, 0.25) is None  # -20% is within
    assert drawdown_trigger(80.0, 100.0, None) is None  # off
    assert drawdown_trigger(80.0, 0.0, 0.25) is None  # no peak yet


def test_staleness_trigger():
    assert staleness_trigger(10, 5) is not None
    assert staleness_trigger(3, 5) is None
    assert staleness_trigger(10, None) is None  # off
