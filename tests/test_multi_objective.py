"""tests/test_multi_objective.py - W4 多目标约束 (鼓励 3)."""
from agents.result_analyzer import summarize


def _baseline(**kw):
    base = {
        "max_temp_K": 313.15, "max_temp_C": 40.0,
        "outlet_avg_temp_K": 295.71, "outlet_avg_temp_C": 22.56,
        "converged": True, "iterations": 100, "elapsed_seconds": 30.0,
        "params_echo": {},
    }
    base.update(kw)
    return base


def _params(**kw):
    base = {"chip_power_watts": 15.0, "inlet_velocity_ms": 1.0, "max_iterations": 100}
    base.update(kw)
    return base


def test_user_only_specifies_max_limit():
    """用户只给 limit_C, outlet 不算 active."""
    out = summarize(_baseline(case_mode="manifold_cht"), _params(limit_C=60.0))
    # max=40 ≤ 60 中, outlet 不算 active
    assert "max_temp" in out["active_constraints"]
    # mixing_elbow 默认 outlet 也算 active, 但 case_mode=manifold_cht 不算
    assert "outlet" not in out["active_constraints"]
    assert out["all_within_spec"] is True


def test_user_only_specifies_outlet_limit():
    """用户只给 outlet_limit_C, max 仍算 active (mixing_elbow 默认)."""
    out = summarize(_baseline(case_mode="mixing_elbow"), _params(outlet_limit_C=21.0))
    assert "outlet" in out["active_constraints"]
    assert out["outlet_within_spec"] is False  # 22.56 > 21.0
    assert out["all_within_spec"] is False
    assert "outlet" in out["failed_constraints"]


def test_both_constraints_specified_both_pass():
    """两个约束都给, 都中, all_within_spec=True."""
    out = summarize(
        _baseline(case_mode="mixing_elbow"),
        _params(limit_C=85.0, outlet_limit_C=25.0),  # max=40<85, outlet=22.56<25
    )
    assert set(out["active_constraints"]) >= {"max_temp", "outlet"}
    assert out["all_within_spec"] is True
    assert out["failed_constraints"] == []


def test_both_constraints_specified_one_fails():
    """两个约束都给, max 中 outlet 不中."""
    out = summarize(
        _baseline(case_mode="mixing_elbow"),
        _params(limit_C=85.0, outlet_limit_C=21.0),  # outlet=22.56>21 不中
    )
    assert out["all_within_spec"] is False
    assert "outlet" in out["failed_constraints"]
    assert "max_temp" not in out["failed_constraints"]


def test_both_constraints_both_fail():
    out = summarize(
        _baseline(case_mode="mixing_elbow", max_temp_K=400.0, max_temp_C=126.85),
        _params(limit_C=85.0, outlet_limit_C=21.0),
    )
    assert out["all_within_spec"] is False
    assert set(out["failed_constraints"]) == {"max_temp", "outlet"}


def test_active_constraints_list_format():
    """active_constraints 必须是 list, failed_constraints 必须是 list."""
    out = summarize(_baseline(case_mode="manifold_cht"), _params())
    assert isinstance(out["active_constraints"], list)
    assert isinstance(out["failed_constraints"], list)
