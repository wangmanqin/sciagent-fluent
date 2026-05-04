"""
agents/iter_planner.py
============================================================
多轮迭代的"决策器"——纯规则版。"达标判定温度"可配置 (metric_key)。

为什么需要这一层(写给以后的你 / 答辩用):
  W1/W2 的 graph 是单程的: intent → simulate → report, 跑一次就结束。
  W3 要让 agent "试错": 温度超过警戒线 → 自己加风速再跑一次, 直到达标。

  这个文件只回答一个问题:
      "看完上一轮结果, 下一轮参数怎么定? 还是干脆别跑了?"

  graph 负责"边"——按本文件返回的 action 决定走哪个分支。
  本文件负责"决策"——纯函数, 给同样输入永远同样输出, 测试方便。

为什么是规则不是 LLM:
  - 规则可控, 不会"建议把 chip_power 砍半"这种偷换任务条件的胡话
  - 规则是确定性的, max_rounds=3 的预算可以精确预估
  - 留好接口, W4 真要换 LLM 时只改这一个函数, graph 不用动
  (架构决策 #1: LLM 只生成 JSON, 流程决策走规则——参见 设计思路.md)

调什么参数:
  只调 inlet_velocity_ms。理由:
  - chip_power_watts 是"用户给的任务条件", agent 不能擅自改 (改了等于"我把题目改简单了
    所以达标了", 是欺骗)
  - max_iterations 跟稳态最高温度无关 (只影响是否收敛)
  - limit_C / chip_material / mesh_quality 不是"超温缓解杠杆"

  CFD 直觉: 风速大 → 对流换热强 → 温度低。这是最稳的规则。

[关键升级] 用哪个温度判达标 (metric_key):
  踩坑 #6 的延伸: mixing_elbow 上 max_temp 是 hot-inlet 边界温度钉死的 (永远 313.15 K),
  迭代用它会"第一轮就达标", 永远进不了 continue 分支. 所以 W3 默认用 outlet_avg_temp_C.
  manifold_cht 上 chip_power 真驱动 max_temp, 用 max_temp_C 才合理.
  这一层差异通过 metric_key 参数让 graph 节点选, iter_planner 自己不感知 case.

  接口契约:
    metric_key="outlet_avg_temp_C" (mixing_elbow, W3 默认) → 配 limit_key="outlet_limit_C"
    metric_key="max_temp_C"        (manifold_cht / 真有发热源) → 配 limit_key="limit_C"

调多少 (规则):
      over_C  = metric_C - limit_C                          # 超标多少度
      delta_v = clip(over_C / 20.0, _DELTA_MIN, _DELTA_MAX) # 每 20°C → +1 m/s
      new_v   = clip(old_v + delta_v, _VELOCITY_MIN, _VELOCITY_MAX)

  为什么用比例而非"+0.5 一刀切": 一刀切在严重超标时浪费轮次预算 (超 200 °C 也只加 0.5,
  3 轮总共才加 1.5, 大概率仍超标)。比例化能用最少轮次拉到合理范围。

  风速软上限 10 m/s: mixing-elbow 常规 0.4-2.0, manifold 常规 0.5-3.0, 10 已经远超合理
  CFD 工况, 撞顶就承认"光靠加风速救不回来", 放弃 (stop_reason="velocity_capped")。

终止信号 (return["action"] == "stop"):
  - "within_spec"      : 上一轮已达标, 不需要再跑
  - "velocity_capped"  : 风速已撞 _VELOCITY_MAX, 再加也无意义
  - "max_rounds"       : 已用满预算 (round_idx >= max_rounds), 出"未达标但探索完毕"报告

  必须有"放弃"出口——不能让 LLM/规则 yes/no 决定停不停, 否则可能死循环 (踩坑预防)。

接口契约 (graph 节点用):
  调用:
      plan = plan_next_round(
          last_params, last_result, round_idx,
          max_rounds=3,
          metric_key="outlet_avg_temp_C",  # mixing_elbow 默认
          limit_key="outlet_limit_C",
      )
  返回 dict:
      action       : "continue" | "stop"
      stop_reason  : None | "within_spec" | "velocity_capped" | "max_rounds"
      next_params  : dict (action="continue" 时是下一轮 params, "stop" 时回传 last_params)
      delta        : dict 形如 {"inlet_velocity_ms": +0.7} (日志/报告用)
      reasoning    : 中文一句话 (给 report 节点直接打)

测试入口:
  python -m agents.iter_planner
"""
# ↑ 模块 docstring, help(agents.iter_planner) 能读到。

