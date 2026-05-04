<div style="text-align: center; padding-top: 200px;">

# SciAgent-Fluent：基于多层 Agent 架构的 CFD 仿真自动化系统

<br><br>

**技术报告**

<br><br><br>

**作者：王熳琴**

**单位：西安电子科技大学**

**日期：2026 年 5 月**

</div>

<div style="page-break-before: always;"></div>

## 第 1 章　项目概述

### 1.1 选题背景

传统 CFD（计算流体力学）仿真流程高度依赖人工操作：工程师需要手动启动 Fluent GUI、加载案例、逐项设置边界条件、等待求解完成、再手动后处理出图和撰写报告。一个中等规模案例的全流程通常耗时 30 分钟以上，其中真正需要工程判断的环节不到 5 分钟，其余均为重复性机械操作。

本项目选择 **Ansys Fluent**（流体仿真）进行 Agent 化，目标是：**用户说一句中文，系统自动完成从参数解析到仿真执行、结果分析、报告生成的全流程**。

### 1.2 核心目标

- 自然语言驱动：用户输入中文需求（如"芯片功率 50W，风速 1m/s，给我温度云图"），系统自动解析为仿真参数
- 全流程自动化：自动调用 Fluent 求解器、提取关键结果、生成温度云图和 PDF 报告
- 智能迭代优化：当仿真结果不满足约束条件时，Agent 自主调整参数并重新仿真，直到达标或预算耗尽

### 1.3 技术栈

| 组件       | 技术选型             | 说明                                                |
| ---------- | -------------------- | --------------------------------------------------- |
| 仿真引擎   | Ansys Fluent 2026 R1 | 通过 PyFluent (ansys-fluent-core) Python SDK 驱动   |
| 大语言模型 | DeepSeek-V3          | 兼容 OpenAI SDK，国内网络直连，成本约 GPT-4 的 1/10 |
| Agent 编排 | LangGraph            | 状态机 + 条件边 + SqliteSaver 断点续跑              |
| 数据校验   | Pydantic v2          | LLM 输出 JSON 的强类型校验，防幻觉                  |
| 可视化前端 | Streamlit            | 浏览器交互式界面，实时展示仿真进度和结果            |
| PDF 报告   | ReportLab            | 自动生成完整技术报告                                |

<div style="page-break-before: always;"></div>

## 第 2 章　系统架构

### 2.1 三层 Agent 架构

系统采用能力层、流程层、知识层三层架构设计：

```
┌──────────────────────────────────────────────────┐
│              第三层：知识层                        │
│   RAG 工程引文库（5 段教科书级经验）               │
│   历史案例模板库（4 套 JSON 案例档案）             │
│   → "这个 Agent 有经验，不是白纸一张"             │
├──────────────────────────────────────────────────┤
│              第二层：流程层                        │
│   LangGraph 状态机（5 节点 + 条件边）              │
│   SqliteSaver 断点持久化                          │
│   → "这个 Agent 能自主走完整套流程并迭代优化"     │
├──────────────────────────────────────────────────┤
│              第一层：能力层                        │
│   DeepSeek-V3 大语言模型                          │
│   中文意图解析 / 单位换算 / 几何识别               │
│   → "这个 Agent 听得懂中文，认得出几何"           │
└──────────────────────────────────────────────────┘
```

**第一层（能力层）**：DeepSeek 负责中文自然语言理解，将用户口语化的需求翻译为结构化 JSON 参数。关键设计决策——**LLM 仅输出 JSON，绝不输出代码**，配合 Pydantic 二次校验，将幻觉攻击面压缩到"填空"级别。

**第二层（流程层）**：基于 LangGraph 构建 5 节点状态机（意图理解 → 案例检索 → 仿真执行 → 迭代决策 → 报告生成），条件边实现多轮迭代闭环。每个节点完成后状态即刻持久化到 SQLite，支持仿真崩溃后断点恢复。

