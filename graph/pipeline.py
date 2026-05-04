"""
graph/pipeline.py
============================================================
用 LangGraph 把 intent / simulate / report 三件部件串成端到端流水线。
加 iter_planner 节点 + 条件边, 让 graph 能多轮迭代;
加 SqliteSaver checkpoint, 让 5 分钟仿真崩了能从中间恢复。

W1 单程拓扑 → W3 迭代拓扑:
        START                              START
          ↓                                  ↓
       [intent]                           [intent]
          ↓                                  ↓
       [simulate]                         [simulate] ←──┐
          ↓                                  ↓           │ continue
       [report]                          [iter_planner] ─┘ (next_params 写回 state.params)
          ↓                                  ↓ stop
         END                              [report]
                                             ↓
                                            END

设计原则 (跟 W1 保持一致):
  1. 节点单一职责. 每个 node 函数: 读 state -> 返回要更新的键. 不 mutate state.
  2. 错误隔离. 任意节点抛错 → state["error"] → 后续节点跳过.
  3. LLM 不做流程决策. iter_planner 是规则版 (agents/iter_planner.py), W4 才考虑 LLM.
  4. State 可序列化. 没有 session 这种活对象, JSON 可以 dump → SqliteSaver 直接持久化.

W3 新增字段 (GraphState):
  round_idx     : 已跑完的轮数 (从 0 起, simulate 完成后 +1)
  max_rounds    : 最多允许的轮数 (默认 3)
  iter_history  : 每轮记录 list, 元素 = {round, params, result_kpi, plan}
                  给 report 节点出"迭代历史表"用
  iter_metric   : "outlet_avg_temp_C" (mixing_elbow 默认) | "max_temp_C" (manifold_cht)
  iter_limit_key: 配套的 limit 字段名

CLI:
  python run.py --query "..." --max-rounds 3              # 真跑 Fluent
  python run.py --query "..." --dry-run                   # 测拓扑
  python run.py --query "..." --thread-id deploy-001      # 指定 checkpoint thread
  python run.py --query "..." --thread-id deploy-001 --resume   # 续跑

测试入口:
  python -m graph.pipeline                                # dry-run selftest
"""
# ↑ 模块文档字符串。help(graph.pipeline) 能读到。

import os
import sys
import time
import traceback
from typing import Any, Optional, TypedDict


# ============================================================
# 全局: 项目根加进 sys.path
# ============================================================

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


# ============================================================
# 实时进度回调 (W4+ UI streaming)
# ============================================================
# 模块级 hook — run_pipeline 进入时设, 退出时清.
# UI 端通过 progress= 参数注入 callable, 节点关键点调 _emit() 即时推事件.
# 不走 GraphState 是因为 SqliteSaver 不能序列化 callable.

_PROGRESS_HOOK = None  # type: Optional[Any]


def _emit(event_type: str, message: str = "", **data: Any) -> None:
    """节点级进度推送. UI 端 hook 收到后立即渲染. 没 hook 时只 print 不报错."""
    payload = {"type": event_type, "message": message, **data}
    hook = _PROGRESS_HOOK
    if hook is not None:
        try:
            hook(payload)
        except Exception as e:
            print(f"[progress][WARN] hook 抛异常 (non-fatal): {e}", file=sys.stderr)
    # console 总是 print, 方便排错跟视频录制
    print(f"[progress] {event_type}: {message}")


# ============================================================
# State 定义
# ============================================================

class GraphState(TypedDict, total=False):
    """流水线全局状态. total=False = 所有键都可选, 节点按职责往里加."""
    query: str                          # 用户输入
    dry_run: bool                       # 跳过 Fluent
    case_mode: str                      # "mixing_elbow" | "manifold_cht"
    params: Optional[dict]              # intent 节点产出, 也是 iter_planner 写回的下一轮参数
    result: Optional[dict]              # simulate 节点产出 (含 analyzer 输出)
    error: Optional[str]                # 错误传递

    # W3 新增 — 迭代闭环状态
    round_idx: int                      # 已跑完的轮数, 从 0 起
    max_rounds: int                     # 上限 (默认 3)
    iter_history: list                  # [{round, params, result_kpi, plan}, ...]
    iter_metric: str                    # "outlet_avg_temp_C" | "max_temp_C"
    iter_limit_key: str                 # "outlet_limit_C" | "limit_C"
    last_plan: Optional[dict]           # iter_planner 上一次返回的 plan, 给 report 用

    # [W4 案例模板库] case_retriever_node 写入, report 节点展示
    retrieved_case_id: str
    retrieved_case_title: str
    retrieved_case_score: float


