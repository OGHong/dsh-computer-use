# -*- coding: utf-8 -*-
"""
ax_bridge —— macOS 无障碍（Accessibility）桥接层。

用 ctypes 直调 ApplicationServices/HIServices 里的 C 函数（当前 pyobjc 版本
没有导出这些符号），提供 ZCode 风格的能力：

  - 运行中 GUI 应用枚举（NSWorkspace）
  - 语义元素树观察：稳定索引 + 结点 id，支持 diff（只回新增/变化的行）
  - 元素级操作：AXPress 语义点击、AXRaise、AXValue 写入、AXSelectedTextRange
  - 后台应用可读可操作（不需要把窗口带到前台）
  - Electron/Chromium 应用强制开启 AX（AXManualAccessibility）
  - 动作效果证据（操作前后树哈希对比）
  - 类型化错误码（PERMISSION_DENIED / STALE_STATE / ELEMENT_UNAVAILABLE / …）

所有 CF 对象按 Copy/Create 规则手工管理引用计数。
"""
from __future__ import annotations

import ctypes
import os
import re
import subprocess
import time
import types
import hashlib
import threading

import cg_input  # noqa: F401

from ax_error import AXBridgeError  # 只有一份定义：server.py catch 的必须是同一个类

# ---------------------------------------------------------------------------
# 底层绑定
# ---------------------------------------------------------------------------

# CoreFoundation / ApplicationServices 都延迟到第一次真的用它才 dlopen：这两个一进来
# 就是十几 MB 常驻，而工具服务器是 dsh 每次会话都拉起的（见 cg_input.LazyLib）。
from cg_input import LazyLib  # noqa: E402

_c = ctypes.c_void_p
_UTF8 = 0x08000100


def _bind(obj, name, restype, argtypes):
    fn = getattr(obj, name)
    fn.restype = restype
    fn.argtypes = argtypes
    return fn


def _bind_cf(lib):
    _bind(lib, "CFStringCreateWithCString", _c, [_c, ctypes.c_char_p, ctypes.c_uint32])
    _bind(lib, "CFStringGetLength", ctypes.c_long, [_c])
    _bind(lib, "CFStringGetCString", ctypes.c_bool, [_c, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32])
    _bind(lib, "CFRelease", None, [_c])
    _bind(lib, "CFRetain", _c, [_c])
    _bind(lib, "CFArrayGetCount", ctypes.c_long, [_c])
    _bind(lib, "CFArrayGetValueAtIndex", _c, [_c, ctypes.c_long])
    _bind(lib, "CFDictionaryGetValue", _c, [_c, _c])
    _bind(lib, "CFDictionaryGetCount", ctypes.c_long, [_c])
    _bind(lib, "CFDictionaryGetKeysAndValues", None, [_c, _c, _c])
    _bind(lib, "CFGetTypeID", ctypes.c_long, [_c])
    _bind(lib, "CFNumberGetValue", ctypes.c_bool, [_c, ctypes.c_long, _c])
    _bind(lib, "CFBooleanGetValue", ctypes.c_bool, [_c])


_kCF = LazyLib("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation", _bind_cf)

_cf_const = None


def T():
    """CFTypeID / kCFBoolean 这些只能问运行时 —— 所以要等库真的被用到才取一次。"""
    global _cf_const
    if _cf_const is None:
        lib = _kCF._load()
        alib = _AS._load()
        _cf_const = types.SimpleNamespace(
            STRING=lib.CFStringGetTypeID(),
            NUMBER=lib.CFNumberGetTypeID(),
            BOOL=lib.CFBooleanGetTypeID(),
            ARRAY=lib.CFArrayGetTypeID(),
            DICT=lib.CFDictionaryGetTypeID(),
            AX_ELEMENT=alib.AXUIElementGetTypeID(),
            AX_VALUE=alib.AXValueGetTypeID(),
            TRUE=ctypes.c_void_p.in_dll(lib, "kCFBooleanTrue").value,
            FALSE=ctypes.c_void_p.in_dll(lib, "kCFBooleanFalse").value,
        )
    return _cf_const


