"""
agents/case_retriever.py - 案例模板库 (W4 老师鼓励 2: 知识沉淀与复用)
============================================================
回答老师答辩问题: "类似的工况你们做过吗? 直接给我推荐基线参数."
答: 有, 这个模块扫 knowledge/cases/*.json, 关键词命中后推荐 baseline_params.

工作原理:
  1. 加载 knowledge/cases/*.json (每份 = 一个历史案例)
  2. 给一个 query, 用 tags + good_for_query_keywords 命中打分
  3. 返回得分最高的 case, 调用方可以 mege 它的 baseline_params 当默认值

跟 RAG (rag_advisor) 的区别:
  - rag_advisor: 答 "为什么这么调" (工程引文)
  - case_retriever: 答 "我以前做过类似的, 直接复用" (历史 case)
  老师鼓励 2 原文 "模板复用、案例检索" 这两件事都要有.

接口:
  >>> from agents.case_retriever import retrieve_case, suggest_params
  >>> case = retrieve_case("帮我跑个 50W 芯片高温 demo")
  >>> case.case_id
  'manifold_high_power_50w'
  >>> suggest_params("出口低于 21 度 多轮迭代")
  {"chip_power_watts": 15.0, "inlet_velocity_ms": 0.4, ...}

测试:
  python -m agents.case_retriever
"""
import glob
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional


_CASES_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "knowledge", "cases"
))


@dataclass
class CaseTemplate:
    case_id: str
    title: str
    description: str
    tags: List[str]
    case_mode: str
    baseline_params: Dict
    expected_kpi: Dict
    good_for_query_keywords: List[str]
    score: float = 0.0


# 模块缓存
_CACHE: Optional[List[CaseTemplate]] = None


def _load_cases() -> List[CaseTemplate]:
    """加载 knowledge/cases/*.json 成 CaseTemplate 列表."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    cases = []
    if not os.path.isdir(_CASES_DIR):
        _CACHE = []
        return _CACHE
    for fp in sorted(glob.glob(os.path.join(_CASES_DIR, "*.json"))):
        try:
            with open(fp, encoding="utf-8") as f:
                d = json.load(f)
            cases.append(CaseTemplate(
                case_id=d.get("case_id", os.path.basename(fp)),
                title=d.get("title", ""),
                description=d.get("description", ""),
                tags=d.get("tags", []),
                case_mode=d.get("case_mode", "mixing_elbow"),
                baseline_params=d.get("baseline_params", {}),
                expected_kpi=d.get("expected_kpi", {}),
                good_for_query_keywords=d.get("good_for_query_keywords", []),
            ))
        except (json.JSONDecodeError, OSError) as e:
            print("[case_retriever][WARN] " + fp + ": " + str(e), file=sys.stderr)
    _CACHE = cases
    return cases


def retrieve_case(query: str, top_k: int = 1) -> Optional[CaseTemplate]:
    """
    返回最匹配的历史案例 (top_k=1 默认), 没匹配返 None.
    打分规则: 命中一个 tag/keyword +1, 命中 case_mode +0.5.
    """
    if not query:
        return None
    cases = _load_cases()
    if not cases:
        return None

    q_lower = query.lower()
    scored: List[CaseTemplate] = []
    for c in cases:
        score = 0.0
        for kw in c.tags + c.good_for_query_keywords:
            if kw.lower() in q_lower:
                score += 1.0
        # case_mode 出现在 query 也加分
        if c.case_mode.lower() in q_lower:
            score += 0.5
        if score > 0:
            scored.append(CaseTemplate(
                case_id=c.case_id, title=c.title, description=c.description,
                tags=c.tags, case_mode=c.case_mode,
                baseline_params=c.baseline_params, expected_kpi=c.expected_kpi,
                good_for_query_keywords=c.good_for_query_keywords,
                score=score,
            ))
    if not scored:
        return None
    scored.sort(key=lambda x: x.score, reverse=True)
    return scored[0] if top_k == 1 else scored[:top_k]


def suggest_params(query: str) -> Optional[Dict]:
    """便捷接口: 直接返回最匹配 case 的 baseline_params (浅拷贝, 调用方可 merge)."""
    case = retrieve_case(query)
    if case is None:
        return None
    return dict(case.baseline_params)


def list_all_cases() -> List[CaseTemplate]:
    """供前端 / CLI 列出整个案例库 (无打分排序)."""
    return list(_load_cases())


# ============================================================
# 自测试
# ============================================================

def _selftest() -> int:
    print("=" * 60)
    print("case_retriever selftest")
    print("=" * 60)

    cases = _load_cases()
    print("loaded " + str(len(cases)) + " cases:")
    for c in cases:
        print("  - " + c.case_id + " (mode=" + c.case_mode + ")")
    print()

    test_queries = [
        ("管壁排气 15W 演示", "manifold_low_power_15w"),
        ("50W 芯片对比", "manifold_high_power_50w"),
        ("出口低于 21 度 mixing_elbow 多轮", "mixing_elbow_iter_demo"),
        ("快速 smoke 测试一下", "quick_smoke_test"),
    ]
    n_pass = 0
    n_fail = 0
    for q, expected_id in test_queries:
        c = retrieve_case(q)
        if c is None:
            print("[FAIL] '" + q + "' -> 未检索到 case")
            n_fail += 1
            continue
        if c.case_id == expected_id:
            print("[OK]   '" + q + "' -> " + c.case_id + " (score=" + str(c.score) + ")")
            n_pass += 1
        else:
            print("[FAIL] '" + q + "' -> got " + c.case_id + ", expected " + expected_id)
            n_fail += 1

    print()
    print("Summary: " + str(n_pass) + " PASS / " + str(n_fail) + " FAIL")

    # suggest_params 形态测试
    p = suggest_params("管壁 15W")
    if p and "chip_power_watts" in p:
        print("[OK]   suggest_params returns " + str(len(p)) + " fields")
    else:
        print("[FAIL] suggest_params 返回异常: " + str(p))
        n_fail += 1

    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(_selftest())
