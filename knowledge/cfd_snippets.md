# CFD 工程经验片段 — SciAgent-Fluent 知识库

> **W4 RAG 接口的最小知识库**. 每个段落 frontmatter 标 `topic` / `keywords` / `source`,
> 让 `agents/rag_advisor.py` 关键词命中后回引文.

---

## 强制对流换热经验关联式
- topic: convection_velocity_temperature
- keywords: 风速,流速,inlet_velocity,对流换热,温度,降温
- source: Incropera Fundamentals of Heat and Mass Transfer Ch.7

强制对流的 Nusselt 数对雷诺数指数小于 1 (典型 0.5-0.8), 即 `Nu ~ Re^n`.
对流换热系数 `h = Nu·k/D ~ v^n`. 入口风速翻倍, 换热系数大约提升 1.4-1.7 倍,
表面温度下降但**非线性**: 风速从 0.4 翻到 1.6 (4倍) 时温度降幅大约只是 1.4-2 倍体感.

工程结论: 调参时不要期待"风速翻倍 = 温度降一半". 0.5-3 m/s 区间下,
每 +1 m/s 大致对应 5-15 °C 降幅 (随几何/材质变化).

---

## 工业冷却风道典型工况
- topic: typical_velocity_range
- keywords: 风速,工况,合理范围,默认值,典型值
- source: 数据中心 / 电子冷却工程手册经验

PCB 强制风冷: 1-3 m/s 是甜区. <0.4 m/s 接近自然对流, 失去"强制"意义;
>5 m/s 流噪声大, 风扇能耗激增, 工程上很少用.
`iter_planner` 把上限设 10 m/s 是软安全网, 真接近 10 应当主动报"几何/边界设定问题".

---

## 共轭传热 (CHT) 网格策略
- topic: cht_mesh
- keywords: 共轭传热,CHT,网格,mesh,网格独立性
- source: Ansys Fluent User Guide §13.2

CHT 案例 (流-固耦合) 的网格关键: 流-固界面要 `share topology` + 法向至少 5 层棱柱.
官方 example case (manifold_solution) 已经满足, 我们的 `chip_power` source term 是
叠加在已收敛温度场上的小扰动, **mesh 收敛性继承自原 case** — 不需要做完整的 mesh
independence study (粗/中/细对比), 这是 docs/网格策略.md 的论证依据.

---

## chip_power 物理映射的工程合理性
- topic: chip_power_mapping
- keywords: chip_power,功率,体积热源,W/m3,密度
- source: SciAgent-Fluent 设计思路 §6.2 + W2 Day 8 实测

W2 把 query 里的 `chip_power_watts` 换算成 `Q = power / chip_volume_default(10cm³)`,
而不是除以 `solid_up` 真实 691 cm³, 是因为:

- 691 cm³ 上加 15W = 21,700 W/m³ → ΔT 约 0.1 K, 落进 Fluent 数值噪声里看不见.
- 10 cm³ 假设让 15W = 1.5 MW/m³, ΔT 约 100-150 K, demo 视觉效果可见.
- 真做 PCB+chip 案例时, 5 cm³ silicon 上放 30W ≈ 6 MW/m³ — 跟我们的虚拟映射量级一致.

诚实声明: 这是工程简化, 不是真实 PCB 几何. README known limit #2 写明.

---

## 多轮迭代终止条件 (W3)
- topic: iteration_termination
- keywords: 迭代,多轮,终止,停止,收敛,死循环
- source: SciAgent-Fluent W3 设计 + LangGraph 最佳实践

W3 多轮迭代必须有三层终止信号 (按优先级):
1. **达标** (`within_spec=True`): 核心成功路径
2. **预算用尽** (`round_idx >= max_rounds`, 默认 3): 防漫无边际
3. **撞顶** (`inlet_velocity >= 10 m/s`): 物理上承认"光靠加风速救不回来"

绝对不能让 LLM 自己 yes/no 决定停不停 — agentic 系统死循环最常见的来源.
踩坑预防: `iter_planner` 是规则版, max_rounds 是 graph 节点强制 cut.
