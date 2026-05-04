"""
tools/pressure_drop_query.py
============================================================
W4+ 增强模块 - 压降 + cell 数查询.
压降单位: Pa. None 表示查询失败.
"""
import sys
from typing import Optional


def query_pressure_drop(session, inlet_face="inlet", outlet_face="outlet") -> Optional[float]:
    p_in = _query_face_avg_pressure(session, inlet_face)
    p_out = _query_face_avg_pressure(session, outlet_face)
    if p_in is None or p_out is None:
        return None
    drop = p_in - p_out
    print(f"[pressure_drop] {p_in:.2f} - {p_out:.2f} = {drop:.2f} Pa")
    return drop


def _query_face_avg_pressure(session, face_name: str) -> Optional[float]:
    sr = None
    try:
        sr = session.settings.results.report.surface_integrals
    except Exception:
        sr = None

    if sr is not None:
        for method_name in ("get_area_weighted_avg", "get_area_weighted_average",
                            "get_facet_avg", "get_mass_weighted_avg"):
            method = getattr(sr, method_name, None)
            if method is None:
                continue
            for kwargs in (
                {"surface_names": [face_name], "report_of": "pressure"},
                {"surface_names": [face_name], "cell_function": "pressure"},
                {"surface_names": [face_name], "scalar": "pressure"},
            ):
                try:
                    r = method(**kwargs)
                    v = _coerce_float(r)
                    if v is not None:
                        return v
                except Exception:
                    continue

    try:
        import os, tempfile, re
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        tmp.close()
        tui_path = tmp.name.replace("\\", "/")
        try:
            session.tui.report.surface_integrals.area_weighted_avg(
                face_name, "()", "pressure", "yes", tui_path)
        except Exception:
            try:
                session.tui.report.surface_integrals.area_weighted_avg(
                    face_name, "pressure", "yes", tui_path)
            except Exception:
                if os.path.isfile(tmp.name):
                    os.unlink(tmp.name)
                return None
        if os.path.isfile(tmp.name):
            with open(tmp.name, "r", encoding="utf-8", errors="ignore") as f:
                txt = f.read()
            os.unlink(tmp.name)
            nums = re.findall(r"[-+]?\d+\.?\d*(?:[eE][-+]?\d+)?", txt)
            if nums:
                try:
                    return float(nums[-1])
                except ValueError:
                    pass
        return None
    except Exception:
        return None


def query_cell_count(session) -> Optional[int]:
    try:
        info = session.settings.mesh.size_info()
        if isinstance(info, dict):
            for k in ("cells", "n_cells", "Cells"):
                if k in info:
                    return int(info[k])
    except Exception:
        pass
    try:
        info = session.scheme_eval.scheme_eval('(report-system-info)')
        if isinstance(info, str):
            import re
            m = re.search(r"[Cc]ells?\s*[:=]?\s*(\d[\d,]*)", info)
            if m:
                return int(m.group(1).replace(",", ""))
    except Exception:
        pass
    return None


def _coerce_float(r) -> Optional[float]:
    if r is None:
        return None
    if isinstance(r, (int, float)):
        return float(r)
    if isinstance(r, dict) and r:
        return float(next(iter(r.values())))
    if isinstance(r, (list, tuple)) and r:
        return float(r[0])
    try:
        return float(r)
    except (TypeError, ValueError):
        return None