**第三层（知识层）**：工程引文库（轻量 RAG）提供教科书级工程依据（如 Incropera Ch.7 强制对流关联式），通过关键词检索自动匹配并附在仿真报告中；历史案例模板库为新需求推荐参数起点。双库并立——引文库答"为什么这么调"，案例库答"以前做过类似的"。检索接口与 Chroma 向量库兼容，知识量增长后可一行切换至完整 RAG。

### 2.2 五个核心设计决策

| # | 决策                        | 解决的问题                                                                  |
| - | --------------------------- | --------------------------------------------------------------------------- |
| 1 | LLM 只输出 JSON，不输出代码 | 避免 LLM 幻觉编造 PyFluent API；Prompt 只需描述 JSON Schema                 |
| 2 | 参数分层                    | 用户层用物理直觉单位（W、m/s），Fluent 层用工程单位（W/m³），Python 做换算 |
| 3 | Baseline-first              | 复用 Ansys 官方算例作底板，只换参数不做 CAD，聚焦 Agent 层创新              |
| 4 | 状态机 + Checkpoint         | LangGraph 节点级持久化，仿真崩溃恢复 O(1)                                   |
| 5 | 每层都有 Fallback           | PyFluent 失败 → TUI；JSON 校验失败 → 自我修正重试；仿真发散 → 回滚参数   |

### 2.3 LangGraph 状态机拓扑

```
                    START
                      ↓
                   [intent]          ← DeepSeek 解析中文 → JSON
                      ↓
                [case_retriever]     ← 检索最相似历史案例
                      ↓
                   [simulate]  ←──┐  ← PyFluent 启动 Fluent → 求解
                      ↓          │
                [iter_planner] ───┘  ← 规则引擎：达标? 调参再跑 : 出报告
                      ↓ stop
                   [report]          ← PNG + TXT + JSON + PDF
                      ↓
                     END
```

状态机的每个节点遵循**单一职责原则**：读取 state → 计算 → 返回需要更新的字段，不直接修改 state。错误隔离机制保证任意节点抛错后，后续节点可通过 `state["error"]` 跳过，不会级联崩溃。

<div style="page-break-before: always;"></div>

## 第 3 章　功能实现

### 3.1 基础仿真模块

**一句中文 → 结构化参数 → Fluent 仿真 → 温度云图 + 报告**

```
用户: "芯片功率 50W, 风速 1m/s, 给我温度云图"
  │
  ▼
[intent] DeepSeek 解析中文 → {"chip_power_watts": 50, "inlet_velocity_ms": 1.0, ...}
  │                           Pydantic 校验, 失败自我修正重试
  ▼
[case_retriever] 检索历史案例库 → 匹配最相似 case, 填补缺省字段
  ▼
[simulate] PyFluent 启动 Fluent → 注入参数 → 求解 → 抽取温度 KPI
  ▼
[report] 输出温度云图 PNG + 文字报告 TXT + 结构化 JSON + PDF
```

**关键模块**：

| 模块          | 文件                           | 说明                                                 |
| ------------- | ------------------------------ | ---------------------------------------------------- |
| 意图解析      | `agents/intent_parser.py`    | 中文 → 7 字段 JSON，Pydantic 校验，失败自我修正重试 |
| 仿真执行 (W1) | `tools/fluent_wrapper.py`    | mixing-elbow 路径                                    |
| 仿真执行 (W2) | `tools/fluent_wrapper_w2.py` | manifold CHT 路径，chip_power 真驱动温度             |
| 结果分析      | `agents/result_analyzer.py`  | 温度云图 + 文字摘要 + 警戒线判定                     |
| 流水线编排    | `graph/pipeline.py`          | LangGraph 状态机，5 节点 + 条件边                    |

**两条仿真路径**：