# ============================================================
# Node 1: intent — 中文 → params
# ============================================================

def intent_node(state: GraphState) -> dict:
    """把 state['query'] 翻成 params dict. 跟 W1 一致, 不变."""
    if state.get("error"):
        return {}
    query = state.get("query", "").strip()
    if not query:
        return {"error": "intent: query 为空"}

    _emit("intent.start", f"DeepSeek 解析中文 query...")
    print(f"\n[intent] 解析: {query!r}")
    try:
        from agents.intent_parser import parse_intent
        params = parse_intent(query)
        print(f"[intent] -> {params}")
        # 只挑 UI 关心的几个字段, 避免事件过大
        brief = {k: params.get(k) for k in
                 ("chip_power_watts", "inlet_velocity_ms", "max_iterations",
                  "limit_C", "outlet_limit_C") if k in params}
        _emit("intent.done", f"意图解析完成 · {brief}", params=brief)
        return {"params": params}
    except Exception as e:
        traceback.print_exc()
        _emit("intent.error", f"intent 解析失败: {e}")
        return {"error": f"intent 节点失败: {type(e).__name__}: {e}"}


def case_retriever_node(state: GraphState) -> dict:
    """
    [W4] 在 intent 之后, simulate 之前, 用 query 检索历史案例库.
    命中就用 case 的 baseline_params 填补 query 没说的字段.
    """
    if state.get("error"):
        return {}
    query = state.get("query", "")
    if not query:
        return {}
    _emit("case_retriever.start", "RAG 案例库检索...")
    try:
        from agents.case_retriever import retrieve_case
        case = retrieve_case(query)
    except Exception as e:
        print(f"[case_retriever][WARN] 检索失败: {e}", file=sys.stderr)
        _emit("case_retriever.done", f"案例库检索异常: {e}")
        return {}
    if case is None:
        print("[case_retriever] 无相似历史案例, 跳过推荐")
        _emit("case_retriever.done", "无相似历史案例, 跳过推荐")
        return {}
    print(f"[case_retriever] 推荐案例: {case.case_id} (score={case.score})")
    print(f"  title: {case.title}")
    _emit("case_retriever.done",
          f"命中案例 {case.case_id} (score={case.score}) · {case.title}",
          case_id=case.case_id, score=case.score)

    user_params = state.get("params") or {}
    merged = dict(case.baseline_params)
    merged.update(user_params)

    return {
        "params": merged,
        "retrieved_case_id": case.case_id,
        "retrieved_case_title": case.title,
        "retrieved_case_score": case.score,
    }


# ============================================================
# Node 2: simulate — params → Fluent 仿真 + 自动 analyze
# ============================================================

