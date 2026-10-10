#!/usr/bin/env python3
"""CoreGraphics 输入与屏幕几何 —— 纯 ctypes，不走 pyobjc、不走 pyautogui。

为什么自己写这一层（实测数字都在 README「轻量」一节）：

  - `import pyautogui` 会把 Quartz + AppKit + Foundation + objc + PIL + tkinter 全部
    拉进进程：常驻 RSS 从 55MB 涨到 101MB，而它真正做的事就是几个 CGEventPost；
  - 它自带的 `PAUSE = 0.25` 让**每一个**输入动作多等四分之一秒；
  - pyobjc 的 AppKit/Quartz 本身就有 33~47MB，鼠标键盘这条热路径没必要付出这个钱。

这里只依赖 CoreGraphics / ApplicationServices 的 C 符号（后者 ax_bridge 已经加载），
所以坐标路径（截屏几何 + 点击 + 打字 + 滚动 + 拖拽 + 前台判定）可以在完全不加载
pyobjc 的前提下跑完。

坐标约定：全部用「全局、左上角原点、点」，即 CGDisplayBounds / CGEventPost 的空间，
和 server.py 的 `_to_screen_point` 输出一致。
"""
from __future__ import annotations

import ctypes
import sys
import time

_HID_TAP = 0
_kCGMouseLeft = 0
_kCGMouseRight = 1
_kCGMouseOther = 2
# CGEventType：kCGEventNull=0, LeftMouseDown=1, LeftMouseUp=2, RightMouseDown=3,
# RightMouseUp=4, MouseMoved=5, LeftMouseDragged=6, OtherMouseDown=25, OtherMouseUp=26
_LEFT_DOWN, _LEFT_UP = 1, 2
_RIGHT_DOWN, _RIGHT_UP = 3, 4
_MOVED, _LEFT_DRAGGED = 5, 6
_OTHER_DOWN, _OTHER_UP = 25, 26
_KEY_DOWN, _KEY_UP = 10, 11
# CGScrollEventUnit：Pixel=0、Line=1、Page=2。写成 2 时 Chromium 还认（所以坐标路径
# 在 dsh/Electron 里看着正常），而原生 AppKit 的 NSScrollView 完全不理 —— 实测访达
# 滚 30 格图标纹丝不动。单位必须是 1。
_UNIT_LINE = 1


class _CGRect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double), ("w", ctypes.c_double), ("h", ctypes.c_double)]


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _PSN(ctypes.Structure):
    """ProcessSerialNumber —— GetFrontProcess 的输出，再用 GetProcessPID 换成 pid。"""

    _fields_ = [("hi", ctypes.c_uint32), ("lo", ctypes.c_uint32)]


class LazyLib:
    """第一次真的用到某个符号时才 dlopen 那个库。

    CoreGraphics / ApplicationServices 一映射就是十几 MB 常驻，而工具服务器是 dsh 每次
    会话都要拉起来的进程 —— 没人用电脑的时候，不该替鼠标键盘和 AX 预付这笔内存。
    代理只做一件事：首次属性访问时 CDLL + 绑好签名，其余调用原样转发。
    """

    def __init__(self, path, setup=None):
        self._path, self._setup, self._lib = path, setup, None

    def _load(self):
        if self._lib is None:
            lib = ctypes.CDLL(self._path)
            if self._setup:
                self._setup(lib)
            self._lib = lib
        return self._lib

    def __getattr__(self, name):
        return getattr(self._load(), name)


def _bind_cg(lib):
    lib.CGMainDisplayID.restype = ctypes.c_uint32
    lib.CGDisplayBounds.restype = _CGRect
    lib.CGDisplayBounds.argtypes = [ctypes.c_uint32]
    lib.CGGetActiveDisplayList.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
    lib.CGEventCreate.restype = ctypes.c_void_p
    lib.CGEventGetLocation.restype = _CGPoint
    lib.CGEventGetLocation.argtypes = [ctypes.c_void_p]
    lib.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    lib.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, _CGPoint, ctypes.c_int]
    lib.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
    lib.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_ushort, ctypes.c_bool]
    lib.CGEventCreateScrollWheelEvent.restype = ctypes.c_void_p
    lib.CGEventCreateScrollWheelEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_int]
    lib.CGEventKeyboardSetUnicodeString.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    lib.CGEventPost.argtypes = [ctypes.c_int, ctypes.c_void_p]


#: TransformProcessType 返回它 = paramErr，表示进程已经是 UIElement（重复补刀时的正常回包）
_PARAM_ERR = -50

_dock_hidden = False