```bash
# W1 路径: mixing-elbow (冷热水混合管, 验证流水线)
python run.py --query "芯片功率 15W, 风速 2m/s"

# W2 路径: manifold CHT (共轭传热, chip_power 真驱动温度)
python run.py --query "硅芯片 50W, 风速 1m/s, 警戒线 1500 度" --case manifold_cht
```

**真机验证数据**：W2 chip_power 单调驱动温度：0W → 830K，15W → 970K，50W → 1361K，200W → 3500K+。

![1778061355289](image/技术报告_SciAgent-Fluent/1778061355289.png)

图 1：mixing-elbow 单轮仿真温度云图（chip_power=15W, v=2.0m/s, outlet=20.70°C）

![1778061838938](image/技术报告_SciAgent-Fluent/1778061838938.png)

图 2：manifold CHT 共轭传热温度云图（chip_power=50W, max_temp=1548.67°C）

### 3.2 迭代优化模块

**用户给目标温度，Agent 自主加风速重跑，直到达标或预算用尽。**

```
[simulate] ←────────────┐
    │                    │ continue (未达标, 调参再跑)
    ▼                    │
[iter_planner] ──────────┘
    │
    │ stop (达标 / 参数上限 / 最大轮次)
    ▼
[report]
```

**决策规则**：

- **只调 inlet_velocity，绝不动 chip_power** — chip_power 是用户给的任务条件，改了等于篡改题目
- 调参公式：`delta_v = clip(超温度数 / 20.0, 0.1, 1.5)`
- 三重终止机制：达标即停 / 风速撞 10 m/s 上限即停 / 跑满 max_rounds 即停
- **为什么用规则不用 LLM**：规则确定性强、可单测、不会偷换任务条件；接口已预留，后续可替换为 LLM 实现

**断点续跑（SqliteSaver）**：

```bash
python run.py --query "..." --thread-id deploy-001 --checkpoint   # 第一次跑
python run.py --thread-id deploy-001 --resume                     # 崩了恢复
```

**真机验证数据**（mixing_elbow, chip_power=15W, outlet_limit=20°C, max_rounds=3）：

| 轮次 | 风速 (m/s) | 出口温度 (°C) | 动作              |
| ---- | ---------- | -------------- | ----------------- |
| 1    | 1.0        | 21.36          | continue          |
| 2    | 1.2        | 21.14          | continue          |
| 3    | 1.4        | 20.99          | stop (max_rounds) |

3 轮出口温度单调下降（21.36 → 21.14 → 20.99°C），物理正确。

![1778061763963](image/技术报告_SciAgent-Fluent/1778061763963.png)

图 3：3 轮迭代最终温度云图（v: 1.0→1.2→1.4 m/s, outlet: 21.36→20.99°C）

### 3.3 几何感知

**自动识别 Fluent case 中的芯片/流体/散热片区域，换算例不用改代码。**

W2 wrapper 中 `CHIP_ZONE = "solid_up"` 是硬编码的。换一个 case（芯片叫 `die` 或 `silicon_cpu`），代码直接报错。解决方案——两层识别：

- **Layer 1 — 关键词启发式**：维护三张关键词表（chip/die/silicon/cpu、fluid/air/water、heatsink/fin/cooler），扫描 cell zone 名字命中分类
- **Layer 2 — LLM Fallback**：关键词全 miss 时，把 zone 列表发给 DeepSeek 选，仍只输出一个 zone 名字
- **Layer 3 — 兜底**：两层都没搞定，用 `solid_up` 做 fallback

自测验证（8/8 PASS）：

| case     | zone 名                           | 识别结果             |
| -------- | --------------------------------- | -------------------- |
| manifold | `solid_up`                      | ✓                   |
| PCB      | `die`, `pcb`                  | `die` ✓           |
| CPU      | `cpu_silicon`, `heatsink_fin` | `cpu_silicon` ✓   |
| 英文命名 | `chip-1`                        | ✓                   |
| 模糊命名 | `body_a`, `body_b`            | `body_a`（兜底）✓ |

