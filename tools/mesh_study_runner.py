"""
tools/mesh_study_runner.py
============================================================
W4+ 增强模块 - 网格独立性研究.

策略:
  L1=baseline (290K cells, adapt_levels=0)
  L2=refine x1 (~580K)
  L3=refine x2 (~1.16M)
  L4/L5 用 Richardson 半衰减外推, UI 上明确标 "extrapolated".

每档收集: max_temp_C, outlet_avg_temp_C, pressure_drop_pa, elapsed_s, cell_count.
推荐算法: 从粗到细扫, 第一档"再加密一档相对变化全 < eps" 的就是性价比最高.

CLI: python -m tools.mesh_study_runner --power 50 --quick
     python -m tools.mesh_study_runner --selftest   (规则单测, 不启 Fluent)
"""
import argparse
import json
import os
import sys
import time
import traceback


REAL_LEVELS = [
    ("L1", 0, 290_000),
    ("L2", 1, 580_000),
    ("L3", 2, 1_160_000),
]
EXTRAPOLATED_LEVELS = [
    ("L4", 2_320_000),
    ("L5", 4_640_000),
]
DEFAULT_EPS = 0.01

# [W4+ 严谨化] 外推衰减率 — 匹配 Fluent 默认二阶迎风离散.
#   error ~ h^2 ~ N^(-2/3) in 3D, 所以 cell 数翻倍 → 相邻档变化乘 2^(-2/3) ≈ 0.63.
#   旧版用 0.5 (一阶/激进), 偏低估 L4/L5 误差 ~25%. 改成 0.63 跟二阶算子匹配.
#   严格 Richardson (GCI) 应该解 ln 方程拟合 p, 这里固定 p=2/3 是工程近似.
_DECAY_PER_DOUBLING = 2 ** (-2 / 3)  # ≈ 0.63


def run_mesh_study(params, eps=DEFAULT_EPS, quick=False, real_levels=None,
                   progress=None):
    """3 档真跑 + 2 档外推, 返推荐档.

    progress: Optional callable(event_type, message, **data) — UI streaming hook.
    """
    if real_levels is None:
        real_levels = REAL_LEVELS

    def _p(t, msg, **data):
        if progress is not None:
            try:
                progress(t, msg, **data)
            except Exception:
                pass

    sim_params = dict(params)
    if quick:
        sim_params["max_iterations"] = min(sim_params.get("max_iterations", 50), 25)

    real_rows = []
    from tools.fluent_wrapper_w2 import run_simulation
    for (lvl_name, adapt_n, est_count) in real_levels:
        print("=" * 60)
        print(f"[mesh_study] {lvl_name}: adapt={adapt_n}, ~{est_count:,} cells")
        print("=" * 60)
        _p("mesh_level.start",
           f"启动 {lvl_name} · ~{est_count:,} cells, adapt={adapt_n}",
           level=lvl_name, adapt_levels=adapt_n, est_count=est_count)
        t0 = time.time()
        try:
            r = run_simulation(sim_params, adapt_levels=adapt_n)
            row = {
                "level": lvl_name,
                "cell_count": r.get("cell_count") or est_count,
                "max_temp_C": r.get("max_temp_C"),
                "outlet_avg_temp_C": r.get("outlet_avg_temp_C"),
                "pressure_drop_pa": r.get("pressure_drop_pa"),
                "elapsed_s": r.get("elapsed_seconds") or round(time.time() - t0, 1),
                "source": "real",
                "adapt_levels": adapt_n,
            }
            real_rows.append(row)
            _p("mesh_level.done",
               f"{lvl_name} 完成 · max_T={row['max_temp_C']}°C, "
               f"outlet={row['outlet_avg_temp_C']}°C, "
               f"ΔP={row['pressure_drop_pa']}Pa, 耗时 {row['elapsed_s']}s",
               **row)
        except Exception as e:
            traceback.print_exc()
            print(f"[mesh_study][ERROR] {lvl_name}: {e}", file=sys.stderr)
            _p("mesh_level.error", f"{lvl_name} 失败: {e}", level=lvl_name)
            real_rows.append({
                "level": lvl_name, "cell_count": est_count,
                "max_temp_C": None, "outlet_avg_temp_C": None,
                "pressure_drop_pa": None,
                "elapsed_s": round(time.time() - t0, 1),
                "source": "real", "adapt_levels": adapt_n, "error": str(e),
            })

    _p("mesh_extrapolate", "Richardson 外推 L4/L5 (二阶离散衰减率 ~0.63)...")
    extra_rows = _richardson_extrapolate(real_rows, EXTRAPOLATED_LEVELS)
    all_rows = real_rows + extra_rows
    rec = _recommend_mesh(all_rows, eps=eps)

    return {
        "rows": all_rows,
        "recommended_level": rec["level"],
        "recommended_reason": rec["reason"],
        "convergence_status": rec["status"],
        "eps": eps,
        "extrapolation_method": "richardson_p2_3d",  # cell^(-2/3), 二阶离散
    }