def _bind_as(lib):
    # 登记成前台应用、在 Dock 上留火箭的就是第一次 AX 调用，所以抢在它前面摘掉
    cg_input.hide_from_dock(lib)
    global HAS_MSG_TIMEOUT
    _bind(lib, "AXUIElementCreateSystemWide", _c, [])
    _bind(lib, "AXUIElementCreateApplication", _c, [ctypes.c_int])
    _bind(lib, "AXUIElementCopyAttributeValue", ctypes.c_int, [_c, _c, ctypes.POINTER(_c)])
    _bind(lib, "AXUIElementCopyAttributeNames", ctypes.c_int, [_c, ctypes.POINTER(_c)])
    _bind(lib, "AXUIElementCopyActionNames", ctypes.c_int, [_c, ctypes.POINTER(_c)])
    _bind(lib, "AXUIElementPerformAction", ctypes.c_int, [_c, _c])
    _bind(lib, "AXUIElementSetAttributeValue", ctypes.c_int, [_c, _c, _c])
    _bind(lib, "AXUIElementGetPid", ctypes.c_int, [_c, ctypes.POINTER(ctypes.c_int)])
    _bind(lib, "AXUIElementCopyElementAtPosition", ctypes.c_int, [_c, ctypes.c_double, ctypes.c_double, ctypes.POINTER(_c)])
    _bind(lib, "AXValueGetType", ctypes.c_long, [_c])
    _bind(lib, "AXValueGetValue", ctypes.c_bool, [_c, ctypes.c_long, _c])
    _bind(lib, "AXValueCreate", _c, [ctypes.c_long, _c])
    _bind(lib, "AXIsProcessTrusted", ctypes.c_bool, [])
    try:
        _bind(lib, "AXUIElementSetMessagingTimeout", ctypes.c_int, [_c, ctypes.c_float])
        HAS_MSG_TIMEOUT = True
    except AttributeError:
        HAS_MSG_TIMEOUT = False


HAS_MSG_TIMEOUT = False
_AS = LazyLib("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices", _bind_as)


K_POINT, K_SIZE, K_RANGE = 1, 2, 4


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _Size(ctypes.Structure):
    _fields_ = [("w", ctypes.c_double), ("h", ctypes.c_double)]


class _CFRange(ctypes.Structure):
    _fields_ = [("loc", ctypes.c_long), ("len", ctypes.c_long)]


def _timeout(p: int) -> int:
    """给刚建出来的 AX 引用设 2 秒消息超时。

    没设过超时的时候，一个卡死/繁忙的 App 会让 AXUIElementCopyAttributeValue 一直挂着，
    表现为整次工具调用无响应；设了上限就变成可读的错误码。
    """
    if p and HAS_MSG_TIMEOUT:
        try:
            _AS.AXUIElementSetMessagingTimeout(p, ctypes.c_float(2.0))
        except Exception:
            pass
    return p


def ax_trusted() -> bool | None:
    """辅助功能权限是否已授予宿主进程（None = 查询失败）。

    顺带补一次身份转换：这是每次 AX 操作的第一口（`observe()` 开头就是它），也正是 framework
    解释器被 LaunchServices 登记成 Python.app 的地方 —— 登记会覆盖掉 dlopen 时的「早鸟」结论。
    它在换不了 UIElement bundle 的环境里是保底手段（真正摘图块的是 ui_element_app 的自建
    bundle），详见 cg_input.hide_from_dock。
    """
    try:
        ok = bool(_AS.AXIsProcessTrusted())
    except Exception:
        return None
    cg_input.hide_from_dock(force=True)
    return ok


# ---------------------------------------------------------------------------
# CF 工具
# ---------------------------------------------------------------------------

def _cfstr(s: str) -> int:
    return _kCF.CFStringCreateWithCString(None, s.encode("utf-8"), _UTF8)


def _read_cfstr(p: int) -> str:
    n = _kCF.CFStringGetLength(p)
    buf = ctypes.create_string_buffer(max(8, n * 4 + 1))
    _kCF.CFStringGetCString(p, buf, len(buf), _UTF8)
    return buf.value.decode("utf-8", "replace")


def _cf_to_py(p: int, max_str: int = 160):
    """把一个「我们拥有」的 CF 对象转成 python 值；负责 CFRelease。"""
    t = T()
    try:
        tid = _kCF.CFGetTypeID(p)
        if tid == t.STRING:
            return _read_cfstr(p)[:max_str]
        if tid == t.BOOL:
            return bool(_kCF.CFBooleanGetValue(p))
        if tid == t.NUMBER:
            out = ctypes.c_double()
            _kCF.CFNumberGetValue(p, 6, ctypes.byref(out))  # float64
            v = out.value
            return int(v) if float(v).is_integer() and abs(v) < 2**53 else v
        if tid == t.DICT:
            n = _kCF.CFDictionaryGetCount(p)
            if n > 64:
                return f"<dict:{n}>"
            keys = (_c * n)()
            vals = (_c * n)()
            _kCF.CFDictionaryGetKeysAndValues(p, keys, vals)
            out = {}
            for i in range(n):
                k, v = _cf_copy(keys[i]), _cf_copy(vals[i])
                if isinstance(k, str):
                    out[k] = v
            return out
        if tid == t.ARRAY:
            n = _kCF.CFArrayGetCount(p)
            # CFArrayGetValueAtIndex 给的是**借用**引用，而 _cf_to_py 结尾会 CFRelease ——
            # 直接传进去等于给数组元素多减一次计数，实测这样能把进程打到 SIGSEGV。
            return [_cf_copy(_kCF.CFArrayGetValueAtIndex(p, i)) for i in range(min(n, 8))]
        if tid == t.AX_ELEMENT:
            return "<ax-element>"
        if tid == t.AX_VALUE:
            vt = _AS.AXValueGetType(p)
            if vt == K_POINT:
                pt = _Point()
                _AS.AXValueGetValue(p, vt, ctypes.byref(pt))
                return (round(pt.x), round(pt.y))
            if vt == K_SIZE:
                sz = _Size()
                _AS.AXValueGetValue(p, vt, ctypes.byref(sz))
                return (round(sz.w), round(sz.h))
            if vt == K_RANGE:
                rg = _CFRange()
                _AS.AXValueGetValue(p, vt, ctypes.byref(rg))
                return (int(rg.loc), int(rg.len))
            return "<ax-value>"
        return f"<cf-{tid}>"
    finally:
        _kCF.CFRelease(p)


