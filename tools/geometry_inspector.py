"""
tools/geometry_inspector.py - 几何感知 (W4 老师鼓励 1)
============================================================
回答老师答辩问题: "换一个 CHT 案例, 你这套还能跑吗?"
答: 能, 这个模块自动扫 cell zones, 用关键词识别 chip/fluid/heatsink.

为什么需要这一层:
  W2 wrapper 里 CHIP_ZONE = "solid_up" 是 hardcoded.
  换 PCB 案例 (chip 叫 "die" 或 "silicon_chip") 就跪.
  这个模块在 wrapper 启动后跑一次 discover, 动态拿 chip_zone 名.

工作原理 (两层):
  Layer 1: 关键词启发式 (chip/die/silicon/cpu/gpu/ic, fluid/air/water,
                          heatsink/fin/cooler)
  Layer 2: LLM fallback (启发式失败时, DeepSeek 选, 仍只输出 zone 名字)

接口:
  >>> from tools.geometry_inspector import discover_zones, find_chip_zone
  >>> classification = discover_zones(session)
  >>> chip_zone = find_chip_zone(session)

测试:
  python -m tools.geometry_inspector  # mock session 自测
"""
import sys
from typing import Dict, List, Optional


_CHIP_KEYWORDS = (
    "chip", "die", "silicon", "cpu", "gpu", "ic",
    "transistor", "mosfet", "processor", "solid_up",
)
_FLUID_KEYWORDS = (
    "fluid", "air", "water", "coolant", "gas", "oil",
)
_HEATSINK_KEYWORDS = (
    "heatsink", "heat_sink", "heat-sink", "fin", "cooler", "radiator",
)


def discover_zones(session, use_llm_fallback: bool = False) -> Dict[str, List[str]]:
    """扫 cell_zones 分类 chip/fluid/heatsink. 接口契约见模块 docstring."""
    all_zones = _list_all_cell_zones(session)
    if not all_zones:
        return {"chip": [], "fluid": [], "heatsink": []}

    classification = _classify_by_keywords(all_zones)

    if use_llm_fallback and not classification["chip"]:
        chip_guess = _classify_by_llm(all_zones)
        if chip_guess:
            classification["chip"] = [chip_guess]

    return classification


def find_chip_zone(session, fallback: str = "solid_up") -> str:
    """便捷接口: 拿一个最可能是 chip 的 zone 名, 没识别就回退 fallback."""
    classification = discover_zones(session)
    if classification["chip"]:
        return classification["chip"][0]
    print("[geo] no chip zone detected, fallback to " + repr(fallback))
    return fallback


def _list_all_cell_zones(session) -> List[Dict[str, str]]:
    out = []
    try:
        czc = session.settings.setup.cell_zone_conditions
    except Exception:
        return out

    for kind in ("fluid", "solid"):
        container = getattr(czc, kind, None)
        if container is None:
            continue
        try:
            names = list(container)
        except (TypeError, Exception):
            try:
                names = list(container.keys())
            except Exception:
                continue
        for n in names:
            out.append({"name": n, "type": kind})
    return out


def _classify_by_keywords(zones: List[Dict[str, str]]) -> Dict[str, List[str]]:
    """关键词分类. 命中优先级: chip > heatsink > fluid. solid 兜底进 chip."""
    classification: Dict[str, List[str]] = {"chip": [], "fluid": [], "heatsink": []}
    for z in zones:
        n_lower = z["name"].lower()
        ztype = z["type"]

        if any(kw in n_lower for kw in _CHIP_KEYWORDS):
            classification["chip"].append(z["name"])
        elif any(kw in n_lower for kw in _HEATSINK_KEYWORDS):
            classification["heatsink"].append(z["name"])
        elif any(kw in n_lower for kw in _FLUID_KEYWORDS):
            classification["fluid"].append(z["name"])
        elif ztype == "fluid":
            classification["fluid"].append(z["name"])
        elif ztype == "solid" and not classification["chip"]:
            classification["chip"].append(z["name"])

    return classification


def _classify_by_llm(zones: List[Dict[str, str]]) -> Optional[str]:
    """LLM fallback - 失败返 None."""
    try:
        from agents.intent_parser import _get_client
        client = _get_client()
        zone_list_str = ", ".join(z["name"] + "(" + z["type"] + ")" for z in zones)
        prompt = (
            "下面是 Ansys Fluent cell zone 名字列表 (含类型 fluid/solid). "
            "选最可能是芯片/发热源的 zone, 只输出 zone 名字不解释:\n"
            + zone_list_str
        )
        resp = client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=50,
            temperature=0,
        )
        guess = resp.choices[0].message.content.strip().strip("'\"")
        for z in zones:
            if z["name"] == guess:
                return guess
    except Exception as e:
        print("[geo][WARN] LLM fallback failed: " + str(e), file=sys.stderr)
    return None


class _MockContainer:
    def __init__(self, names):
        self._names = names
    def __iter__(self):
        return iter(self._names)


class _MockCzc:
    def __init__(self, fluid_names, solid_names):
        self.fluid = _MockContainer(fluid_names) if fluid_names else None
        self.solid = _MockContainer(solid_names) if solid_names else None


class _MockSettings:
    def __init__(self, fluid, solid):
        class _Setup:
            cell_zone_conditions = _MockCzc(fluid, solid)
        self.setup = _Setup()


class _MockSession:
    def __init__(self, fluid, solid):
        self.settings = _MockSettings(fluid, solid)


def _selftest() -> int:
    print("=" * 60)
    print("geometry_inspector selftest")
    print("=" * 60)

    cases = [
        ("manifold case", ["fluid1"], ["solid_up"], "solid_up"),
        ("PCB case (die)", ["air"], ["die", "pcb"], "die"),
        ("CPU case", ["fluid"], ["cpu_silicon", "heatsink_fin"], "cpu_silicon"),
        ("English chip", ["coolant"], ["chip-1"], "chip-1"),
        ("ambiguous solid", ["fluid"], ["body_a", "body_b"], "body_a"),
    ]
    n_pass = 0
    n_fail = 0
    for name, fluid, solid, expected in cases:
        session = _MockSession(fluid, solid)
        result = discover_zones(session)
        chip_list = result["chip"]
        if expected in chip_list:
            print("[OK]   " + name + ": chip=" + str(chip_list))
            n_pass += 1
        else:
            print("[FAIL] " + name + ": expected=" + repr(expected) + ", got=" + str(result))
            n_fail += 1

    print()
    print("Summary: " + str(n_pass) + " PASS / " + str(n_fail) + " FAIL (total " + str(len(cases)) + ")")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(_selftest())
