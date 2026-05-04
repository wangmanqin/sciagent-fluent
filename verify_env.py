"""
verify_env.py — PyFluent 环境验证
目标: 确认 PyFluent 能启动本地 Ansys Fluent (无界面模式)
运行: python verify_env.py
成功标志: 看到 3 个 [OK]
"""
# ↑ 文件最上面用三个双引号 """...""" 包起来的叫"模块文档字符串"(docstring)
# 它不是注释(#)，而是"文档"——别的代码可以读取它。作用相当于这个文件的说明书。


import sys          # sys 是 Python 自带的"系统"模块，后面用 sys.exit() 来带着"退出码"结束程序
import traceback    # traceback 也是自带模块，用来把报错的完整调用栈打印出来（排查 bug 必备）
# "import X" 的意思是"把 X 这个工具箱拿进来"。没 import 就不能用里面的函数。


def main() -> int:
    # def 表示"定义一个函数"，函数名叫 main，括号里 () 是参数列表（这里没参数）
    # -> int 是"类型标注"：告诉别人这个函数会返回一个整数(int)。只是给人看的提示，Python 不会强制检查
    # 冒号 : 后面缩进的所有代码都属于这个函数的"身体"
    # Python 用缩进(4 个空格)来表示代码块，不像 C/Java 用大括号 {}

    print("=" * 50)
    # print() 是"打印到屏幕"的函数
    # "=" * 50 是字符串乘法：把 "=" 这个字符重复 50 次，得到 "=================================================="
    # 用来画一条分隔线，让输出好看一点

    print("PyFluent Environment Verification")
    print("=" * 50)
    # 再画一条线。上下两条线中间夹一行标题，是打印日志的常见排版

    # Step 1: import
    # ↑ 开头是 # 的是"单行注释"，Python 会忽略它，只给人看
    try:
        # try: 的意思是"试着执行下面的代码，如果出错别让程序崩溃，跳到 except 那里去处理"
        # 这是 Python 的"异常处理"机制，专门用来防止小错误炸掉整个程序

        import ansys.fluent.core as pyfluent
        # 把 ansys.fluent.core 这个包导入进来，并起个短名字叫 pyfluent（因为原名太长）
        # 之所以放在 try 里面而不是文件顶部，是因为"如果这个包没装，我们想给出友好的提示"
        # 如果放文件顶部，包没装的话程序一开头就崩了，用户看到一堆天书

        print("[OK] ansys-fluent-core imported")
        # 到这一步说明 import 成功，打 [OK] 让人安心

        print(f"     version: {pyfluent.__version__}")
        # f"..." 叫 "f-string"，里面 {} 里的东西会被当成代码执行，结果拼进字符串
        # pyfluent.__version__ 是这个包自带的"版本号"变量（前后两个下划线是 Python 约定的"内部属性"）
        # 作用：把装了哪个版本打出来，方便以后对照文档

    except ImportError as e:
        # 如果 try 里面抛出的错误类型是 ImportError（导入失败），就跑到这里
        # "as e" 把这个错误对象赋给变量 e，后面可以读它的信息

        print(f"[FAIL] cannot import ansys.fluent.core: {e}")
        # 打 [FAIL] + 错误信息，告诉用户哪一步失败了

        print("       fix: pip install -r requirements.txt")
        # 直接把"修复建议"打出来，用户不用自己查怎么办

        return 1
        # 函数到这里结束，返回 1
        # 约定：0 = 成功；非 0 = 失败。不同的失败用不同的数字区分
        # 这里返回 1 代表"第 1 步失败"

    # Step 2: launch Fluent (headless)
    session = None
    # 先声明一个变量 session，初始值是 None（Python 里表示"空/没东西"）
    # 放在 try 外面，是为了让它在整个 main 函数里都可见
    # 如果只在 try 里面定义，try 出错时这个变量就不存在了，后面想引用会报 NameError

    try:
        print("\nLaunching Fluent (this takes ~30-60 seconds)...")
        # \n 是"换行符"，让这行前面先空一行，视觉上和上一段隔开
        # 提示用户耐心等——Fluent 启动本来就慢，没这句用户会以为卡死了

        session = pyfluent.launch_fluent(
            mode="solver",         # 启动"求解器"模块（Fluent 还有前处理/后处理，这里只要解题的那部分）
            ui_mode="no_gui",      # 无界面模式：不弹窗、不显示 GUI，适合被脚本/Agent 自动调用
            dimension=3,           # 3D 仿真（对应的是 2D）
            precision="double",    # 双精度浮点，工业仿真标配（另一个选项是 single 单精度，省内存但精度低）
            processor_count=2,     # 用 2 个 CPU 核心并行计算
        )
        # ↑ 这是一个函数调用，但参数多，所以分成多行写。每个参数后面用逗号隔开，最后一个逗号可留可不留
        # launch_fluent(...) 返回一个"会话对象"，赋值给 session，后面用 session 来和 Fluent 通信
