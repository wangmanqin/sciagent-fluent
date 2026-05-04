# manifold_solution.cas.h5 zone 清单

> 基于 manifold_solution.cas.h5 探查. 踩坑 #9 后从 unit_battery 切换过来, 用已 setup CHT case.

- 来源: `C:\Users\wmq30\Desktop\sciagent_fluent\sciagent-fluent\manifold_solution.cas.h5` (47.4 MB)
- + dat: `C:\Users\wmq30\Desktop\sciagent_fluent\sciagent-fluent\manifold_solution.dat.h5` (20.6 MB) — 已收敛解
- 探查: 2026-04-29 20:57:46

## Cell Zones

| 名字 | 类型 | Day 9 用途 |
|------|------|-----------|
| `fluid1` | fluid | 热气体流域 (case 自带的边界条件已经驱动它) |
| `solid_up` | solid | **chip 主体候选** — Day 9 在这里加 source_terms.energy |

## Face Zones

| 名字 | BC 类型 |
|------|---------|
| `in1` | wall |
| `in2` | wall |
| `in3` | wall |
| `out1` | wall |
| `solid_up:1:830` | wall |
| `solid_up:1` | wall |
| `solid_up:1:830-shadow` | wall |
| `inlet` | velocity_inlet |
| `inlet1` | velocity_inlet |
| `inlet2` | velocity_inlet |
| `outlet` | pressure_outlet |

## Baseline (无 chip_power)

| solid zone | max_temp (K) | max_temp (°C) | volume (m³) | volume (cm³) |
|-----------|--------------|---------------|-------------|--------------|
| `solid_up` | 830.25 | 557.10 | 6.9104e-04 | 691.04 |

## Day 9 行动项

1. 在 `tools/fluent_wrapper.py` 顶部加常量:
   ```python
   CASE_REMOTE = ("manifold_solution.cas.h5", "pyfluent/exhaust_manifold")
   DAT_REMOTE  = ("manifold_solution.dat.h5", "pyfluent/exhaust_manifold")
   CHIP_ZONE_NAME = "solid_up"  # ← W2 "管壁内嵌芯片"
   ```
2. `_apply_chip_power` 在 solid zone 加 source_terms.
   先验证 modern API 行不行 (manifold case 已 setup, 比裸 mesh 兼容性好)
3. baseline 跑 50 步对比: 加 source vs 不加 source 的 max_temp
   不加 source 已知 = 上面表里的值, 加了 source 应该更高
4. **诚实声明**: 这是"管壁内嵌芯片"简化映射, 不是真'PCB+chip die'.
   写到 README known limit 里, demo 时主动说出来.