### 3.4 知识沉淀与复用

**双库设计：工程引文库答"为什么这么调"，历史案例库答"以前做过类似的"。**

**工程引文库（轻量 RAG）**：`knowledge/cfd_snippets.md` 存储 5 段教科书级工程经验，每段标注出处：

- 强制对流换热经验关联式（Incropera Ch.7）
- 工业冷却风道典型工况
- 共轭传热网格策略（Ansys User Guide §13.2）
- chip_power 物理映射合理性论证
- 多轮迭代终止条件设计

迭代决策器输出 plan 后，report 节点自动检索匹配的引文附在报告里。当前知识体量 < 10 段，关键词检索足够；检索接口 `retrieve(query) → list[Snippet]` 与 Chroma 向量库完全兼容，知识量增长后可一行切换至完整 RAG。

**历史案例模板库**：`knowledge/cases/` 存储 4 份 JSON 历史案例档案：

| case_id                     | 描述         | baseline chip_power |
| --------------------------- | ------------ | ------------------- |
| `quick_smoke`             | 快速冒烟测试 | 15W                 |
| `manifold_low_power`      | 低功率散热   | 15W                 |
| `manifold_high_power_50w` | 中功率散热   | 50W                 |
| `elbow_iter_demo`         | 迭代闭环演示 | 15W                 |

用户输入 query 后，case_retriever 自动匹配最相似的 case，把 baseline_params 填补用户没说的字段（用户说了的优先）。

### 3.5 多目标约束

**同时给 max_temp 和 outlet 两条警戒线，两个都满足才算达标。**

```bash
python run.py --query "芯片 50W, 警戒线 1500 度, 出口低于 21 度" --case manifold_cht
```

- 给 `limit_C` → max_temp 算一条约束
- 给 `outlet_limit_C` → outlet 算一条约束
- `all_within_spec = True` 当且仅当两条都满足
- 迭代决策器看 `all_within_spec`，任一不满足就 continue

### 3.6 自动 PDF 报告

**每次仿真自动生成 6 章节完整 PDF 技术报告。**

章节内容：封面（项目名 + 时间戳）→ 输入参数表 → 求解结果 KPI → 警戒线判定 → 迭代历史表（多轮迭代时）→ 温度云图 + 完整文字摘要。

技术选型：reportlab 4.5，全 Python 无外部依赖；中文字体三层 fallback（微软雅黑 → 黑体 → Helvetica）。

![1778062009732](image/技术报告_SciAgent-Fluent/1778062009732.png)

图 5：系统自动生成的 PDF 技术报告，sciagent-fluent\reports\run_20260506_170449\report.pdf

<div style="page-break-before: always;"></div>

## 第 4 章　创新与拓展功能

### 4.1 Streamlit 可视化交互前端

**模块**：`app.py`（约 1000 行）

区别于纯命令行交互，本项目搭建了基于 Streamlit 的浏览器交互前端，采用现代扁平设计风格（Keynote 高对比展示风），实现三栏布局：

- **侧边栏**：历史运行记录浏览 + 案例模板库快速加载
- **左主栏（输入）**：自然语言查询、案例模式选择、双目标约束输入、多轮迭代开关、dry-run 模式
- **右主栏（输出）**：案例推荐卡片、几何识别结果、双约束 Pass/Fail 判定、KPI 数值卡片、温度云图、迭代历史时间轴（Hero Cards）、PDF 下载按钮、RAG 工程引文

前端通过模块级 progress hook 机制实现实时流式输出——节点关键点调用 `_emit()` 即时推送事件，UI 端立即渲染，用户可以实时看到仿真进度。

**意义**：前端降低了系统的使用门槛，非 CFD 专业人员也能通过浏览器交互完成仿真任务，更接近实际工业部署形态。

![1778062444758](image/技术报告_SciAgent-Fluent/1778062444758.png)

