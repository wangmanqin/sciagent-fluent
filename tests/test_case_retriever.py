"""tests/test_case_retriever.py - W4 案例模板库."""
from agents.case_retriever import (
    retrieve_case, suggest_params, list_all_cases, CaseTemplate,
)


def test_load_cases_returns_list():
    cases = list_all_cases()
    assert len(cases) >= 4  # 至少 4 份 (manifold_low, manifold_high, elbow, smoke)
    assert all(isinstance(c, CaseTemplate) for c in cases)


def test_retrieve_manifold_low_power():
    c = retrieve_case("管壁 15W 演示")
    assert c is not None
    assert c.case_id == "manifold_low_power_15w"
    assert c.score > 0


def test_retrieve_manifold_high_power():
    c = retrieve_case("50W 中功率 manifold")
    assert c is not None
    assert c.case_id == "manifold_high_power_50w"


def test_retrieve_elbow_iter():
    c = retrieve_case("出口低于 21 度 多轮迭代")
    assert c is not None
    assert c.case_id == "mixing_elbow_iter_demo"


def test_retrieve_no_match_returns_none():
    c = retrieve_case("完全无关的字符 xyz123")
    assert c is None


def test_empty_query_returns_none():
    assert retrieve_case("") is None


def test_suggest_params_returns_baseline():
    p = suggest_params("管壁 15W")
    assert p is not None
    assert "chip_power_watts" in p
    assert "inlet_velocity_ms" in p
    assert p["chip_power_watts"] == 15.0


def test_suggest_params_no_match():
    p = suggest_params("xyz 完全不匹配")
    assert p is None


def test_score_descending():
    """如果命中多个 case, 应该按 score 降序选."""
    # smoke 案例 keywords 含 "随便" — 跟 manifold_high "中功率" 一句 query 同时命中
    c = retrieve_case("管壁 15W 演示 mixing_elbow")
    assert c is not None
    # mixing_elbow 也命中 (case_mode), 但 manifold_low 应该 score 更高
    assert c.case_id == "manifold_low_power_15w"