def hide_from_dock(lib=None, force: bool = False) -> None:
    """把进程在 LaunchServices 里的身份转成 UIElement。

    python.org / Homebrew 的 framework 版解释器，主程序就在 `Resources/Python.app` 里面。
    进程一旦和窗口服务器接上，LaunchServices 就按这个 bundle 把进程 —— **以及它 spawn 的
    子进程**（osascript / screencapture / sips / lsappinfo…）—— 登记成一个应用，Dock 上于是
    多出一枚 Python 火箭，并且一直留到进程退出，dsh 每开一轮电脑操作就攒一枚。

    **注意（macOS 27.2 实测）**：运行时转 UIElement 只改得动 `lsappinfo` 里的 type，
    **拦不住 Dock 那颗图块**；真正管用的是 plist 级的 `LSUIElement` —— 见
    `server/ui_element_app.py`（启动最早期把进程搬进自建 bundle）。这条保留是因为：
    换不了 bundle 的环境（非 framework 解释器 / `DSH_CUA_NO_REEXEC=1`）里它仍是对的姿态
    （旧版 macOS 上确实能摘掉图块），而且它让 LS 记录里的 type 与实际相符 ——
    `scripts/mcp_wire_check.py` 里那条断言看的就是它。

    时机：加载 ApplicationServices 时转的这一次是「早鸟」，那时进程还没有 LS 身份，
    TransformProcessType 返回 0 却等于空转；AX 调用把进程登记成 org.python.python 时会把
    早鸟的结论一起覆盖，所以登记之后要 force=True 再补一次 —— 单次 3.3µs，比它要抢的
    GetFrontProcess（2.7µs）还便宜，成功转过之后回 -50，等于免费的空操作。

    用 Xcode 命令行工具那份解释器时根本没有 bundle 身份，这条会直接成功或无害地失败。
    """
    global _dock_hidden
    if _dock_hidden and not force:
        return
    _dock_hidden = True
    try:
        if lib is None:
            lib = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        rc = lib.TransformProcessType(ctypes.byref(_PSN(0, 2)), 4)  # kProcessTransformToUIElementApplication
        if rc not in (0, _PARAM_ERR):  # -50 = 早就是 UIElement 了（补刀时的正常回包，不是失败）
            print(f"computer-use: TransformProcessType 返回 {rc}，Dock 上会留一枚 Python 图章", file=sys.stderr)
    except Exception as e:  # 符号缺失也不能拖垮输入路径
        print(f"computer-use: 摘除 Dock 图章失败：{e}", file=sys.stderr)


def _bind_as(lib):
    hide_from_dock(lib)
    lib.GetFrontProcess.argtypes = [ctypes.c_void_p]
    lib.GetProcessPID.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    lib.TransformProcessType.argtypes = [ctypes.c_void_p, ctypes.c_uint32]


_CG = LazyLib("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics", _bind_cg)
_AS = LazyLib("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices", _bind_as)

#: 修饰键与常见键的真实键码（组合键主路径仍走 osascript，这里只兜底与长按用）。
KEYCODES = {
    "return": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51, "escape": 53, "esc": 53,
    "forwarddelete": 117, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    "command": 55, "cmd": 55, "shift": 56, "option": 58, "alt": 58, "control": 59, "ctrl": 59,
}
MODIFIER_KEYS = ("command", "cmd", "shift", "option", "alt", "control", "ctrl")


class FailSafe(Exception):
    """光标在左上角时拒绝注入 —— 等价于原来 pyautogui.FAILSAFE 的急停开关。"""


def _post(event: int | None) -> None:
    if event:
        _CG.CGEventPost(_HID_TAP, event)


def _code(name: str) -> int:
    return KEYCODES.get(str(name).lower(), 0)


def displays() -> list[dict]:
    """所有活动显示器：全局左上角（点）+ 尺寸（点）+ 像素/点倍率，index 从 1 开始。

    CGDisplayBounds 本身就是「左上角原点、y 向下」，不需要像 NSScreen 那样再翻一次 y。
    """
    n = ctypes.c_uint32()
    _CG.CGGetActiveDisplayList(0, None, ctypes.byref(n))
    ids = (ctypes.c_uint32 * max(1, n.value))()
    _CG.CGGetActiveDisplayList(n.value, ids, ctypes.byref(n))
    out = []
    for i in range(n.value):
        b = _CG.CGDisplayBounds(ids[i])
        out.append(
            {
                "index": i + 1,
                "topleft": (round(b.x), round(b.y)),
                "size_pt": (round(b.w), round(b.h)),
            }
        )
    return out


def main_size() -> tuple[int, int]:
    """主显示器逻辑尺寸（点）—— 原来用 pyautogui.size() 的地方。"""
    b = _CG.CGDisplayBounds(_CG.CGMainDisplayID())
    return round(b.w), round(b.h)


def cursor_position() -> tuple[float, float]:
    p = _CG.CGEventGetLocation(_CG.CGEventCreate(None))
    return p.x, p.y


def _fail_safe(x: float, y: float) -> None:
    if x <= 0 and y <= 0:
        raise FailSafe("光标在屏幕左上角，已按 FAILSAFE 中止本次注入（把鼠标移开再继续）")


def move(x: float, y: float, duration: float = 0.0) -> None:
    _fail_safe(x, y)
    steps = max(1, int(duration * 60))
    sx, sy = cursor_position()
    for i in range(1, steps + 1):
        t = i / steps
        _post(_CG.CGEventCreateMouseEvent(None, _MOVED, _CGPoint(sx + (x - sx) * t, sy + (y - sy) * t), _kCGMouseLeft))
    _post(_CG.CGEventCreateMouseEvent(None, _MOVED, _CGPoint(x, y), _kCGMouseLeft))


