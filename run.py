"""
run.py — SciAgent-Fluent 端到端入口
============================================================
W1 MVP 验收命令:
    python run.py --query "芯片功率 15W, 风速 2m/s, 给我温度云图"

W3 多轮迭代命令:
    python run.py --query "..." --max-rounds 3
    python run.py --query "..." --thread-id deploy-001 --checkpoint
    python run.py --thread-id deploy-001 --resume         # 崩了恢复

干什么:
  把用户的一句中文, 经过
      intent → simulate → iter_planner →[continue 回 simulate / stop 走 report]→ report
  跑完, 在 reports/run_<时间戳>/ 下生成 PNG + txt + JSON, 终端打印迭代历史表.

为什么 run.py 这么薄(就是一层 argparse + 调 run_pipeline):
  把 CLI 解析和图构建分开. 单测可以直接 import run_pipeline, 不用 subprocess.

模式:
  python run.py --query "..."             # 真跑 Fluent (~3-5 分钟)
  python run.py --query "..." --dry-run   # 跳过 Fluent (~5 秒)
  python run.py --selftest                # 跑 graph.pipeline._selftest

退出码:
  0 = 成功
  1 = 流水线 state.error 非空
  2 = 调用层异常
  3 = 参数错误
"""
import argparse
import sys
import traceback


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python run.py",
        description=(
            "SciAgent-Fluent: 一句中文 → DeepSeek 解析 → PyFluent 仿真 → "
            "(W3) 多轮迭代 → 温度云图 + 报告."
        ),
    )
    parser.add_argument("--query", type=str,
                        help="用户中文输入. 例: '芯片功率 15W, 风速 2m/s'")
    parser.add_argument("--dry-run", action="store_true",
                        help="跳过 Fluent 仿真 (假数据走流程, 测拓扑用).")
    parser.add_argument("--case", type=str,
                        choices=("mixing_elbow", "manifold_cht"),
                        default="mixing_elbow",
                        help="基线案例. mixing_elbow=W1 默认, manifold_cht=W2.")

    # W3 新增 — 迭代 + checkpoint
    parser.add_argument("--max-rounds", type=int, default=3,
                        help="[W3] 最多迭代轮数 (默认 3). 1 = 单程退化到 W1 行为.")
    parser.add_argument("--checkpoint", action="store_true",
                        help="[W3] 启用 SqliteSaver 持久化 (.langgraph_checkpoint.sqlite). "
                             "5 分钟仿真崩了能用 --resume 续.")
    parser.add_argument("--thread-id", type=str, default=None,
                        help="[W3] checkpoint thread id. 不传时按时间戳生成.")
    parser.add_argument("--resume", action="store_true",
                        help="[W3] 从已有 thread_id 续跑 (必须配 --thread-id + --checkpoint).")

    parser.add_argument("--selftest", action="store_true",
                        help="跑 graph.pipeline 自测试 (等价 python -m graph.pipeline).")

    args = parser.parse_args()

    if args.selftest:
        from graph.pipeline import _selftest
        return _selftest()

    if args.resume:
        if not args.thread_id:
            print("[ERROR] --resume 必须配 --thread-id", file=sys.stderr)
            return 3
        if not args.checkpoint:
            print("[ERROR] --resume 必须配 --checkpoint (没 sqlite 何来续跑)",
                  file=sys.stderr)
            return 3
        # resume 模式 query 可空 (从 checkpoint 恢复)
    elif not args.query:
        parser.print_help(sys.stderr)
        print("\n[ERROR] 必须传 --query 或 --selftest 或 --resume", file=sys.stderr)
        return 3

    try:
        from graph.pipeline import run_pipeline
        final_state = run_pipeline(
            query=args.query or "",
            dry_run=args.dry_run,
            case_mode=args.case,
            max_rounds=args.max_rounds,
            thread_id=args.thread_id,
            use_checkpoint=args.checkpoint,
            resume=args.resume,
        )
    except Exception as e:
        print(f"\n[FATAL] 调用 run_pipeline 抛异常: {type(e).__name__}: {e}",
              file=sys.stderr)
        traceback.print_exc()
        return 2

    if final_state.get("error"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