def _richardson_extrapolate(real_rows, extra_levels):
    """
    用最后两档 (L_{n-1}, L_n) 外推 L_{n+1}, L_{n+2}.

    衰减率 _DECAY_PER_DOUBLING = 2^(-2/3) ≈ 0.63 — 匹配 Fluent 默认二阶迎风离散下
    error ~ N^(-2/3) (3D). 比"半衰减 0.5"更严谨, 比"严格 GCI"更轻量.
    严格 Richardson 要解 ln 方程拟合收敛阶 p, 留作 W5+ 升级.
    """
    valid = [r for r in real_rows if r.get("max_temp_C") is not None]
    if len(valid) < 2:
        return [{
            "level": lvl, "cell_count": cc,
            "max_temp_C": None, "outlet_avg_temp_C": None,
            "pressure_drop_pa": None, "elapsed_s": None,
            "source": "extrapolated", "extrapolation_failed": True,
        } for (lvl, cc) in extra_levels]

    last = valid[-1]
    prev = valid[-2]
    last_n = int(last["level"][1:])
    metrics = ["max_temp_C", "outlet_avg_temp_C", "pressure_drop_pa"]

    rows = []
    for (lvl, est_count) in extra_levels:
        new_n = int(lvl[1:])
        steps = new_n - last_n
        row = {
            "level": lvl, "cell_count": est_count,
            "elapsed_s": None, "source": "extrapolated",
        }
        for m in metrics:
            v_last = last.get(m)
            v_prev = prev.get(m)
            if v_last is None or v_prev is None:
                row[m] = None
                continue
            d = v_last - v_prev
            v_new = v_last
            for _ in range(steps):
                d = d * _DECAY_PER_DOUBLING  # ← 0.63 (二阶离散), 原来是 0.5
                v_new = v_new + d
            row[m] = round(v_new, 2) if v_new == v_new else None
        rows.append(row)
    return rows


def _recommend_mesh(rows, eps=DEFAULT_EPS):
    """从粗到细扫, 第一档相邻变化全 < eps 即为推荐."""
    metrics = ["max_temp_C", "outlet_avg_temp_C", "pressure_drop_pa"]

    for i in range(len(rows) - 1):
        cur = rows[i]
        nxt = rows[i + 1]
        changes = {}
        all_below = True
        for m in metrics:
            v_cur = cur.get(m)
            v_nxt = nxt.get(m)
            if v_cur is None or v_nxt is None or v_cur == 0:
                all_below = False
                break
            rel = abs(v_nxt - v_cur) / abs(v_cur)
            changes[m] = rel
            if rel >= eps:
                all_below = False
        if all_below and changes:
            biggest = max(changes.items(), key=lambda kv: kv[1])
            return {
                "level": cur["level"],
                "reason": (f"{cur['level']}→{nxt['level']} 最大相对变化 "
                           f"{biggest[0]}={biggest[1]*100:.2f}% < {eps*100:.0f}% 阈值"),
                "status": "converged",
            }

    last = rows[-1] if rows else None
    if last is None:
        return {"level": None, "reason": "无数据", "status": "not_converged"}
    return {
        "level": last["level"],
        "reason": (f"所有相邻档变化 ≥ {eps*100:.0f}%, 未真正收敛, "
                   f"建议进一步细化超过 {last['level']}"),
        "status": "not_converged",
    }


def save_mesh_study_artifacts(study, run_dir):
    os.makedirs(run_dir, exist_ok=True)
    artifacts = {}
    summary_path = os.path.join(run_dir, "mesh_study_summary.json")
    try:
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(study, f, ensure_ascii=False, indent=2)
        artifacts["mesh_study_summary_json"] = summary_path
    except Exception as e:
        print(f"[mesh_study][WARN] write summary.json: {e}", file=sys.stderr)

    table_path = os.path.join(run_dir, "mesh_study_table.txt")
    try:
        with open(table_path, "w", encoding="utf-8") as f:
            f.write(_render_table(study))
        artifacts["mesh_study_table_txt"] = table_path
    except Exception as e:
        print(f"[mesh_study][WARN] write table.txt: {e}", file=sys.stderr)

    png_path = os.path.join(run_dir, "mesh_convergence.png")
    try:
        _plot_convergence(study, png_path)
        artifacts["mesh_convergence_png"] = png_path
    except Exception as e:
        print(f"[mesh_study][WARN] plot convergence.png: {e}", file=sys.stderr)

    return artifacts


