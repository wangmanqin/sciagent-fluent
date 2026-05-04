"""
tests/test_iter_planner.py — W3 iter_planner 规则覆盖
======================================================
比 agents/iter_planner.py:_selftest 更完整 — pytest 风格, 失败定位精确.
"""
import math
import pytest

from agents.iter_planner import plan_next_round


@pytest.fixture
def base_params():
    return {
        "chip_power_watts": 15.0,
        "inlet_velocity_ms": 2.0,
        "max_iterations": 150,
    }


def _result(within: bool, outlet_C: float, limit_C: float = 21.0):
    return {
        "outlet_within_spec": within,
        "outlet_avg_temp_C": outlet_C,
        "outlet_limit_C": limit_C,
    }


# ============================================================
# 主路径
# ============================================================

def test_within_spec_stops_immediately(base_params):
    plan = plan_next_round(base_params, _result(True, 20.5), round_idx=1)
    assert plan["action"] == "stop"
    assert plan["stop_reason"] == "within_spec"
    assert plan["next_params"] == base_params


def test_minor_overshoot_proportional_delta(base_params):
    # over=10°C, delta=0.5, new_v=2.5
    plan = plan_next_round(base_params, _result(False, 31.0), round_idx=1)
    assert plan["action"] == "continue"
    assert plan["next_params"]["inlet_velocity_ms"] == pytest.approx(2.5)


def test_severe_overshoot_clamped_to_delta_max(base_params):
    # over=115°C, raw_delta=5.75, clamp to 2.0, new_v=4.0
    plan = plan_next_round(base_params, _result(False, 136.0), round_idx=1)
    assert plan["action"] == "continue"
    assert plan["next_params"]["inlet_velocity_ms"] == pytest.approx(4.0)


def test_velocity_capped_stops(base_params):
    high_v = dict(base_params, inlet_velocity_ms=9.5)
    plan = plan_next_round(high_v, _result(False, 80.0), round_idx=1)
    assert plan["action"] == "stop"
    assert plan["stop_reason"] == "velocity_capped"


def test_max_rounds_stops(base_params):
    plan = plan_next_round(base_params, _result(False, 60.0),
                           round_idx=3, max_rounds=3)
    assert plan["action"] == "stop"
    assert plan["stop_reason"] == "max_rounds"


# ============================================================
# 边界 / 鲁棒性
# ============================================================

def test_max_temp_path_for_manifold(base_params):
    """metric_key 切到 max_temp_C, manifold_cht 路径也要工作."""
    last_result = {"within_spec": False, "max_temp_C": 95.0, "limit_C": 85.0}
    plan = plan_next_round(
        base_params, last_result, round_idx=1,
        metric_key="max_temp_C", limit_key="limit_C",
    )
    assert plan["action"] == "continue"
    assert plan["next_params"]["inlet_velocity_ms"] == pytest.approx(2.5)


def test_nan_temperature_uses_min_delta(base_params):
    """outlet 是 NaN 时 (查询失败), 用最小步 _DELTA_MIN=0.2 兜底."""
    last_result = {
        "outlet_within_spec": False,
        "outlet_avg_temp_C": float("nan"),
        "outlet_limit_C": 21.0,
    }
    plan = plan_next_round(base_params, last_result, round_idx=1)
    assert plan["action"] == "continue"
    # 2.0 + 0.2 = 2.2
    assert plan["next_params"]["inlet_velocity_ms"] == pytest.approx(2.2)


def test_returns_dict_no_exception_on_garbage(base_params):
    """脏数据进来不能抛异常 — graph 节点不用 try/except 包它."""
    plan = plan_next_round(base_params, {}, round_idx=1)  # 完全空 result
    assert isinstance(plan, dict)
    assert "action" in plan


def test_does_not_mutate_input(base_params):
    """plan 不能改 last_params (state 不可变约定)."""
    snapshot = dict(base_params)
    plan_next_round(base_params, _result(False, 31.0), round_idx=1)
    assert base_params == snapshot


def test_continue_carries_other_params(base_params):
    """next_params 应继承 chip_power / max_iter, 只改 inlet_velocity."""
    plan = plan_next_round(base_params, _result(False, 31.0), round_idx=1)
    np = plan["next_params"]
    assert np["chip_power_watts"] == base_params["chip_power_watts"]
    assert np["max_iterations"] == base_params["max_iterations"]
    assert np["inlet_velocity_ms"] != base_params["inlet_velocity_ms"]
