"""
agents/intent_parser.py
============================================================
用 DeepSeek 把一句中文需求翻译成 run_simulation() 能直接吃的 params dict。

为什么要这一层(写给以后的你/答辩用):
  fluent_wrapper.run_simulation(params) 接受的是结构化字典:
        {"chip_power_watts": 15.0, "inlet_velocity_ms": 2.0, "max_iterations": 200}
  但用户嘴里说的是:
        "芯片功耗 15 瓦, 风速 2 米每秒"
  这中间需要一个"翻译官", 把自然语言 → 结构化 JSON。
  这一层就是 LangGraph 流程里的第一个节点 (intent), 见架构决策 #4。

最关键的设计原则(架构决策 #1): LLM 只出 JSON, 绝不出代码。
  - 不让 DeepSeek 写 Python 代码去调 PyFluent — 那样幻觉成本高, 调试困难。
  - 只让它做"自然语言 → 受限 schema JSON"这种很窄的任务, 错的空间最小。
  - JSON 出来后, 由 Pydantic 校验; 校验失败就重试一次 (架构决策 #5)。

为什么用 Pydantic 校验而不是手写 if/else:
  - 手写校验只面对自家代码, 输入只可能是 dict; 接 LLM 后可能给:
        类型乱(用字符串 "15.0" 而不是数字 15.0)
        多余字段(回答多塞个 "explanation": "...")
        缺字段(只给 2 个键)
        嵌套过多({"params": {...}})
  - Pydantic 一句 BaseModel 就能处理类型强转 + 缺失检测 + 多余拒绝, 比手写鲁棒。
  - 加新字段(比如 "ambient_temperature")时, 只改 Pydantic 模型一处。

调用接口(API):
  >>> from agents.intent_parser import parse_intent
  >>> params = parse_intent("芯片功耗 15W, 风速 2 m/s, 跑 200 步")
  >>> params
  {"chip_power_watts": 15.0, "inlet_velocity_ms": 2.0, "max_iterations": 200}

CLI 入口:
  python -m agents.intent_parser "芯片功耗 20 瓦, 风速 1.5 m/s"
  → 打印出对应 JSON, 且能直接喂给 run_simulation()

回退策略(架构决策 #5):
  - DeepSeek 调用失败 → 重试一次 (网络抖动很常见)
  - JSON 解析/Pydantic 校验失败 → 把错误信息拼回 prompt 让模型自己改, 再试一次
  - 两次都失败 → 抛 RuntimeError, 上层(LangGraph)决定怎么办
"""
#LangGraph 是一个工作流编排框架，
#负责将大模型的输出与其他工具（如 run_simulation ）连接起来，形成完整的工作流。
# ↑ 模块文档字符串。help(agents.intent_parser) 能读到。

import argparse           # 解析命令行参数, CLI 入口要用
import json               # 解析 LLM 返回的 JSON 字符串
import os                 # 读环境变量(给单测留 hook)
import sys                # sys.exit / sys.stderr
from typing import Optional  # 整个文件里，后面所有地方都可以随便用 Optional，有些字段不是必须传的，可以为空（None）

import yaml               # 读 config.yaml 拿 DeepSeek key
from openai import OpenAI  # DeepSeek 兼容 OpenAI SDK, 直接用同一个客户端
from pydantic import BaseModel, Field, ValidationError, field_validator
# ↑ Pydantic v2 的核心: BaseModel 定义结构, Field 设默认/约束,
#   ValidationError 是校验失败时抛的异常, field_validator 写自定义校验逻辑。


# ============================================================
# 全局常量
# ============================================================

# config.yaml 路径 — 相对项目根。intent_parser.py 在 agents/ 里, 上一级就是项目根。
_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
#".."的意思是返回上一级目录，即项目根目录、"config.yaml"找到这个目录下的config.yaml文件
# 默认值 — 用户没说清楚时填这些, 而不是让 LLM 瞎猜或报错。
# 选这几个数是因为它们是 mixing-elbow 案例的"中间档", 不极端。
_DEFAULT_CHIP_POWER_W = 10.0
_DEFAULT_INLET_VELOCITY_MS = 1.0
_DEFAULT_MAX_ITERATIONS = 150