def simulate_node(state: GraphState) -> dict:
    """
    跑一次 Fluent 仿真. analyzer hook 出云图 + 报告.
    W3 升级: 同一个 graph 实例可能多次进 simulate (迭代回路).
    """
    if state.get("error"):
        return {}

    params = state.get("params")
    if not params:
        return {"error": "simulate: 上游没产出 params"}

    round_idx = int(state.get("round_idx", 0))
    case_mode = state.get("case_mode") or "mixing_elbow"
    print(f"\n[simulate] round {round_idx + 1}, 参数: {params}")
    _emit("simulate.start",
          f"Round {round_idx + 1} 启动 Fluent ({case_mode}) · "
          f"v={params.get('inlet_velocity_ms')}m/s, "
          f"P={params.get('chip_power_watts')}W",
          round=round_idx + 1, case_mode=case_mode, params=dict(params))

    if state.get("dry_run"):
        out = _simulate_dry_run(state, params, round_idx)
        kpi = (out.get("iter_history") or [{}])[-1].get("result_kpi", {})
        _emit("simulate.done",
              f"Round {round_idx + 1} 完成 (dry-run) · "
              f"max_T={kpi.get('max_temp_C')}°C, outlet={kpi.get('outlet_avg_temp_C')}°C",
              round=round_idx + 1, kpi=kpi)
        return out

    print(f"[simulate] case_mode = {case_mode}")
    try:
        if case_mode == "manifold_cht":
            from tools.fluent_wrapper_w2 import run_simulation
        else:
            from tools.fluent_wrapper import run_simulation
        from agents.result_analyzer import analyze

        def analyzer_hook(session: Any, partial_result: dict) -> dict:
            return analyze(session, params, partial_result)

        result = run_simulation(params, analyzer=analyzer_hook)

        new_history = list(state.get("iter_history", []))
        new_history.append({
            "round": round_idx + 1,
            "params": dict(params),
            "result_kpi": _extract_kpi(result),
            "plan": None,
        })
        _emit("simulate.done",
              f"Round {round_idx + 1} 求解完成 · "
              f"max_T={result.get('max_temp_C')}°C, "
              f"outlet={result.get('outlet_avg_temp_C')}°C, "
              f"耗时 {result.get('elapsed_seconds')}s",
              round=round_idx + 1, kpi=_extract_kpi(result))
        return {
            "result": result,
            "round_idx": round_idx + 1,
            "iter_history": new_history,
        }

    except Exception as e:
        traceback.print_exc()
        _emit("simulate.error", f"Fluent 求解失败: {e}")
        return {"error": f"simulate 节点失败: {type(e).__name__}: {e}"}


def _extract_kpi(result: dict) -> dict:
    """从 result 抽出迭代历史表要展示的字段, 不存整个 result (节省 checkpoint)."""
    return {
        "max_temp_C": result.get("max_temp_C"),
        "outlet_avg_temp_C": result.get("outlet_avg_temp_C"),
        "limit_C": result.get("limit_C"),
        "outlet_limit_C": result.get("outlet_limit_C"),
        "within_spec": result.get("within_spec"),
        "outlet_within_spec": result.get("outlet_within_spec"),
        "elapsed_seconds": result.get("elapsed_seconds"),
    }


def _simulate_dry_run(state: GraphState, params: dict, round_idx: int) -> dict:
    """
    Dry-run 假数据 — W3 升级: 让假温度跟着 inlet_velocity 走, 模拟"加风速 → outlet 降温"
    这样 iter_planner 真能触发 continue 分支, 验拓扑不用真起 Fluent.
    """
    print(f"[simulate] [dry-run] round {round_idx + 1}, 跳过 Fluent")

    # 假物理: outlet_avg_T = 295 + 5/v_inlet (风速越大 → outlet 越接近 cold-inlet 295K)
    # 给 1 m/s 时 ~300K=27°C, 给 5 m/s 时 ~296K=23°C, 给 10 m/s 时 ~295.5K=22.4°C
    v = float(params.get("inlet_velocity_ms", 1.0))
    outlet_T_K = 295.0 + 5.0 / max(v, 0.1)
    fake_run_summary = {
        "max_temp_K": 313.15,                # mixing_elbow hot-inlet 钉死
        "max_temp_C": 40.00,
        "outlet_avg_temp_K": outlet_T_K,
        "outlet_avg_temp_C": round(outlet_T_K - 273.15, 2),
        "converged": True,
        "iterations": params.get("max_iterations", 100),
        "elapsed_seconds": 0.0,
        "velocity_applied": True,
        "params_echo": dict(params),
    }
    from agents.result_analyzer import summarize
    enriched = summarize(fake_run_summary, params)
    enriched["contour_image_path"] = None
    enriched["contour_skipped_reason"] = "dry-run"
    enriched["report_text_path"] = None
    enriched["result_json_path"] = None
    enriched["dry_run"] = True

    new_history = list(state.get("iter_history", []))
    new_history.append({
        "round": round_idx + 1,
        "params": dict(params),
        "result_kpi": _extract_kpi(enriched),
        "plan": None,
    })
    return {
        "result": enriched,
        "round_idx": round_idx + 1,
        "iter_history": new_history,
    }


# ============================================================
# Node 3: iter_planner — 决定要不要再跑一轮
# ============================================================