def click(x: float, y: float, button: str = "left", double: bool = False) -> None:
    _fail_safe(x, y)
    move(x, y)
    down, up = {"left": (_LEFT_DOWN, _LEFT_UP), "right": (_RIGHT_DOWN, _RIGHT_UP)}.get(
        str(button).lower(), (_OTHER_DOWN, _OTHER_UP))
    for _ in range(2 if double else 1):
        _post(_CG.CGEventCreateMouseEvent(None, down, _CGPoint(x, y), _kCGMouseLeft))
        _post(_CG.CGEventCreateMouseEvent(None, up, _CGPoint(x, y), _kCGMouseLeft))


def drag(fx: float, fy: float, tx: float, ty: float, duration: float) -> None:
    """按住拖动：必须显式发 kCGEventLeftMouseDragged。

    pyautogui 的 drag 在按住状态下发的仍是 mouseMoved，AppKit / Electron 都不把它
    当作拖拽 —— 目标只收到 mouseDown 与 mouseUp，窗口/滑块纹丝不动（实测如此）。
    """
    _fail_safe(fx, fy)
    move(fx, fy)
    steps = max(10, min(60, int(duration * 60)))
    gap = max(0.004, duration / steps)
    _post(_CG.CGEventCreateMouseEvent(None, _LEFT_DOWN, _CGPoint(fx, fy), _kCGMouseLeft))
    time.sleep(0.06)
    for i in range(1, steps + 1):
        t = i / steps
        _post(_CG.CGEventCreateMouseEvent(None, _LEFT_DRAGGED, _CGPoint(fx + (tx - fx) * t, fy + (ty - fy) * t), _kCGMouseLeft))
        time.sleep(gap)
    time.sleep(0.06)
    _post(_CG.CGEventCreateMouseEvent(None, _LEFT_UP, _CGPoint(tx, ty), _kCGMouseLeft))


def scroll(x: float, y: float, lines: int) -> None:
    """滚轮：lines>0 向上，<0 向下（与原来 pyautogui.scroll 的符号约定一致）。"""
    move(x, y)
    _post(_CG.CGEventCreateScrollWheelEvent(None, _UNIT_LINE, 1, int(lines)))


def key_down(name: str) -> None:
    _post(_CG.CGEventCreateKeyboardEvent(None, _code(name), True))


def key_up(name: str) -> None:
    _post(_CG.CGEventCreateKeyboardEvent(None, _code(name), False))


def press(name: str) -> None:
    """单键。有真实键码的用键码；字母/数字这类没有键码表的用 Unicode 注入
    （CGEventKeyboardSetUnicodeString 直接把字符塞进事件，绕开键位布局表）。"""
    key = str(name).lower()
    if key in KEYCODES:
        _post(_CG.CGEventCreateKeyboardEvent(None, KEYCODES[key], True))
        _post(_CG.CGEventCreateKeyboardEvent(None, KEYCODES[key], False))
        return
    for down in (True, False):
        ev = _CG.CGEventCreateKeyboardEvent(None, 0, down)
        text = str(name)
        buf = ctypes.create_unicode_buffer(text)
        _CG.CGEventKeyboardSetUnicodeString(ev, len(text), buf)
        _post(ev)


def hold(name: str, seconds: float) -> None:
    """长按一个键。"""
    key_down(name)
    time.sleep(max(0.0, min(float(seconds), 5.0)))
    key_up(name)


def release_modifiers() -> None:
    """强制松开全部修饰键：组合键漏发 keyUp 会粘连（cmd+v 被当成 ctrl+cmd+v）。"""
    for k in ("command", "shift", "option", "control"):
        key_up(k)


def hotkey(parts: list[str]) -> None:
    """组合键兜底路径（主路径是 osascript → System Events）。"""
    for p in parts:
        key_down(p)
    press(parts[-1])
    for p in reversed(parts[:-1]):
        key_up(p)
    release_modifiers()


def screen_recording_ok() -> bool | None:
    """屏幕录制权限是否授予宿主（CGPreflightScreenCaptureAccess，macOS 10.15+）。
    旧系统没有这个符号 → 返回 None 表示未知。"""
    try:
        fn = _CG.CGPreflightScreenCaptureAccess
    except AttributeError:
        return None
    fn.restype = ctypes.c_bool
    return bool(fn())


def frontmost_pid() -> int | None:
    """当前前台应用的 pid —— Process Manager 的两个 C 调用，不必加载 NSWorkspace。

    这两个调用会把本进程接上窗口服务器（framework 解释器在这一刻被 LaunchServices 认成
    Python.app）。在换不了 UIElement bundle 的环境里，这是最后一道能补身份的机会，
    所以抢在它前面补一次（见 hide_from_dock；真正摘图块的是 ui_element_app 的自建 bundle）。
    """
    hide_from_dock(force=True)
    psn = _PSN()
    if _AS.GetFrontProcess(ctypes.byref(psn)) != 0:
        return None
    pid = ctypes.c_int()
    if _AS.GetProcessPID(ctypes.byref(psn), ctypes.byref(pid)) != 0:
        return None
    return int(pid.value) or None