# DeepSeek 调用最大重试次数(不含首次)。两次足够覆盖网络抖动 + 一次"自我修正"。
_MAX_RETRIES = 2

# System prompt — 给 LLM 的"角色 + 输出 schema 约束"。
# 关键技巧: 把示例(few-shot)写进去, LLM 看着例子学比看描述准。
# 写中文是为了和用户输入语种一致, 减少混淆。
_SYSTEM_PROMPT = """你是一个 CFD 仿真参数解析器。#角色
#约束 schema
任务: 把用户用中文描述的仿真需求, 翻译成严格的 JSON 对象。
不要写任何解释, 不要写代码, 只输出 JSON。

JSON 必须且仅包含以下六个键 (前三个必有, 后三个 W2 起加, 用户没说就给默认值):
  - chip_power_watts (number): 芯片功耗, 单位瓦特(W)。常见说法: "功率/功耗 15W", "10瓦"。
  - inlet_velocity_ms (number): 入口风速/流速, 单位米每秒(m/s)。常见说法: "风速 2 米每秒", "吹 1.5 m/s"。
  - max_iterations (integer): 最大迭代步数。常见说法: "跑 200 步", "迭代 300 次", "最多 500 步"。
  - chip_material (string): 芯片材料, 合法值 "silicon" / "aluminum" / "copper" / "steel"。
    常见说法: "硅芯片" → "silicon", "铜质" → "copper", 没说就 "silicon"。
  - limit_C (number 或 null): 温度警戒线, 单位摄氏度。
    常见说法: "警戒线 100 度", "不能超过 90 ℃"。**用户没说就填 null** (不要瞎猜, null 让 Python 端按 case 选默认).
  - outlet_limit_C (number 或 null): 出口平均温度警戒线, 单位摄氏度.
    常见说法: "出口低于 21 度", "outlet 不高于 25°C", "出水温度小于 X 度". 用户没说填 null.
  - mesh_quality (string): 网格档位, 合法值 "coarse" / "medium" / "fine"。
    常见说法: "精细网格"→"fine", "粗一点"→"coarse", 没说就 "medium"。

如果用户没说某个值, 用以下默认值:
  - chip_power_watts: 10.0
  - inlet_velocity_ms: 1.0
  - max_iterations: 150
  - chip_material: "silicon"
  - limit_C: null
  - outlet_limit_C: null
  - mesh_quality: "medium"

单位换算约定:
  - "千瓦/kW" → 乘 1000 转成 W
  - "厘米每秒/cm/s" → 除 100 转成 m/s
  - "公里每小时/km/h" → 除以 3.6 转成 m/s
  - 用户给一个范围(比如 "10 到 15 瓦"), 取中位数。

示例:
  输入: "芯片功耗 15 瓦, 风速 2 米每秒, 跑 200 步"
  输出: {"chip_power_watts": 15.0, "inlet_velocity_ms": 2.0, "max_iterations": 200, "chip_material": "silicon", "limit_C": null, "outlet_limit_C": null, "mesh_quality": "medium"}

  输入: "5kW 的芯片, 给我吹 50 cm/s 的风"
  输出: {"chip_power_watts": 5000.0, "inlet_velocity_ms": 0.5, "max_iterations": 150, "chip_material": "silicon", "limit_C": null, "outlet_limit_C": null, "mesh_quality": "medium"}

  输入: "随便跑跑试试"
  输出: {"chip_power_watts": 10.0, "inlet_velocity_ms": 1.0, "max_iterations": 150, "chip_material": "silicon", "limit_C": null, "outlet_limit_C": null, "mesh_quality": "medium"}

  输入: "铜质芯片 50W, 风速 1.5 m/s, 警戒线 100 度, 精细网格"
  输出: {"chip_power_watts": 50.0, "inlet_velocity_ms": 1.5, "max_iterations": 150, "chip_material": "copper", "limit_C": 100.0, "outlet_limit_C": null, "mesh_quality": "fine"}

  输入: "硅芯片 200 瓦, 风 2 m/s, 跑 300 步, 不能超 110 度"
  输出: {"chip_power_watts": 200.0, "inlet_velocity_ms": 2.0, "max_iterations": 300, "chip_material": "silicon", "limit_C": 110.0, "outlet_limit_C": null, "mesh_quality": "medium"}

  输入: "芯片功率 15W, 风速 0.4 m/s, 跑 80 步, 出口低于 21 度"
  输出: {"chip_power_watts": 15.0, "inlet_velocity_ms": 0.4, "max_iterations": 80, "chip_material": "silicon", "limit_C": null, "outlet_limit_C": 21.0, "mesh_quality": "medium"}
"""