class Element:
    """持有引用计数的 AXUIElementRef。p == 0 视为无效。"""

    __slots__ = ("p",)

    def __init__(self, p: int, retain: bool = True):
        if retain and p:
            _kCF.CFRetain(p)
        self.p = p

    @classmethod
    def for_pid(cls, pid: int) -> "Element":
        p = _timeout(_AS.AXUIElementCreateApplication(int(pid)))
        if not p:
            raise AXBridgeError("APP_NOT_FOUND", f"pid {pid} 无法创建 AX 元素（应用可能已退出）")
        return cls(p, retain=False)

    @classmethod
    def systemwide(cls) -> "Element":
        return cls(_timeout(_AS.AXUIElementCreateSystemWide()), retain=False)

    def dispose(self):
        if self.p:
            _kCF.CFRelease(self.p)
            self.p = 0

    def get(self, name: str, max_str: int = 160):
        """读属性，恰好一次 IPC。返回 (True, 值) 或 (False, 错误码)。"""
        a = _cfstr(name)
        out = _c()
        try:
            err = _AS.AXUIElementCopyAttributeValue(self.p, a, ctypes.byref(out))
        finally:
            _kCF.CFRelease(a)
        if err != 0 or not out:
            return False, err
        return True, _cf_to_py(out, max_str)

    def attr_names(self) -> list[str]:
        out = _c()
        err = _AS.AXUIElementCopyAttributeNames(self.p, ctypes.byref(out))
        if err != 0 or not out:
            return []
        return _cf_array_strings(out)

    def action_names(self) -> list[str]:
        out = _c()
        err = _AS.AXUIElementCopyActionNames(self.p, ctypes.byref(out))
        if err != 0 or not out:
            return []
        return _cf_array_strings(out)

    def pid(self) -> int:
        pid = ctypes.c_int()
        _AS.AXUIElementGetPid(self.p, ctypes.byref(pid))
        return pid.value

    def perform(self, action: str) -> int:
        a = _cfstr(action)
        try:
            return _AS.AXUIElementPerformAction(self.p, a)
        finally:
            _kCF.CFRelease(a)

    def set_attr(self, name: str, value) -> int:
        a = _cfstr(name)
        owned: list[int] = []
        try:
            if isinstance(value, bool):
                v = T().TRUE if value else T().FALSE
            elif isinstance(value, str):
                v = _cfstr(value)
                owned.append(v)
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                num = ctypes.c_double(float(value))
                v = _kCF.CFNumberCreate(None, 6, ctypes.byref(num))
                owned.append(v)
            else:
                return 2  # invalid argument
            return _AS.AXUIElementSetAttributeValue(self.p, a, v)
        finally:
            for p in owned:
                _kCF.CFRelease(p)
            _kCF.CFRelease(a)

    def set_selected_range(self, loc: int, length: int) -> int:
        rng = _CFRange(int(loc), int(length))
        v = _AS.AXValueCreate(K_RANGE, ctypes.byref(rng))
        if not v:
            return 1
        a = _cfstr("AXSelectedTextRange")
        try:
            return _AS.AXUIElementSetAttributeValue(self.p, a, v)
        finally:
            _kCF.CFRelease(a)
            _kCF.CFRelease(v)

    def parent(self) -> "Element | None":
        """AXParent（Copy 规则：引用归调用方，用完要 dispose）。

        `strategy=a11y` 的坐标点击要从命中的元素往上找「可按的那一层」——`_cf_to_py`
        把 AX 元素读成字符串 `<ax-element>`，所以必须在这里开一个真正返回引用的口子。
        """
        a = _cfstr("AXParent")
        out = _c()
        try:
            err = _AS.AXUIElementCopyAttributeValue(self.p, a, ctypes.byref(out))
        finally:
            _kCF.CFRelease(a)
        if err != 0 or not out:
            return None
        return Element(out, retain=False)

    def children(self) -> list["Element"]:
        """AXChildren（每个子元素都已 retain，调用方负责 dispose）。"""
        a = _cfstr("AXChildren")
        out = _c()
        try:
            err = _AS.AXUIElementCopyAttributeValue(self.p, a, ctypes.byref(out))
        finally:
            _kCF.CFRelease(a)
        if err != 0 or not out:
            return []
        try:
            n = _kCF.CFArrayGetCount(out)
            kids = []
            for i in range(min(n, 400)):
                q = _kCF.CFArrayGetValueAtIndex(out, i)
                if q:
                    kids.append(Element(q, retain=True))
            return kids
        finally:
            _kCF.CFRelease(out)