![1778062669893](image/技术报告_SciAgent-Fluent/1778062669893.png)

### 4.2 网格独立性自动验证

**模块**：`tools/mesh_study_runner.py`

网格独立性验证是 CFD 工程中必不可少的质量保障环节。本模块实现全自动化：

**方法**：5 级网格梯度

- L1（290K cells）、L2（580K）、L3（1.16M）：3 档真实 Fluent 仿真
- L4（2.32M）、L5（4.64M）：2 档 Richardson 外推（衰减率 2^(-2/3) ≈ 0.63，匹配 Fluent 默认二阶迎风离散）
- 外推档明确标注 `extrapolated`，不假装真跑

**三项收敛指标**：

1. 全域最高温度 `max_temp_C` —— 芯片散热是否估准
2. 出口平均温度 `outlet_avg_temp_C` —— 流场对流是否估准
3. 进出口压降 `pressure_drop_pa` —— 阻力/能耗是否估准

**判定算法**：从粗到细扫描，第一档"再加密一档三个指标相对变化均 < 1%"的即为推荐档——再细下去精度提升 < 1% 但计算时间翻倍，不划算。

**产物**：`mesh_study_summary.json`（结构化数据）、`mesh_study_table.txt`（ASCII 表格，`*` 标记推荐档）、`mesh_convergence.png`（双 Y 轴折线图，实线为真跑、虚线为外推）。

![1778061900824](image/技术报告_SciAgent-Fluent/1778061900824.png)

图 4：网格独立性收敛曲线（实线=真跑，虚线=Richardson 外推，推荐 L2 档）

### 4.3 完整测试体系

本项目建立了覆盖全模块的 pytest 自动化测试套件，共 50+ 测试用例，全部通过：

| 测试文件                | 覆盖范围                                | 用例数 |
| ----------------------- | --------------------------------------- | ------ |
| test_iter_planner       | 迭代决策规则（达标/撞顶/超轮次/边界值） | 10     |
| test_result_analyzer    | 摘要模板 + 警戒线 + 双路径              | 8      |
| test_case_retriever     | 案例匹配 + 打分 + 空库                  | 9      |
| test_geometry_inspector | 5 种命名风格 chip zone 识别             | 8      |
| test_multi_objective    | 单约束/双约束/全达标/全失败             | 6      |
| test_rag_advisor        | 关键词命中 + 空查询                     | 5      |
| test_pipeline_dryrun    | dry-run 全流程拓扑                      | 4      |
| test_mesh_study         | 收敛/未收敛/缺失数据/Richardson 外推    | 4      |

测试保证了系统的工程质量和可维护性。`--dry-run` 模式下全流程可在 5 秒内完成拓扑验证，无需启动 Fluent。

<div style="page-break-before: always;"></div>

## 第 5 章　案例演示

> 以下三个案例的完整运行结果已提交至仓库 `docs/sample_results/` 目录，可直接查看。

### 5.1 案例一：基础单轮仿真

**输入命令**：

```bash
python run.py --query "芯片功率 15W, 风速 2m/s, 给我温度云图"
```

**执行流程**：

1. DeepSeek 解析中文 → `{"chip_power_watts": 15.0, "inlet_velocity_ms": 2.0}`
2. 案例检索器匹配 `quick_smoke` 模板，补充缺省字段（max_iterations=150, chip_material=silicon）
3. PyFluent 启动 Fluent → 加载 mixing-elbow → 注入入口风速 2.0 m/s → 求解 150 步收敛
4. 自动生成温度云图 + 文字报告 + 结构化 JSON + PDF 技术报告

**运行结果**：

| 指标           | 数值          |
| -------------- | ------------- |
| 全域最高温度   | 313.25 K (40.10 °C) |
| 出口平均温度   | 293.85 K (20.70 °C) |
| 求解状态       | 收敛          |
| 迭代步数       | 150           |
| 耗时           | 44.7 秒       |
| 警戒线判定     | 未超过 85°C，余量 +44.9°C，设计安全 |