def iter_planner_node(state: GraphState) -> dict:
    """
    [W3 Day 16] 看 last_result, 决定 continue 还是 stop.

    返回值的形态:
      stop  → {last_plan: <plan dict>}  (params/result 都不动, 让 report 用)
      cont  → {params: next_params, last_plan: <plan>}  (params 被覆盖, 下一轮 simulate 读它)

    迭代终止靠 conditional_edge 看 last_plan["action"], 不靠这里 raise/return 特殊值.
    """
    if state.get("error"):
        return {}

    result = state.get("result")
    params = state.get("params")
    if not result or not params:
        return {"error": "iter_planner: 缺 result 或 params"}

    round_idx = int(state.get("round_idx", 0))
    max_rounds = int(state.get("max_rounds", 3))
    metric_key = state.get("iter_metric") or "outlet_avg_temp_C"
    limit_key = state.get("iter_limit_key") or "outlet_limit_C"

    try:
        from agents.iter_planner import plan_next_round
        plan = plan_next_round(
            last_params=params,
            last_result=result,
            round_idx=round_idx,
            max_rounds=max_rounds,
            metric_key=metric_key,
            limit_key=limit_key,
        )
    except Exception as e:
        traceback.print_exc()
        return {"error": f"iter_planner 节点失败: {type(e).__name__}: {e}"}

    print(f"\n[iter_planner] round={round_idx}, action={plan['action']}, "
          f"reason={plan.get('stop_reason')}")
    print(f"[iter_planner] {plan['reasoning']}")

    # progress hook: 让 UI 实时看到决策
    if plan["action"] == "continue":
        next_v = (plan.get("next_params") or {}).get("inlet_velocity_ms")
        _emit("iter_planner.continue",
              f"未达标 → 调风速到 {next_v} m/s, 进入第 {round_idx + 1} 轮",
              round=round_idx, next_velocity=next_v,
              reasoning=plan.get("reasoning"))
    else:
        _emit("iter_planner.stop",
              f"终止 ({plan.get('stop_reason')}) · {plan.get('reasoning')}",
              round=round_idx, stop_reason=plan.get("stop_reason"),
              reasoning=plan.get("reasoning"))

    # 把 plan 写回 iter_history 最后一条 (那条是 simulate_node 刚加的)
    new_history = list(state.get("iter_history", []))
    if new_history:
        new_history[-1] = dict(new_history[-1], plan=dict(plan))

    update: dict = {
        "last_plan": dict(plan),
        "iter_history": new_history,
    }
    if plan["action"] == "continue":
        # 下一轮 simulate 节点会读 state["params"] — 把它覆盖掉
        update["params"] = plan["next_params"]
    return update


def iter_decide(state: GraphState) -> str:
    """
    [W3 Day 16] 条件边的 router 函数. 由 LangGraph 调用,
    返回字符串 = "continue" | "stop", 决定下一节点是 simulate 还是 report.
    """
    plan = state.get("last_plan") or {}
    if state.get("error"):
        return "stop"
    return "continue" if plan.get("action") == "continue" else "stop"


# ============================================================
# Node 4: report — 终端打印 + 列产物
# ============================================================

def report_node(state: GraphState) -> dict:
    """打印最终结果 + 迭代历史表. 上游 error 也走这, 显示友好错误."""
    print("\n" + "=" * 60)
    print("流水线最终结果")
    print("=" * 60)

    err = state.get("error")
    if err:
        print(f"\n[FAIL] 流水线出错:\n  {err}")
        _print_iter_history(state)
        _emit("report.error", f"流水线出错: {err}")
        return {}

    result = state.get("result")
    if not result:
        print("\n[FAIL] 没拿到 result")
        _emit("report.error", "report: 没拿到 result")
        return {}

    _emit("report.start", "生成 PDF + RAG 引文 + 历史记录...")

    summary_text = result.get("summary_text")
    if summary_text:
        print("\n" + summary_text)
    else:
        print(f"\n  最高温度: {result.get('max_temp_K', '?')} K")
        print(f"  出口平均: {result.get('outlet_avg_temp_K', '?')} K")
        print(f"  收敛: {result.get('converged', '?')}")

    print("\n[产物]")
    for label, key in [
        ("温度云图 PNG", "contour_image_path"),
        ("文字报告 .txt", "report_text_path"),
        ("结构化 JSON", "result_json_path"),
    ]:
        p = result.get(key)
        if p:
            print(f"  {label}: {p}")
        else:
            reason = ""
            if key == "contour_image_path":
                reason = result.get("contour_skipped_reason") or ""
            print(f"  {label}: (未生成{': ' + reason if reason else ''})")

    print("\n[KPI]")
    print(f"  within_spec        : {result.get('within_spec')}")
    print(f"  margin_C           : {result.get('margin_C')} °C")
    print(f"  outlet_within_spec : {result.get('outlet_within_spec')}")
    print(f"  outlet_margin_C    : {result.get('outlet_margin_C')} °C")
    print(f"  耗时               : {result.get('elapsed_seconds')} 秒")

    # W3 迭代历史表
    _print_iter_history(state)

    # [W4] RAG 引文: 给 last_plan.reasoning 找一段工程依据
    _print_rag_citation(state)

    # [W4] 最终 PDF: 重生成一次, 包含完整 iter_history (analyze 单轮 hook 不知道历史)
    _regenerate_pdf_with_history(state, result)

    # 历史记录追加 (失败不阻断)
    _append_to_history(state)

    _emit("report.done",
          f"报告产物已生成 · max_T={result.get('max_temp_C')}°C, "
          f"outlet={result.get('outlet_avg_temp_C')}°C")

    print("\n" + "=" * 60)
    return {}


