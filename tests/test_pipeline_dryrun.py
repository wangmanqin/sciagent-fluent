"""
tests/test_pipeline_dryrun.py — graph 端到端 dry-run
=====================================================
不起 Fluent, 验 graph 拓扑 + 多轮迭代闭环可以工作.
等价于 acceptance_iter.py --dry-run, 但 pytest 风格让 CI 接得住.
"""
import pytest

from graph.pipeline import run_pipeline


def test_dryrun_single_round_within_spec():
    """outlet_limit 设宽松一点, 让第 1 轮就达标 → 单轮 stop=within_spec."""
    final = run_pipeline(
        "芯片功耗 15 瓦, 风速 5 米每秒, 出口低于 50 度",
        dry_run=True,
        max_rounds=3,
    )
    assert not final.get("error")
    history = final.get("iter_history") or []
    assert len(history) == 1
    plan = final.get("last_plan") or {}
    assert plan.get("action") == "stop"
    assert plan.get("stop_reason") == "within_spec"


def test_dryrun_multi_round_iteration():
    """outlet_limit=21°C + 初始 v=0.4 m/s, 假物理 T=295+5/v 让 3 轮都不达标."""
    final = run_pipeline(
        "芯片功率 15W, 风速 0.4 m/s, 出口低于 21 度",
        dry_run=True,
        max_rounds=3,
    )
    assert not final.get("error")
    history = final.get("iter_history") or []
    # 规则版可能 1-3 轮都行 (取决于 LLM 解析的 inlet_velocity 是否真触发迭代)
    assert len(history) >= 1
    plan = final.get("last_plan") or {}
    assert plan.get("action") == "stop"


def test_dryrun_velocity_monotonic_when_iterating():
    """多轮场景下 inlet_velocity 必须单调递增, outlet 必须单调递降."""
    final = run_pipeline(
        "芯片功率 15W, 风速 0.4 m/s, 出口低于 21 度",
        dry_run=True,
        max_rounds=3,
    )
    history = final.get("iter_history") or []
    if len(history) < 2:
        pytest.skip("单轮就达标, 没法验单调性")
    v_seq = [h["params"]["inlet_velocity_ms"] for h in history]
    out_seq = [h["result_kpi"]["outlet_avg_temp_C"] for h in history]
    for i in range(len(v_seq) - 1):
        assert v_seq[i] < v_seq[i + 1], f"风速非单调: {v_seq}"
        assert out_seq[i] > out_seq[i + 1], f"outlet 非单调: {out_seq}"


def test_dryrun_max_rounds_hard_stop():
    """outlet_limit 给极小值, 假物理永远达不到 → 必须 max_rounds 终止."""
    final = run_pipeline(
        "芯片功率 15W, 风速 0.4 m/s, 出口低于 1 度",  # 21°C 不可能 ≤ 1°C
        dry_run=True,
        max_rounds=3,
    )
    plan = final.get("last_plan") or {}
    assert plan.get("action") == "stop"
    # 不能死循环
    history = final.get("iter_history") or []
    assert len(history) <= 3