#session 是 pyfluent.launch_fluent() 函数的返回值，作为 Python 与 Fluent 之间的通信桥梁，提供了控制 Fluent 执行、发送命令和获取结果的方法。
        print("[OK] Fluent launched successfully")
        # 能走到这就说明 Fluent 真的起来了，打 [OK]

        try:
            # try 可以嵌套。这层 try 是为了"额外查询一下运行时信息，查不到也不算大事"
            info = session.scheme_eval.scheme_eval('(inquire-system-info)')
            # session.scheme_eval 是 PyFluent 提供的接口，可以给 Fluent 发一条 Scheme 语言指令
            # Scheme 是 Fluent 内部用的脚本语言（Lisp 方言），(inquire-system-info) 是一条查询系统信息的命令
            # 相当于对 Fluent 说："告诉我你的运行环境"
            print(f"     runtime info: {info}")
        except Exception:
            # Exception 是"所有错误的总父类"，等于"不管什么错都抓"
            # 这里不关心错误具体是什么，查不到就跳过
            print("     (skip runtime info query)")

    except Exception as e:
        # 外层的 except：Step 2（启动 Fluent）出任何错就进这里
        # 用 Exception 而不是具体错误类型，因为 launch_fluent 能抛的错太多种，不好一一枚举

        print(f"[FAIL] launch_fluent raised: {type(e).__name__}: {e}")
        # type(e).__name__ 是"这个错误对象的类名"，比如 FileNotFoundError、RuntimeError
        # {e} 是错误的具体描述信息
        # 合起来：告诉用户"什么类型的错 + 错在哪"

        print("\n--- full traceback ---")
        traceback.print_exc()
        # 打印完整的调用栈（traceback）—— 就是"从哪里调到哪里最后在哪一行炸了"的完整路径
        # 排查问题必看。开头 import 的 traceback 模块就是为了这一行

        print("\nCommon causes:")
        print(" 1. License not available. Check ANSYSLMD_LICENSE_FILE env var.")
        print(" 2. Fluent path not found. Set AWP_ROOT241 (or your version) env var.")
        print(" 3. Version too old. PyFluent needs Fluent >= 2023R2.")
        # 列出三个最常见的失败原因，让用户不用 Google 就能定位
        # 这是"防御性编程"的体现：预判用户会掉哪个坑，提前把答案准备好

        return 2
        # 返回 2 代表"第 2 步失败"（和 Step 1 的返回 1 区分开，方便脚本调用方判断）

    # Step 3: clean exit
    try:
        session.exit()
        # 告诉 Fluent："我用完了，你可以关掉了"
        # 必须主动 exit，否则 Fluent 进程会留在后台占内存、占 License（License 有并发上限）

        print("[OK] Exited cleanly")
    except Exception as e:
        # 退出失败也不算真的失败——Fluent 已经用完了，要关的是清理工作
        print(f"[WARN] exit raised (non-fatal): {e}")
        # 所以打 [WARN] 而不是 [FAIL]，意思是"有警告但不影响结果"

    print("\n" + "=" * 50)
    # "\n" + "=" * 50 是字符串拼接：先一个换行，再 50 个等号
    # + 在字符串之间表示"连接"

    print("验证通过, 环境就绪.")
    print("=" * 50)

    return 0
    # 全程无异常，返回 0 = 成功。shell 脚本判断"上一条命令成功了吗"就是看这个退出码


if __name__ == "__main__":
    # 这是 Python 的一个经典习惯用法，读作 "if name main"
    # 含义：只有当这个文件被"直接运行"时（python verify_env.py），下面的代码才会执行
    # 如果这个文件被别的文件 import 了，下面的代码不会自动跑
    # 作用：让这个文件既能当脚本跑，又能被别人当工具箱导入，互不干扰

    sys.exit(main())
    # 先调用 main() 函数，拿到它返回的数字（0/1/2）
    # 再用 sys.exit(那个数字) 带着这个退出码退出 Python 进程
    # 这样外部脚本/CI 系统就能通过退出码判断这次跑成功没
    #sys 模块 ：包含与 Python 解释器和运行环境相关的变量和函数，
    #但不保证能关掉后台的 Fluent 进程