import sys
import traceback
from typing import Optional


# ============================================================
# 规则常量 — 集中放, 调参时只改这一处
# ============================================================

_VELOCITY_MIN = 0.1
_VELOCITY_MAX = 10.0
_DELTA_PER_C = 1.0 / 20.0
_DELTA_MIN = 0.2
_DELTA_MAX = 2.0


# ============================================================
# 主函数: plan_next_round
# ============================================================

def plan_next_round(
    last_params: dict,
    last_result: dict,
    round_idx: int,
    max_rounds: int = 3,
    metric_key: str = "outlet_avg_temp_C",
    limit_key: str = "outlet_limit_C",
) -> dict:
    """
    根据上一轮结果, 决定下一轮怎么跑(或者干脆别跑了)。

    Args:
        last_params: 上一轮的 params dict (chip_power_watts/inlet_velocity_ms/...)。
        last_result: 上一轮的 analyzer 输出 dict, 至少含 within_spec 和 metric_key /
                     limit_key 指向的两个数值字段。
        round_idx:   当前已跑完的轮数, 从 1 起。round_idx=1 表示"刚跑完第 1 轮, 在判断要不要跑第 2 轮"。
        max_rounds:  最多允许的总轮数。round_idx >= max_rounds 时强制 stop。
        metric_key:  result 里"被判达标"的温度键名。默认 outlet_avg_temp_C
                     (mixing_elbow 适用), manifold_cht 应传 "max_temp_C"。
        limit_key:   result 里对应的警戒线键名。配套 metric_key 用。

    Returns:
        dict, 五个键: action / stop_reason / next_params / delta / reasoning
        约定: 永远返回 dict, 不抛异常 (graph 节点不用 try/except 包它)。
    """
    # ---------- (0) 取字段 ----------
    # within_spec 也要按 metric_key 选 — outlet 路径读 outlet_within_spec.
    # 否则 mixing_elbow 上 max_temp=40°C 永远 ≤ limit=85°C, within_spec=True, 永远 stop.
    if metric_key == "outlet_avg_temp_C":
        within_spec = last_result.get("outlet_within_spec")
    else:
        within_spec = last_result.get("within_spec")
    metric_C    = last_result.get(metric_key)
    limit_C     = last_result.get(limit_key)
    old_v       = float(last_params.get("inlet_velocity_ms", 1.0))

    # ---------- (1) 达标即停 ----------
    if within_spec is True:
        return {
            "action": "stop",
            "stop_reason": "within_spec",
            "next_params": dict(last_params),
            "delta": {},
            "reasoning": _fmt_reason_stop_within(round_idx, metric_key, metric_C, limit_C),
        }

    # ---------- (2) 轮数用尽 ----------
    if round_idx >= max_rounds:
        return {
            "action": "stop",
            "stop_reason": "max_rounds",
            "next_params": dict(last_params),
            "delta": {},
            "reasoning": _fmt_reason_stop_max(round_idx, max_rounds, metric_key, metric_C, limit_C),
        }

    # ---------- (3) 算 delta_v ----------
    if metric_C is None or limit_C is None or metric_C != metric_C:  # NaN check
        delta_v = _DELTA_MIN
        over_C = float("nan")
    else:
        over_C = float(metric_C) - float(limit_C)
        raw_delta = over_C * _DELTA_PER_C
        delta_v = max(_DELTA_MIN, min(_DELTA_MAX, raw_delta))

    new_v = old_v + delta_v

    # ---------- (4) 撞顶判定 ----------
    if new_v >= _VELOCITY_MAX or old_v >= _VELOCITY_MAX:
        new_v_clipped = min(new_v, _VELOCITY_MAX)
        return {
            "action": "stop",
            "stop_reason": "velocity_capped",
            "next_params": dict(last_params),
            "delta": {"inlet_velocity_ms": round(new_v_clipped - old_v, 3)},
            "reasoning": (
                f"风速已达上限 ({_VELOCITY_MAX} m/s), 仍未达标 "
                f"({metric_key}={_fmt_num(metric_C)}°C > "
                f"{limit_key}={_fmt_num(limit_C)}°C), "
                f"光靠加风速救不回来, 终止迭代。"
            ),
        }

    # ---------- (5) 正常 continue ----------
    next_params = dict(last_params)
    next_params["inlet_velocity_ms"] = round(new_v, 3)

    return {
        "action": "continue",
        "stop_reason": None,
        "next_params": next_params,
        "delta": {"inlet_velocity_ms": round(delta_v, 3)},
        "reasoning": (
            f"第 {round_idx} 轮超标 {_fmt_num(over_C)}°C "
            f"({metric_key}={_fmt_num(metric_C)}°C, "
            f"{limit_key}={_fmt_num(limit_C)}°C), "
            f"风速 {old_v:.2f} → {new_v:.2f} m/s (+{delta_v:.2f}), "
            f"进入第 {round_idx + 1} 轮。"
        ),
    }


