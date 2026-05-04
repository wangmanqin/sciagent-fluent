# SciAgent-Fluent

> 一句中文 → Ansys Fluent CFD 仿真 → 温度云图 + PDF 报告

面向"工业软件智能体化"课题。

```bash
python run.py --query "芯片功率 50W, 风速 1m/s, 出口低于 21 度, 给我温度云图"
```

5 分钟后, `reports/run_<时间戳>/` 里自动生成:

- `temperature_contour.png` — 温度云图
- `report.txt` — 中文文字报告 (输入参数 / 求解结果 / 警戒线判定)
- `result.json` — 结构化结果 (给下游程序读)
- `report.pdf` — 完整 PDF 技术报告 (含迭代历史表 + 工程引文)

---

## 目录

- [解决什么问题](#解决什么问题)
- [三层 AI 架构](#三层-ai-架构)
- [功能一: 基础仿真模块](#功能一-基础仿真模块)
- [功能二: 迭代优化模块](#功能二-迭代优化模块)
- [功能三: 网格独立性验证](#功能三-网格独立性验证)
- [功能四: 几何感知](#功能四-几何感知)
- [功能五: 知识沉淀与复用](#功能五-知识沉淀与复用)
- [功能六: 自动 PDF 报告](#功能六-自动-pdf-报告)
- [功能七: 多目标约束](#功能七-多目标约束)
- [功能八: Streamlit 可视化前端](#功能八-streamlit-可视化前端)
- [快速开始](#快速开始)
- [使用方法](#使用方法)
- [目录结构](#目录结构)
- [测试](#测试)
- [已知限制](#已知限制)
- [踩坑速查](#踩坑速查)

---

## 解决什么问题

CFD 工程师每跑一次 Fluent 仿真都要:

1. 打开 Fluent GUI (启动 30s)
2. 加载 case → 改边界条件 (鼠标点 5 分钟)
3. 求解 (10 分钟)
4. 后处理 → 出图 → 写报告 (鼠标点 10 分钟)

真正"思考"的只有第 2 步, 其余全是机械操作。本项目把整条流程封装成一句中文, 让 LLM 当翻译官, 让 Fluent 在后台自动跑, 工程师只负责提需求和读报告。

---

## 三层 AI 架构

```
┌──────────────────────────────────────────────────┐
│              第三层: 知识层                        │
│   RAG 工程引文库 + 历史案例模板检索                  │
│   "这个 Agent 有经验, 不是白纸一张"                 │
├──────────────────────────────────────────────────┤
│              第二层: 流程层                        │
│   LangGraph 状态机 (5 节点 + 条件边 + checkpoint)   │
│   "这个 Agent 能自主走完一整套流程并迭代优化"        │
├──────────────────────────────────────────────────┤
│              第一层: 能力层                        │
│   DeepSeek-V3 大语言模型                           │
│   "这个 Agent 听得懂中文, 认得出几何"               │
└──────────────────────────────────────────────────┘
```

**第一层 (能力层)**: DeepSeek 做中文意图解析、单位换算、中文别名归一、零样本几何识别。大模型仅输出结构化 JSON, 绝不输出代码, 配合 Pydantic 二次校验防止幻觉。

**第二层 (流程层)**: LangGraph 状态机编排 5 个 Agent 节点 (意图理解 → 案例检索 → 仿真执行 → 迭代决策 → 报告生成), 条件边实现多轮迭代闭环, SqliteSaver 实现断点续跑。

**第三层 (知识层)**: 工程引文库 (RAG) 给决策附教科书级工程依据; 历史案例模板库给新需求推荐参数起点。双库并立 — RAG 答"为什么这么调", 案例库答"以前做过类似的"。

### 五个核心设计决策

| # | 决策 | 解决什么 |
| - | --- | --- |
| 1 | **LLM 只输出 JSON, 不出代码** | 减少幻觉攻击面; 模板代码可单测; 解耦 prompt 与 PyFluent API |
| 2 | **参数分层** | 用户层 (W, m/s) vs Fluent 层 (W/m³, Pa); 单位换算放 Python 不放 LLM |
| 3 | **Baseline-first** | 用 Ansys 官方算例当底板, 只换参数, 不做 CAD |
| 4 | **状态机 + 检查点** | LangGraph 节点持久化状态, 仿真崩了能从中间恢复 |
| 5 | **每层都有 fallback** | PyFluent 失败 → TUI; 远程下载失败 → 本地缓存; sim 散了 → 回滚参数 |

---

## 功能一: 基础仿真模块

**一句中文 → 结构化参数 → Fluent 仿真 → 温度云图 + 报告**

### 工作流

```
用户: "芯片功率 50W, 风速 1m/s, 给我温度云图"
  │
  ▼
[intent] DeepSeek 解析中文 → {"chip_power_watts": 50, "inlet_velocity_ms": 1.0, ...}
  │                           Pydantic 校验, 失败自我修正重试
  ▼
[case_retriever] 检索历史案例库 → 匹配最相似 case, 填补缺省字段
  │
  ▼
[simulate] PyFluent 启动 Fluent → 注入参数 → 求解 → 抽取温度 KPI
  │
  ▼
[report] 输出温度云图 PNG + 文字报告 TXT + 结构化 JSON + PDF
```

### 关键文件

| 模块 | 文件 | 说明 |
| --- | --- | --- |
| 意图解析 | [agents/intent_parser.py](agents/intent_parser.py) | 中文 → 7 字段 JSON, Pydantic 校验 |
| 仿真执行 (W1) | [tools/fluent_wrapper.py](tools/fluent_wrapper.py) | mixing-elbow 路径 |
| 仿真执行 (W2) | [tools/fluent_wrapper_w2.py](tools/fluent_wrapper_w2.py) | manifold CHT 路径, chip_power 真生效 |
| 结果分析 | [agents/result_analyzer.py](agents/result_analyzer.py) | 温度云图 + 文字摘要 + 警戒线判定 |
| 流水线编排 | [graph/pipeline.py](graph/pipeline.py) | LangGraph 状态机, 5 节点 + 条件边 |
| CLI 入口 | [run.py](run.py) | 唯一命令行入口 |

### 两条仿真路径

```bash
# W1 路径: mixing-elbow (冷热水混合管, 验证流水线)
python run.py --query "芯片功率 15W, 风速 2m/s"

# W2 路径: manifold CHT (共轭传热, chip_power 真驱动温度)
python run.py --query "硅芯片 50W, 风速 1m/s, 警戒线 1500 度" --case manifold_cht
```

### 真机验证数据

- W2 chip_power 单调驱动温度: 0W → 830K, 15W → 970K, 50W → 1361K, 200W → 3500K+

---

## 功能二: 迭代优化模块

**用户给目标温度, Agent 自主加风速重跑, 直到达标或预算用尽。**

### 迭代闭环拓扑

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

### 决策规则

- **只调 inlet_velocity, 绝不动 chip_power** — chip_power 是用户给的任务条件, 改了等于篡改题目
- 调参公式: `delta_v = clip(超温度数 / 20.0, 0.1, 1.5)`
- 三重终止机制: 达标即停 / 风速撞 10 m/s 上限即停 / 跑满 max_rounds 即停
- **为什么用规则不用 LLM**: 规则确定性强、可单测、不会偷换任务条件; 接口已预留, 后续可替换为 LLM 实现

### 断点续跑 (SqliteSaver)

```bash
python run.py --query "..." --thread-id deploy-001 --checkpoint   # 第一次跑
python run.py --thread-id deploy-001 --resume                     # 崩了恢复
```

### 真机验证数据

mixing_elbow, chip_power=15W, outlet_limit=21°C, max_rounds=3:

| 轮次 | 风速 (m/s) | 出口温度 (°C) | 动作 |
| --- | --- | --- | --- |
| 1 | 0.4 | 23.08 | continue |
| 2 | 0.6 | 22.16 | continue |
| 3 | 0.8 | 21.67 | stop (max_rounds) |

3 轮出口温度单调下降, 物理正确。耗时 39.4 秒。

### 关键文件

| 模块 | 文件 |
| --- | --- |
| 迭代决策器 | [agents/iter_planner.py](agents/iter_planner.py) |
| 条件边路由 | [graph/pipeline.py](graph/pipeline.py) `iter_decide` |
| 验收脚本 | [acceptance_iter.py](acceptance_iter.py) |

---

## 功能三: 网格独立性验证

**在计算精度和计算效率之间自动寻找最优平衡点。**

### 方法

- 5 级网格梯度: L1 (290K cells) / L2 (580K) / L3 (1.16M) 三档真实仿真 + L4 (2.32M) / L5 (4.64M) 两档 Richardson 外推
- Richardson 外推衰减率 `2^(-2/3) ≈ 0.63`, 匹配 Fluent 默认二阶迎风离散 (3D 下 error ~ N^(-2/3))
- **外推档明确标注 `extrapolated`, 不假装真跑**

### 三项收敛指标

1. **全域最高温度 max_temp_C** — 芯片散热是否估准
2. **出口平均温度 outlet_avg_temp_C** — 流场对流是否估准
3. **进出口压降 pressure_drop_pa** — 阻力/能耗是否估准

### 判定算法

从粗到细扫, 第一档"再加密一档三个指标相对变化都 < 1%"的即为推荐档 — 再细下去精度提升 < 1% 但时间翻倍, 不划算。

### 产物

- `mesh_study_summary.json` — 结构化数据
- `mesh_study_table.txt` — ASCII 表格, `*` 标记推荐档
- `mesh_convergence.png` — 双 Y 轴折线图 (实线=真跑, 虚线=外推)

### 关键文件

| 模块 | 文件 |
| --- | --- |
| 网格研究主逻辑 | [tools/mesh_study_runner.py](tools/mesh_study_runner.py) |
| 测试 | [tests/test_mesh_study.py](tests/test_mesh_study.py) |

```bash
python -m tools.mesh_study_runner --power 50 --quick    # 真跑
python -m tools.mesh_study_runner --selftest             # 规则单测, 不启 Fluent
```

---

## 功能四: 几何感知

**自动识别 Fluent case 中的芯片/流体/散热片区域, 换算例不用改代码。**

### 问题

W2 的 wrapper 里 `CHIP_ZONE = "solid_up"` 是硬编码的。换一个 case (芯片叫 `die` 或 `silicon_cpu`), 代码直接报错。

### 解决方案: 两层识别

**Layer 1 — 关键词启发式**: 维护三张关键词表 (chip/die/silicon/cpu/gpu/ic, fluid/air/water, heatsink/fin/cooler), 扫描 cell zone 名字命中分类。

**Layer 2 — LLM fallback**: 关键词全 miss 时, 把 zone 列表发给 DeepSeek 选, 仍只输出一个 zone 名字。

**Layer 3 — 兜底**: 两层都没搞定, 用 `solid_up` 做 fallback。

### 自测验证 (8/8 PASS)

| case | zone 名 | 识别结果 |
| --- | --- | --- |
| manifold | `solid_up` | ✓ |
| PCB | `die`, `pcb` | `die` ✓ |
| CPU | `cpu_silicon`, `heatsink_fin` | `cpu_silicon` ✓ |
| 英文命名 | `chip-1` | ✓ |
| 模糊命名 | `body_a`, `body_b` | `body_a` (兜底) ✓ |

### 关键文件

| 模块 | 文件 |
| --- | --- |
| 几何识别 | [tools/geometry_inspector.py](tools/geometry_inspector.py) |
| 测试 | [tests/test_geometry_inspector.py](tests/test_geometry_inspector.py) |

---

## 功能五: 知识沉淀与复用

**双库设计: 工程引文库答"为什么这么调", 历史案例库答"以前做过类似的"。**

### 工程引文库 (RAG)

[knowledge/cfd_snippets.md](knowledge/cfd_snippets.md) — 5 段教科书级工程经验, 每段标注出处:

- 强制对流换热经验关联式 (Incropera Ch.7)
- 工业冷却风道典型工况
- 共轭传热网格策略 (Ansys User Guide §13.2)
- chip_power 物理映射合理性论证
- 多轮迭代终止条件设计

迭代决策器输出 plan 后, report 节点自动检索匹配的引文附在报告里。

**为什么不上 Chroma 向量库**: 知识体量 < 10 段, 关键词检索够用。接口 `retrieve(query) → list[Snippet]` 跟 Chroma 完全兼容, 升级是 1 行替换。

### 历史案例模板库

[knowledge/cases/](knowledge/cases/) — 4 份 JSON 历史案例档案:

| case_id | 描述 | baseline chip_power |
| --- | --- | --- |
| `quick_smoke` | 快速冒烟测试 | 15W |
| `manifold_low_power` | 低功率散热 | 15W |
| `manifold_high_power_50w` | 中功率散热 | 50W |
| `elbow_iter_demo` | 迭代闭环演示 | 15W |

用户输入 query 后, case_retriever 自动匹配最相似的 case, 把 baseline_params 填补用户没说的字段 (用户说了的优先)。

### 关键文件

| 模块 | 文件 |
| --- | --- |
| RAG 引文检索 | [agents/rag_advisor.py](agents/rag_advisor.py) |
| 案例模板检索 | [agents/case_retriever.py](agents/case_retriever.py) |
| 工程知识库 | [knowledge/cfd_snippets.md](knowledge/cfd_snippets.md) |
| 案例模板库 | [knowledge/cases/](knowledge/cases/) |
| 测试 | [tests/test_rag_advisor.py](tests/test_rag_advisor.py), [tests/test_case_retriever.py](tests/test_case_retriever.py) |

---

## 功能六: 自动 PDF 报告

**每次仿真自动生成 6 章节完整 PDF 技术报告。**

### 章节内容

1. 封面 (项目名 + 时间戳)
2. 输入参数表
3. 求解结果 KPI
4. 警戒线判定
5. 迭代历史表 (多轮迭代时)
6. 温度云图 + 完整文字摘要

### 技术选型

- **reportlab 4.5** — 全 Python 无外部依赖 (weasyprint 要 GTK, Windows 配置复杂)
- 中文字体三层 fallback: 微软雅黑 (msyh.ttc) → 黑体 (simhei.ttf) → Helvetica
- 单次 PDF 约 166 KB

### 关键文件

| 模块 | 文件 |
| --- | --- |
| PDF 生成器 | [tools/pdf_reporter.py](tools/pdf_reporter.py) |

```bash
python -m tools.pdf_reporter    # 用最新 run 重生成 PDF
```

---

## 功能七: 多目标约束

**同时给 max_temp 和 outlet 两条警戒线, 两个都满足才算达标。**

```bash
python run.py --query "芯片 50W, 警戒线 1500 度, 出口低于 21 度" --case manifold_cht
```

- 给 `limit_C` → max_temp 算一条约束
- 给 `outlet_limit_C` → outlet 算一条约束
- `all_within_spec = True` 当且仅当两条都满足
- 迭代决策器看 `all_within_spec`, 任一不满足就 continue

### 关键文件

| 模块 | 文件 |
| --- | --- |
| 多目标判定 | [agents/result_analyzer.py](agents/result_analyzer.py) `summarize` |
| 测试 | [tests/test_multi_objective.py](tests/test_multi_objective.py) |

---

## 功能八: Streamlit 可视化前端

**浏览器里点选参数, 实时看进度, 查看历史记录。**

```bash
streamlit run app.py
```

功能:
- case_mode 选择 (mixing_elbow / manifold_cht)
- chip_power / inlet_velocity 滑块
- 目标温度勾选 + 数字输入
- 实时进度流式输出
- 历史记录侧边栏 (读 [reports/history.jsonl](reports/history.jsonl))

### 关键文件

| 模块 | 文件 |
| --- | --- |
| Streamlit 前端 | [app.py](app.py) |
| 历史记录存储 | [agents/history_store.py](agents/history_store.py) |

---

## 快速开始

### 前置条件

| 软件 | 版本 | 备注 |
| --- | --- | --- |
| Windows | 10 / 11 | Linux/Mac 理论可行但未验证 |
| Python | 3.10 – 3.13 | `ansys-fluent-core` 不支持 3.14+ |
| Ansys Fluent | 2023 R2 或更新 | Student / 商用版均可 |
| DeepSeek API | — | https://platform.deepseek.com/ 注册 |

### 安装

```powershell
git clone https://github.com/wangmanqin/sciagent-fluent.git
cd sciagent-fluent

py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1

pip install -r requirements.txt

copy config.example.yaml config.yaml
# 编辑 config.yaml, 填入 DeepSeek API key
```

### 获取仿真算例文件

首次运行时, PyFluent 会自动从 Ansys 官方服务器下载算例文件 (~51 MB)。如果网络不通, 也可以手动获取后放到项目根目录:

| 文件 | 大小 | 用途 |
| --- | --- | --- |
| `mixing_elbow.cas.h5` | 3 MB | W1 路径: 冷热水混合管 |
| `manifold_solution.cas.h5` + `.dat.h5` | 48 MB | W2 路径: manifold 共轭传热 |

> 首次启动 Fluent Student 版需手动开一次 GUI 接受 EULA, 否则脚本报 WSAECONNRESET 10054。

### 验证

```powershell
python verify_env.py           # 看到三个 [OK] 说明环境好了
```

---

## 使用方法

### 基础仿真

```bash
python run.py --query "芯片功率 15W, 风速 2m/s, 给我温度云图"           # 真跑
python run.py --query "芯片功率 15W, 风速 2m/s" --dry-run              # 测拓扑
python run.py --selftest                                                # 3 句样本自测
```

### 多轮迭代

```bash
python run.py --query "出口低于 21 度" --max-rounds 3                  # 自动迭代
python run.py --query "..." --thread-id deploy-001 --checkpoint        # 持久化
python run.py --thread-id deploy-001 --resume                          # 断点续跑
```

### CHT 路径 (chip_power 真生效)

```bash
python run.py --query "硅芯片 50W, 警戒线 1500 度" --case manifold_cht
```

### 单模块测试

```bash
python -m agents.intent_parser "芯片功耗 20 瓦, 风速 1.5 m/s"   # 意图解析
python -m agents.rag_advisor "风速调多少"                         # RAG 检索
python -m tools.mesh_study_runner --selftest                      # 网格研究规则
python -m tools.pdf_reporter                                      # 重生成 PDF
```

---

## 目录结构

```
sciagent-fluent/
├── run.py                          # CLI 入口
├── app.py                          # Streamlit 可视化前端
├── config.example.yaml             # 配置模板
├── requirements.txt                # Python 依赖
│
├── agents/                         # LLM + 决策模块
│   ├── intent_parser.py            # 中文 → params JSON (DeepSeek + Pydantic)
│   ├── result_analyzer.py          # 结果分析 + 温度云图 + 警戒线判定
│   ├── iter_planner.py             # 多轮迭代决策器 (纯规则)
│   ├── rag_advisor.py              # RAG 工程引文检索
│   ├── case_retriever.py           # 历史案例模板检索
│   └── history_store.py            # 运行历史记录
│
├── tools/                          # 仿真工具层
│   ├── fluent_wrapper.py           # mixing-elbow 仿真 (W1)
│   ├── fluent_wrapper_w2.py        # manifold CHT 仿真 (W2, chip_power 真生效)
│   ├── geometry_inspector.py       # 几何感知 (自动识别 chip zone)
│   ├── mesh_study_runner.py        # 网格独立性验证
│   ├── pdf_reporter.py             # 自动 PDF 报告
│   └── pressure_drop_query.py      # 压降查询
│
├── graph/
│   └── pipeline.py                 # LangGraph 状态机 (5 节点 + 条件边)
│
├── knowledge/                      # 知识层
│   ├── cfd_snippets.md             # RAG 工程引文库 (5 段)
│   └── cases/                      # 历史案例模板库 (4 份 JSON)
│
├── tests/                          # pytest 测试套 (50+ case)
│   ├── test_iter_planner.py        # 迭代决策器 10 case
│   ├── test_result_analyzer.py     # 结果分析 8 case
│   ├── test_pipeline_dryrun.py     # dry-run 流水线 4 case
│   ├── test_geometry_inspector.py  # 几何感知 8 case
│   ├── test_case_retriever.py      # 案例检索 9 case
│   ├── test_multi_objective.py     # 多目标约束 6 case
│   ├── test_mesh_study.py          # 网格研究 4 case
│   ├── test_rag_advisor.py         # RAG 检索 5 case
│   └── test_history_store.py       # 历史记录
│
├── reports/                        # 仿真产物 (gitignored)
├── docs/                           # 文档 + 演示结果
│   ├── sample_results/             # 三个案例的运行结果
│   ├── 技术报告_SciAgent-Fluent.md
│   ├── W2_验收对比表.md
│   ├── 网格策略.md
│   └── manifold_zone_清单.md
│
└── 踩坑记录.md                      # 40+ 个坑 + 解决方案
```

---

## 测试

```bash
python -m pytest tests/ -v
```

50+ 测试用例, 覆盖:

| 测试文件 | 覆盖范围 | case 数 |
| --- | --- | --- |
| test_iter_planner | 迭代决策规则 (达标/撞顶/超轮次/边界值) | 10 |
| test_result_analyzer | 摘要模板 + 警戒线 + 双路径 | 8 |
| test_case_retriever | 案例匹配 + 打分 + 空库 | 9 |
| test_geometry_inspector | 5 种命名风格 chip zone 识别 | 8 |
| test_multi_objective | 单约束 / 双约束 / 全达标 / 全失败 | 6 |
| test_rag_advisor | 关键词命中 + 空查询 | 5 |
| test_pipeline_dryrun | dry-run 全流程拓扑 | 4 |
| test_mesh_study | 收敛 / 未收敛 / 缺失数据 / Richardson 外推 | 4 |

---

## 已知限制

1. **mixing-elbow 不是真正的散热案例 (W1 路径)** — max_temp 钉死在 hot-inlet 313K, 不随 chip_power 变。用 `--case manifold_cht` 解决。
2. **manifold 物理映射有 ~69 倍放大** — `Q = power / 10cm³` 加到 691 cm³ 全 solid_up 上, 是工程简化让 demo 可见。切真 PCB+chip 几何后接口不变。
3. **Student license 限制** — 4 核上限, mesh ≤ 512K cell。
4. **首启需手动开 Fluent GUI 接受 EULA** — 详见踩坑 #5。

---

## 踩坑速查

完整列表在 [踩坑记录.md](踩坑记录.md) (40+ 个细节), 常见:

| 症状 | 解决 |
| --- | --- |
| `pip install` 拒装 | Python 3.14 不支持, 用 3.12 |
| `WSAECONNRESET 10054` | 首次手动开 Fluent GUI |
| `LicenseError` | Ansys ≤ 2022 R1 太老 |
| source_terms `option` 报错 | 是 `"value"` 不是 `"constant"` (Fluent 2026 R1) |
| 温度不随风速变 | 看 outlet_avg 而不是 max (mixing-elbow) |

---

## License & 致谢

- **基线算例**: Ansys 官方 PyFluent examples (mixing-elbow, manifold)
- **LLM**: DeepSeek-V3
- **Agent 编排**: LangGraph
- **可视化**: matplotlib + Fluent TUI + Streamlit