def _append_to_history(state: GraphState) -> None:
    """把本次 run 摘要追加到 reports/history.jsonl."""
    try:
        from agents.history_store import append_run
        entry = append_run(state)
        print(f"[report] 历史记录已追加: {entry.get('timestamp')}")
    except Exception as e:
        print(f"[report][WARN] 历史追加失败 (非致命): "
              f"{type(e).__name__}: {e}", file=sys.stderr)


def _print_rag_citation(state: GraphState) -> None:
    """[W4] 调 rag_advisor 给最终 plan 找一段工程引文打到终端."""
    last_plan = state.get("last_plan") or {}
    reasoning = last_plan.get("reasoning") or ""
    if not reasoning:
        return
    try:
        from agents.rag_advisor import cite_for_plan
        cite = cite_for_plan(reasoning, top_k=1)
    except Exception as e:
        print(f"[report][WARN] RAG 检索失败 (非致命): {e}", file=sys.stderr)
        return
    if cite:
        print(f"\n{cite}")


def _regenerate_pdf_with_history(state: GraphState, result: dict) -> None:
    """[W4] 多轮迭代结束后重生成 PDF, 把 iter_history 表合进去."""
    history = state.get("iter_history") or []
    if not history:
        return  # 单轮, analyze hook 已经写过 PDF, 不必重做
    out_dir = None
    rp = result.get("report_text_path")
    if rp:
        out_dir = os.path.dirname(rp)
    try:
        from tools.pdf_reporter import generate_pdf_report
        pdf_path = generate_pdf_report(result, iter_history=history, out_dir=out_dir)
        print(f"[report] 含迭代历史的最终 PDF: {pdf_path}")
    except Exception as e:
        print(f"[report][WARN] 最终 PDF 重生成失败 (非致命): "
              f"{type(e).__name__}: {e}", file=sys.stderr)


def _print_iter_history(state: GraphState) -> None:
    """[W3 Day 16] 打印迭代历史: 每轮 inlet_velocity / metric / 是否达标."""
    history = state.get("iter_history") or []
    if not history:
        return
    metric = state.get("iter_metric") or "outlet_avg_temp_C"
    print("\n[迭代历史]")
    print(f"  共 {len(history)} 轮 (metric={metric}):")
    print(f"  {'round':>5} {'v(m/s)':>8} {metric+'(°C)':>22} "
          f"{'within_spec':>12} {'action':>10}")
    for h in history:
        v = h.get("params", {}).get("inlet_velocity_ms", "?")
        kpi = h.get("result_kpi", {})
        m_C = kpi.get(metric)
        ws = kpi.get("outlet_within_spec" if "outlet" in metric else "within_spec")
        plan = h.get("plan") or {}
        act = plan.get("action") or "—"
        m_str = f"{m_C:.2f}" if isinstance(m_C, (int, float)) else "?"
        print(f"  {h['round']:>5} {v!s:>8} {m_str:>22} "
              f"{ws!s:>12} {act:>10}")
    last_plan = state.get("last_plan") or {}
    if last_plan:
        print(f"\n  终止原因: {last_plan.get('stop_reason')}")
        print(f"  说明    : {last_plan.get('reasoning')}")