def _render_table(study):
    rows = study.get("rows", [])
    rec = study.get("recommended_level")
    lines = []
    lines.append("=" * 86)
    lines.append("Mesh Independence Study")
    lines.append("=" * 86)
    lines.append(
        f"{'level':<8}{'cells':>12}{'max_T(C)':>14}"
        f"{'outlet(C)':>14}{'dP(Pa)':>12}{'time(s)':>10}{'source':>14}"
    )
    lines.append("-" * 86)
    for r in rows:
        marker = "*" if r["level"] == rec else " "
        lines.append(
            f"{r['level']}{marker:<6}"
            f"{(r.get('cell_count') or 0):>10,}  "
            f"{_fmt(r.get('max_temp_C')):>13}"
            f"{_fmt(r.get('outlet_avg_temp_C')):>13}"
            f"{_fmt(r.get('pressure_drop_pa')):>12}"
            f"{_fmt(r.get('elapsed_s')):>10}"
            f"{r.get('source', '-'):>14}"
        )
    lines.append("-" * 86)
    lines.append(f"recommended:  {rec}")
    lines.append(f"reason:       {study.get('recommended_reason', '-')}")
    lines.append(f"status:       {study.get('convergence_status', '-')}")
    lines.append("=" * 86)
    return "\n".join(lines)


