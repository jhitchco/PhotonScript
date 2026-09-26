"""Auto-arm decision gating (pure logic, no asyncio/httpx/astropy)."""

from datetime import datetime, timedelta

from photonscript.scheduler.auto_armer import auto_arm_decision

PRE = "2026-08-11T03:08:00Z"  # a night's pre-config time (UTC)


def _dec(**kw):
    base = dict(
        enabled=True,
        state="COMPLETE",
        now=datetime(2026, 8, 11, 1, 0),  # 2h before pre-config, inside a 3h window
        preconfig_utc=PRE,
        night_of="2026-08-11",
        last_armed_night=None,
        lead_hours=3.0,
    )
    base.update(kw)
    return auto_arm_decision(**base)


def test_arms_within_window():
    arm, _ = _dec()
    assert arm is True


def test_disabled_never_arms():
    arm, reason = _dec(enabled=False)
    assert arm is False and "disabled" in reason


def test_never_touches_a_busy_armer():
    for state in ("ARMED", "RUNNING", "PAUSED_UNSAFE"):
        arm, reason = _dec(state=state)
        assert arm is False and "busy" in reason


def test_idle_states_are_eligible():
    for state in ("DISARMED", "COMPLETE", "ERROR"):
        arm, _ = _dec(state=state)
        assert arm is True


def test_too_early_before_window():
    # 5h before pre-config, window only opens 3h before
    arm, reason = _dec(now=datetime(2026, 8, 10, 22, 8))
    assert arm is False and "too early" in reason


def test_no_double_arm_same_night():
    arm, reason = _dec(last_armed_night="2026-08-11")
    assert arm is False and "already" in reason


def test_past_preconfig_still_arms_for_remaining_night():
    # nobody armed; we're already past pre-config but before dawn
    arm, reason = _dec(now=datetime(2026, 8, 11, 4, 0))
    assert arm is True and "past pre-config" in reason


def test_no_plan_no_arm():
    arm, reason = _dec(preconfig_utc=None)
    assert arm is False and "no plan" in reason


def test_lead_hours_widens_window():
    # 5h before pre-config is too early at 3h lead, fine at 6h lead
    early = datetime(2026, 8, 10, 22, 8)
    assert _dec(now=early, lead_hours=3.0)[0] is False
    assert _dec(now=early, lead_hours=6.0)[0] is True


# --- noon auto re-arm (2026-09-15) -----------------------------------------

EARLY = datetime(2026, 8, 10, 22, 8)  # 5h before pre-config: outside the window


def test_noon_arm_fires_from_local_noon():
    assert _dec(now=EARLY)[0] is False  # window alone says too early
    arm, reason = _dec(now=EARLY, noon_arm=True, local_hour=12)
    assert arm is True and "noon" in reason


def test_noon_arm_waits_for_noon():
    arm, reason = _dec(now=EARLY, noon_arm=True, local_hour=9)
    assert arm is False and "too early" in reason


def test_noon_only_config_still_arms_afternoon():
    # nightly window arming off (auto_arm_enabled=False), noon on: an idle
    # afternoon armer still arms via the noon path
    arm, reason = _dec(window_arm=False, noon_arm=True, local_hour=18)
    assert arm is True and "noon" in reason


def test_window_disabled_and_before_noon_never_arms():
    arm, _ = _dec(now=EARLY, window_arm=False, noon_arm=True, local_hour=9)
    assert arm is False


def test_noon_arm_respects_busy_and_double_arm_gates():
    arm, reason = _dec(state="RUNNING", noon_arm=True, local_hour=13)
    assert arm is False and "busy" in reason
    arm, reason = _dec(last_armed_night="2026-08-11", noon_arm=True,
                       local_hour=13)
    assert arm is False and "already" in reason