# ============================================================
# Graph 构建
# ============================================================

def build_graph(checkpointer: Any = None):
    """
    构建并编译 LangGraph state graph.

    Args:
        checkpointer: LangGraph checkpointer (W3 用 SqliteSaver). None = 不持久化.

    返回: 已编译的 graph.
    """
    from langgraph.graph import StateGraph, END

    workflow = StateGraph(GraphState)
    workflow.add_node("intent", intent_node)
    workflow.add_node("case_retriever", case_retriever_node)  # [W4 子模块 2]
    workflow.add_node("simulate", simulate_node)
    workflow.add_node("iter_planner", iter_planner_node)
    workflow.add_node("report", report_node)

    workflow.set_entry_point("intent")
    workflow.add_edge("intent", "case_retriever")
    workflow.add_edge("case_retriever", "simulate")
    workflow.add_edge("simulate", "iter_planner")

    # [W3 Day 16] 条件边: iter_planner → simulate (continue) 或 → report (stop)
    workflow.add_conditional_edges(
        "iter_planner",
        iter_decide,
        {"continue": "simulate", "stop": "report"},
    )
    workflow.add_edge("report", END)

    if checkpointer is not None:
        return workflow.compile(checkpointer=checkpointer)
    return workflow.compile()


# ============================================================
# Checkpoint 工具 (SqliteSaver)
# ============================================================

def make_sqlite_checkpointer(db_path: Optional[str] = None):
    """
    [W3 Day 16] 构造 SqliteSaver checkpointer.

    Args:
        db_path: sqlite 文件路径. None = 项目根/.langgraph_checkpoint.sqlite

    Returns:
        (checkpointer, context_manager) — 用法见 run_pipeline.

    SqliteSaver.from_conn_string 返回的是 context manager, 必须 with 包住才有效.
    我们这里把它"展开"成普通对象, 让 graph.invoke 能用.
    依赖: pip install langgraph-checkpoint-sqlite
    """
    if db_path is None:
        db_path = os.path.join(_PROJECT_ROOT, ".langgraph_checkpoint.sqlite")
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError as e:
        raise ImportError(
            "缺 langgraph-checkpoint-sqlite. 装: pip install langgraph-checkpoint-sqlite"
        ) from e
    # SqliteSaver.from_conn_string 是 contextmanager — __enter__ 返回 saver 实例.
    cm = SqliteSaver.from_conn_string(db_path)
    saver = cm.__enter__()
    print(f"[checkpoint] SqliteSaver 已挂上: {db_path}")
    return saver, cm


# ============================================================
# 公开 API: run_pipeline
# ============================================================