def _cf_copy(p: int):
    """把「借用」的 CF 引用转成 python 值（_cf_to_py 会 release，所以先补一次 retain）。"""
    if not p:
        return None
    _kCF.CFRetain(p)
    return _cf_to_py(p)

def _cf_array_strings(out: int) -> list[str]:
    try:
        n = _kCF.CFArrayGetCount(out)
        names = []
        for i in range(n):
            q = _kCF.CFArrayGetValueAtIndex(out, i)
            if q:
                _kCF.CFRetain(q)
                names.append(_read_cfstr(q))
                _kCF.CFRelease(q)
        return names
    finally:
        _kCF.CFRelease(out)


def _windows_of(app_el: Element) -> list[tuple[Element, dict]]:
    a = _cfstr("AXWindows")
    out = _c()
    try:
        err = _AS.AXUIElementCopyAttributeValue(app_el.p, a, ctypes.byref(out))
    finally:
        _kCF.CFRelease(a)
    if err != 0 or not out:
        return []
    wins = []
    try:
        n = _kCF.CFArrayGetCount(out)
        for i in range(min(n, 12)):
            q = _kCF.CFArrayGetValueAtIndex(out, i)
            if not q:
                continue
            w = Element(q, retain=True)
            title = w.get("AXTitle")[1] if w.get("AXTitle")[0] else ""
            main = w.get("AXMain")
            pos = w.get("AXPosition")[1] if w.get("AXPosition")[0] else None
            size = w.get("AXSize")[1] if w.get("AXSize")[0] else None
            meta = {
                "index": i,
                "title": str(title) if title else "(无标题)",
                "main": bool(main[1]) if main[0] and main[1] is not None else None,
                "pos": pos if isinstance(pos, tuple) else None,
                "size": size if isinstance(size, tuple) else None,
            }
            wins.append((w, meta))
        return wins
    finally:
        _kCF.CFRelease(out)


def windows_of(app_el: Element) -> list[dict]:
    """应用的窗口元数据列表。"""
    pairs = _windows_of(app_el)
    metas = [m for _, m in pairs]
    for w, _ in pairs:
        w.dispose()
    return metas


def window_element(app_el: Element, index: int) -> Element | None:
    """按索引取一个窗口元素（调用方负责 dispose）。"""
    a = _cfstr("AXWindows")
    out = _c()
    try:
        err = _AS.AXUIElementCopyAttributeValue(app_el.p, a, ctypes.byref(out))
    finally:
        _kCF.CFRelease(a)
    if err != 0 or not out:
        return None
    try:
        n = _kCF.CFArrayGetCount(out)
        if index < 0 or index >= n:
            return None
        q = _kCF.CFArrayGetValueAtIndex(out, index)
        return Element(q, retain=True) if q else None
    finally:
        _kCF.CFRelease(out)


def element_at_position(x: float, y: float) -> Element | None:
    """系统级命中测试：全局点 (x,y) 上是哪个 AX 元素（调用方负责 dispose）。

    `strategy=a11y` 的坐标点击靠它把「一个点」变成「一个元素」——命中处没有语义动作就
    报 `ACTION_UNAVAILABLE`，而不是悄悄退回合成点击（§3.3：不静默降级、不污染用户鼠标）。
    坐标空间与 CGWindowBounds / AXPosition 一致：全局、左上角原点、点。
    """
    sw = Element.systemwide()
    out = _c()
    try:
        err = _AS.AXUIElementCopyElementAtPosition(
            sw.p, ctypes.c_double(float(x)), ctypes.c_double(float(y)), ctypes.byref(out)
        )
    finally:
        sw.dispose()
    if err != 0 or not out:
        return None
    return Element(out, retain=False)   # Copy 规则：这个引用归我们


# ---------------------------------------------------------------------------
# 运行中的应用
# ---------------------------------------------------------------------------


def _bind_lp(lib):
    _bind(lib, "proc_pidpath", ctypes.c_int, [ctypes.c_int, _c, ctypes.c_uint32])


_LP = LazyLib("/usr/lib/libproc.dylib", _bind_lp)
_bundle_cache: dict[int, tuple[str, str]] = {}


