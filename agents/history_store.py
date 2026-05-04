"""
agents/history_store.py - 历史记录存储
============================================================
用 JSONL (每行一个 JSON 对象) 追加式存储每一次 run 的精简摘要.
为什么不用 sqlite: JSON 文件人眼能读 + VSCode 直接打开, demo 友好.
为什么不用 JSON (单个 list): JSONL 追加是 O(1), 不用读整个文件改再写.

接口:
  append_run(final_state) -> history_entry dict
  load_history(limit=20)  -> list[dict] 最新在前
  clear_history()         -> bool
  history_stats()         -> dict (次数, 达标率, 平均耗时等)

测试:
  python -m agents.history_store
"""
import datetime
import json
import os
import sys
from typing import Any, Dict, List


_HISTORY_FILE = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "reports", "history.jsonl"
))


def _ensure_dir() -> None:
    os.makedirs(os.path.dirname(_HISTORY_FILE), exist_ok=True)


def append_run(final_state: Dict[str, Any]) -> Dict[str, Any]:
    """把一次 run 的精简摘要追加到 history.jsonl, 返回写入的 entry."""
    result = final_state.get("result") or {}
    params = final_state.get("params") or result.get("params_echo") or {}
    history = final_state.get("iter_history") or []
    last_plan = final_state.get("last_plan") or {}
    mesh_study = final_state.get("mesh_study") or {}

    # [W4+ mesh study] 特殊 run 类型 — 没有单次 result 只有 5 档对比表
    is_mesh_study = bool(mesh_study)

    entry: Dict[str, Any] = {
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "query": final_state.get("query"),
        "case_mode": final_state.get("case_mode"),
        "run_type": "mesh_study" if is_mesh_study else "simulation",
        "params": dict(params),
        "max_temp_C": result.get("max_temp_C"),
        "outlet_avg_temp_C": result.get("outlet_avg_temp_C"),
        "elapsed_seconds": result.get("elapsed_seconds"),
        "converged": result.get("converged"),
        "active_constraints": result.get("active_constraints") or [],
        "failed_constraints": result.get("failed_constraints") or [],
        "all_within_spec": result.get("all_within_spec"),
        "within_spec": result.get("within_spec"),
        "outlet_within_spec": result.get("outlet_within_spec"),
        "retrieved_case_id": final_state.get("retrieved_case_id"),
        "chip_zone": (result.get("hotspot") or {}).get("zone"),
        "iter_rounds": len(history),
        "stop_reason": last_plan.get("stop_reason"),
        "contour_image_path": result.get("contour_image_path"),
        "report_text_path": result.get("report_text_path"),
        "result_json_path": result.get("result_json_path"),
        "pdf_report_path": result.get("pdf_report_path"),
        "error": final_state.get("error"),
    }

    # [W4+ mesh study] 扩展字段 - 只有 mesh_study run 才有
    if is_mesh_study:
        entry["mesh_study_recommended_level"] = mesh_study.get("recommended_level")
        entry["mesh_study_recommended_reason"] = mesh_study.get("recommended_reason")
        entry["mesh_study_convergence_status"] = mesh_study.get("convergence_status")
        entry["mesh_study_rows_count"] = len(mesh_study.get("rows") or [])
        entry["mesh_study_extrapolation_method"] = mesh_study.get("extrapolation_method")
        entry["mesh_study_run_dir"] = mesh_study.get("run_dir")
        # 每档关键数据 (精简版, 跟 rows 保持同步)
        entry["mesh_study_rows"] = [
            {
                "level": r.get("level"),
                "cell_count": r.get("cell_count"),
                "max_temp_C": r.get("max_temp_C"),
                "outlet_avg_temp_C": r.get("outlet_avg_temp_C"),
                "pressure_drop_pa": r.get("pressure_drop_pa"),
                "elapsed_s": r.get("elapsed_s"),
                "source": r.get("source"),
            }
            for r in (mesh_study.get("rows") or [])
        ]
        # elapsed 用 3 档真跑耗时之和 (外推不耗时)
        real_elapsed = sum(
            r.get("elapsed_s") or 0
            for r in (mesh_study.get("rows") or [])
            if r.get("source") == "real"
        )
        entry["elapsed_seconds"] = round(real_elapsed, 1)

    try:
        _ensure_dir()
        with open(_HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError as e:
        print("[history_store][WARN] append 失败: " + str(e), file=sys.stderr)
    return entry


def load_history(limit: int = 20, newest_first: bool = True) -> List[Dict[str, Any]]:
    """加载历史. 默认返回最新 20 条, 最新在前. limit=0 返回全部."""
    if not os.path.isfile(_HISTORY_FILE):
        return []
    entries: List[Dict[str, Any]] = []
    try:
        with open(_HISTORY_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        print("[history_store][WARN] load 失败: " + str(e), file=sys.stderr)
        return []

    if newest_first:
        entries.reverse()
    if limit > 0:
        entries = entries[:limit]
    return entries


def clear_history() -> bool:
    if not os.path.isfile(_HISTORY_FILE):
        return True
    try:
        os.remove(_HISTORY_FILE)
        return True
    except OSError as e:
        print("[history_store][WARN] clear 失败: " + str(e), file=sys.stderr)
        return False


def history_stats() -> Dict[str, Any]:
    """统计摘要: 次数 / 达标率 / 平均耗时 / case_mode 分布."""
    entries = load_history(limit=0, newest_first=False)
    if not entries:
        return {
            "total": 0, "pass_count": 0, "fail_count": 0,
            "pass_rate": 0.0, "avg_elapsed_seconds": 0.0,
            "by_case_mode": {},
        }
    n = len(entries)
    pass_count = sum(1 for e in entries if e.get("all_within_spec") is True)
    fail_count = sum(1 for e in entries if e.get("all_within_spec") is False)
    elapsed_vals = [e.get("elapsed_seconds") for e in entries
                    if isinstance(e.get("elapsed_seconds"), (int, float))]
    avg_elapsed = sum(elapsed_vals) / len(elapsed_vals) if elapsed_vals else 0.0
    by_case: Dict[str, int] = {}
    for e in entries:
        cm = e.get("case_mode") or "unknown"
        by_case[cm] = by_case.get(cm, 0) + 1
    return {
        "total": n,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "pass_rate": pass_count / n if n > 0 else 0.0,
        "avg_elapsed_seconds": round(avg_elapsed, 2),
        "by_case_mode": by_case,
    }


def get_history_file_path() -> str:
    return _HISTORY_FILE


def _selftest() -> int:
    import tempfile
    global _HISTORY_FILE
    saved = _HISTORY_FILE
    _HISTORY_FILE = os.path.join(tempfile.gettempdir(), "_test_history.jsonl")
    if os.path.isfile(_HISTORY_FILE):
        os.remove(_HISTORY_FILE)

    try:
        print("=" * 60)
        print("history_store selftest")
        print("=" * 60)

        assert load_history() == []
        assert history_stats()["total"] == 0
        print("[OK] 空库加载正确")

        fake_states = [
            {
                "query": "15W demo", "case_mode": "manifold_cht",
                "params": {"chip_power_watts": 15.0},
                "result": {
                    "max_temp_C": 697, "elapsed_seconds": 30,
                    "converged": True,
                    "active_constraints": ["max_temp"], "failed_constraints": [],
                    "all_within_spec": True, "within_spec": True,
                    "hotspot": {"zone": "solid_up"},
                },
                "retrieved_case_id": "manifold_low_power_15w",
                "iter_history": [],
            },
            {
                "query": "50W 高温", "case_mode": "manifold_cht",
                "params": {"chip_power_watts": 50.0},
                "result": {
                    "max_temp_C": 1547, "elapsed_seconds": 92,
                    "active_constraints": ["max_temp"], "failed_constraints": ["max_temp"],
                    "all_within_spec": False, "within_spec": False,
                    "hotspot": {"zone": "solid_up"},
                },
                "iter_history": [],
            },
            {
                "query": "elbow 多轮", "case_mode": "mixing_elbow",
                "params": {"chip_power_watts": 15.0},
                "result": {
                    "max_temp_C": 40, "outlet_avg_temp_C": 21.67,
                    "active_constraints": ["outlet"], "failed_constraints": ["outlet"],
                    "all_within_spec": False, "outlet_within_spec": False,
                },
                "iter_history": [1, 2, 3],
                "last_plan": {"stop_reason": "max_rounds"},
            },
        ]
        for s in fake_states:
            append_run(s)
        print("[OK] 追加 3 条")

        loaded = load_history()
        assert len(loaded) == 3
        assert loaded[0]["query"] == "elbow 多轮"
        print("[OK] 加载 3 条, 最新在前")

        top1 = load_history(limit=1)
        assert len(top1) == 1
        print("[OK] limit=1")

        stats = history_stats()
        assert stats["total"] == 3
        assert stats["pass_count"] == 1
        assert stats["fail_count"] == 2
        assert stats["by_case_mode"]["manifold_cht"] == 2
        print("[OK] stats 正确")

        assert clear_history()
        assert load_history() == []
        print("[OK] 清空")

        print()
        print("Summary: all PASS")
        return 0
    finally:
        _HISTORY_FILE = saved


if __name__ == "__main__":
    sys.exit(_selftest())