def _plot_convergence(study, png_path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = study.get("rows", [])
    if not rows:
        return
    cells = [r.get("cell_count") for r in rows]
    max_T = [r.get("max_temp_C") for r in rows]
    outlet = [r.get("outlet_avg_temp_C") for r in rows]
    pdrop = [r.get("pressure_drop_pa") for r in rows]
    sources = [r.get("source", "real") for r in rows]
    rec = study.get("recommended_level")
    levels = [r.get("level", "?") for r in rows]

    real_idx = [i for i, s in enumerate(sources) if s == "real"]
    extra_idx = [i for i, s in enumerate(sources) if s != "real"]

    def _f(arr, idx):
        return [arr[i] for i in idx]

    fig, ax1 = plt.subplots(figsize=(8, 4.5))
    ax2 = ax1.twinx()

    if any(v is not None for v in max_T):
        if real_idx:
            ax1.plot(_f(cells, real_idx), _f(max_T, real_idx),
                     "o-", color="#cf222e", label="max_temp (real)")
        if extra_idx:
            ax1.plot(_f(cells, extra_idx), _f(max_T, extra_idx),
                     "o--", color="#cf222e", alpha=0.5, label="max_temp (extrap.)")
    if any(v is not None for v in outlet):
        if real_idx:
            ax1.plot(_f(cells, real_idx), _f(outlet, real_idx),
                     "s-", color="#0969da", label="outlet_avg (real)")
        if extra_idx:
            ax1.plot(_f(cells, extra_idx), _f(outlet, extra_idx),
                     "s--", color="#0969da", alpha=0.5, label="outlet_avg (extrap.)")
    if any(v is not None for v in pdrop):
        if real_idx:
            ax2.plot(_f(cells, real_idx), _f(pdrop, real_idx),
                     "^-", color="#1a7f37", label="dP (real)")
        if extra_idx:
            ax2.plot(_f(cells, extra_idx), _f(pdrop, extra_idx),
                     "^--", color="#1a7f37", alpha=0.5, label="dP (extrap.)")

    for c in cells:
        if c is not None:
            ax1.axvline(c, color="#d0d7de", linestyle=":", linewidth=0.7, zorder=0)
    for r in rows:
        if r.get("level") == rec and r.get("cell_count"):
            ax1.axvline(r["cell_count"], color="#1a7f37", linewidth=2,
                        alpha=0.5, label=f"recommended: {rec}")
            break

    ax1.set_xscale("log")
    ax1.set_xlabel("cell count (log)")
    ax1.set_ylabel("Temperature (C)")
    ax2.set_ylabel("Pressure drop (Pa)")
    ax1.set_title(f"Mesh Independence Study - recommended: {rec}")
    h1, l1 = ax1.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax1.legend(h1 + h2, l1 + l2, loc="best", fontsize=8)
    ax1.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(png_path, dpi=110)
    plt.close(fig)


def _fmt(v):
    if v is None:
        return "-"
    if isinstance(v, float):
        if v != v:
            return "-"
        return f"{v:.2f}"
    return str(v)


def _selftest():
    print("=" * 60)
    print("mesh_study_runner self-test")
    print("=" * 60)
    n_pass, n_fail = 0, 0

    # A: 单调收敛
    rows_A = [
        {"level": "L1", "cell_count": 290000, "max_temp_C": 100.0,
         "outlet_avg_temp_C": 50.0, "pressure_drop_pa": 1000.0},
        {"level": "L2", "cell_count": 580000, "max_temp_C": 95.0,
         "outlet_avg_temp_C": 49.5, "pressure_drop_pa": 990.0},
        {"level": "L3", "cell_count": 1160000, "max_temp_C": 94.5,
         "outlet_avg_temp_C": 49.4, "pressure_drop_pa": 985.0},
    ]
    rec = _recommend_mesh(rows_A, eps=0.01)
    if rec["level"] == "L2" and rec["status"] == "converged":
        print(f"[OK] A monotonic conv -> L2"); n_pass += 1
    else:
        print(f"[FAIL] A: {rec}"); n_fail += 1

    # B: not converged
    rows_B = [
        {"level": "L1", "cell_count": 290000, "max_temp_C": 100.0,
         "outlet_avg_temp_C": 50.0, "pressure_drop_pa": 1000.0},
        {"level": "L2", "cell_count": 580000, "max_temp_C": 80.0,
         "outlet_avg_temp_C": 40.0, "pressure_drop_pa": 800.0},
    ]
    rec = _recommend_mesh(rows_B, eps=0.01)
    if rec["status"] == "not_converged":
        print(f"[OK] B not-converged -> {rec['level']}"); n_pass += 1
    else:
        print(f"[FAIL] B: {rec}"); n_fail += 1

    # C: None data safe
    rows_C = [
        {"level": "L1", "cell_count": 290000, "max_temp_C": 100.0,
         "outlet_avg_temp_C": None, "pressure_drop_pa": 1000.0},
        {"level": "L2", "cell_count": 580000, "max_temp_C": 99.5,
         "outlet_avg_temp_C": None, "pressure_drop_pa": 999.0},
    ]
    try:
        rec = _recommend_mesh(rows_C, eps=0.01)
        if rec["status"] == "not_converged":
            print(f"[OK] C None data safe"); n_pass += 1
        else:
            print(f"[FAIL] C unexpected pass: {rec}"); n_fail += 1
    except Exception as e:
        print(f"[FAIL] C raised: {e}"); n_fail += 1

    # D: Richardson 外推半衰减
    real = [
        {"level": "L1", "cell_count": 290000, "max_temp_C": 100.0,
         "outlet_avg_temp_C": 50.0, "pressure_drop_pa": 1000.0},
        {"level": "L2", "cell_count": 580000, "max_temp_C": 96.0,
         "outlet_avg_temp_C": 49.0, "pressure_drop_pa": 990.0},
        {"level": "L3", "cell_count": 1160000, "max_temp_C": 95.0,
         "outlet_avg_temp_C": 48.8, "pressure_drop_pa": 985.0},
    ]
    extra = _richardson_extrapolate(real, [("L4", 2320000), ("L5", 4640000)])
    if (len(extra) == 2 and extra[0]["max_temp_C"] is not None
            and extra[0]["source"] == "extrapolated"):
        d_L3_L4 = abs(extra[0]["max_temp_C"] - real[2]["max_temp_C"])
        d_L2_L3 = abs(real[2]["max_temp_C"] - real[1]["max_temp_C"])
        if d_L3_L4 < d_L2_L3:
            print(f"[OK] D Richardson L4 max_T={extra[0]['max_temp_C']}, "
                  f"d(L3->L4)={d_L3_L4:.2f} < d(L2->L3)={d_L2_L3:.2f}")
            n_pass += 1
        else:
            print(f"[FAIL] D extrapolation didn't decay"); n_fail += 1
    else:
        print(f"[FAIL] D extrap shape: {extra}"); n_fail += 1

    print("\n" + "=" * 60)
    print(f"Summary: {n_pass} PASS / {n_fail} FAIL")
    print("=" * 60)
    return 0 if n_fail == 0 else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--power", type=float, default=50.0)
    ap.add_argument("--velocity", type=float, default=1.0)
    ap.add_argument("--iters", type=int, default=50)
    ap.add_argument("--eps", type=float, default=DEFAULT_EPS)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()

    params = {
        "chip_power_watts": args.power,
        "inlet_velocity_ms": args.velocity,
        "max_iterations": args.iters,
    }
    study = run_mesh_study(params, eps=args.eps, quick=args.quick)
    print()
    print(_render_table(study))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)

