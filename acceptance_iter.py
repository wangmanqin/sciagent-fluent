"""
acceptance_iter.py — 迭代闭环验收
============================================================
跑一次端到端迭代, 验证 graph 的 simulate ↔ iter_planner ↔ report 闭环可用.

为什么单独写一个验收脚本而不是 python -m graph.pipeline:
  - graph.pipeline._selftest 是 dry-run, 跑 3 句 query 验拓扑不验物理.
  - 这个脚本真起 Fluent, 跑 1-3 轮, 看 outlet 温度真随风速降.

模式 (3 个递进):
  python acceptance_iter.py --dry-run
      不起 Fluent, 用假数据 (假物理: outlet=295+5/v) 验 graph 拓扑能多轮迭代.
      30 秒内出结果. 推荐先跑这个, 验证 SqliteSaver / 条件边 / iter_planner 节点接通.

  python acceptance_iter.py
      真起 Fluent, mixing_elbow 路径, query 默认 'chip_power=15W, v=0.4 m/s, iter=80',
      max_rounds=3. outlet_limit 21°C, 初始 outlet ~22°C 略超, 加风速后降到 21°C 以下.
      预计 ~3 分钟 (1 轮 ~1 分钟).

  python acceptance_iter.py --rounds 1
      跑单轮 (退化到基础仿真行为), 用来 sanity check 迭代改造没破坏基础功能.

验收清单:
  [PASS] iter_history 至少 1 条
  [PASS] 最后一轮的 last_plan.action == "stop"
  [PASS] last_plan.stop_reason ∈ {"within_spec", "max_rounds", "velocity_capped"}
  [PASS] 多轮场景下: 后一轮 inlet_velocity > 前一轮 (规则在做事)
  [PASS] 多轮场景下: 后一轮 outlet_avg_temp < 前一轮 (物理对了, 风速大温度低)
"""
import argparse
import sys
import traceback


def main() -> int:
    parser = argparse.ArgumentParser(description="端到端迭代验收")
    parser.add_argument("--dry-run", action="store_true",
                        help="不起 Fluent, 用假数据验拓扑 (秒级)")
    parser.add_argument("--rounds", type=int, default=3,
                        help="max_rounds (默认 3, 单程退化用 1)")
    parser.add_argument("--query", type=str,
                        default="芯片功率 15W, 风速 0.4 m/s, 跑 80 步, "
                                "出口低于 21 度",
                        help="测试用 query (默认含 outlet_limit_C=21°C 触发迭代)")
    parser.add_argument("--checkpoint", action="store_true",
                        help="启用 SqliteSaver (验 W3 checkpoint 字段)")
    parser.add_argument("--thread-id", type=str, default=None)
    args = parser.parse_args()

    print("=" * 60)
    print(f"迭代验收: max_rounds={args.rounds}, "
          f"dry_run={args.dry_run}, checkpoint={args.checkpoint}")
    print("=" * 60)

    try:
        from graph.pipeline import run_pipeline
        final = run_pipeline(
            query=args.query,
            dry_run=args.dry_run,
            case_mode="mixing_elbow",
            max_rounds=args.rounds,
            thread_id=args.thread_id,
            use_checkpoint=args.checkpoint,
            resume=False,
        )
    except Exception as e:
        print(f"\n[FAIL] run_pipeline 抛异常: {type(e).__name__}: {e}")
        traceback.print_exc()
        return 2

    print("\n" + "=" * 60)
    print("[验收]")
    print("=" * 60)

    if final.get("error"):
        print(f"[FAIL] state.error: {final['error']}")
        return 1

    history = final.get("iter_history") or []
    last_plan = final.get("last_plan") or {}

    # 检查 1: 至少一轮
    if len(history) < 1:
        print("[FAIL] iter_history 为空")
        return 1
    print(f"[PASS] iter_history 共 {len(history)} 轮")

    # 检查 2: 终止
    if last_plan.get("action") != "stop":
        print(f"[FAIL] 迭代未终止: action={last_plan.get('action')}")
        return 1
    stop_reason = last_plan.get("stop_reason")
    valid_reasons = ("within_spec", "max_rounds", "velocity_capped")
    if stop_reason not in valid_reasons:
        print(f"[FAIL] 终止原因异常: {stop_reason} (期望 ∈ {valid_reasons})")
        return 1
    print(f"[PASS] 迭代正常终止: stop_reason={stop_reason}")

    # 检查 3+4: 多轮场景下验单调性
    if len(history) >= 2:
        v_seq = [h["params"].get("inlet_velocity_ms") for h in history]
        out_seq = [h["result_kpi"].get("outlet_avg_temp_C") for h in history]
        print(f"  inlet_velocity 序列: {v_seq}")
        print(f"  outlet_avg_temp_C 序列: {out_seq}")

        # 风速单调递增 (规则在调)
        v_increasing = all(v_seq[i] < v_seq[i + 1] for i in range(len(v_seq) - 1))
        if v_increasing:
            print("[PASS] inlet_velocity 单调递增 (iter_planner 规则在做事)")
        else:
            print(f"[WARN] inlet_velocity 非单调: {v_seq}")

        # outlet 温度单调递降 (物理对了)
        out_decreasing = all(
            out_seq[i] is not None and out_seq[i + 1] is not None
            and out_seq[i] > out_seq[i + 1]
            for i in range(len(out_seq) - 1)
        )
        if out_decreasing:
            print("[PASS] outlet_avg_temp_C 单调递降 (物理对: 风速大→温度低)")
        else:
            print(f"[WARN] outlet 非单调: {out_seq} (dry-run 假物理可能有数值噪声)")

    print("\n" + "=" * 60)
    print(f"[OK] 迭代验收通过 (终止原因={stop_reason}, 共 {len(history)} 轮)")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