# ============================================================
# 文本辅助 — 把"NaN 或 None 时不报错"集中在这里
# ============================================================

def _fmt_num(x) -> str:
    """格式化温度值. None/NaN 显示 '?', 不让 f-string 崩."""
    if x is None:
        return "?"
    try:
        if x != x:  # NaN
            return "?"
        return f"{float(x):.2f}"
    except (TypeError, ValueError):
        return "?"


def _fmt_reason_stop_within(round_idx, metric_key, metric_C, limit_C) -> str:
    return (
        f"第 {round_idx} 轮已达标 "
        f"({metric_key}={_fmt_num(metric_C)}°C ≤ "
        f"limit={_fmt_num(limit_C)}°C), 终止迭代。"
    )


def _fmt_reason_stop_max(round_idx, max_rounds, metric_key, metric_C, limit_C) -> str:
    return (
        f"已跑 {round_idx} 轮(上限 {max_rounds}), 仍未达标 "
        f"({metric_key}={_fmt_num(metric_C)}°C > "
        f"limit={_fmt_num(limit_C)}°C), 终止迭代。"
    )


# ============================================================
# 自测试 — 5 个 case 覆盖主要分支
# ============================================================

def _make_result(within_spec: Optional[bool], outlet_C: Optional[float],
                 outlet_limit_C: float = 21.0) -> dict:
    """构造一个最小的假 result dict, 默认走 outlet_avg_temp_C 路径 (W3 mixing_elbow)."""
    return {
        "within_spec": within_spec,
        "outlet_avg_temp_C": outlet_C,
        "outlet_limit_C": outlet_limit_C,
    }


def _check(name: str, plan: dict, expected: dict) -> bool:
    ok = True
    for k, v in expected.items():
        got = plan.get(k)
        if isinstance(v, float):
            if got is None or abs(float(got) - v) > 1e-6:
                ok = False
                print(f"  [字段 {k}] 期望 {v}, 得到 {got}")
        else:
            if got != v:
                ok = False
                print(f"  [字段 {k}] 期望 {v!r}, 得到 {got!r}")
    if ok:
        print(f"[OK] {name}")
    else:
        print(f"[FAIL] {name}")
        print(f"       完整 plan: {plan}")
    return ok


