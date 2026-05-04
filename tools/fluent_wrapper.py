"""
tools/fluent_wrapper.py
============================================================
把仿真流程封装成可重复调用的 run_simulation(params) 函数。

为什么要把脚本变成函数(给以后的你/答辩时用):
  原始基线脚本是"流水账"——从启动到出结果一条直线跑下来,
  跑完就退出。这种代码没法被别人调,也没法在循环里跑多次。
  封装成函数:
        result = run_simulation(params)
  之后:
    - intent_parser 把中文翻译成 params dict, 直接 run_simulation(params)
    - result_analyzer 接 result dict 出云图
    - LangGraph 把这个函数当作"setup → run"节点
  函数化是把"一次性 demo"变成"基础设施零件"的关键一步。

调用接口(API):
  >>> from tools.fluent_wrapper import run_simulation
  >>> result = run_simulation({
  ...     "chip_power_watts": 15.0,
  ...     "inlet_velocity_m/s": 2.0,
  ...     "max_iterations": 200,
  ... })
  >>> print(result)
  {"max_temp_K": 352.1, "converged": True, "iterations": 200, ...}

参数的"分层"含义(架构决策 #2 — 写在 设计思路.md §6.2):
  - 用户层 (params dict): 物理直觉单位, 瓦 / (米/秒) / 步数
  - Fluent 层 (本模块负责换算): 工程单位, W/m^3 / Pa / 边界条件 ID
  把单位换算放在 wrapper 而不是 LLM, 是因为:
    (a) LLM 算 W/m^3 容易掉精度, JSON 里写整数最稳;
    (b) 维持 LLM 输出 schema 简单, 校验也简单。

仍用 mixing-elbow 案例(架构决策 #3 baseline-first):
  - inlet_velocity_ms 会真正生效(只改 cold-inlet 的 vmag, hot-inlet 保留默认)。
    "为什么只改一个": 见 踩坑记录 #6 — 同时改两个会让冷/热流量比保持不变,
    混合温度也跟着不变, 看不出参数影响。
  - max_iterations 会真正生效(传给 iterate) #最大的迭代次数
  - chip_power_watts 在 mixing-elbow 里"没地方用"——它是混合管,没芯片;
    所以暂时只把数值原样回传到 result["params_echo"], 不动 Fluent。
    W2 换共轭传热(CHT)案例后, 这里会改成:
        Q_volumetric = chip_power_watts / chip_volume_m3   (W/m^3)
        cell_zone_conditions['chip-die'].source_terms.energy = Q_volumetric
    "接口先稳, 案例后换"是为了让 intent_parser 的 schema 在 W1 就稳定下来,
    不必等 W2 才开始训练。

回退策略(架构决策 #5 fallback-everywhere):
  - 设置边界条件: 先试 modern settings API, 失败就用 TUI;
    再失败就只警告不抛错, 让仿真用案例自带默认值跑下去——
    "宁可参数没生效, 也别整个流程崩"。
  - 求解 / 取结果: 用 try/except + 多 cell-zone 名字尝试。
  - finally: 无论成败必关 Fluent。学生版 license 数量极少, 关不掉就一直占着。
"""
# ↑ 模块文档字符串。可以被 help(tools.fluent_wrapper) 读到。
import os                  # 路径处理
import re                  # 正则, 从 Fluent 输出文本里抠数字
import sys                 # sys.exit()、sys.stderr
import time                # 计时
import traceback           # 出错打印调用栈
from typing import Callable, Optional   # analyzer hook 的类型标注


# ============================================================
# 全局常量 — 改这里就能调"基线", 不动函数体
# ============================================================

# mixing-elbow 案例在 PyFluent 官方示例库里的位置
# 元组拆开传给 examples.download_file(*CASE_REMOTE)
CASE_REMOTE = ("mixing_elbow.cas.h5", "pyfluent/mixing_elbow")

# 启动 Fluent 用几个 CPU 核。学生版上限 4, 用 2 给系统留余量。
N_PROC = 2

# 必填参数键名。集中放成元组, 改 schema 时只改这一处。
_REQUIRED_KEYS = ("chip_power_watts", "inlet_velocity_ms", "max_iterations")

# 临时文件名(给 volume-integrals 命令写结果用)。函数内部用绝对路径。
_TEMP_REPORT_FILE = "_wrapper_max_temp.txt"