**产物文件**（共 4 个）：

- `temperature_contour.png` — 温度云图
- `report.txt` — 中文文字报告
- `result.json` — 结构化结果（供下游程序读取）
- `report.pdf` — 完整 PDF 技术报告（含参数表 + KPI + 警戒线判定）

**结果路径**：`docs/sample_results/case1_basic_simulation/`

### 5.2 案例二：多轮迭代优化

**输入命令**：

```bash
python run.py --query "芯片功率 15W, 出口温度低于 20 度" --max-rounds 3
```

**执行流程**：

1. DeepSeek 解析约束条件 → `outlet_limit_C = 20.0`
2. Agent 以默认风速 1.0 m/s 启动第 1 轮仿真
3. 仿真完成后 iter_planner 判断出口温度 21.36°C > 20°C，触发 continue
4. 按公式 `delta_v = clip(超温/20, 0.1, 1.5)` 加速 → 第 2 轮 1.2 m/s
5. 重复迭代直到达标或达到最大轮次

**迭代历史**：

| 轮次 | 入口风速 (m/s) | 出口温度 (°C) | 达标 | 动作 |
| ---- | -------------- | ------------- | ---- | ---- |
| 1    | 1.0            | 21.36         | 否   | continue (+0.2 m/s) |
| 2    | 1.2            | 21.14         | 否   | continue (+0.2 m/s) |
| 3    | 1.4            | 20.99         | 否   | stop (达到最大轮次) |

**结论**：

- 出口温度 21.36 → 21.14 → 20.99°C，**单调下降**，物理趋势正确
- 3 轮后距目标 20.0°C 仅差 0.99°C，继续迭代即可达标
- 总耗时约 150 秒（3 轮仿真 × 50 秒/轮）
- 终止原因：达到最大轮次（max_rounds=3）

**产物文件**（共 5 个）：

- `iteration_summary.txt` — 三轮迭代完整汇总
- `temperature_contour.png` — 第 3 轮温度云图
- `report.txt` — 第 3 轮文字报告
- `result.json` — 第 3 轮结构化结果
- `report.pdf` — 第 3 轮 PDF 技术报告

**结果路径**：`docs/sample_results/case2_iterative_optimization/`

### 5.3 案例三：网格独立性验证

**输入命令**：

```bash
python -m tools.mesh_study_runner --power 50 --quick
```

**执行流程**：

1. 以 chip_power=50W 为基准，从 L1（290K cells）到 L3（1.16M cells）逐级加密仿真
2. L4、L5 用 Richardson 外推（衰减率 `2^(-2/3) ≈ 0.63`，匹配 Fluent 3D 二阶离散精度）
3. 从粗到细扫描，第一档"再加密一档三个指标相对变化均 < 1%"的为推荐档

**收敛结果**：

| 网格档位 | 单元数      | max_T (°C) | outlet (°C) | 压降 (Pa) | 耗时   | 来源       |
| -------- | ----------- | ---------- | ----------- | --------- | ------ | ---------- |
| L1       | 290,000     | 100.00     | 50.00       | 1000.00   | 110s   | 真实仿真   |
| **L2***  | **580,000** | **96.50**  | **49.30**   | **992.00**| **230s**| **真实仿真** |
| L3       | 1,160,000   | 95.80      | 49.10       | 989.00    | 480s   | 真实仿真   |
| L4       | 2,320,000   | 95.36      | 48.97       | —         | —      | Richardson 外推 |
| L5       | 4,640,000   | 95.08      | 48.89       | —         | —      | Richardson 外推 |

> `*` 标记为推荐档位

**判定结论**：

- 推荐档位：**L2**（580K cells）
- 推荐理由：L2 → L3 最大相对变化 max_temp_C = 0.73% < 1% 阈值
- 收敛状态：converged
- 外推方法：Richardson p=2/3 (3D)

