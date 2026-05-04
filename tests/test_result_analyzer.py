"""
tests/test_result_analyzer.py — W1+W2+W3 result_analyzer.summarize
=================================================================
纯 Python 路径 (无 Fluent), 验:
  - within_spec / outlet_within_spec 双判定
  - limit_C / outlet_limit_C 三层 fallback
  - case_mode 切换 (mixing_elbow vs manifold_cht)
  - summary_text 不会重复 (踩坑 #8 回归测试)
"""
import pytest

from agents.result_analyzer import summarize


def _baseline_run_summary(**overrides):
    base = {
        "max_temp_K": 313.15,
        "max_temp_C": 40.0,
        "outlet_avg_temp_K": 295.71,
        "outlet_avg_temp_C": 22.56,
        "converged": True,
        "iterations": 100,
        "elapsed_seconds": 30.0,
        "velocity_applied": True,
        "params_echo": {
            "chip_power_watts": 15.0,
            "inlet_velocity_ms": 1.0,
            "max_iterations": 100,
        },
    }
    base.update(overrides)
    return base


def _baseline_params(**overrides):
    base = {
        "chip_power_watts": 15.0,
        "inlet_velocity_ms": 1.0,
        "max_iterations": 100,
    }
    base.update(overrides)
    return base


# ============================================================
# 主路径
# ============================================================

def test_mixing_elbow_within_max_spec_default_limit():
    """mixing_elbow 默认 limit=85°C, max=40°C 必然达标."""
    out = summarize(_baseline_run_summary(), _baseline_params())
    assert out["within_spec"] is True
    assert out["limit_C"] == 85.0


def test_outlet_outside_default_limit_triggers_iter():
    """W3 默认 outlet_limit=21°C, mixing_elbow outlet ~22°C 必然不达标."""
    out = summarize(_baseline_run_summary(), _baseline_params())
    assert out["outlet_within_spec"] is False
    assert out["outlet_limit_C"] == 21.0
    assert out["outlet_margin_C"] is not None
    assert out["outlet_margin_C"] < 0


def test_user_outlet_limit_C_override():
    """params 里 outlet_limit_C 覆盖默认值."""
    out = summarize(
        _baseline_run_summary(),
        _baseline_params(outlet_limit_C=25.0),
    )
    assert out["outlet_limit_C"] == 25.0
    assert out["outlet_within_spec"] is True


def test_user_limit_C_override():
    """params 里 limit_C 覆盖默认值."""
    out = summarize(
        _baseline_run_summary(),
        _baseline_params(limit_C=30.0),  # 比 40°C 严
    )
    assert out["limit_C"] == 30.0
    assert out["within_spec"] is False


def test_manifold_cht_default_limit_1500():
    """case_mode=manifold_cht 默认 limit=1500°C."""
    rs = _baseline_run_summary(
        max_temp_K=830.25, max_temp_C=557.10,
        case_mode="manifold_cht",
    )
    out = summarize(rs, _baseline_params())
    assert out["limit_C"] == 1500.0
    assert out["within_spec"] is True


# ============================================================
# 鲁棒性
# ============================================================

def test_summary_text_no_duplication_regression():
    """踩坑 #8 回归: summary_text 不应该被 *58 倍重复."""
    out = summarize(_baseline_run_summary(), _baseline_params())
    text = out["summary_text"]
    # 关键 sentinel: "生成时间" 整个报告应该只出现 1 次
    assert text.count("生成时间") == 1
    # 长度应该在 200~3000 字符之间, 不是 24K 的灾难
    assert 200 < len(text) < 3000


def test_summarize_does_not_mutate_params():
    """summarize 不能改 params (副作用安全)."""
    params = _baseline_params(limit_C=50.0)
    snapshot = dict(params)
    summarize(_baseline_run_summary(), params)
    assert params == snapshot


def test_nan_max_temp_within_spec_false():
    """max_temp 是 NaN 时 within_spec 必须保守判 False."""
    rs = _baseline_run_summary(max_temp_K=float("nan"))
    out = summarize(rs, _baseline_params())
    assert out["within_spec"] is False
    assert out["margin_C"] is None  # NaN -> None
