"""tests/test_history_store.py - 历史记录存储."""
import os
import tempfile

import pytest

from agents import history_store


@pytest.fixture
def tmp_history(tmp_path, monkeypatch):
    """每个测试独立的临时 history 文件, 不污染真实 reports/history.jsonl."""
    fake_path = str(tmp_path / "test_history.jsonl")
    monkeypatch.setattr(history_store, "_HISTORY_FILE", fake_path)
    yield fake_path


def _fake_state(query="test", passed=True, elapsed=30.0, case_mode="manifold_cht"):
    return {
        "query": query,
        "case_mode": case_mode,
        "params": {"chip_power_watts": 15.0, "inlet_velocity_ms": 1.0},
        "result": {
            "max_temp_C": 700, "outlet_avg_temp_C": 400,
            "elapsed_seconds": elapsed, "converged": True,
            "active_constraints": ["max_temp"],
            "failed_constraints": [] if passed else ["max_temp"],
            "all_within_spec": passed, "within_spec": passed,
            "hotspot": {"zone": "solid_up"},
        },
        "retrieved_case_id": "manifold_low_power_15w",
        "iter_history": [],
    }


def test_empty_history_returns_empty_list(tmp_history):
    assert history_store.load_history() == []


def test_empty_stats(tmp_history):
    s = history_store.history_stats()
    assert s["total"] == 0
    assert s["pass_rate"] == 0.0


def test_append_and_load(tmp_history):
    history_store.append_run(_fake_state())
    loaded = history_store.load_history()
    assert len(loaded) == 1
    assert loaded[0]["query"] == "test"


def test_append_multiple_newest_first(tmp_history):
    history_store.append_run(_fake_state(query="old"))
    history_store.append_run(_fake_state(query="new"))
    loaded = history_store.load_history()
    assert loaded[0]["query"] == "new"
    assert loaded[1]["query"] == "old"


def test_limit(tmp_history):
    for i in range(5):
        history_store.append_run(_fake_state(query="q" + str(i)))
    top2 = history_store.load_history(limit=2)
    assert len(top2) == 2


def test_stats_accuracy(tmp_history):
    history_store.append_run(_fake_state(passed=True, elapsed=30))
    history_store.append_run(_fake_state(passed=False, elapsed=50))
    history_store.append_run(_fake_state(passed=True, elapsed=40, case_mode="mixing_elbow"))
    s = history_store.history_stats()
    assert s["total"] == 3
    assert s["pass_count"] == 2
    assert s["fail_count"] == 1
    assert abs(s["pass_rate"] - 2/3) < 1e-6
    assert s["avg_elapsed_seconds"] == 40.0
    assert s["by_case_mode"]["manifold_cht"] == 2
    assert s["by_case_mode"]["mixing_elbow"] == 1


def test_clear(tmp_history):
    history_store.append_run(_fake_state())
    assert history_store.load_history() != []
    assert history_store.clear_history() is True
    assert history_store.load_history() == []


def test_clear_nonexistent_file_ok(tmp_history):
    assert history_store.clear_history() is True


def test_entry_has_key_fields(tmp_history):
    history_store.append_run(_fake_state(query="关键字段测试"))
    e = history_store.load_history()[0]
    assert "timestamp" in e
    assert e["query"] == "关键字段测试"
    assert e["case_mode"] == "manifold_cht"
    assert e["chip_zone"] == "solid_up"
    assert e["retrieved_case_id"] == "manifold_low_power_15w"
    assert e["all_within_spec"] is True


def test_error_state_also_recorded(tmp_history):
    history_store.append_run({"query": "崩了", "error": "something broke"})
    e = history_store.load_history()[0]
    assert e["error"] == "something broke"