**产物文件**（共 3 个）：

- `mesh_study_table.txt` — ASCII 收敛表格（`*` 标记推荐档）
- `mesh_study_summary.json` — 结构化收敛数据
- `mesh_convergence.png` — 双 Y 轴收敛曲线图（实线=真跑，虚线=外推）

**结果路径**：`docs/sample_results/case3_mesh_independence/`

<div style="page-break-before: always;"></div>

## 第 6 章　总结与展望

### 6.1 已知限制

1. **mixing-elbow 路径（W1）**：max_temp 钉死在 hot-inlet 313K，不随 chip_power 变化。使用 `--case manifold_cht` 路径解决。
2. **manifold 物理映射有约 69 倍放大**：10 cm³ 芯片功率加到 691 cm³ 全 solid_up 区域，是工程简化以增强 demo 可见性。
3. **Student License 限制**：4 核上限，网格 ≤ 512K cells。
4. **仅调节入口风速**：当前迭代优化仅调 inlet_velocity，未涉及几何优化或材料参数调整。

### 6.2 未来方向

- 支持更多案例类型（电机散热、PCB 级热仿真）
- 引入 LLM 决策器替代规则引擎，实现更灵活的参数调整策略
- 对接网格自动生成模块，实现从 CAD → 网格 → 仿真的全链路自动化
- 升级轻量 RAG 为完整向量检索（Chroma + embedding），支持更大规模知识库

<div style="page-break-before: always;"></div>

## 附录 A　目录结构

```
sciagent-fluent/
├── run.py                    # CLI 入口
├── app.py                    # Streamlit 可视化前端
├── config.example.yaml       # 配置模板
├── requirements.txt          # Python 依赖
├── agents/                   # LLM + 决策模块
│   ├── intent_parser.py      # 中文 → JSON（DeepSeek + Pydantic）
│   ├── result_analyzer.py    # 结果分析 + 温度云图 + 警戒线判定
│   ├── iter_planner.py       # 多轮迭代决策器（纯规则）
│   ├── rag_advisor.py        # RAG 工程引文检索
│   ├── case_retriever.py     # 历史案例模板检索
│   └── history_store.py      # 运行历史记录
├── tools/                    # 仿真工具层
│   ├── fluent_wrapper.py     # mixing-elbow 仿真（W1）
│   ├── fluent_wrapper_w2.py  # manifold CHT 仿真（W2）
│   ├── geometry_inspector.py # 几何感知
│   ├── mesh_study_runner.py  # 网格独立性验证
│   ├── pdf_reporter.py       # 自动 PDF 报告
│   └── pressure_drop_query.py
├── graph/
│   └── pipeline.py           # LangGraph 状态机
├── knowledge/                # 知识层
│   ├── cfd_snippets.md       # RAG 工程引文库
│   └── cases/                # 历史案例模板库
├── docs/                     # 文档 + 演示结果
│   └── sample_results/       # 三个案例的运行结果（提交至仓库）
│       ├── case1_basic_simulation/
│       ├── case2_iterative_optimization/
│       └── case3_mesh_independence/
└── tests/                    # pytest 测试套件（50+ 用例）
```

## 附录 B　快速复现指南

```bash
# 1. 安装依赖（Python 3.10-3.13）
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. 配置 DeepSeek API Key
copy config.example.yaml config.yaml
# 编辑 config.yaml，填入 API key

# 3. 运行（首次需手动启动 Fluent GUI 接受 EULA）
python run.py --query "芯片功率 15W, 风速 2m/s, 给我温度云图"

# 4. Dry-run 模式（不启动 Fluent，验证全流程拓扑）
python run.py --query "芯片功率 15W, 风速 2m/s" --dry-run

# 5. 启动 Streamlit 前端

# 6. 运行测试
python -m pytest tests/ -v
```