def _run_mesh_study_path(query: str, case_mode: str, dry_run: bool) -> dict:
    """
    [W4+ mesh study] 绕过 graph 的专用路径.
    1. 调 intent_parser 解析 query 拿 params
    2. 调 mesh_study_runner.run_mesh_study 跑 3 档真仿真 + 2 档外推
    3. 落盘 artifacts 到 reports/run_<ts>/, 返回合成的 state dict
    """
    import os
    import datetime

    state: dict = {"query": query, "case_mode": case_mode}

    _emit("mesh_study.start",
          f"启动网格独立性研究 · case={case_mode}" +
          (" (dry-run)" if dry_run else ""),
          case_mode=case_mode, dry_run=dry_run)

    # case_mode 必须 manifold_cht, 否则当前没 adapt_levels 能力
    if case_mode != "manifold_cht":
        msg = (f"mesh_study 当前仅支持 case_mode='manifold_cht', "
               f"收到 '{case_mode}'")
        _emit("mesh_study.error", msg)
        state["error"] = msg
        return state

    # intent 解析
    _emit("intent.start", "DeepSeek 解析 query...")
    try:
        from agents.intent_parser import parse_intent
        params = parse_intent(query)
        state["params"] = params
        brief = {k: params.get(k) for k in
                 ("chip_power_watts", "inlet_velocity_ms", "max_iterations")
                 if k in params}
        _emit("intent.done", f"意图解析完成 · {brief}", params=brief)
    except Exception as e:
        _emit("intent.error", f"intent 解析失败: {e}")
        state["error"] = f"intent 解析失败: {type(e).__name__}: {e}"
        return state

    if dry_run:
        # dry-run: 用假数据跑 study, 测拓扑
        _emit("mesh_study.dry_run",
              "Dry-run: 用假数据模拟 L1/L2/L3, 跳过 Fluent")
        from tools.mesh_study_runner import _recommend_mesh, _richardson_extrapolate
        fake_real = [
            {"level": "L1", "cell_count": 290000, "max_temp_C": 100.0,
             "outlet_avg_temp_C": 50.0, "pressure_drop_pa": 1000.0,
             "elapsed_s": 110, "source": "real"},
            {"level": "L2", "cell_count": 580000, "max_temp_C": 96.5,
             "outlet_avg_temp_C": 49.3, "pressure_drop_pa": 992.0,
             "elapsed_s": 230, "source": "real"},
            {"level": "L3", "cell_count": 1160000, "max_temp_C": 95.8,
             "outlet_avg_temp_C": 49.1, "pressure_drop_pa": 989.0,
             "elapsed_s": 480, "source": "real"},
        ]
        for r in fake_real:
            _emit("mesh_level.done",
                  f"{r['level']} (dry-run) · ~{r['cell_count']:,} cells, "
                  f"max_T={r['max_temp_C']}°C", **r)
        extra = _richardson_extrapolate(fake_real, [("L4", 2320000), ("L5", 4640000)])
        _emit("mesh_extrapolate", "Richardson 外推 L4 / L5 ...")
        all_rows = fake_real + extra
        rec = _recommend_mesh(all_rows, eps=0.01)
        study = {
            "rows": all_rows,
            "recommended_level": rec["level"],
            "recommended_reason": rec["reason"],
            "convergence_status": rec["status"],
            "eps": 0.01,
            "extrapolation_method": "richardson_p2_3d",
            "dry_run": True,
        }
    else:
        # 真跑
        try:
            from tools.mesh_study_runner import run_mesh_study
            study = run_mesh_study(params, eps=0.01, progress=_emit)
        except Exception as e:
            import traceback
            traceback.print_exc()
            _emit("mesh_study.error", f"mesh_study 跑飞: {e}")
            state["error"] = f"mesh_study 跑飞: {type(e).__name__}: {e}"
            return state

    _emit("mesh_study.recommended",
          f"推荐档位: {study['recommended_level']} · "
          f"{study.get('recommended_reason', '')}",
          level=study["recommended_level"],
          reason=study.get("recommended_reason"))

    # 落盘
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "reports",
        f"run_{ts}_mesh_study",
    )
    try:
        from tools.mesh_study_runner import save_mesh_study_artifacts
        artifacts = save_mesh_study_artifacts(study, run_dir)
        study["artifacts"] = artifacts
        study["run_dir"] = run_dir
    except Exception as e:
        print(f"[pipeline][WARN] mesh_study artifact 落盘失败: {e}")

    state["mesh_study"] = study

    # [W4+ mesh study] 写进 history.jsonl (跟常规 run 同一份历史文件, 类型字段区分)
    try:
        from agents.history_store import append_run
        entry = append_run(state)
        print(f"[mesh_study] 历史记录已追加: {entry.get('timestamp')} "
              f"(run_type=mesh_study, recommended={study.get('recommended_level')})")
    except Exception as e:
        print(f"[mesh_study][WARN] 历史追加失败 (非致命): "
              f"{type(e).__name__}: {e}")

    return state


def run_pipeline(
    query: str,
    dry_run: bool = False,
    case_mode: str = "mixing_elbow",
    max_rounds: int = 3,
    thread_id: Optional[str] = None,
    use_checkpoint: bool = False,
    resume: bool = False,
    mesh_study: bool = False,
    progress=None,
) -> dict:
    """
    端到端跑一次流水线. 给 run.py 和单元测试用同一个入口.

    Args:
        query / dry_run / case_mode / max_rounds / thread_id / use_checkpoint /
        resume / mesh_study: 见各自字段说明.
        progress: [W4+ UI streaming] Optional callable(event: dict) -> None.
            每个节点开始/结束时会被调用, UI 端可接成 st.status() 实时显示.
            不传 = 传统一次性返回, 跟原行为兼容.

    Returns:
        最终 state dict.
    """
    global _PROGRESS_HOOK
    _PROGRESS_HOOK = progress
    try:
        # [W4+ mesh study] 特殊路径 - 绕过 graph
        if mesh_study:
            return _run_mesh_study_path(query, case_mode, dry_run)

        return _run_normal_pipeline(
            query=query, dry_run=dry_run, case_mode=case_mode,
            max_rounds=max_rounds, thread_id=thread_id,
            use_checkpoint=use_checkpoint, resume=resume,
        )
    finally:
        _PROGRESS_HOOK = None