# ============================================================
# Pydantic 模型 — LLM 输出的 schema 校验
# ============================================================

class SimulationParams(BaseModel):
    """
    run_simulation 接受的参数 schema。
    用 Pydantic 而非 dataclass: 自带类型强转 + 边界校验 + 错误信息友好。

    W2 起多 3 个可选字段 (chip_material / limit_C / mesh_quality).
    用户没说就走默认, run_simulation 见到也不会崩 (用 .get() 取).
    """
    # Field(...) 第一个参数是默认值; 后面 ge / le / gt 是数值约束。
    # description 不仅给人看, Pydantic 也会写进 .model_json_schema(), 以后给 LangGraph 用。
    chip_power_watts: float = Field(
        default=_DEFAULT_CHIP_POWER_W, # 如果你不传这个值，就用默认值
        ge=0.0,                                    # ge = greater or equal, >= 0
        description="芯片功耗, 单位 W; 0 = 关芯片",   #这个是给ai的提示词，给后面所有的ai
    )
    #field(...): 定义字段的默认值、约束、描述等
    inlet_velocity_ms: float = Field(
        default=_DEFAULT_INLET_VELOCITY_MS,
        gt=0.0,                                    # gt = greater than, 严格 > 0
        description="入口风速, 单位 m/s; 必须 > 0 否则没有流动",
    )
    max_iterations: int = Field(
        default=_DEFAULT_MAX_ITERATIONS,
        ge=10,                                     # 至少 10 步, 太少没意义
        le=2000,                                   # 上限 2000 防止 LLM 给 999999 之类
        description="最大迭代步数, [10, 2000]",
    )

    # ---------- W2 新增字段 ----------
    # 这三个字段 wrapper 当前只消费 limit_C, chip_material 暂时只 echo 到 result.
    # mesh_quality 留给 W3 — 写在 schema 里是为了 query 现在就能学正确写法.
    chip_material: str = Field(
        default="silicon",
        description="芯片材料. 当前只 echo 到 result, W4 会真切换 Fluent 材料库. "
                    "合法值: silicon / aluminum / copper / steel.",
    )
    limit_C: Optional[float] = Field(
        default=None,
        description="温度警戒线 (°C). None = 按 case_mode 取默认 "
                    "(mixing_elbow=85, manifold_cht=1500). 用户显式指定就覆盖.",
    )
    # [W3] outlet 平均温度警戒线 — 用于多轮迭代判达标 (mixing_elbow 路径).
    # mixing_elbow 上 max_temp 是 hot-inlet 边界温度钉死 (踩坑 #6),
    # 不能用 limit_C 触发迭代; outlet_limit_C 给 iter_planner 用.
    # 用户 query 写"出口低于 21 度" / "outlet 不高于 25°C" 等, LLM 写到这里.
    outlet_limit_C: Optional[float] = Field(
        default=None,
        description="出口平均温度警戒线 (°C). None = 用 result_analyzer 默认值 21°C "
                    "(mixing_elbow). 用户写'出口低于 X 度'时填这里.",
    )
    mesh_quality: str = Field(
        default="medium",
        description="网格档位 (coarse/medium/fine). W2 暂未实施 mesh adapt, "
                    "只 echo 到 result. W3 后接 mesh_study.py.",
    )

    # model_config 是 Pydantic v2 的配置入口。
    # extra="forbid" 表示 LLM 多塞键(比如 "explanation")就直接报错 → 触发重试。
    # 这样能保证返回给 run_simulation 的 dict 绝对干净, 没多余字段。
    model_config = {"extra": "forbid"} #禁止传入多余的字段！

    @field_validator("max_iterations", mode="before")
    #下面这个函数，专门用来处理 max_iterations，而且先处理，再校验！
    @classmethod
    #把普通函数，变成「类专属函数」下面这个第一个参数是cls
    def _coerce_iterations(cls, v):
        """
        LLM 偶尔会把 "200" 字符串塞进来 (尽管 system prompt 要求 number)。
        mode='before' 表示在标准类型校验之前先跑这个函数, 把字符串转成 int。
        """
        if isinstance(v, str):
            # 去掉常见后缀("步", "次", "iterations") 再转
            v = v.strip().rstrip("步次iterations")   #删除空格、迭代次数后缀
            return int(float(v))   # 先 float 再 int, 容忍 "200.0" 这种
        return v

    @field_validator("chip_material", mode="before")
    @classmethod
    def _coerce_material(cls, v):
        """LLM 可能给 'Silicon' / '硅' / '硅芯片', 全部归一到小写英文."""
        if not isinstance(v, str):
            return v
        v = v.strip().lower()
        # 中文别名
        cn2en = {"硅": "silicon", "铝": "aluminum", "铜": "copper", "钢": "steel"}
        for cn, en in cn2en.items():
            if cn in v:
                return en
        # 英文里只取第一个词 (容忍 "silicon chip" 这种)
        return v.split()[0] if v else "silicon"

    @field_validator("mesh_quality", mode="before")
    @classmethod
    def _coerce_mesh_quality(cls, v):
        """归一到 coarse/medium/fine, 兜底 medium."""
        if not isinstance(v, str):
            return "medium"
        v = v.strip().lower()
        # 中文别名
        if any(k in v for k in ("粗", "coarse")):
            return "coarse"
        if any(k in v for k in ("细", "精", "fine")):
            return "fine"
        return "medium"

