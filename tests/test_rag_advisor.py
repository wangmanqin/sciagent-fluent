"""
tests/test_rag_advisor.py — W4 RAG 接口
"""
from agents.rag_advisor import retrieve, cite_for_plan, Snippet


def test_retrieve_returns_snippets():
    results = retrieve("风速 多少合适")
    assert len(results) > 0
    assert all(isinstance(s, Snippet) for s in results)


def test_retrieve_score_descending():
    results = retrieve("CHT 网格")
    if len(results) >= 2:
        for i in range(len(results) - 1):
            assert results[i].score >= results[i + 1].score


def test_retrieve_top_k():
    results = retrieve("风速 流速 对流", top_k=1)
    assert len(results) == 1


def test_retrieve_no_match_returns_empty():
    results = retrieve("完全无关的词 xyzabc123")
    assert results == []


def test_cite_for_plan_returns_string_or_none():
    cite = cite_for_plan("第 1 轮超标, 风速 0.4 → 1.0")
    assert cite is None or isinstance(cite, str)