def _bundle_of(pid: int) -> tuple[str, str]:
    """pid → (bundle id, 英文包名)。从 bundle 的 Info.plist 读，按 pid 缓存一次。"""
    if pid in _bundle_cache:
        return _bundle_cache[pid]
    buf = ctypes.create_string_buffer(4096)
    out = ("", "")
    if _LP.proc_pidpath(int(pid), ctypes.cast(buf, _c), 4096) > 0:
        path = buf.value.decode("utf-8", "replace")
        if ".app/" in path:
            import plistlib

            try:
                with open(path.split(".app/")[0] + ".app/Contents/Info.plist", "rb") as f:
                    pl = plistlib.load(f)
                out = (pl.get("CFBundleIdentifier") or "",
                       pl.get("CFBundleDisplayName") or pl.get("CFBundleName") or "")
            except Exception:
                pass
    _bundle_cache[pid] = out
    return out


_LS_RE = re.compile(r'^\s*\d+\) "(?P<name>[^"]*)" ASN:(?P<front>.*?)\n(?P<body>(?:.*\n)*?)(?=^\s*\d+\) "|\Z)', re.M)
_LS_TTL = 0.5
_ls_cache: list = [0.0, []]  # [时间戳, 结果] —— 半秒缓存，一次任务里会被问很多次