def _selftest() -> int:
    print("=" * 60)
    print("Day 15/16 自测试: iter_planner 规则版 (outlet_avg_temp_C 路径)")
    print("=" * 60)

    n_pass = 0
    n_fail = 0

    base_params = {
        "chip_power_watts": 15.0,
        "inlet_velocity_ms": 2.0,
        "max_iterations": 150,
    }

    # ---------- A: 达标即停 ----------
    print("\n--- Case A: within_spec=True 应该立刻 stop ----")
    try:
        plan = plan_next_round(
            last_params=base_params,
            last_result=_make_result(within_spec=True, outlet_C=20.5, outlet_limit_C=21.0),
            round_idx=1,
        )
        ok = _check("A 达标即停", plan, {"action": "stop", "stop_reason": "within_spec"})
        n_pass += 1 if ok else 0
        n_fail += 0 if ok else 1
    except Exception as e:
        traceback.print_exc()
        print(f"[FAIL] A 抛异常: {e}")
        n_fail += 1

    # ---------- B: 微超标, 比例规则 ----------
    # over=10, raw_delta=0.5, 在 [0.2, 2.0] 内, 取 0.5; new_v=2.5
    print("\n--- Case B: 超 10°C → 风速 +0.5 ---")
    try:
        plan = plan_next_round(
            last_params=base_params,
            last_result=_make_result(within_spec=False, outlet_C=31.0, outlet_limit_C=21.0),
            round_idx=1,
        )
        ok = _check("B 微超标", plan, {
            "action": "continue",
            "stop_reason": None,
        })
        if plan.get("next_params", {}).get("inlet_velocity_ms") != 2.5:
            ok = False
            print(f"  [next_v] 期望 2.5, 得到 {plan.get('next_params', {}).get('inlet_velocity_ms')}")
        n_pass += 1 if ok else 0
        n_fail += 0 if ok else 1
    except Exception as e:
        traceback.print_exc()
        print(f"[FAIL] B 抛异常: {e}")
        n_fail += 1

    # ---------- C: 严重超标 → delta 撞 _DELTA_MAX ----------
    # over=115, raw=5.75, clip 到 2.0; new_v = 2.0 + 2.0 = 4.0
    print("\n--- Case C: 超 115°C → delta 撞 2.0 上限, new_v=4.0 ---")
    try:
        plan = plan_next_round(
            last_params=base_params,
            last_result=_make_result(within_spec=False, outlet_C=136.0, outlet_limit_C=21.0),
            round_idx=1,
        )
        ok = _check("C 严重超标", plan, {"action": "continue", "stop_reason": None})
        if plan.get("next_params", {}).get("inlet_velocity_ms") != 4.0:
            ok = False
            print(f"  [next_v] 期望 4.0, 得到 {plan.get('next_params', {}).get('inlet_velocity_ms')}")
        n_pass += 1 if ok else 0
        n_fail += 0 if ok else 1
    except Exception as e:
        traceback.print_exc()
        print(f"[FAIL] C 抛异常: {e}")
        n_fail += 1

    # ---------- D: 撞顶放弃 ----------
    print("\n--- Case D: 风速 9.5 + 还要加 → 撞顶 stop ---")
    try:
        params_high_v = dict(base_params, inlet_velocity_ms=9.5)
        plan = plan_next_round(
            last_params=params_high_v,
            last_result=_make_result(within_spec=False, outlet_C=80.0, outlet_limit_C=21.0),
            round_idx=1,
        )
        ok = _check("D 撞顶", plan, {"action": "stop", "stop_reason": "velocity_capped"})
        n_pass += 1 if ok else 0
        n_fail += 0 if ok else 1
    except Exception as e:
        traceback.print_exc()
        print(f"[FAIL] D 抛异常: {e}")
        n_fail += 1

    # ---------- E: max_rounds 终止 ----------
    print("\n--- Case E: round_idx=3, max_rounds=3, 未达标 → max_rounds stop ---")
    try:
        plan = plan_next_round(
            last_params=base_params,
            last_result=_make_result(within_spec=False, outlet_C=60.0, outlet_limit_C=21.0),
            round_idx=3,
            max_rounds=3,
        )
        ok = _check("E max_rounds", plan, {"action": "stop", "stop_reason": "max_rounds"})
        n_pass += 1 if ok else 0
        n_fail += 0 if ok else 1
    except Exception as e:
        traceback.print_exc()
        print(f"[FAIL] E 抛异常: {e}")
        n_fail += 1

    # ---------- F: max_temp 路径 (manifold_cht) ----------
    print("\n--- Case F: metric_key=max_temp_C (manifold 路径) 也要工作 ---")
    try:
        plan = plan_next_round(
            last_params=base_params,
            last_result={
                "within_spec": False,
                "max_temp_C": 95.0,
                "limit_C": 85.0,
            },
            round_idx=1,
            metric_key="max_temp_C",
            limit_key="limit_C",
        )
        ok = _check("F max_temp 路径", plan, {"action": "continue", "stop_reason": None})
        if plan.get("next_params", {}).get("inlet_velocity_ms") != 2.5:
            ok = False
            print(f"  [next_v] 期望 2.5, 得到 {plan.get('next_params', {}).get('inlet_velocity_ms')}")
        n_pass += 1 if ok else 0
        n_fail += 0 if ok else 1
    except Exception as e:
        traceback.print_exc()
        print(f"[FAIL] F 抛异常: {e}")
        n_fail += 1

    # ---------- 汇总 ----------
    print("\n" + "=" * 60)
    print(f"Summary: {n_pass} PASS / {n_fail} FAIL (共 6)")
    print("=" * 60)

    if n_fail == 0:
        print("Day 15/16 PASSED — iter_planner 双 metric_key 路径可用。")
        print("下一步 (Day 16): graph/pipeline.py 加 analyze→iter_planner 条件边。")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(_selftest())