def _resolve_case_file(examples_module) -> str:
    """
    返回 mixing-elbow case 文件的绝对路径。优先本地, 再 fallback 远程。

    查找顺序:
      1. <项目根>/mixing_elbow.cas.h5  (已落盘的文件)
      2. cwd/mixing_elbow.cas.h5       (脚本从别处启动时的当前目录)
      3. examples.download_file(...)   (真去 GitHub 拉, 可能 404)

    为什么单写一个函数:
      run_simulation 已经够长, 这个"找文件 + 失败兜底"细节单拆一层
      函数, 看主流程时不被打断, 排错时直接 import 这个函数验证文件路径。
    """
    case_filename = CASE_REMOTE[0]    # "mixing_elbow.cas.h5"

    # tools/fluent_wrapper.py 上一级就是项目根。
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    candidates = [
        os.path.join(project_root, case_filename),
        os.path.join(os.getcwd(), case_filename),
    ]
    for p in candidates:
        if os.path.isfile(p):
            print(f"[wrapper] 命中本地 case 文件: {p}")
            return p

    # 本地没有就尝试 PyFluent 官方下载。失败时给用户清晰的下一步指示,
    # 比 RemoteFileNotFoundError 这条裸异常友好得多。
    print(f"[wrapper] 本地未找到 {case_filename}, 尝试从 GitHub 下载...")
    try:
        return examples_module.download_file(*CASE_REMOTE)
    except Exception as e:
        raise RuntimeError(
            f"无法获取基线 case 文件 {case_filename}:\n"
            f"  - 本地查找路径都不存在: {candidates}\n"
            f"  - 远程下载失败: {type(e).__name__}: {e}\n"
            f"  解决: 把 mixing_elbow.cas.h5 (~3MB) 放到项目根目录后重跑。"
        ) from e


# ============================================================
# 公开 API: run_simulation
# ============================================================

