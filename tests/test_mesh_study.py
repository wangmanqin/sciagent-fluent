"""
tests/test_mesh_study.py
============================================================
W4+ unit tests - mesh_study_runner rules + pipeline dry-run.
No Fluent.
"""
from tools.mesh_study_runner import (
    _recommend_mesh,
    _richardson_extrapolate,
)


def _make_row(level, cells, max_T, outlet, dP, source="real"):
    return {
        "level": level,
        "cell_count": cells,
        "max_temp_C": max_T,
        "outlet_avg_temp_C": outlet,
        "pressure_drop_pa": dP,
        "elapsed_s": 100,
        "source": source,
    }


def test_recommend_converged_L2():
    rows = [
        _make_row("L1", 290000, 100.0, 50.0, 1000.0),
        _make_row("L2", 580000, 95.0, 49.5, 990.0),
        _make_row("L3", 1160000, 94.5, 49.4, 985.0),
    ]
    rec = _recommend_mesh(rows, eps=0.01)
    assert rec["level"] == "L2"
    assert rec["status"] == "converged"


def test_recommend_not_converged():
    rows = [
        _make_row("L1", 290000, 100.0, 50.0, 1000.0),
        _make_row("L2", 580000, 80.0, 40.0, 800.0),
        _make_row("L3", 1160000, 70.0, 35.0, 700.0),
    ]
    rec = _recommend_mesh(rows, eps=0.01)
    assert rec["status"] == "not_converged"
    assert rec["level"] == "L3"


def test_recommend_none_metric_safe():
    rows = [
        _make_row("L1", 290000, 100.0, None, 1000.0),
        _make_row("L2", 580000, 99.5, None, 999.0),
    ]
    rec = _recommend_mesh(rows, eps=0.01)
    assert rec["status"] == "not_converged"


def test_recommend_empty():
    rec = _recommend_mesh([], eps=0.01)
    assert rec["level"] is None


def test_recommend_eps_loose():
    rows = [
        _make_row("L1", 290000, 100.0, 50.0, 1000.0),
        _make_row("L2", 580000, 97.0, 48.5, 970.0),
        _make_row("L3", 1160000, 96.5, 48.3, 965.0),
    ]
    rec = _recommend_mesh(rows, eps=0.05)
    assert rec["level"] == "L1"
    assert rec["status"] == "converged"


def test_richardson_basic():
    real = [
        _make_row("L1", 290000, 100.0, 50.0, 1000.0),
        _make_row("L2", 580000, 96.0, 49.0, 990.0),
        _make_row("L3", 1160000, 95.0, 48.8, 985.0),
    ]
    extra = _richardson_extrapolate(real, [("L4", 2320000), ("L5", 4640000)])
    assert len(extra) == 2
    assert extra[0]["source"] == "extrapolated"
    d_L2_L3 = abs(real[2]["max_temp_C"] - real[1]["max_temp_C"])
    d_L3_L4 = abs(extra[0]["max_temp_C"] - real[2]["max_temp_C"])
    assert d_L3_L4 < d_L2_L3


def test_richardson_insufficient_data():
    real = [_make_row("L1", 290000, 100.0, 50.0, 1000.0)]
    extra = _richardson_extrapolate(real, [("L2", 580000), ("L3", 1160000)])
    assert len(extra) == 2
    assert extra[0].get("extrapolation_failed") is True
    assert extra[0]["max_temp_C"] is None


def test_richardson_partial_none():
    real = [
        _make_row("L1", 290000, 100.0, 50.0, None),
        _make_row("L2", 580000, 96.0, 49.0, None),
        _make_row("L3", 1160000, 95.0, 48.8, None),
    ]
    extra = _richardson_extrapolate(real, [("L4", 2320000), ("L5", 4640000)])
    assert extra[0]["max_temp_C"] is not None
    assert extra[0]["pressure_drop_pa"] is None


def test_pipeline_mesh_study_dry_run_manifold():
    from graph.pipeline import run_pipeline
    r = run_pipeline(
        query="chip 50W, velocity 1 m/s",
        dry_run=True,
        case_mode="manifold_cht",
        mesh_study=True,
    )
    assert not r.get("error"), f"unexpected error: {r.get('error')}"
    study = r.get("mesh_study")
    assert study is not None
    assert len(study["rows"]) == 5
    assert study["recommended_level"] in ("L1", "L2", "L3", "L4", "L5")
    assert study["convergence_status"] in ("converged", "not_converged")


def test_pipeline_mesh_study_rejects_elbow():
    from graph.pipeline import run_pipeline
    r = run_pipeline(
        query="chip 15W",
        dry_run=True,
        case_mode="mixing_elbow",
        mesh_study=True,
    )
    assert r.get("error") is not None
    assert "manifold_cht" in r["error"]