def _run_normal_pipeline(
    query: str, dry_run: bool, case_mode: str, max_rounds: int,
    thread_id: Optional[str], use_checkpoint: bool, resume: bool,
) -> dict:
    """常规 W1-W3 路径 — 原 run_pipeline 的实现, 抽出来让 progress hook 生命周期管清楚."""
    # 选 metric_key 配套: mixing_elbow 用 outlet, manifold 用 max_temp
    if case_mode == "manifold_cht":
        iter_metric = "max_temp_C"
        iter_limit_key = "limit_C"
    else:
        iter_metric = "outlet_avg_temp_C"
        iter_limit_key = "outlet_limit_C"

    # 准备 checkpointer
    cm = None
    checkpointer = None
    if use_checkpoint:
        checkpointer, cm = make_sqlite_checkpointer()

    try:
        graph = build_graph(checkpointer=checkpointer)

        if resume:
            if not thread_id:
                raise ValueError("--resume 必须配 --thread-id")
            # resume: 不传 initial state, langgraph 自己从 checkpoint 恢复
            print(f"[resume] thread_id={thread_id}")
            cfg = {"configurable": {"thread_id": thread_id}}
            return graph.invoke(None, config=cfg)

        initial: GraphState = {
            "query": query,
            "dry_run": dry_run,
            "case_mode": case_mode,
            "round_idx": 0,
            "max_rounds": max_rounds,
            "iter_history": [],
            "iter_metric": iter_metric,
            "iter_limit_key": iter_limit_key,
        }

        if checkpointer is not None:
            tid = thread_id or f"run-{int(time.time())}"
            cfg = {"configurable": {"thread_id": tid}}
            print(f"[checkpoint] thread_id={tid}")
            # 设置较大的 recursion_limit 防多轮迭代撞 LangGraph 默认 25 上限
            cfg["recursion_limit"] = 50
            return graph.invoke(initial, config=cfg)
        else:
            return graph.invoke(initial, config={"recursion_limit": 50})

    finally:
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception:
                pass


# ============================================================
# 自测试: dry-run 跑 3 句中文
# ============================================================

_SELFTEST_QUERIES = [
    "芯片功耗 15 瓦, 风速 0.4 米每秒, 跑 200 步",
    "5 kW 的芯片, 风速 50 厘米每秒",
    "随便跑跑试试",
]


def _selftest() -> int:
    print("=" * 60)
    print("自测试 (dry-run, 无 Fluent, 验迭代闭环)")
    print("=" * 60)

    n_pass = 0
    n_fail = 0

    for i, q in enumerate(_SELFTEST_QUERIES, 1):
        print(f"\n--- Case {i}/{len(_SELFTEST_QUERIES)} ---")
        try:
            final = run_pipeline(q, dry_run=True, max_rounds=3)
        except Exception as e:
            print(f"[FAIL] graph.invoke 抛异常: {type(e).__name__}: {e}")
            traceback.print_exc()
            n_fail += 1
            continue

        if final.get("error"):
            print(f"[WARN] state.error: {final['error']}")
            n_fail += 1
            continue

        history = final.get("iter_history") or []
        if not history:
            print("[FAIL] iter_history 为空")
            n_fail += 1
            continue

        last_plan = final.get("last_plan") or {}
        action = last_plan.get("action")
        if action != "stop":
            print(f"[FAIL] 迭代未终止: action={action}")
            n_fail += 1
            continue

        print(f"[OK] {len(history)} 轮迭代, 终止原因={last_plan.get('stop_reason')}")
        n_pass += 1

    print("\n" + "=" * 60)
    print(f"Summary: {n_pass} PASS / {n_fail} FAIL (共 {len(_SELFTEST_QUERIES)})")
    print("=" * 60)

    if n_fail == 0:
        print("自测试 PASSED — 迭代闭环可用.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(_selftest())