def run_simulation(
    params: dict, #用户层参数字典
    analyzer: Optional[Callable[[object, dict], Optional[dict]]] = None,
) -> dict:
    """
    跑一次仿真并以 dict 形式返回结果。

    Args:
        params: 用户层参数字典, 必须包含:
            - chip_power_watts (float): 芯片功耗。mixing-elbow 路径未施加,
              仅原样回传到 result["params_echo"]; W2 换 CHT 案例后才真正注入。
            - inlet_velocity_ms (float): 入口速度。只设到 cold-inlet, hot-inlet 保留
              案例默认值(详见踩坑 #6: 同时改两个会让流量比不变, 看不出效果)。
            - max_iterations (int): 最大迭代步数, 直接传给 iterate。
        analyzer (可选): 后处理钩子, 形如 fn(session, current_result) -> dict。
            会在 session.exit() 之前被调用, 拿到的 session 还是活的, 可以抓云图、查
            场数据。它返回的 dict 会被 update 进最终 result——典型用法是
            agents/result_analyzer.analyze, 把云图 PNG 路径 / 报告路径 塞进来。
            不传 = 只返回数字。

    Returns:
        result dict, 字段:
            max_temp_K / max_temp_C (float):
                全域最高温度, 开尔文 / 摄氏度。在 mixing-elbow 上恒等于 hot-inlet
                边界温度(313.15 K), 因为最大值出现在边界本身——这是物理而非 bug,
                W1 手册要求输出, 答辩时讲解物理边界用。
            outlet_avg_temp_K / outlet_avg_temp_C (float):
                outlet 边界质量加权平均温度。这才是混合效果指标, 会随
                cold-inlet 速度变化, _selftest 用它做 PASS 判定。
            converged (bool):       简化版判定 = "是否成功跑完 + max_temp 在
                                    物理合理范围"。result_analyzer 做真正的残差判定。
            iterations (int):       请求的迭代步数(原样回传)
            elapsed_seconds (float): 全流程耗时, 秒
            velocity_applied (bool): cold-inlet 速度是否成功设进去(modern API 或 TUI)
            params_echo (dict):     原样回传 params, 排错时方便对照

    Raises:
        ImportError: ansys-fluent-core 未装。
        TypeError / KeyError / ValueError: params 不合法。
        RuntimeError: 仿真过程中任意阶段失败。
    """
    # ---------- (1) 校验参数 ----------
    # 校验放在最前面: 参数错就别浪费 30s 启 Fluent 才报错。
    _validate_params(params)

    # ---------- (2) 延迟导入 PyFluent ----------
    # 不在文件顶部 import: 让 "import tools.fluent_wrapper" 这种不实际跑仿真的
    # 调用(比如单元测试 import _validate_params)不需要装 PyFluent。
    try:
        import ansys.fluent.core as pyfluent
        from ansys.fluent.core import examples
    except ImportError as e:
        raise ImportError(
            "ansys-fluent-core 未安装。激活 .venv 后 pip install -r requirements.txt"
        ) from e
    # ↑ raise X from e 这种写法叫"链式异常",会保留原因, traceback 里显示
    #   "The above exception was the direct cause of the following exception"。

    # ---------- (3) 定位基线案例文件 ----------
    # 优先级: 项目根 → cwd → PyFluent 官方 examples 下载。
    # 为什么不直接 download_file: 2026-04 起, GitHub 上 example-data 仓库
    # 的 mixing_elbow.cas.h5 路径偶发 404 (RemoteFileNotFoundError),
    # 而文件本身已经下到项目根 (~3MB)。本地有就用本地, 既快又稳;
    # 真没有再 fallback 到 GitHub。"fallback at every layer" — 架构决策 #5。
    case_path = _resolve_case_file(examples)

    # ---------- (4) 启动 Fluent ----------
    t_total = time.time()
    print(f"[wrapper] 启动 Fluent (no_gui, {N_PROC} 核)...")
    session = pyfluent.launch_fluent(
        mode="solver",          # 只要求解器, 不要前/后处理 GUI
        ui_mode="no_gui",       # 完全无界面, 被脚本驱动必须如此
        dimension=3,            # mixing-elbow 是 3D
        precision="double",     # 双精度, 工业仿真标配
        processor_count=N_PROC,
    )
    # ↑ launch_fluent 返回的 session 就是"遥控器", 后续所有命令都通过它发。

    # 这两个变量先在 try 外声明, finally 里要用; 用 NaN 避免误读"成功"
    converged = False
    max_t = float("nan")
    outlet_avg_t = float("nan")
    velocity_applied = False  # 记录 inlet 速度是否真改进去了, 写到日志方便排错
    analyzer_extra: dict = {}  # analyzer hook 的额外字段(报告路径/PNG 路径等)
    #以上是结果变量

    try:
        # ---------- (5) 读案例 + 网格检查 ----------
        _read_case(session, case_path)
        print("[wrapper] 案例已加载")
        _check_mesh(session)
        print("[wrapper] 网格检查通过")

        # ---------- (6) 应用 user 参数 (架构决策 #2 — 分层换算在这里) ----------
        velocity_applied = _apply_inlet_velocity(session, params["inlet_velocity_ms"])   #把案例里面的冷水流速改为用户自定义的
        _record_chip_power(params["chip_power_watts"])  # mixing-elbow 阶段仅打印，关于功耗的现在先不改
        # mixing-elbow 之后的 chip-cooling 场景预留的钩子

        # ---------- (7) 初始化 + 迭代 ----------
        _initialize(session)
        print(f"[wrapper] 开始迭代 {params['max_iterations']} 步...")
        _iterate(session, params["max_iterations"]) #迭代计算的函数

        # ---------- (8) 提取最高温度 + 出口平均温度 ----------
        # 为什么两个都要(踩坑 #6):
        #   max_temp 在 mixing-elbow 永远 = hot-inlet 边界温度(313.15 K),
        #   不随入口速度变化, 所以不能用它判定"参数有没有生效"。
        #   outlet 平均温度才反映"冷热混合", 会随 cold-inlet 流量比变化。
        #   max_temp 仍然返回, 因为 W1 手册要求"输出最高温度", 答辩时也要讲它。
        max_t = _query_max_temperature(session)
        print(f"[wrapper] 最高温度 = {max_t:.2f} K  ({max_t - 273.15:.2f} °C)")

        outlet_avg_t = _query_outlet_avg_temperature(session)
        print(f"[wrapper] 出口平均温度 = {outlet_avg_t:.2f} K  "
              f"({outlet_avg_t - 273.15:.2f} °C)")

        # 简化收敛判定: 走到这一步说明"没崩"。物理合理性也顺手检查一下。
        # mixing-elbow 入口 ~293K/313K, 任何超出 200~500K 的都是异常。
        if 200.0 < max_t < 500.0:
            converged = True
        else:
            converged = False
            print(f"[wrapper][WARN] max_temp={max_t} K 不在合理范围(200-500K), "
                  "判定为未收敛", file=sys.stderr)

        # ---------- (8.5) analyzer hook ----------
        # 必须放在 finally 之前: hook 要拿活的 session 抓云图。
        # 设计取舍: hook 内部异常不能让 run_simulation 整体失败——分析失败比
        # 仿真失败"轻"得多, 数字都已经算出来了, 不该因为图没画出来就 raise。
        # 出错只 print 到 stderr, 主结果照常返回。
        if analyzer is not None:
            try:
                # 提前算出 partial result 给 hook 看, 它需要这些数字做警戒线判定
                partial = {
                    "max_temp_K": round(max_t, 2),
                    "max_temp_C": round(max_t - 273.15, 2),
                    "outlet_avg_temp_K": round(outlet_avg_t, 2),
                    "outlet_avg_temp_C": round(outlet_avg_t - 273.15, 2),
                    "converged": converged,
                    "iterations": params["max_iterations"],
                    "elapsed_seconds": round(time.time() - t_total, 1),
                    "velocity_applied": velocity_applied,
                    "params_echo": dict(params),
                }
                
                ret = analyzer(session, partial)
                #analyze 拿到 session 后，调 Fluent 的画图 API，写文件，然后把 PNG 路径打包成 dict 返回给 wrapper（就是 ret）
                
                if isinstance(ret, dict):
                    analyzer_extra = ret
                    #analyzer 函数（实际就是 agents/result_analyzer.py:245 的 analyze）：知道怎么画图，不知道怎么跑仿真
                    #analyzer 是参数没错，但它的类型是 Callable —— 也就是说传进来的值本身就是一个函数。
                    #Python 里函数和数字、字符串一样是"一等公民"，可以塞进变量

                elif ret is not None:
                    print(f"[wrapper][WARN] analyzer 返回了非 dict({type(ret).__name__}), "
                          "已忽略", file=sys.stderr)
                    
            except Exception as e:
                print(f"[wrapper][WARN] analyzer hook 抛异常 (非致命): "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
                traceback.print_exc()

    except Exception as e:
        # 把详细 traceback 打到 stderr, 但向调用者抛 RuntimeError, 信息更整洁
        traceback.print_exc()
        raise RuntimeError(f"run_simulation 失败: {type(e).__name__}: {e}") from e

    finally:
        # finally 块 — 无论 try 成败必跑。这里只做一件事: 关 Fluent。
        # 不关会:
        #   - 占着 license 槽位(学生版总共就几个)
        #   - 留个 fluent.exe 进程, 下次启动可能端口冲突
        if session is not None:
            try:
                session.exit()
                print("[wrapper] Fluent 干净退出")
            except Exception as e:
                print(f"[wrapper][WARN] session.exit() 抛错(非致命): {e}",
                      file=sys.stderr)

    # ---------- (9) 组装 result dict 返回 ----------
    result = {
        "max_temp_K": round(max_t, 2),
        "max_temp_C": round(max_t - 273.15, 2),
        "outlet_avg_temp_K": round(outlet_avg_t, 2),
        "outlet_avg_temp_C": round(outlet_avg_t - 273.15, 2),
        "converged": converged,
        "iterations": params["max_iterations"],
        "elapsed_seconds": round(time.time() - t_total, 1),
        "velocity_applied": velocity_applied,
        "params_echo": dict(params),  # 浅拷贝, 防止外部 mutate 影响日志
    }
    # 把 analyzer hook 的扩展字段(report path / png path / within_spec ...) 合并进来。
    # 用 update 而不是字面量重写: 让 analyzer 既能加新键, 也能覆盖默认值(比如更精细的 converged 判定)。
    # analyzer_extra 在 try 块开头被初始化为 {}, 走到这里一定存在; hook 没传或失败时它就是 {} (no-op)。
    if analyzer_extra:
        result.update(analyzer_extra)
    return result


# ============================================================
# 参数校验
# ============================================================

def _validate_params(params: dict) -> None:
    """
    简单 schema 校验。LLM 侧由 Pydantic 处理, 这里做基本兜底。
    """
    if not isinstance(params, dict):
        raise TypeError(f"params 必须是 dict, 实际是 {type(params).__name__}")

    # 用列表推导找缺失的 key, 一次性报告所有缺失项
    missing = [k for k in _REQUIRED_KEYS if k not in params]
    if missing:
        raise KeyError(f"params 缺少必要键: {missing}")

    # 数值范围检查 — 给"明显不合理"的值早期拦截
    if not (params["inlet_velocity_ms"] > 0):
        # 写成 not (...) > 0 而不是 <= 0, 是为了 NaN 也被拦下(NaN 比较都返回 False)
        raise ValueError(
            f"inlet_velocity_ms 必须 > 0, 实际为 {params['inlet_velocity_ms']}"
        )
    if params["max_iterations"] < 10:
        raise ValueError(
            f"max_iterations 必须 >= 10, 实际为 {params['max_iterations']}"
        )
    if params["chip_power_watts"] < 0:
        # 功耗负数没物理意义。0 允许(代表"关芯片")。
        raise ValueError(
            f"chip_power_watts 必须 >= 0, 实际为 {params['chip_power_watts']}"
        )


# ============================================================
# 把 user 参数应用到 Fluent
# ============================================================

def _apply_inlet_velocity(session, velocity_ms: float) -> bool:
    """
    把 mixing-elbow 的 cold-inlet 速度改成 user 给的值。hot-inlet 保留案例默认值。
    返回 True = 改成功; False = 两条路都失败(继续用案例默认值)。

    回退顺序(架构决策 #5):
      (a) modern settings API — PyFluent 0.30+ 标准方式
      (b) TUI 命令          — 老 PyFluent / 偶尔 modern API path 变了
      (c) 都失败就只 warn   — 让仿真继续跑, 用案例默认值
    """
    target_inlet = "cold-inlet"
    ok = False

    # ---------- 路线 (a): modern settings API ----------
    if _try_modern_velocity(session, target_inlet, velocity_ms):
        ok = True
    else:
        # modern 失败, 试 TUI
        if _try_tui_velocity(session, target_inlet, velocity_ms):
            ok = True

    if ok:
        print(f"[wrapper] {target_inlet} 速度已设为 {velocity_ms} m/s "
              "(hot-inlet 保留默认值)")
    else:
        print(f"[wrapper][WARN] {target_inlet} 没设成 {velocity_ms} m/s, "
              "本次跑会用案例默认值。如果两次结果一样, 多半是这里没生效。",
              file=sys.stderr) 
        #file=sys.stderr 用于将输出重定向到标准错误流，是 Python 中区分正常日志和错误/警告信息的常用做法。
    return ok


def _try_modern_velocity(session, inlet_name: str, vmag: float) -> bool:
    #vmag 是入口速度的目标值
    """
    用 modern settings API 设 velocity-inlet 的速度大小。
    PyFluent 在 0.30+ 提供了 dict-like 的边界条件接口, 但属性路径在版本间略变,
    所以我们试几个常见写法。哪个不抛错 AttributeError 就用哪个。
    """
    try:
        bc = session.setup.boundary_conditions
    except AttributeError:
        return False
    #为什么使用三种写法？  因为不同的版本/案例, 边界条件的属性路径会不同。 Fluent 的 Python 接口（API）经常变！
    # 写法 1 (新): bc.velocity_inlet["cold-inlet"].momentum.velocity = vmag
    try:
        bc.velocity_inlet[inlet_name].momentum.velocity = vmag #修改流速的公式
        #momentum.velocity 是 PyFluent 0.30+ 新增的属性, 用于设置入口速度。
        return True
    except (AttributeError, TypeError, KeyError):
        pass

    # 写法 2 (老): bc.velocity_inlet["cold-inlet"].vmag = vmag
    try:
        bc.velocity_inlet[inlet_name].vmag = vmag #修改流速的公式
        #vmag 是 PyFluent 0.30+ 新增的属性, 用于设置入口速度。
        return True
    except (AttributeError, TypeError, KeyError):
        pass

    # 写法 3: 通用字典 set_state
    try:
        bc.velocity_inlet[inlet_name].set_state({"vmag": vmag}) #修改流速的公式
        #set_state 是 PyFluent 0.30+ 新增的方法, 用于设置边界条件的属性。
        return True
    except (AttributeError, TypeError, KeyError):
        pass

    return False


def _try_tui_velocity(session, inlet_name: str, vmag: float) -> bool:
    """
    用 TUI 命令设 velocity-inlet 的速度大小。
    Fluent 的 TUI 是个"问答式"命令: 一条命令会顺序问你多个 prompt,
    PyFluent 把每个 prompt 答案当作一个位置参数。
    不同版本/案例 prompt 数量不同, 我们试 PyFluent docs 里最常见的两种序列。
    """
    try:
        tui_set = session.tui.define.boundary_conditions.set
    except AttributeError:
        return False

    # 序列 1: PyFluent 文档里典型的 mixing-elbow set 序列
    try:
        tui_set.velocity_inlet(
            inlet_name,           # 选择哪个 BC
            "()",                 # 结束选择
            "vmag",               # 要设的字段
            "no",                 # constant? no=constant
            vmag,                 # 数值
            "quit",               # 退出 set 上下文
        )
        return True
    except Exception:
        pass

    # 序列 2: 一些案例需要先指定 frame-of-ref
    try:
        tui_set.velocity_inlet(
            inlet_name,
            "()",
            "vmag",
            "no",
            "no",                 # 第二个 "no": profile? no
            vmag,
            "quit",
        )
        return True
    except Exception:
        pass

    return False


def _record_chip_power(power_watts: float) -> None:
    """
    占位函数 — mixing-elbow 没有"芯片"几何, 暂时不能把功耗加进去。
    放这个函数只是为了:
      (1) 占据架构上"应该把 W 换算成 W/m^3"的位置, 让以后改最少;
      (2) 在日志里说一句"我看到这个值了, 没忘", 方便答辩时讲。

    W2 换 CHT 案例后, 这里会变成大致这样:
        chip_volume = 0.01 * 0.01 * 0.001   # 假设芯片 1cm x 1cm x 1mm
        q_vol = power_watts / chip_volume   # W/m^3
        session.setup.cell_zone_conditions.fluid['chip-die'].source_terms.energy = q_vol
    """
    print(f"[wrapper] chip_power_watts={power_watts} W "
          "(mixing-elbow 阶段未施加, 仅记录到 result.params_echo)")


# ============================================================
# 下面这几个是从基线脚本搬过来的小工具函数, 几乎一模一样。
# 重复一份而不是 import, 是因为 wrapper 是"基础设施", 要能独立运行, 不耦合到外部脚本。
# ============================================================

def _read_case(session, case_path: str) -> None:
    """读 .cas / .cas.h5 案例文件。modern API 失败就 fallback TUI。"""
    try:
        session.file.read_case(file_name=case_path)
        return
    except (AttributeError, TypeError):
        pass
    # TUI 路径里反斜杠会被 Lisp 当转义符吃掉, 必须换成正斜杠
    session.tui.file.read_case(case_path.replace("\\", "/"))


def _check_mesh(session) -> None:
    """跑一次网格检查。Fluent 自检连通性、负体积单元等。"""
    try:
        session.mesh.check()# 调用 session.mesh.check() 执行网格检查
    except (AttributeError, TypeError):
        session.tui.mesh.check()


def _initialize(session) -> None:
    """hybrid 初始化: Fluent 自动猜一个合理初值, 比 standard 收敛快。"""
    try:
        session.solution.initialization.hybrid_initialize()
    except (AttributeError, TypeError):
        session.tui.solve.initialize.hyb_initialization()


def _iterate(session, n: int) -> None:
    """跑 n 步迭代。"""
    try:
        session.solution.run_calculation.iterate(iter_count=n)
    except (AttributeError, TypeError):
        session.tui.solve.iterate(n)


def _query_max_temperature(session) -> float: #让 Fluent 算出整个区域的最高温度 → 存进文件 → 代码读文件 → 把数字抠出来 → 返回给你
    """
    通过 TUI /report/volume-integrals/maximum 把流体域最高温度写到文件,
    再用正则抠出数字。TUI 路径稳定, modern API 太多变。
    """
    out_file = os.path.abspath(_TEMP_REPORT_FILE).replace("\\", "/")
    #把相对路径变成绝对路径, 避免 TUI 路径里反斜杠会被 Lisp 当转义符吃掉, 必须换成正斜杠、
    #A().B() 是 Python 的链式调用, 用于在一行里调用多个方法。先执行 A，再对 A 的结果执行 B
    if os.path.exists(out_file):
        os.remove(out_file)

    # mixing_elbow 案例 cell zone 名字在不同发布里有 elbow-fluid / fluid 两种
    # 顺次尝试, 谁能产生输出文件就用谁
    candidates = [
        ("elbow-fluid", "()"),
        ("fluid", "()"),
        ("()",),                # 全选所有 cell zones
    ]
    #准备要尝试的区域名称列表,因为不同版本 Fluent 里，流体区域名字不一样，所以要尝试多个名字
    last_err = None
    for cz in candidates:
        try:
            session.tui.report.volume_integrals.maximum(
                *cz,
                "temperature",
                "yes",          # 写到文件 yes/no
                out_file,
            )
            #对象.子对象.子子对象.方法()。链式对象调用
            if os.path.exists(out_file):
                break            # 成功生成文件就停下
        except Exception as e:
            last_err = e
            continue

    if not os.path.exists(out_file):
        raise RuntimeError(f"无法生成结果文件, 最后一次报错: {last_err}")

    with open(out_file, "r", encoding="utf-8", errors="ignore") as f:
        #    with 保证仿真会话用完自动退出、释放许可证，不会残留后台进程
        text = f.read()

    # 正则解释:
    #   -?                     可选负号
    #   \d+\.\d+               至少一位整数 + 小数点 + 至少一位小数
    #   (?:[eE][-+]?\d+)?      非捕获组, 可选的科学计数法后缀
    numbers = re.findall(r"-?\d+\.\d+(?:[eE][-+]?\d+)?", text)
    #re是一个模块，re 是 Python 的 标准库模块 ，用于处理正则表达式（一种用于匹配字符串模式的工具）
    #findall是在字符串中查找所有匹配正则表达式的子字符串，返回一个包含所有匹配项的列表。
    if not numbers:
        raise RuntimeError(f"未从输出解析出数值:\n{text}")
    return max(float(n) for n in numbers)


def _query_outlet_avg_temperature(session) -> float:  #找到outlet这个面上的最大的质量加权平均温度
    """
    用 TUI /report/surface-integrals/mass-weighted-avg 计算 outlet 边界上的
    质量加权平均温度。"质量加权"意思是: T_avg = ∫(ρ·v·T)dA / ∫(ρ·v)dA,
    比"面积加权"更能代表"流出去的水实际是几度", CFD 教材里讲混合就用这个量。

    返回: outlet 平均温度, 单位 K。
    """
    out_file = os.path.abspath("_wrapper_outlet_temp.txt").replace("\\", "/")
    if os.path.exists(out_file):
        os.remove(out_file)

    # mixing-elbow 案例的 outlet 边界叫 "outlet"。如果改了案例可能要扩这个列表。
    candidates = [
        ("outlet", "()"),
        ("()",),  # 全选所有 surface
    ]
    last_err = None
    for surf in candidates:
        try:
            session.tui.report.surface_integrals.mass_weighted_avg(
                *surf,
                "temperature",
                "yes",       # 是否写到文件
                out_file,
            )
            if os.path.exists(out_file):
                break
        except Exception as e:
            last_err = e
            continue

    if not os.path.exists(out_file):
        # outlet 平均温度失败时返回 NaN
        # 而不是抛错, 让主流程继续。result_analyzer 会更严格地处理。
        print(f"[wrapper][WARN] 出口平均温度查询失败, 返回 NaN: {last_err}",
              file=sys.stderr)
        return float("nan")

    with open(out_file, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()

    numbers = re.findall(r"-?\d+\.\d+(?:[eE][-+]?\d+)?", text)
    if not numbers:
        print(f"[wrapper][WARN] 出口平均温度文本解析不出数, 返回 NaN:\n{text}",
              file=sys.stderr)
        return float("nan")
    # mass-weighted-avg 报告通常只有一个数据行 + 一个汇总行, 取最大那个
    # (单一 surface 时两个数应该相等)。
    return max(float(n) for n in numbers)


# ============================================================
# 自测试: python -m tools.fluent_wrapper
# 跑两次仿真, 不同入口速度 + 不同迭代步, 验证"换参数 → 换结果"。
# 验收标准: 输出两个不同的 max_temp, 证明参数确实生效了。
# ============================================================

def _selftest() -> int:
    """
    自测试入口。返回 0 = PASS, 非 0 = FAIL, 给 sys.exit() 用。
    """
    cases = [
        # 慢风 + 少迭代: 期望温度比较接近 inlet 平均
        {"chip_power_watts": 10.0, "inlet_velocity_ms": 0.4, "max_iterations": 80},
        # 快风 + 多迭代: 混合更剧烈, 温度场会和上面不同
        {"chip_power_watts": 15.0, "inlet_velocity_ms": 1.5, "max_iterations": 120},
    ]

    print("=" * 60)
    print("自测试: 跑 2 次 mixing-elbow, 不同参数 → 期望不同 max_temp")
    print("=" * 60)

    results = []  # [(params, result_or_None, exception_or_None), ...]
    for i, params in enumerate(cases, 1):
        print(f"\n--- Run {i}/{len(cases)} ---")
        print(f"params = {params}")
        try:
            r = run_simulation(params)  #r是一个字典，包含max_temp_K、outlet_avg_temp_K、elapsed_seconds、velocity_applied
            print(f"[OK] result = {r}")
            results.append((params, r, None))
        except Exception as e:
            print(f"[FAIL] run #{i}: {type(e).__name__}: {e}")
            traceback.print_exc()
            results.append((params, None, e))

    # ----- 汇总打印 -----
    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    for i, (params, r, err) in enumerate(results, 1): 
        #err 是一个 异常对象变量，用于存储仿真异常退出的异常信息
        v = params["inlet_velocity_ms"]
        n = params["max_iterations"]
        if err is None:
            print(f"  Run {i} | v={v} m/s, iter={n} | "
                  f"max_T = {r['max_temp_K']:.2f} K  | "
                  f"outlet_avg_T = {r['outlet_avg_temp_K']:.2f} K | "
                  f"{r['elapsed_seconds']}s | "
                  f"velocity_applied={r['velocity_applied']}")
        else:
            print(f"  Run {i} | v={v} m/s, iter={n} | FAIL: {err}")

    # ----- 判定通过/失败 -----
    success = [r for _, r, e in results if e is None]
    if len(success) != len(results):
        print("\n[FAIL] 至少一次仿真异常退出, 自测不算通过。")
        return 6

    # 关键判定: 用 outlet 平均温度, 不用 max_temp (踩坑 #6: max_temp 是边界条件
    # 钉住的, 不会动)。outlet 平均温度反映冷/热混合比, 改 cold-inlet 速度就会变。
    outlet_temps = [round(r["outlet_avg_temp_K"], 2) for r in success]

    # NaN 不能比较, 先过滤掉
    finite = [t for t in outlet_temps if t == t]  # NaN != NaN，把所有失败的 NaN 都丢掉，只留下正常的温度数字。
    if len(finite) < len(outlet_temps):
        print(f"\n[WARN] outlet 平均温度查询失败 ({outlet_temps}) — "
              "无法判定参数是否生效。检查 _query_outlet_avg_temperature。")
        return 7

    if len(set(finite)) <= 1:
        print(f"\n[WARN] 两次 outlet_avg_T 几乎相同 ({finite}) — "
              "检查 _apply_inlet_velocity 是否真的改了 cold-inlet 边界条件。")
        return 5

    # 物理 sanity check: outlet 温度应在 cold-inlet (293.15 K) 与 hot-inlet
    # (313.15 K) 之间。超出说明仿真有问题(发散 / 边界条件搞反等)。
    out_of_range = [t for t in finite if not (290.0 <= t <= 315.0)]
    if out_of_range:
        print(f"\n[WARN] outlet_avg_T 超出物理合理范围 290~315 K: {out_of_range}")
        return 8

    print(f"\n[OK] 两次 outlet_avg_T 不同 ({finite} K), "
          "证明 cold-inlet 速度参数确实驱动了仿真。")
    print(f"     (max_temp 都是 {success[0]['max_temp_K']} K, 这是 hot-inlet 边界值, "
          "不变是物理上正确的——见踩坑 #6。)")
    print("PASSED — fluent_wrapper.run_simulation 可用。")
    return 0


if __name__ == "__main__":
    # __name__ == "__main__" 表示"被直接运行"而非被 import。
    # `python -m tools.fluent_wrapper` 会让 Python 把 tools 当 package 装好,
    # 再执行本文件的顶层代码 — 这里就走到 _selftest()。
    sys.exit(_selftest())