# ============================================================
# DeepSeek 客户端工厂
# ============================================================

def _load_config() -> dict:
    """
    读 config.yaml, 返回 dict。
    把这步独立出来, 方便单测里 monkeypatch (替成 fake config)。
    """
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) #把 YAML 文件 → 变成 Python 字典


def _make_client(cfg: Optional[dict] = None) -> OpenAI:
    """
    构造 DeepSeek 客户端 (走 OpenAI 兼容协议)。
    传 cfg=None 就读 config.yaml; 传 cfg dict 是给单测注入用。
    """
    if cfg is None:
        cfg = _load_config()
    return OpenAI(
        api_key=cfg["deepseek"]["api_key"],
        base_url=cfg["deepseek"]["base_url"],
    )


# ============================================================
# 核心 API: parse_intent
# ============================================================

def parse_intent(user_text: str, *, _client: Optional[OpenAI] = None,
                 _model: Optional[str] = None) -> dict:
    # *的意思是前面是必传参数，后面是可选参数
    """
    把一段中文需求 → params dict (run_simulation 直接可用)。

    Args:
        user_text: 用户的自然语言输入, 比如 "芯片 15W, 风速 2 m/s"。
        _client / _model: 单测后门, 业务代码不要传, 默认从 config.yaml 自动构造。

    Returns:
        dict, 三键: chip_power_watts (float), inlet_velocity_ms (float),
                    max_iterations (int)。结构与 fluent_wrapper._REQUIRED_KEYS 一致。

    Raises:
        ValueError: user_text 是空字符串。
        RuntimeError: DeepSeek 多次重试后仍失败 (网络/JSON/校验都算)。
    """
    # ---------- (1) 入参防呆 ----------
    # 空字符串没法翻译, 早期拦截比让 LLM 输出垃圾再校验强。
    if not user_text or not user_text.strip():
        raise ValueError("user_text 不能为空")

    # ---------- (2) 准备客户端 + 模型名 ----------
    if _client is None or _model is None:
        cfg = _load_config()
        _client = _client or _make_client(cfg)
        _model = _model or cfg["deepseek"]["model"]

    # ---------- (3) 构造 messages ----------
    # 第一条 system prompt 锁定输出格式; 第二条放用户输入。
    # 把 user_text 单独放一条 user 消息, 不和 system 拼在一起 — 这是 OpenAI 兼容协议
    # 的标准做法, 模型对角色边界更敏感, 输出更稳定。
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_text.strip()},
    ]

    # ---------- (4) 重试循环: 调用 → 解析 → 校验 ----------
    last_err: Optional[str] = None  # 最后一次失败原因, 拼进下一轮 prompt 让模型改
    # Python 的类型注解语法，Optional[str] 表示 last_err 可以是 str 类型，也可以是 None 类型，
    # range(_MAX_RETRIES + 1) = [0, 1, 2], 共 3 次机会(首次 + 2 次重试)
    for attempt in range(_MAX_RETRIES + 1):
        # 如果是重试, 把上次错误塞进对话, 让模型"看到自己上次错在哪"再改。
        # 这种"自我修正"模式比无脑重试有效得多。
        if last_err is not None:
            messages.append({
                "role": "user",
                "content": (
                    f"上一次你的回答有问题: {last_err}\n"
                    "请重新输出严格符合要求的 JSON, 不要任何解释文字。"
                ),
            })

        try:
            # 真正调用 AI 模型，获取 JSON 字符串
            # response_format={"type": "json_object"} 让 DeepSeek 强制输出合法 JSON,
            # 不会夹杂 markdown 代码围栏(```json ... ```)或前后废话。
            # 这是和 GPT-4-turbo 的 JSON mode 同名同效, DeepSeek 文档明确支持。
            resp = _client.chat.completions.create(
                model=_model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.0,   # 0 = 最确定性, 解析任务不需要创造性
            )
            raw = resp.choices[0].message.content  # LLM 返回的字符串
        except Exception as e:
            # 网络错误/超时/auth 失败等。算一次失败, 进入下一轮重试。
            last_err = f"DeepSeek API 调用失败: {type(e).__name__}: {e}"
            print(f"[intent][WARN] attempt {attempt + 1}: {last_err}",
                  file=sys.stderr)
            continue

        # ---------- (4a) JSON 解析 ----------
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            last_err = f"返回不是合法 JSON: {e}; 原文: {raw!r}"
            print(f"[intent][WARN] attempt {attempt + 1}: {last_err}",
                  file=sys.stderr)
            continue  # 重试

        # ---------- (4b) Pydantic 校验 ----------
        # 这一步会做: 类型强转 + 范围检查 + extra="forbid" 拒多余键。
        # 返回 SimulationParams 实例, 通过 .model_dump() 转回普通 dict。
        try:
            params = SimulationParams(**data).model_dump()
        except ValidationError as e:
            # ValidationError.errors() 是结构化的失败列表, 比 str(e) 更紧凑给 LLM 看。
            last_err = f"JSON 校验失败: {e.errors()}"
            print(f"[intent][WARN] attempt {attempt + 1}: {last_err}",
                  file=sys.stderr)
            continue

        # 全部成功 — 直接返回, 不再重试
        return params

    # ---------- (5) 重试用尽, 抛错 ----------
    raise RuntimeError(
        f"parse_intent 失败, 已重试 {_MAX_RETRIES} 次。最后一次错误: {last_err}"
    )