def _ls_apps() -> list[dict]:
    """LaunchServices 里的「普通前台应用」全集：`lsappinfo list` 一次子进程，零常驻内存。

    为什么要它：CGWindowList 只看得到「有窗口」的应用，而 Agent 需要先 list_apps 看到
    正在运行但窗口全关着的 App 才能 get_app / focus_app —— 这正是 NSWorkspace 原来提供的
    东西（代价 33MB）。结果缓存半秒：一次任务里 list_apps→get_app→动作会连着问很多次。
    """
    now = time.time()
    if now - _ls_cache[0] < _LS_TTL:
        return _ls_cache[1]
    out: list[dict] = []
    try:
        text = subprocess.run(["lsappinfo", "list"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        text = ""
    for m in _LS_RE.finditer(text):
        body = m.group("body")
        if 'type="Foreground"' not in body:
            continue
        pid = re.search(r"\bpid = (\d+)\b", body)
        bundle = re.search(r'bundleID="([^"]+)"', body)
        if not pid:
            continue
        out.append(
            {
                "name": m.group("name"),
                "bundle": bundle.group(1) if bundle else "",
                "pid": int(pid.group(1)),
                "active": "(in front)" in m.group("front"),
            }
        )
    _ls_cache[:] = [now, out]
    return out


def running_apps() -> list[dict]:
    """当前有界面的应用：[{name, bundle, pid, active}]，纯 ctypes + lsappinfo，不加载 AppKit。

    NSWorkspace 一进来就是 33MB，而这里只需要「谁在跑 + 谁在前台 + 本地化名字」，
    lsappinfo 给名字（本来就是本地化的，「访达 / 备忘录」照样匹配得上），
    CGWindowList + Process Manager 补窗口与前台判定。
    """
    front = cg_input.frontmost_pid()
    out = []
    for a in _ls_apps():
        if a["pid"] == os.getpid():
            continue
        a = dict(a)
        a["active"] = a["pid"] == front or a["active"]
        out.append(a)
    if not out:  # lsappinfo 不可用（极少数系统）时退回窗口栈，至少坐标路径还能用
        for w in window_list():
            if not w["pid"] or w["layer"] != 0 or w["pid"] == os.getpid():
                continue
            bundle, en = _bundle_of(w["pid"])
            out.append({"name": w["owner"] or en or str(w["pid"]), "bundle": bundle,
                        "pid": w["pid"], "active": w["pid"] == front})
    out.sort(key=lambda a: (not a["active"], a["name"].lower()))
    return out


def find_app(spec: str) -> dict:
    """按 名称/bundle/pid 解析应用；spec 为空或 'front' → 前台应用。"""
    apps = running_apps()
    if not apps:
        raise AXBridgeError("APP_NOT_FOUND", "当前没有可用的 GUI 应用")
    if not spec or spec in ("front", "前台", "当前"):
        for a in apps:
            if a["active"]:
                return a
        return apps[0]
    low = spec.strip().lower()
    exact = [
        a for a in apps
        if low == a["name"].lower() or low == a["bundle"].lower() or (spec.isdigit() and int(spec) == a["pid"])
    ]
    if exact:
        hits = exact
    else:
        hits = [a for a in apps if low in a["name"].lower() or low in a["bundle"].lower()]
    if not hits:
        names = ", ".join(a["name"] for a in apps[:15])
        raise AXBridgeError("APP_NOT_FOUND", f"找不到应用 {spec!r}；正在运行的有: {names}")
    if len(hits) > 1:
        # 同一个 bundle id 的多个进程（Electron/Chromium 必然有主进程 + 若干 helper）
        # 不该逼用户去手挑 pid：前台那个、其次有窗口的就是屏幕上看得见的那一个。
        same_bundle = len({a["bundle"] for a in hits}) == 1 and hits[0]["bundle"]
        if same_bundle:
            hits.sort(key=lambda a: (not a["active"], a["pid"]))
            return hits[0]
        detail = ", ".join(
            f"{a['name']} pid={a['pid']}" + ("（前台，通常要的就是它）" if a["active"] else "（后台）")
            for a in sorted(hits[:6], key=lambda a: not a["active"])
        )
        raise AXBridgeError("AMBIGUOUS_APP", f"{spec!r} 匹配到多个应用: {detail}；用 pid 指定，或直接 get_app(\"front\") 绑前台那个")
    return hits[0]


def enable_chromium_accessibility(app_el: Element):
    """Electron/Chromium 应用默认不暴露 web 内容的 AX 树，设置这两个属性强制开启。"""
    for key in ("AXManualAccessibility", "AXEnhancedUserInterface"):
        try:
            app_el.set_attr(key, True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 元素树观察 + diff 注册表
# ---------------------------------------------------------------------------

MAX_NODES = 400
MAX_DEPTH = 22
# 未接线：一度打算给这些角色输出 `actions=[...]`，但 `_row()` 从未实现（get_ax_state 的
# 文档也已改成不再承诺该字段）。保留是因为它记录了「哪些角色的动作值得看」，接上去时直接用。
ACTION_ROLES_HINT = ("AXButton", "AXMenuItem", "AXCheckBox", "AXPopUpButton", "AXLink", "AXTabGroup", "AXToolbar")


class _Node:
    __slots__ = ("idx", "el", "role", "title", "pos", "size")

    def __init__(self, idx, el, role, title, pos, size):
        self.idx = idx
        self.el = el
        self.role = role
        self.title = title
        self.pos = pos
        self.size = size


class AppObserver:
    """一个应用一份：元素注册表（索引 → 元素）+ 上次快照（diff 用）。"""

    def __init__(self, pid: int):
        self.pid = pid
        self.nodes: dict[str, _Node] = {}   # node_id -> node
        self.snapshot: dict[str, str] = {}  # node_id -> 行哈希
        self.next_idx = 0

    def forget(self):
        for n in self.nodes.values():
            n.el.dispose()
        self.nodes.clear()
        self.snapshot.clear()

    def _row(self, el: Element, node_id: str) -> tuple[str, str, str, tuple | None, tuple | None]:
        ok, role = el.get("AXRole")
        role = role if ok and isinstance(role, str) else "?"
        ok, title = el.get("AXTitle")
        if not ok or not title:
            ok2, desc = el.get("AXDescription")
            title = desc if ok2 and desc else ""
        ok, value = el.get("AXValue", max_str=90)
        value = value if ok and value not in ("", None) else ""
        pos = el.get("AXPosition")[1] if el.get("AXPosition")[0] else None
        size = el.get("AXSize")[1] if el.get("AXSize")[0] else None
        seg = [f"{role}"]
        if title:
            seg.append(str(title)[:60])
        if value:
            seg.append(f"= {str(value)[:90]}")
        if isinstance(pos, tuple) and isinstance(size, tuple):
            seg.append(f"@({pos[0]},{pos[1]}) {size[0]}x{size[1]}")
        line = " ".join(seg)
        return node_id, line, role, str(title)[:60], pos, size

    def observe(self, full: bool = False, max_nodes: int = MAX_NODES, window: Element | None = None) -> str:
        """遍历元素树并更新注册表。返回文本：full=全量；否则只回新增/变化行。"""
        if ax_trusted() is False:
            raise AXBridgeError(
                "PERMISSION_DENIED",
                "辅助功能权限未授予宿主 App（系统设置→隐私与安全性→辅助功能→添加 DeepSeek Harness）",
            )
        root = Element.for_pid(self.pid)
        if window is not None and window.p != root.p:
            root.dispose()  # 指定窗口时不需要 app 根元素
        target = window or root
        rows: list[tuple[str, str]] = []
        invalid_idx: list[int] = []
        queue: list[tuple[Element, int, str]] = [(target, 0, "")]
        seen = 0
        truncated = False
        try:
            while queue and seen < max_nodes:
                el, depth, path = queue.pop(0)
                seen += 1
                node_id = path or "/root"
                ok_line = True
                try:
                    node_id, line, role, title, pos, size = self._row(el, node_id)
                except Exception:
                    ok_line = False
                if ok_line:
                    h = hashlib.md5(line.encode("utf-8", "replace")).hexdigest()[:10]
                    rows.append((node_id, f"{line}  #{h}"))
                    node = self.nodes.get(node_id)
                    if node is not None and (node.role, node.title) != (role, title):
                        # 路径带位置序号（`AXWindow[0]/AXButton[3]`），子节点一增删、窗口一重排
                        # 就会让同一路径指向**另一个**元素；标题也一样算身份，因为「窗口改名 /
                        # 换了一扇窗」正是最容易静默点错的时候。老代码在这里只更新显示元数据、
                        # 却保留旧元素引用，于是「打印出来的」和「点下去命中的」不是同一个东西
                        # —— 实测出现过按旧索引点「下载」结果点了「Documents」。现在旧索引直接
                        # 失效（动作会拿到 ELEMENT_UNAVAILABLE），并另发一个新索引。
                        stale = self.nodes.pop(node_id)
                        stale.el.dispose()
                        invalid_idx.append(stale.idx)
                        node = None
                    if node is None:
                        node = _Node(self.next_idx, Element(el.p), role, title, pos, size)
                        self.next_idx += 1
                        self.nodes[node_id] = node
                    else:
                        node.role, node.title, node.pos, node.size = role, title, pos, size
                if depth < MAX_DEPTH:
                    kids = el.children()
                    for i, k in enumerate(kids):
                        if seen + len(queue) >= max_nodes:
                            k.dispose()
                            truncated = True
                            continue
                        krole = k.get("AXRole")[1] if k.get("AXRole")[0] else "?"
                        queue.append((k, depth + 1, f"{node_id}/{krole}[{i}]"))
                el.dispose()
        finally:
            for el, _, _ in queue:
                el.dispose()

        new_snapshot = {nid: hashlib.md5(line.encode("utf-8", "replace")).hexdigest()[:10] for nid, line in rows}
        # macOS 的「查询帮助」浮层（AXHelpTag）弹出来时会成为唯一的可访问子树，
        # 读到的树会突然缩到一两个元素 —— 不点破就会被当成「这个应用没有界面」。
        hud = ""
        if len(rows) <= 4 and (self.snapshot and len(self.snapshot) > 8):
            hud = "\n⚠️ 这次只读到 " + str(len(rows)) + " 个元素（上次 " + str(len(self.snapshot)) +                   " 个）：多半有系统浮层/提示（AXHelpTag、菜单、拖拽反馈）盖住了界面。" \
                  "把鼠标移开或按 esc 后重新观察，不要据此判断应用没有 UI。"
        added_ids = [nid for nid in new_snapshot if nid not in self.snapshot]
        changed_ids = [nid for nid in new_snapshot if nid in self.snapshot and self.snapshot[nid] != new_snapshot[nid]]
        removed_ids = [nid for nid in self.snapshot if nid not in new_snapshot]
        for rid in removed_ids:
            node = self.nodes.pop(rid, None)
            if node:
                node.el.dispose()
        self.snapshot = new_snapshot

        def line_of(nid: str) -> str:
            node = self.nodes.get(nid)
            text = dict(rows)[nid] if nid in dict(rows) else ""
            text = text.rsplit("  #", 1)[0]
            return f"[{node.idx}] {text}"

        if full:
            body = "\n".join(line_of(nid) for nid, _ in rows)
            header = f"共 {len(rows)} 个元素（全量；截断={'是' if truncated else '否'}）"
        else:
            lines = [line_of(nid) for nid in added_ids]
            lines += [line_of(nid) + "  (已变化)" for nid in changed_ids]
            header = (
                f"diff: +{len(added_ids)} 新增 / ~{len(changed_ids)} 变化 / -{len(removed_ids)} 消失 / "
                f"注册表共 {len(self.nodes)} 元素（未变化行已省略，其索引仍有效；截断={'是' if truncated else '否'}）"
            )
            if invalid_idx:
                header += (
                    f"\n⚠️ 界面结构变了，这些旧索引已失效（元素换了位置，不是同一个东西）："
                    f"{sorted(invalid_idx)[:12]}；请重新 get_ax_state 再操作。"
                )
            body = "\n".join(lines) if lines else "（与上次观察无差异）"
        return f"{header}{hud}\n{body}"

    def node_by_index(self, index: int) -> _Node:
        for n in self.nodes.values():
            if n.idx != index:
                continue
            if n.el.p == 0:
                raise AXBridgeError("ELEMENT_UNAVAILABLE", f"元素[{index}] 的引用已失效；请重新 get_ax_state")
            # 用引用现读一次角色：与观察时记下的不一致，说明界面结构已经变了
            # （旧引用可能指向一个已被复用/回收的元素），照着它点就是点错东西。
            ok, role = n.el.get("AXRole")
            if ok and isinstance(role, str) and n.role and role != n.role:
                raise AXBridgeError(
                    "STALE_STATE",
                    f"元素[{index}] 已从 {n.role} 变成 {role}；请重新 get_ax_state 拿新索引，"
                    "别按旧索引操作",
                )
            return n
        raise AXBridgeError("ELEMENT_UNAVAILABLE", f"索引 {index} 不存在；请先重新 get_ax_state")

    def by_line(self, line_text: str) -> _Node | None:
        for n in self.nodes.values():
            if line_text in (n.title, n.role):
                return n
        return None


_OBSERVERS: dict[int, AppObserver] = {}
_LOCK = threading.RLock()


def observer_for(pid: int) -> AppObserver:
    with _LOCK:
        if pid not in _OBSERVERS:
            _OBSERVERS[pid] = AppObserver(pid)
        return _OBSERVERS[pid]


def window_list(on_screen_only: bool = True) -> list[dict]:
    """CGWindowList 的 python 视图（纯 ctypes，不加载 pyobjc）。

    返回 [{id, pid, owner, name, layer, x, y, w, h}]，坐标是全局「左上角、点」，与
    cg_input.displays() / CGEventPost 同一套空间；layer 0 = 普通窗口，非 0 是菜单栏、
    桌面图标层、我们的浮层面板这类。
    """
    _CG = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    _CG.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
    _CG.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint, ctypes.c_uint]
    opts = (1 << 0) if on_screen_only else (1 << 1) | (1 << 2)  # OnScreenOnly / All
    arr = _CG.CGWindowListCopyWindowInfo(opts, 0)
    if not arr:
        return []

    def value(d: int, key: str):
        k = _cfstr(key)
        try:
            raw = _kCF.CFDictionaryGetValue(d, k)
        finally:
            _kCF.CFRelease(k)
        return _cf_copy(raw)

    try:
        out = []
        for i in range(_kCF.CFArrayGetCount(arr)):
            d = _kCF.CFArrayGetValueAtIndex(arr, i)
            if not d:
                continue
            b = value(d, "kCGWindowBounds") or {}
            out.append(
                {
                    "id": value(d, "kCGWindowNumber"),
                    "pid": value(d, "kCGWindowOwnerPID"),
                    "owner": value(d, "kCGWindowOwnerName") or "",
                    "name": value(d, "kCGWindowName") or "",
                    "layer": value(d, "kCGWindowLayer") or 0,
                    "x": int(b.get("X", 0)),
                    "y": int(b.get("Y", 0)),
                    "w": int(b.get("Width", 0)),
                    "h": int(b.get("Height", 0)),
                }
            )
        return out
    finally:
        _kCF.CFRelease(arr)


# ---------------------------------------------------------------------------
# 动作效果证据
# ---------------------------------------------------------------------------

def effect_evidence(app_el: Element, before: str | None) -> str:
    """操作后重算轻量树哈希，给出 changed/unchanged 证据。"""
    after = tree_hash(app_el)
    if before is None:
        return "effect: unknown（无操作前基线）"
    return "effect: changed（界面树已变化）" if after != before else "effect: unchanged（界面树逐字节一致）"


def tree_hash(app_el: Element, max_nodes: int = MAX_NODES) -> str:
    """轻量界面指纹：role|title|value|description|位置|尺寸，逐节点拼进 md5。

    三个坑都是实测踩出来的：
      - **必须在 dispose 之前取 children**：引用计数一放，同一个 ref 的属性读取只返回错误，
        遍历会静默退化成「只哈希根节点」—— 那正是 effect 长期恒报 unchanged 的根因
        （界面明明变了，模型却被提示「换一种方式」）。
      - 每个属性只读一次：`if el.get(x)[0] ... el.get(x)[1]` 会把每次动作的 AX IPC 翻倍。
      - 菜单栏只走到 depth 3（条目 + 它们挂着的 AXMenu 节点），不往菜单项里钻：整棵菜单树
        几百个 AXMenuItem，全走一遍每个动作多付 100ms；而「菜单有没有被打开」恰好写在
        AXMenu 节点的位置尺寸上（关着的是 @(0,982) 0x0），3 层就够。
    注意：会消耗传入的 app_el 以及遍历到的所有节点。
    """
    h = hashlib.md5()
    queue: list[tuple[Element, int, int]] = [(app_el, 0, MAX_DEPTH)]  # (元素, 深度, 该子树深度上限)
    seen = 0
    try:
        while queue and seen < max_nodes:
            el, depth, cap = queue.pop(0)
            seen += 1
            kids = el.children() if depth < cap else []
            if depth == 0:
                for kid in kids:
                    ok, role = kid.get("AXRole")
                    queue.append((kid, 1, 3 if ok and role in ("AXMenuBar", "AXExtrasMenuBar") else MAX_DEPTH))
            else:
                queue.extend((kid, depth + 1, cap) for kid in kids)
            parts = []
            for name, width in (("AXRole", 40), ("AXTitle", 80), ("AXValue", 60),
                                ("AXDescription", 60), ("AXPosition", 160), ("AXSize", 160)):
                ok, value = el.get(name, max_str=width)
                parts.append("" if not ok else str(value))
            el.dispose()
            h.update("|".join(parts).encode("utf-8", "replace"))
            h.update(b"\n")
    finally:
        for el, _, _ in queue:
            el.dispose()
    return h.hexdigest()[:12]