# ============================================================
# 自测试: python -m agents.intent_parser
# 不带参数 = 跑 3 句中文样本; 带参数 = 解析这一句
# ============================================================

# 3 句覆盖不同情况的中文样本 — 验收标准就是这 3 种
# 都能解析对。
_SELFTEST_CASES = [
    # 1. 标准三件套: 三个值都明确说了
    ("芯片功耗 15 瓦, 风速 2 米每秒, 跑 200 步",
     {"chip_power_watts": 15.0, "inlet_velocity_ms": 2.0, "max_iterations": 200}),

    # 2. 含单位换算: kW → W, cm/s → m/s, 没说迭代步数 → 用默认 150
    ("5 kW 的芯片, 风速 50 厘米每秒",
     {"chip_power_watts": 5000.0, "inlet_velocity_ms": 0.5, "max_iterations": 150}),

    # 3. 啥都没说 — 全部默认值
    ("帮我跑个仿真",
     {"chip_power_watts": _DEFAULT_CHIP_POWER_W,
      "inlet_velocity_ms": _DEFAULT_INLET_VELOCITY_MS,
      "max_iterations": _DEFAULT_MAX_ITERATIONS}),
]


def _selftest() -> int:
    """
    验收: 跑 3 句中文样本, 全部解析成功 + 关键字段对得上 = PASS。
    返回 0 = PASS, 非 0 = FAIL (供 sys.exit 用)。
    """
    print("=" * 60)
    print("自测试: parse_intent 解析 3 句中文 → params dict")
    print("=" * 60)

    n_pass = 0
    n_fail = 0
    for i, (text, expected) in enumerate(_SELFTEST_CASES, 1):
        print(f"\n--- Case {i}/{len(_SELFTEST_CASES)} ---")
        print(f"输入: {text}")
        print(f"期望: {expected}")
        try:
            got = parse_intent(text)
            print(f"实际: {got}")

            # 完全相等才算 PASS。
            # 注意 LLM 偶尔会把 inlet_velocity_ms 给成 0.5000000001 之类, 容忍 1% 误差。
            ok = (
                abs(got["chip_power_watts"] - expected["chip_power_watts"]) < 0.01 * max(1.0, expected["chip_power_watts"])
                and abs(got["inlet_velocity_ms"] - expected["inlet_velocity_ms"]) < 0.01 * max(0.1, expected["inlet_velocity_ms"])
                and got["max_iterations"] == expected["max_iterations"]
            )
            if ok:
                print("[OK]")
                n_pass += 1
            else:
                print("[FAIL] 数值与期望不一致")
                n_fail += 1
        except Exception as e:
            print(f"[FAIL] 抛异常: {type(e).__name__}: {e}")
            n_fail += 1

    print("\n" + "=" * 60)
    print(f"Summary: {n_pass} PASS / {n_fail} FAIL (共 {len(_SELFTEST_CASES)})")
    print("=" * 60)

    if n_fail == 0:
        print("PASSED — intent_parser 可用。")
        return 0
    return 1

#让你可以在命令行里直接用这个 AI 翻译功能！
def _cli() -> int:
    """命令行入口。带 query 就解析单句, 不带就跑自测试。"""
    parser = argparse.ArgumentParser(
        prog="python -m agents.intent_parser",
        description="把中文仿真需求翻译成 JSON params (供 run_simulation 用)",
    )
    parser.add_argument(
        "query",
        nargs="?",                  # 可选位置参数
        default=None,
        help="一句中文需求; 不传则跑自测试样本",
    )
    args = parser.parse_args()

    if args.query is None:
        return _selftest()

    # 单句模式: 解析 + 打印 JSON
    try:
        params = parse_intent(args.query)
    except Exception as e:
        print(f"[FAIL] {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    # 用 ensure_ascii=False, 中文不被转义成 \uXXXX (虽然这里 key/value 都没中文,
    # 但留个习惯, 后面如果加 description 字段直接受益)。
    print(json.dumps(params, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    # 入口分发: 直接 `python -m agents.intent_parser` 时执行 _cli()。
    sys.exit(_cli())
