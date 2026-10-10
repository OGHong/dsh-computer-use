#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
computer-use MCP 服务器 —— 给 DeepSeek Harness（dsh）或任何支持 MCP 的 Agent
提供「看屏幕 + 动鼠标键盘」的电脑控制能力。

两条操作路径，完整工具清单与说明见 `mcp.instructions` 及各工具的 description：
  - 元素路径（优先）：list_apps / get_app / get_ax_state / click_element /
    set_value / perform_action / select_text —— 走无障碍树，后台应用也生效；
  - 坐标路径（兜底）：screenshot / click / drag / type_text / press_key / scroll /
    paste / sequence / launch_app / focus_app / focus_window / list_windows /
    screen_info / run_shell / wait / preview（预览浮窗开关）/ open_permission_settings；
  - 控制面：stop / resume（急停开关）、release_lease（控制器租约）。

内部分三层（见 action_layer.py 的模块说明，设计任务书 §3.1）：
  - 动作层 `_do_*` —— 执行 + 采证，只返回 ActionResult（ok / effect / settle_ms /
    possibly_sent / error / detail），**不返回自然语言**；
  - 渲染层 `_render_*` —— 把 ActionResult 变成给模型看的自然语言；
  - 注册表 `_ACTIONS` / `_register()` —— 把两者绑成一个 MCP 工具，`sequence` 按名字
    复用同一份清单（工具能做的 = 批处理能做的）。
公共状态与等待语义在 server.py 里，急停在 killswitch.py，租约在 lease.py。

坐标约定: 一律使用「最近一张截图」上的像素位置。Retina 屏的截图像素数是
系统逻辑坐标的 2 倍，本服务器内部自动换算，模型无需关心。

macOS 权限: 运行本服务器的宿主程序（DeepSeek Harness.app / 终端）需要在
系统设置 → 隐私与安全性 中获得「辅助功能」（控制鼠标键盘）和
「屏幕录制」（截屏）两项授权，否则截图会是黑屏/空图、点击会无效。
"""
import os
import sys

if __name__ == "__main__":
    # 最早的一步：把自己换进自建 UIElement bundle（否则 Dock 上会攒 Python 图章，见
    # ui_element_app 的说明）。放在所有重模块 import 之前 —— 被 execve 丢掉的第一个解释器
    # 就没干过什么活，一次换映像只多花一次引导（实测 52ms → 84ms）；换成功时这行不返回。
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import ui_element_app  # noqa: E402

    ui_element_app.ensure_ui_element_reexec()

import inspect
import json
import re
import secrets
import subprocess
import threading
import time
import typing
from contextlib import contextmanager
from pathlib import Path
import shutil

import action_layer
import killswitch
import lease
from action_layer import (
    ActionSpec,
    ActionResult,
    action,
    effect_clause,
    mark_sent,
    render_error,
    step_line,
    tail,
)
from mcp_stdio import BadArgument, Blocks, Image, Server, coerce_for, pause

import importlib


class _LazyModule:
    """第一个真的用到的工具才 import 对应模块。

    光 CPython 解释器自己就 12MB 常驻，而 ax_bridge 会连带 ctypes/hashlib/plistlib、
    cg_input 会连带 ctypes —— 没人用电脑的时候这些都不该付。代理只转发属性访问，
    工具代码里的 `ax.` / `cg.` 一个字都不用改。
    """

    def __init__(self, module_name: str):
        self._name, self._mod = module_name, None

    def __getattr__(self, item):
        if self._mod is None:
            self._mod = importlib.import_module(self._name)
        return getattr(self._mod, item)


ax = _LazyModule("ax_bridge")
cg = _LazyModule("cg_input")
from ax_error import AXBridgeError, err_name  # 异常类型必须启动就有：每处 except 都要拿它匹配

# 与 lib/index.js 同一个开关：只有一侧认的 env 会让两个半区读写不同的目录
SHOT_DIR = Path(os.environ.get("DSH_CUA_DIR", "/tmp/dsh-computer-use"))
MAX_EDGE = 1600     # 发给模型的截图最长边（像素），平衡清晰度与上下文体积
ACTION_DELAY = 0.6   # 动作后最多等多久（下面的 _settle 会提前返回）
SETTLE_FLOOR = 0.25   # 界面变化至少要给它这么久，不然「两次一样」是假稳定
SETTLE_STEP = 0.12

mcp = Server(
    "computer-use",
    version="3.0",
    instructions=(
        "macOS 电脑控制工具集：无障碍树（元素级）+ 截屏坐标两条操作路径。"
        "策略：优先 元素路径 —— list_apps → get_app 绑定目标应用（同时取得控制器租约）→ "
        "get_ax_state 观察界面（diff 模式，只回新增/变化行；screenshot=true 可同时回一张图）→ "
        "click_element / set_value / perform_action 按元素操作；"
        "截图坐标路径（screenshot + click）作为兜底（无 AX 的场景，如游戏/画布）。"
        "每个动作会返回 effect: changed/unchanged 证据，unchanged 说明没生效，换一种方式，不要重复同一动作。"
        "连续确定的动作（点击→输入→回车）用 sequence 一次执行完：它支持全部动作工具，并可用 "
        "{\"observe\": {\"diff\": true}} 伪步在中间插一次观察（插过之后元素索引要用新的）。"
        "动作返回 possibly_sent: true 时，**不要重复同一动作** —— 那一步可能已经下发，"
        "先 get_ax_state 观察当前状态，再决定下一步；重复一个可能已生效的点击会造成重复提交。"
        "**绑定令牌**：get_app 与 get_ax_state 的返回里有一行 binding_id=…；后续动作（含 sequence）"
        "请把它带上。这个服务器进程被同一 DSH 实例的所有会话共用，别的会话 get_app 会改掉绑定 —— "
        "带上令牌时你会拿到 ERROR[BINDING_CHANGED]（这时重新 get_app + get_ax_state 再动手），"
        "不带就只能靠索引碰运气，可能点到别的应用上。"
        "元素路径动作可用 strategy=a11y 强制走无障碍语义（失败即报错，不会偷偷碰用户鼠标），"
        "坐标动作可用 strategy=event 强制合成事件。"
        "多个会话/子代理同时操作同一台电脑时，get_app 会返回 ERROR[CONTROLLER_BUSY]；"
        "确认对方已经不用电脑时可带 force=true 抢占（会写日志）。"
        "用户按了急停（预览浮窗上的 ⏹）之后，动作类工具一律返回 ERROR[KILLED]，观察类工具仍可用，"
        "恢复用 resume()。"
        "只在结果不确定时才 screenshot 确认；如果点击/输入后界面毫无变化，先调 screen_info 检查权限、"
        "急停与租约状态，不要盲目重试。"
        "遇到应用损坏、环境损坏这类非任务本身的问题，报告结论即可，"
        "不要在用户没有要求的情况下主动展开修复。"
    ),
)


def _settle(pid: int | None = None, budget_ms: int | None = None) -> int:
    """等界面稳定，而不是每个动作盲睡 600ms。返回**实际等待的毫秒数**（§3.1）。

    一轮 GUI 任务动辄几十步，固定停顿是时间开销里最大的一块。改成轮询已绑定应用的
    界面指纹（`tree_hash` 一次约 40ms）：过了 SETTLE_FLOOR 之后，连续两次采样一样就
    认为界面不再动了，立刻返回；最多仍只等 ACTION_DELAY（sequence 可以用
    `_SETTLE_BUDGET_MS` 把上限压得更低）。
    没绑定应用、或应用读不到 AX（游戏/画布）时退回原来的固定停顿 —— 只会更快，不会更糙。
    每轮都查一次急停：用户按下停止键之后，正在跑的等待会在 ≤SETTLE_STEP 内让出。
    """
    started = time.monotonic()
    limit = ACTION_DELAY if budget_ms is None else max(0.05, min(budget_ms / 1000.0, ACTION_DELAY))
    budget = _SETTLE_BUDGET_MS[0]
    if budget is not None and budget_ms is None:
        limit = max(0.05, min(budget / 1000.0, ACTION_DELAY))
    target = pid if pid is not None else (_bound_app["pid"] if _bound_app else None)
    if target is None:
        pause(limit)  # 没绑定应用或读不到 AX：退回固定停顿（等满这次的预算）
        return int((time.monotonic() - started) * 1000)
    pause(min(SETTLE_FLOOR, limit))
    deadline = started + limit
    last = None
    while True:
        killswitch.guard()
        try:
            current = ax.tree_hash(ax.Element.for_pid(int(target)), max_nodes=200)
        except Exception:
            pause(max(0.0, deadline - time.monotonic()))
            return int((time.monotonic() - started) * 1000)
        if last is not None and current == last:
            return int((time.monotonic() - started) * 1000)
        last = current
        if time.monotonic() >= deadline:
            return int((time.monotonic() - started) * 1000)
        pause(min(SETTLE_STEP, max(0.0, deadline - time.monotonic())))


# 最近一次发给模型的截图元数据：像素尺寸 + 所在显示器的全局几何（坐标换算基准）
_last_shot = {"size": None, "origin": (0, 0), "size_pt": None, "basis": ""}

# 鼠标键盘操作互斥锁：同一时刻只允许一个动作占用物理输入（对齐 CONTROLLER_BUSY）。
# 必须可重入：_hotkey 的 CGEvent 兜底会在外层动作（type_text / press_key / paste）
# 已经持锁的情况下再申请一次，普通 Lock 会自己跟自己死锁 15 秒。
_input_lock = threading.RLock()


@contextmanager
def _input(timeout: float = 15.0):
    """占用物理鼠标/键盘；被占用时返回 CONTROLLER_BUSY 而不是互相打架。"""
    if not _input_lock.acquire(timeout=timeout):
        raise AXBridgeError(
            "CONTROLLER_BUSY",
            f"鼠标/键盘正被另一个操作占用（等待超过 {timeout:.0f} 秒），请稍后重试",
        )
    try:
        yield
    finally:
        _input_lock.release()


def _displays() -> list[dict]:
    """所有显示器：全局左上角坐标（点）+ 尺寸（点），index 从 1 开始（主屏在前）。"""
    return cg.displays()


def _resize_save(raw: Path, jpg: Path) -> None:
    """把 screencapture 的原生分辨率大图压成发给模型的尺寸，并发布给预览面。

    缩放交给系统自带的 `sips` 而不是 Pillow：Pillow 常驻要 9MB，而这条路径本来就是
    一次截屏跑一次，起个子进程更划算（实测两者缩放耗时同级，sips 略快）。
    """
    try:
        subprocess.run(
            ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "80", "-Z", str(MAX_EDGE),
             str(raw), "--out", str(jpg)],
            check=True, capture_output=True, timeout=20,
        )
        probe = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(jpg)],
                               capture_output=True, text=True, timeout=20).stdout
        wh = re.findall(r"pixel(?:Width|Height): (\d+)", probe)
        _last_shot["size"] = (int(wh[0]), int(wh[1])) if len(wh) == 2 else None
    except Exception:
        _last_shot["size"] = None
    raw.unlink(missing_ok=True)
    _publish_latest(jpg)


def _publish_latest(jpg: Path) -> None:
    """把这张截图交给预览浮窗（/tmp/dsh-computer-use/latest.jpg）。

    第一帧出来时顺带把浮窗拉起来：只有截图、没有其它动作的会话（比如纯读屏）也该看得见预览，
    而且这一刻起才有帧可显示 —— 对齐 ZCode「没有首帧不 promote，出了首帧才浮出来」。
    先写临时文件再 os.replace：同一时刻 Host 的抓帧进程与浮窗都在读这个路径，
    非原子写入会让它们读到一个只落地一半的 JPEG。
    """
    _ensure_panel()
    try:
        tmp = SHOT_DIR / "latest.jpg.tmp"
        shutil.copyfile(jpg, tmp)
        os.replace(tmp, SHOT_DIR / "latest.jpg")
    except Exception:
        pass


PANEL_SCRIPT = Path(__file__).with_name("preview_window.py")
PANEL_LOCK = SHOT_DIR / "preview.lock"
PANEL_GEOMETRY = SHOT_DIR / "panel.json"
PANEL_HIDDEN = SHOT_DIR / "panel.hidden"   # "off:<ts>" = 用户自己关掉了，别再自动拉起
FOCUS_FILE = SHOT_DIR / "focus.json"
PANEL_WINDOW_TITLE = "computer-use 实时预览"  # 与 preview_window.py 的 setTitle_ 保持一致
_panel_proc: subprocess.Popen | None = None
_panel_starting = False   # _panel_start 的重入保护


def _panel_pid() -> int | None:
    """当前活着的预览浮窗进程号；没有就是 None。

    自己拉起的子进程要走 poll() 判断：SIGTERM 之后它可能还没被回收，此时
    os.kill(pid, 0) 对僵尸进程同样返回成功，会把「已经收掉」误读成「还在」。
    """
    global _panel_proc  # noqa: PLW0603
    if _panel_proc is not None:
        if _panel_proc.poll() is None:
            return _panel_proc.pid
        _panel_proc = None
    try:
        pid = int(PANEL_LOCK.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def _panel_stop() -> bool:
    pid = _panel_pid()
    if not pid:
        return False
    try:
        os.kill(pid, 15)  # SIGTERM：浮窗装了处理器，会清掉 lock 再退出
    except OSError:
        pass
    return True


def _panel_start() -> bool:
    """拉起 OS 级预览浮窗（唯一的预览面，浮在所有应用之上）。进程与本子进程同生共死：
    DSH_CUA_PARENT 指向这里，浮窗每 250ms 探一次父进程是否还在。"""
    global _panel_proc, _panel_starting  # noqa: PLW0603
    if _panel_starting:          # _note() 会回头调 _ensure_panel()，这里必须防重入
        return False
    _panel_starting = True
    try:
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        _panel_stop()
        try:
            _panel_proc = subprocess.Popen(
                [sys.executable, str(PANEL_SCRIPT)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env={**os.environ, "DSH_CUA_PARENT": str(os.getpid())},
            )
        except OSError:
            _panel_proc = None
            return False
        PANEL_LOCK.write_text(str(_panel_proc.pid), encoding="utf-8")
        # 续一笔活跃：Agent 空闲超过 2 分钟后才拉起浮窗时，它会立刻按活跃判定把自己藏起来
        # （既不报几何也永远不上屏）。写在这里而不是 Popen 之前，是为了不触发重入。
        _note("🪟 预览浮窗已打开")
        return True
    finally:
        _panel_starting = False


def _panel_user_hidden() -> bool:
    """是不是被**明确**关掉了（panel.hidden 以 off 开头）。

    只有 preview(enable=False) 会写 off。浮窗上那个 × 写的是 "hide:" —— 那是「现在收起来」，
    由浮窗进程自己管（安静 HIDE_QUIET_S 之后下一次动作自己回来），server 不该据此拒绝自动
    拉起：用户随手一点 ×，不该换来一个他再也找不到开关去恢复的状态。"""
    try:
        return PANEL_HIDDEN.read_text(encoding="utf-8").strip().startswith("off")
    except OSError:
        return False


def _panel_remember(state: str) -> None:
    """记下开关意图（带毫秒时间戳，与 card.txt 同一套 nonce 语义）。"""
    try:
        PANEL_HIDDEN.parent.mkdir(parents=True, exist_ok=True)
        tmp = PANEL_HIDDEN.with_suffix(".hidden.tmp")
        tmp.write_text(f"{state}:{int(time.time() * 1000)}", encoding="utf-8")
        os.replace(tmp, PANEL_HIDDEN)
    except OSError:
        pass


def _ensure_panel() -> None:
    """第一次真的用电脑时把预览浮窗带起来（用户要求预览面一直是浮窗）。

    每个动作都判一次（很便宜：一个文件读 + 一次 kill(0)）：浮窗长空闲会自己退出，
    下一次动作要能把它叫回来。`DSH_CUA_NO_PANEL=1` 可整条关掉；失败不吭声 ——
    浮窗起不来（比如 venv 里没装 pyobjc）不该影响任何工具调用。
    """
    if _panel_starting:
        return
    if os.environ.get("DSH_CUA_NO_PANEL", "").lower() in {"1", "true", "yes"}:
        return
    if _panel_user_hidden():
        return                       # 用户点过 ×：尊重他的选择，等他或 Agent 明确要开
    if _panel_pid() is None:
        _panel_start()


def _cg_id_for_window(pid: int, index: int) -> int | None:
    """AX 窗口序号 → CGWindowID：两边都按各自的从上到下排序，用几何对齐。"""
    try:
        root = ax.Element.for_pid(int(pid))
        try:
            wins = [w for w in ax.windows_of(root) if w["pos"] and w["size"]]
        finally:
            root.dispose()
        target = next((w for w in wins if w["index"] == int(index)), None)
        if not target:
            return None
        ours = [w for w in ax.window_list() if w["pid"] == int(pid) and w["layer"] == 0]
        hit = next(
            (w for w in ours if (w["x"], w["y"], w["w"], w["h"]) == (target["pos"][0], target["pos"][1], target["size"][0], target["size"][1])),
            None,
        )
        return hit["id"] if hit else None
    except Exception:
        return None


def _publish_focus(pid: int | None = None, window_id: int | None = None, label: str = "") -> None:
    """把「Agent 正在操作哪扇窗」写进 focus.json，预览浮窗据此按窗口抓帧。

    对齐 Qoder / ZCode 的画中画：看的是那个窗口，不是整块桌面 —— 一屏 1512pt 里
    一个 824x399 的窗口只占 1/8 面积，整屏抓时它在浮窗上就糊成一团；按窗口抓既看得清，
    每帧也更小（实测 21.8KB vs 整屏 33KB）。窗口关了 Host 会自己退回整屏。
    """
    try:
        if window_id is None and pid:
            ours = [w for w in ax.window_list() if w["pid"] == int(pid) and w["layer"] == 0]
            # 取面积最大的那扇：CGWindowList 是按前后顺序排的，而标签选择器、临时浮层
            # 这类 66x20 的小窗常常排在真正的文档窗之前 —— 照顺序取第一扇会让浮窗
            # 盯在一小块无关像素上（实测盯到过 66x20 的窗口）。
            window_id = max(ours, key=lambda w: w["w"] * w["h"])["id"] if ours else None
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = FOCUS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"window": window_id, "pid": pid, "ts": int(time.time() * 1000)}), encoding="utf-8")
        os.replace(tmp, FOCUS_FILE)
    except Exception:
        pass


def _note(text: str) -> None:
    """把当前动作写进状态文件（浮窗据此显示动作字幕），顺带续一次心跳。

    心跳是浮窗判断「还有没有人在用」的依据：超过 15 秒没被写过就自己藏起来，
    不会在 Agent 收工后留一块孤儿画面挂在屏幕上。第一次动作顺带把浮窗拉起来 ——
    预览面就是它，用户要求「一直都是浮窗」。
    """
    _ensure_panel()
    try:
        (SHOT_DIR / "status.txt").write_text(text, encoding="utf-8")
        (SHOT_DIR / "heartbeat").touch()
    except Exception:
        pass


def _capture_jpeg(display: int = 1) -> Path:
    """截取指定显示器 → JPEG。记录该显示器的全局几何供坐标换算。"""
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    raw = SHOT_DIR / "screen_raw.png"
    jpg = SHOT_DIR / "screen.jpg"
    displays = _displays()
    if display < 1 or display > len(displays):
        raise ValueError(f"显示器索引 {display} 不存在（共 {len(displays)} 个，见 screen_info）")
    geo = displays[display - 1]
    ox, oy = geo["topleft"]
    w, h = geo["size_pt"]
    if display == 1:
        subprocess.run(["screencapture", "-x", str(raw)], check=True, timeout=15)
    else:
        subprocess.run(
            ["screencapture", "-x", f"-R{int(ox)},{int(oy)},{int(w)},{int(h)}", str(raw)],
            check=True,
            timeout=15,
        )
    _last_shot["origin"] = (int(ox), int(oy))
    _last_shot["size_pt"] = (int(w), int(h))
    _last_shot["basis"] = f"全屏[{display}] {int(w)}x{int(h)} 点 @({int(ox)},{int(oy)})"
    _resize_save(raw, jpg)
    return jpg


def _capture_window_jpeg(window_id: int) -> Path:
    """按 CGWindowID 截取单个窗口（不含阴影），坐标基准=窗口全局位置。"""
    hit = next((w for w in ax.window_list(on_screen_only=False) if w["id"] == int(window_id)), None)
    if hit is None:
        raise ValueError(f"window_id {window_id} 不存在，用 list_windows 查询")
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    raw = SHOT_DIR / "window_raw.png"
    jpg = SHOT_DIR / "window.jpg"
    subprocess.run(["screencapture", "-x", "-o", f"-l{int(window_id)}", str(raw)], check=True, timeout=15)
    _publish_focus(pid=None, window_id=int(window_id))
    _last_shot["origin"] = (hit["x"], hit["y"])
    _last_shot["size_pt"] = (hit["w"], hit["h"])
    _last_shot["basis"] = f"窗口[{hit['name'] or hit['id']}] {hit['w']}x{hit['h']} 点 @({hit['x']},{hit['y']})"
    _resize_save(raw, jpg)
    return jpg


def _to_screen_point(x: int, y: int) -> tuple[int, int]:
    """截图像素坐标 → 全局逻辑坐标（点）。按截图所属显示器/窗口的几何换算。"""
    if _last_shot["size_pt"]:
        ox, oy = _last_shot["origin"]
        sw, sh = _last_shot["size_pt"]
    else:
        ox, oy = 0, 0
        sw, sh = cg.main_size()
    if _last_shot["size"]:
        iw, ih = _last_shot["size"]
        return round(ox + x * sw / iw), round(oy + y * sh / ih)
    return int(x) + ox, int(y) + oy


KEY_ALIASES = {
    "cmd": "command", "win": "command", "control": "ctrl",
    "alt": "option", "esc": "escape", "enter": "return", "del": "delete",
}


def _ax_trusted():
    """辅助功能权限（TCC）是否已授予宿主 App；查不到时返回 None。"""
    return ax.ax_trusted()


def _screen_recording_ok():
    """屏幕录制权限是否已授予宿主 App；查不到时返回 None。"""
    return cg.screen_recording_ok()


# 注意：动作结果尾部那两类「静默失败」提示（辅助功能权限没给、观察窗被明确关掉）现在是
# **结构化**的：动作层只往 `detail["hints"]` 里塞码（见 `_hint_codes()`），文案在
# `action_layer.HINTS` 里，渲染层负责拼。v1 的 `_ax_hint()` / `_panel_hint()` / `_state_hint()`
# 是「动作层直接拼自然语言」的老写法，已随 §3.1 的分层一起去掉。


# ---------- 基础动作的内部实现（供工具与 sequence 复用） ----------

def _screenshot(path: str | None = None) -> Image:
    p = path or str(_capture_jpeg())
    _note("📷 截屏")
    return Image(path=p)


def _release_modifiers():
    """强制松开全部修饰键。组合键在个别时序下会漏发 keyUp，导致修饰键粘连
    （后续 cmd+v 被系统当成 ctrl+cmd+v 等），每个组合键动作后都兜底释放。"""
    cg.release_modifiers()


# System Events 的 key code 表（修饰键组合走这条通道比直发 CGEvent 可靠）
_SE_KEYCODES = {
    "return": 36, "tab": 48, "space": 49, "delete": 51, "escape": 53,
    "forwarddelete": 117, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}
_SE_MODS = {"command": "command down", "cmd": "command down", "ctrl": "control down",
            "option": "option down", "shift": "shift down"}


def _hotkey(parts: list[str], strategy: str = "auto") -> None:
    """发送组合键。优先 System Events（osascript）注入——对修饰键组合的兼容性
    远好于直发 CGEvent（会间歇性丢失修饰键，实测 cmd+v 敲出 v）；
    单字符用 keystroke，特殊键用 key code，CGEvent 作最后兜底。

    strategy（§3.3）：
      - ``auto``（默认）：System Events 优先，失败降级到 CGEvent；
      - ``event``：跳过 System Events，直接合成 CGEvent；
      - ``a11y``：只用 System Events，失败即抛 ``ACTION_UNAVAILABLE`` —— 绝不静默降级到
        会移动用户鼠标/污染真实输入队列的那条路。
    """
    mods = [p for p in parts[:-1] if p in _SE_MODS]
    key = parts[-1]
    mod_syntax = ""
    if mods:
        seen = []
        for m in mods:
            s = _SE_MODS[m]
            if s not in seen:
                seen.append(s)
        mod_syntax = " using {" + ", ".join(seen) + "}"
    script = None
    if len(key) == 1:
        esc = key.replace("\\", "\\\\").replace('"', '\\"')
        script = f'keystroke "{esc}"{mod_syntax}'
    elif key in _SE_KEYCODES:
        script = f"key code {_SE_KEYCODES[key]}{mod_syntax}"
    if script and strategy != "event":
        try:
            subprocess.run(
                ["osascript", "-e", f'tell application "System Events" to {script}'],
                check=True, timeout=10, capture_output=True,
            )
            return
        except Exception as e:
            if strategy == "a11y":
                raise AXBridgeError(
                    "ACTION_UNAVAILABLE",
                    f"System Events 注入 {'+'.join(parts)} 失败（{type(e).__name__}）；"
                    "strategy=a11y 不降级到合成事件，改用 strategy=auto/event 或先让目标应用获得前台与焦点",
                ) from e
    elif strategy == "a11y":
        raise AXBridgeError(
            "ACTION_UNAVAILABLE",
            f"按键 {'+'.join(parts)} 没有 System Events 路径（strategy=a11y 不降级到合成事件）",
        )
    with _input():
        cg.hotkey(parts)


def _frontmost_pid() -> int | None:
    return cg.frontmost_pid()


def _activate_pid(pid: int) -> bool:
    """把应用带到前台。走 System Events 而不是 NSRunningApplication：后者会把整个
    AppKit（33MB）拉进进程，而组合键这条路径本来就已经在用 osascript 了。
    （实测裸 AXRaise 常假成功，所以还是要一个真激活调用。）"""
    try:
        subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to set frontmost of (first process whose unix id is '
             f'{int(pid)}) to true'],
            check=True, capture_output=True, timeout=10,
        )
    except Exception:
        return False
    # 轮询而不是盲睡：激活通常在 100ms 内就完成，睡死 0.35 秒会摊到每个键盘动作上。
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if _frontmost_pid() == int(pid):
            return True
        time.sleep(0.04)
    return False


# 「这一点上没有窗口」与「窗口列表根本读不到」必须是两种结果：前者可能只是点在桌面空白
# 处（照旧放行），后者意味着落点保护失去了依据 —— 混成同一个 None 就等于「窗口服务一抖，
# 保护静默取消」，而这一点可能正落在消息区、列表项或删除按钮上。哨兵用 dict 而不是新类型，
# 是为了让下游 `top["pid"]` 这类访问不会炸（值全是无效值，identity 才是判据）。
_WINDOW_QUERY_FAILED: dict = {"id": 0, "pid": -1, "owner": "", "name": "", "layer": 0, "x": 0, "y": 0, "w": 0, "h": 0}


def _target_window(px: int, py: int) -> dict | None:
    """全局坐标点（点）上，真正挡在最前面的普通层级窗口 —— 也就是这一下会点到的那个。

    走 CGWindowList 而不是 AX：AX 会把「桌面」也算成一个铺满整屏的窗口，用它判定等于
    永远为真；只看 layer 0，否则壁纸/小组件层会冒充目标窗口。

    读不到窗口列表时返回 `_WINDOW_QUERY_FAILED` 哨兵（**不是** None）：调用方的落点保护
    据此拒绝，而不是当作「没有窗口」放行。
    """
    try:
        infos = ax.window_list()
    except Exception:
        return _WINDOW_QUERY_FAILED
    legacy = PANEL_WINDOW_TITLE
    for w in infos:  # 从前到后排序：谁在最前面，这一下就是点谁
        in_point = w["x"] <= px < w["x"] + w["w"] and w["y"] <= py < w["y"] + w["h"]
        if not in_point:
            continue
        # 自家浮窗故意不看层级：它按设计就浮在所有应用之上
        if w["layer"] != 0 and w["name"] != legacy:
            continue
        return w
    return None


def _raise_window_at(px: int, py: int) -> bool:
    """把点所在的自有窗口提到最前。

    非 key window 的第一次点击只被系统用来激活窗口（Electron/Chromium 尤其如此），
    模型看到「界面没变」就会重复点 —— 这里先 AXRaise，让一次点击真的落在控件上。
    """
    if not _bound_app:
        return False
    try:
        root = ax.Element.for_pid(int(_bound_app["pid"]))
    except Exception:
        return False
    try:
        hit = next(
            (
                w["index"]
                for w in ax.windows_of(root)
                if w["pos"] and w["size"]
                and w["pos"][0] <= px < w["pos"][0] + w["size"][0]
                and w["pos"][1] <= py < w["pos"][1] + w["size"][1]
            ),
            None,
        )
        if hit is None:
            return False
        el = ax.window_element(root, hit)
        if el is None:
            return False
        try:
            if el.get("AXMain")[1] is True:  # 已经是主窗口，不用再抬
                return False
            raised = el.perform("AXRaise") == 0
        finally:
            el.dispose()
        if raised:
            time.sleep(0.12)
        return raised
    finally:
        root.dispose()


def _is_our_surface(w: dict | None) -> bool:
    """这是不是插件自己的观察面。

    浮窗浮在所有应用之上（状态栏层级），「只看普通层级窗口」的落点判定会把它整个
    跳过，于是 `preview` 报出来的坐标反过来被落点保护拒掉 —— 插件的 UI 不该算
    「无关窗口」：点错它不伤用户数据，而那串坐标本来就是给 Agent 拖它、点它的 × 用的。
    """
    if not w:
        return False
    return w["name"] == PANEL_WINDOW_TITLE or w["pid"] in (os.getpid(), _panel_pid())


def _aim_guard(px: int, py: int, force: bool, top: dict | None = None) -> str:
    """点击/拖拽前的落点检查，返回要提前中止的错误串（空串 = 可以继续）。

    绑定了目标应用、而这一下实际会落到别的窗口上时，**拒绝执行**而不是照点完附一句
    警告：实测那种警告拦不住事故 —— 坐标是按目标应用的窗口截图换算的，一旦它被别的
    窗口盖住，这一点就落在无关窗口上（可能正好是消息区、列表项、删除按钮）。
    """
    if not _bound_app or force:
        return ""
    if top is None:
        top = _target_window(px, py)
    if top is _WINDOW_QUERY_FAILED:
        # fail-closed：判据没了就拒绝，而不是照点完附一句警告（警告实测拦不住事故）。
        return (
            "ERROR[TARGET_UNKNOWN]: 读不到窗口列表（窗口服务或录屏权限异常），无法确认这一下会落到哪扇窗上，"
            "已拒绝以免点到无关窗口。稍后重试；确认安全时可带 force=true 强制执行。"
        )
    if top is None or _is_our_surface(top) or top["pid"] == _bound_app["pid"]:
        return ""
    return (
        f"ERROR[NOT_FRONTMOST]: 截图坐标换算到全局 ({px},{py}) 后，挡在最前面的是 "
        f"{top['owner'] or '未知应用'} 的窗口「{top['name'] or '无标题'}」，不是绑定的 {_bound_app['name']}。"
        "本次操作已拒绝（否则会点到无关窗口）。先 focus_app / focus_window 把那扇窗带到前面再重试；"
        "如果本来就是要点这个窗口，带 force=true。"
    )


def _ensure_frontmost() -> str:
    """键盘动作前自动把已绑定的目标应用带到前台（聚焦该聚焦的窗口）。"""
    if not _bound_app:
        return ""
    if _frontmost_pid() == _bound_app["pid"]:
        return ""
    name = _bound_app["name"]
    if _activate_pid(_bound_app["pid"]):
        return f"（已自动聚焦 {name}）"
    return f"⚠️ 目标 {name} 未到前台，输入可能落到当前前台应用；可改用元素路径或先手动激活。"


def _hold_combo(parts: list[str], hold: float) -> None:
    """按住一组键（含修饰键）指定秒数后松开。"""
    for k in parts:
        cg.key_down(k)
    time.sleep(max(0.0, min(float(hold), 5.0)))
    for k in reversed(parts):
        cg.key_up(k)
    cg.release_modifiers()


def _pick_window(wins: list[dict], spec: str) -> dict | None:
    """按序号或标题挑窗口。

    序号在抬窗之后会重排（AXWindows 是按前后顺序编号的），所以标题更稳 —— 实测就踩过
    「记住窗口[1] 是 oghong，抬完一次再按 window=1 打到了下载上」。
    """
    spec = str(spec).strip()
    if spec.isdigit():
        hit = next((w for w in wins if w["index"] == int(spec)), None)
        if hit:
            return hit
    exact = next((w for w in wins if (w["title"] or "") == spec), None)
    if exact:
        return exact
    return next((w for w in wins if spec and spec.lower() in (w["title"] or "").lower()), None)


def _aim_keys(window: str | int | None = None) -> str:
    """把键盘动作对准该去的那扇窗，并回报一行结构化归属。

    - 给了 `window`（AX 窗口序号，与 get_app / focus_window 的 [i] 同一套）就抬那一扇，
      并把它记成后续指令的目标（浮窗也跟着切过去）；
    - 没给就用 `focus.json` 里上一条指令的目标窗。
    不抬的代价实测过：⌘ 系快捷键永远发给应用的 front window，于是「想操作 A 窗、
    ⌘⇧U 却把 B 窗从应用程序翻成实用工具」这类误操作会发生（且不可撤销地改了现场）。
    """
    if not _bound_app:
        return ""
    pid = int(_bound_app["pid"])
    want: dict | None = None
    spec = "" if window is None else str(window).strip()
    picked = bool(spec) and spec != "-1"
    try:
        root = ax.Element.for_pid(pid)
    except Exception:
        return ""
    try:
        wins = [w for w in ax.windows_of(root) if w["pos"] and w["size"]]
        if picked:
            want = _pick_window(wins, spec)
            if want is None:
                return (
                    f"ERROR[WINDOW_NOT_FOUND]: 该应用没有匹配 {spec!r} 的窗口"
                    f"（现有 {[(w['index'], w['title']) for w in wins]}）。"
                    "注意序号会随抬窗重排，用标题更稳（window=\"下载\"）。"
                )
        else:
            try:
                focus = json.loads(FOCUS_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                focus = {}
            wid = focus.get("window")
            if wid and time.time() * 1000 - int(focus.get("ts") or 0) <= 600000:
                rect = next((x for x in ax.window_list(on_screen_only=False) if x["id"] == int(wid)), None)
                if rect:
                    want = next(
                        (
                            w
                            for w in wins
                            if (w["pos"][0], w["pos"][1], w["size"][0], w["size"][1])
                            == (rect["x"], rect["y"], rect["w"], rect["h"])
                        ),
                        None,
                    )
        if want is None:
            return ""
        line = (
            f"窗口归属: [{want['index']}] {want['title'] or '无标题'} "
            f"@({want['pos'][0]},{want['pos'][1]}) {want['size'][0]}x{want['size'][1]}"
        )
        el = ax.window_element(root, want["index"])
        if el is None:
            return line
        try:
            if el.get("AXMain")[1] is True:
                line += "（当前窗，按键发给它）"
            elif el.perform("AXRaise") == 0:
                time.sleep(0.15)
                line += "（已抬到当前，按键发给它）"
        finally:
            el.dispose()
        if picked:
            _publish_focus(pid=pid, window_id=_cg_id_for_window(pid, int(want["index"])))
        # 抬窗会重排序号，用抬完之后的编号回报，模型下一次才能指对
        now = [w for w in ax.windows_of(root) if w["pos"] and w["size"]]
        fresh = _pick_window(now, want["title"] or str(want["index"]))
        return line if not fresh or fresh["index"] == want["index"] else (
            line.replace(f"[{want['index']}]", f"[{fresh['index']}]", 1) + f"（抬窗后序号 {want['index']}→{fresh['index']}）"
        )
    finally:
        root.dispose()


def _basis_note() -> str:
    """本次动作实际用的坐标基准。

    基准会跟着「最近一张截图」静默切换：全屏截图和单窗口截图的像素原点完全不同，
    模型以为还在用全屏坐标、实际按上一张窗口截图换算，实测就因此框选到了别的文件。
    所以每个坐标动作都把这行打在结果里。
    """
    size = _last_shot["size"]
    if not size:
        return "坐标系: 尚未截图（按屏幕点直接换算）"
    return f"坐标系: {_last_shot['basis'] or '全屏'} → 截图 {size[0]}x{size[1]}px"

# ---------- 动作层 / 渲染层 / 工具注册表（设计任务书 §3.1） ----------
# 三层分工（公共契约见 action_layer.py）：
#   动作层 `_do_*` —— 执行 + 采证，只返回 ActionResult，一句自然语言都不许有；
#   渲染层 `_render_*` —— 把 ActionResult 变成给模型看的自然语言；
#   `_register()` —— 把两者绑成一个 MCP 工具，同时登记进 `_ACTIONS` 给 sequence 复用。
# 「工具能做的」与「批处理能做的」从此是同一份清单：v1 的白名单只含坐标工具，
# 最该批处理的元素路径反而一步一回合。

_ACTIONS: dict = {}


def _install(spec) -> None:
    """把 ActionSpec 绑成一个 MCP 工具。

    签名与注解直接**借**动作层函数的：工具的 JSON Schema 和 sequence 内层调用因此永远是
    同一份参数表 —— v1 那种「工具声明了 hold_seconds、内层实现却没有、参数被静默丢弃」
    的漂移，在这个结构里根本表达不出来（mcp_wire_check.py 仍会核对一遍）。
    """
    def tool(**kwargs):
        return spec.render(spec.fn(**kwargs))

    tool.__name__ = spec.name
    tool.__qualname__ = spec.name
    tool.__doc__ = spec.description
    tool.__signature__ = inspect.signature(spec.fn)
    tool.__annotations__ = dict(typing.get_type_hints(spec.fn))
    mcp.tool()(tool)


#: 带 `binding_id` 的动作统一补这句（工具描述里不重复写 12 遍）
_BINDING_CLAUSE = (
    "\nbinding_id=最近一次 get_ax_state / get_app 回显的绑定令牌（可选）。服务器被同一 DSH 实例的多个会话"
    "共享，带上它可以在绑定被别人改过时挡住「照旧索引点到别的应用上」——对不上会返回 ERROR[BINDING_CHANGED]，"
    "提示你重新 get_app + get_ax_state。"
)


def _register(name: str, fn, render, description: str, risk: bool = True, read_only: bool = False):
    if "binding_id" in inspect.signature(fn).parameters:
        description += _BINDING_CLAUSE
    spec = ActionSpec(name=name, fn=fn, render=render, description=description, risk=risk, read_only=read_only)
    _ACTIONS[name] = spec
    _install(spec)
    return spec


# ---------- 动作层的公共件 ----------

def _hint_codes() -> list:
    """权限/观察窗这类「静默失败」的提示码（文案在渲染层，动作层只给码）。"""
    out = []
    if _ax_trusted() is False:
        out.append("AX_PERMISSION")
    if _panel_user_hidden():
        out.append("PANEL_HIDDEN")
    return out


def _tree_hash(pid) -> str | None:
    try:
        return ax.tree_hash(ax.Element.for_pid(int(pid)))
    except Exception:
        return None


def _baseline() -> tuple:
    """动作前的轻量界面指纹（只有绑定了应用才有基线）。"""
    pid = int(_bound_app["pid"]) if _bound_app else None
    return pid, (_tree_hash(pid) if pid else None)


def _evidence(pid, before) -> tuple:
    """(effect, 证据文本)。没基线（坐标动作 / 未绑定应用）时 effect 为 None。"""
    if pid is None or before is None:
        return None, ""
    after = _tree_hash(pid)
    if after is None:
        return None, ""
    return ("changed", "界面树已变化") if after != before else ("unchanged", "界面树逐字节一致")


_ERR_RE = re.compile(r"^ERROR\[([A-Z_]+)\]\s*:?\s*(.*)$", re.S)


def _fail_text(text: str) -> ActionResult:
    """把守卫（_aim_guard / _aim_keys）返回的 “ERROR[CODE]: 说明” 还原成结构化结果。"""
    m = _ERR_RE.match(str(text).strip())
    if m:
        return ActionResult(ok=False, error=m.group(1), message=m.group(2).strip(), possibly_sent=False)
    return ActionResult(ok=False, error="INTERNAL", message=str(text).strip(), possibly_sent=False)


# 真实指针的最近一次「已知位置」：我们自己的动作落点会更新它，于是「它自己动了」= 用户在动
_cursor_mark = {"pos": None, "ts": 0.0}


def _user_takeover() -> str:
    """真实指针在上一次动作之后自己动过 —— 多半是用户接管了（启发式，只提示不阻断，§3.6）。"""
    now = time.time()
    try:
        pos = cg.cursor_position()
    except Exception:
        return ""
    mark, ts = _cursor_mark["pos"], _cursor_mark["ts"]
    moved = bool(mark) and abs(pos[0] - mark[0]) + abs(pos[1] - mark[1]) > 12 and now - ts > 0.7
    _cursor_mark["pos"], _cursor_mark["ts"] = pos, now
    if not moved:
        return ""
    return (
        "⚠️ 检测到鼠标在上一次动作之后被移动过（可能用户正在接管这台电脑）：本次动作照常执行；"
        "如果用户确实接手了，先停下来问他，不要继续点。"
    )


def _mark_cursor() -> None:
    """我们自己的动作刚做完，把光标位置记成基线（免得把自己的移动误判成用户接管）。"""
    try:
        _cursor_mark["pos"], _cursor_mark["ts"] = cg.cursor_position(), time.time()
    except Exception:
        pass


def _interruptible_sleep(seconds: float) -> None:
    """可被急停打断的等待（100ms 一探；宿主断开与调用取消由 pause 负责）。"""
    left = max(0.0, float(seconds))
    while left > 0:
        killswitch.guard()
        step = min(0.1, left)
        pause(step)
        left -= step


def _wait_stable(limit_s: float) -> int:
    """等界面稳定，最多 limit_s 秒；稳定就提前返回。返回实际等待毫秒。

    `wait(until_stable=true)` 与 sequence 里的 wait 步走这条：固定睡死几秒会摊到每一步上，
    而「界面已经不动了」是随时可判的（与 `_settle` 同一套判据，只是上限放宽）。
    """
    started = time.monotonic()
    pid = int(_bound_app["pid"]) if _bound_app else None
    if pid is None:
        _interruptible_sleep(limit_s)
        return int((time.monotonic() - started) * 1000)
    pause(SETTLE_FLOOR)
    last = None
    while True:
        killswitch.guard()
        try:
            current = ax.tree_hash(ax.Element.for_pid(pid), max_nodes=200)
        except Exception:
            current = None
        if last is not None and current is not None and current == last:
            break
        last = current
        if time.monotonic() - started >= limit_s:
            break
        pause(SETTLE_STEP)
    return int((time.monotonic() - started) * 1000)


_STRATEGIES = ("auto", "a11y", "event")


def _strategy(value) -> str:
    s = str(value or "auto").strip().lower()
    if s not in _STRATEGIES:
        raise BadArgument(f"strategy 只能是 {'/'.join(_STRATEGIES)}，收到 {value!r}")
    return s


def _press_at_point(px: int, py: int, where: str) -> ActionResult:
    """a11y 策略的坐标点击：命中元素（或最近的可按祖先）→ AXPress，没有就报错。

    §3.3：`a11y` 不允许静默降级到坐标 —— 那等于「说好了不碰用户鼠标，还是碰了」。
    """
    try:
        el = ax.element_at_position(px, py)
    except Exception as e:
        return ActionResult(ok=False, error="INTERNAL", message=f"命中测试失败 {type(e).__name__}: {e}", possibly_sent=False)
    if el is None:
        return ActionResult(
            ok=False, error="ELEMENT_UNAVAILABLE", possibly_sent=False,
            message=f"{where} 处读不到无障碍元素（该应用可能没有暴露 AX 树）",
            detail={"hint": "改用 strategy=event 走合成点击，或先 get_app + get_ax_state 用元素索引。"},
        )
    chain = [el]
    label = ""
    try:
        cur = el
        for _ in range(5):
            ok_role, role = cur.get("AXRole")
            ok_title, title = cur.get("AXTitle")
            label = f"{role if ok_role else '?'} {title if ok_title and title else ''}".strip()
            acts = cur.action_names()
            if "AXPress" in acts:
                pid = cur.pid() or None
                before = _tree_hash(pid) if pid else None
                mark_sent()
                err = cur.perform("AXPress")
                if err != 0:
                    return ActionResult(
                        ok=False, error="ACTION_UNSUPPORTED", possibly_sent=False,
                        message=f"{where} 的元素 {label} AXPress 失败（{err_name(err)}）",
                        detail={"hint": "改用 strategy=event，或换一个元素索引操作。"},
                    )
                settle = _settle(pid)
                effect, ev = _evidence(pid, before)
                _note(f"🖱 语义点击 {label}")
                return ActionResult(
                    ok=True, effect=effect, evidence=ev, settle_ms=settle,
                    detail={"screen": (px, py), "element": label, "strategy": "a11y", "hints": _hint_codes()},
                )
            up = cur.parent()
            if up is None:
                break
            chain.append(up)
            cur = up
        return ActionResult(
            ok=False, error="ACTION_UNAVAILABLE", possibly_sent=False,
            message=f"{where} 的元素 {label or '(未知)'} 及其上 4 层祖先都没有 AXPress 语义动作",
            detail={"hint": "改用 strategy=event（合成点击），或先 get_ax_state 找一个可按的元素索引。"},
        )
    finally:
        for node in chain:
            try:
                node.dispose()
            except Exception:
                pass


# ---------- 坐标路径的动作 ----------

@action(risk=True)
def _do_click(x: int, y: int, button: str = "left", double: bool = False, force: bool = False, strategy: str = "auto", binding_id: str = "") -> ActionResult:
    """截图坐标点击：auto/event = 真实指针点击；a11y = 这一点的元素 + AXPress（不降级）。"""
    st = _strategy(strategy)
    px, py = _to_screen_point(int(x), int(y))
    top = _target_window(px, py)
    blocked = _aim_guard(px, py, bool(force), top)
    if blocked:
        return _fail_text(blocked)
    if st == "a11y":
        return _press_at_point(px, py, f"截图坐标 ({x},{y})")
    pid, before = _baseline()
    detail = {
        "x": int(x), "y": int(y), "screen": (px, py), "button": str(button), "double": bool(double),
        "basis": _basis_note(), "strategy": st, "hints": _hint_codes(),
    }
    notice = _user_takeover()
    if notice:
        detail["notice"] = notice
    # 点落在绑定应用自己的窗口上：先替它激活，再把这一扇窗抬上来 ——
    # 非 key window 的第一次点击只被系统用来激活窗口，模型会以为没点动而重复点。
    mine = bool(top and _bound_app and top["pid"] == _bound_app["pid"])
    if mine:
        focus = _ensure_frontmost()
        if _frontmost_pid() == _bound_app["pid"]:
            _raise_window_at(px, py)
        detail["focus"] = focus
    mark_sent()
    try:
        with _input():
            cg.click(px, py, button=button, double=bool(double))
    except cg.FailSafe as e:      # 光标在左上角：一次都没发出去
        return ActionResult(ok=False, error="FAILSAFE", message=str(e), possibly_sent=False)
    settle = _settle()
    _mark_cursor()
    _note(f"🖱 {'双击' if double else button + ' 单击'} ({x},{y})")
    effect, ev = _evidence(pid, before)
    return ActionResult(ok=True, effect=effect, evidence=ev, settle_ms=settle, detail=detail)


@action(risk=True)
def _do_scroll(x: int, y: int, direction: str = "down", amount: int = 5, strategy: str = "auto", binding_id: str = "") -> ActionResult:
    """滚动：auto/event 走滚轮事件；a11y 走元素的 AXScroll* 语义动作（没有就报错）。"""
    st = _strategy(strategy)
    px, py = _to_screen_point(int(x), int(y))
    clicks = max(1, min(int(amount), 20))
    if st == "a11y":
        return _scroll_semantic(px, py, direction, clicks)
    pid, before = _baseline()
    mark_sent()
    with _input():
        cg.scroll(px, py, clicks if direction == "up" else -clicks)
    settle = _settle()
    _mark_cursor()
    _note(f"🖱 滚动 {direction}")
    effect, ev = _evidence(pid, before)
    return ActionResult(
        ok=True, effect=effect, evidence=ev, settle_ms=settle,
        detail={"direction": direction, "amount": clicks, "screen": (px, py), "basis": _basis_note(),
                "strategy": st, "hints": _hint_codes()},
    )


def _scroll_semantic(px: int, py: int, direction: str, clicks: int) -> ActionResult:
    """a11y 滚动：命中元素（或祖先）上的 AXScrollDown/Up/Left/Right，没有就报错。"""
    action_name = {"up": "AXScrollUp", "down": "AXScrollDown",
                   "left": "AXScrollLeft", "right": "AXScrollRight"}.get(str(direction).lower(), "AXScrollDown")
    try:
        el = ax.element_at_position(px, py)
    except Exception as e:
        return ActionResult(ok=False, error="INTERNAL", message=f"命中测试失败 {type(e).__name__}: {e}", possibly_sent=False)
    if el is None:
        return ActionResult(
            ok=False, error="ELEMENT_UNAVAILABLE", possibly_sent=False,
            message=f"截图坐标 ({px},{py}) 处读不到无障碍元素",
            detail={"hint": "改用 strategy=event 走滚轮事件。"},
        )
    chain = [el]
    label = ""
    try:
        cur = el
        for _ in range(5):
            ok_role, role = cur.get("AXRole")
            ok_title, title = cur.get("AXTitle")
            label = f"{role if ok_role else '?'} {title if ok_title and title else ''}".strip()
            if action_name in cur.action_names():
                pid = cur.pid() or None
                before = _tree_hash(pid) if pid else None
                mark_sent()
                err = 0
                for _i in range(clicks):
                    err = cur.perform(action_name)
                    if err != 0:
                        break
                if err != 0:
                    return ActionResult(ok=False, error="ACTION_UNSUPPORTED", possibly_sent=False,
                                        message=f"{label} 的 {action_name} 失败（{err_name(err)}）")
                settle = _settle(pid)
                effect, ev = _evidence(pid, before)
                _note(f"🖱 语义滚动 {direction} {label}")
                return ActionResult(ok=True, effect=effect, evidence=ev, settle_ms=settle,
                                    detail={"direction": direction, "amount": clicks, "element": label,
                                            "strategy": "a11y", "hints": _hint_codes()})
            up = cur.parent()
            if up is None:
                break
            chain.append(up)
            cur = up
        return ActionResult(
            ok=False, error="ACTION_UNAVAILABLE", possibly_sent=False,
            message=f"{label or '(未知)'} 及其上 4 层祖先都没有 {action_name} 语义动作",
            detail={"hint": "改用 strategy=event 走滚轮事件（原生滚动区的语义滚动动作并非所有应用都实现）。"},
        )
    finally:
        for node in chain:
            try:
                node.dispose()
            except Exception:
                pass


@action(risk=True)
def _do_type_text(text: str, strategy: str = "auto", binding_id: str = "") -> ActionResult:
    """剪贴板粘贴输入；strategy 决定注入通道（a11y 不降级到合成事件）。"""
    st = _strategy(strategy)
    body = str(text)
    if not body:
        return ActionResult(ok=False, error="INVALID_ARGUMENT", message="text 不能为空", possibly_sent=False)
    try:
        subprocess.run(["pbcopy"], input=body.encode("utf-8"), check=True, timeout=10)
    except Exception as e:
        return ActionResult(ok=False, error="INTERNAL", possibly_sent=False,
                            message=f"写入剪贴板失败 {type(e).__name__}: {e}")
    focus = _ensure_frontmost() + _aim_keys()
    pid, before = _baseline()
    detail = {"chars": len(body), "clipboard": True, "strategy": st, "focus": focus, "hints": _hint_codes()}
    notice = _user_takeover()
    if notice:
        detail["notice"] = notice
    mark_sent()
    with _input():
        _hotkey(["command", "v"], strategy=st)
        _release_modifiers()
    settle = _settle()
    _mark_cursor()
    _note(f"⌨️ 输入 {len(body)} 字")
    effect, ev = _evidence(pid, before)
    return ActionResult(ok=True, effect=effect, evidence=ev, settle_ms=settle, detail=detail)


@action(risk=True)
def _do_press_key(key: str, hold_seconds: float = 0, window: str = "", strategy: str = "auto", binding_id: str = "") -> ActionResult:
    """按键；按键前先对齐窗口归属（见 _aim_keys）。"""
    st = _strategy(strategy)
    parts = [KEY_ALIASES.get(p.strip().lower(), p.strip().lower()) for p in str(key).split("+") if p.strip()]
    if not parts:
        return ActionResult(ok=False, error="INVALID_ARGUMENT", message="key 不能为空", possibly_sent=False)
    hold = max(0.0, min(float(hold_seconds or 0), 5.0))
    if hold > 0 and st == "a11y":
        return ActionResult(
            ok=False, error="ACTION_UNAVAILABLE", possibly_sent=False,
            message="长按只有合成事件路径（System Events 没有按下保持的语义）",
            detail={"hint": "长按请用 strategy=event 或 auto。"},
        )
    focus = _ensure_frontmost()
    aim = _aim_keys(window)
    if aim.startswith("ERROR["):
        # 显式指定了目标窗却匹配不上：宁可不发，也不能把按键丢给随便哪个前台窗
        return _fail_text(aim)
    focus += aim
    pid, before = _baseline()
    detail = {"key": str(key), "hold_seconds": hold, "strategy": st, "focus": focus,
              "basis": _basis_note(), "hints": _hint_codes()}
    notice = _user_takeover()
    if notice:
        detail["notice"] = notice
    mark_sent()
    with _input():
        if hold > 0:
            _hold_combo(parts, hold)
        else:
            try:
                _hotkey(parts, strategy=st)
            except Exception:
                if st == "a11y":
                    raise
                if len(parts) == 1:
                    cg.press(parts[0])
                else:
                    cg.hotkey(parts)
    settle = _settle()
    _mark_cursor()
    _note(f"⌨️ {key}")
    effect, ev = _evidence(pid, before)
    return ActionResult(ok=True, effect=effect, evidence=ev, settle_ms=settle, detail=detail)


@action(risk=True)
def _do_drag(from_x: int, from_y: int, to_x: int, to_y: int, duration: float = 0.5, force: bool = False, strategy: str = "auto", binding_id: str = "") -> ActionResult:
    """拖拽（只有合成事件路径；a11y 无对应语义动作，明确报错而不是装作成功）。"""
    st = _strategy(strategy)
    if st == "a11y":
        return ActionResult(
            ok=False, error="ACTION_UNAVAILABLE", possibly_sent=False,
            message="拖拽没有无障碍语义动作（AX 没有 drag）",
            detail={"hint": "拖拽请用 strategy=event 或 auto。"},
        )
    fx, fy = _to_screen_point(int(from_x), int(from_y))
    tx, ty = _to_screen_point(int(to_x), int(to_y))
    blocked = _aim_guard(fx, fy, bool(force))   # 拖拽起点同样受落点保护
    if blocked:
        return _fail_text(blocked)
    span = max(0.1, min(float(duration), 3.0))
    pid, before = _baseline()
    mark_sent()
    try:
        with _input():
            cg.drag(fx, fy, tx, ty, span)
    except cg.FailSafe as e:
        return ActionResult(ok=False, error="FAILSAFE", message=str(e), possibly_sent=False)
    settle = _settle()
    _mark_cursor()
    _note("🖱 拖拽")
    effect, ev = _evidence(pid, before)
    return ActionResult(ok=True, effect=effect, evidence=ev, settle_ms=settle,
                        detail={"from": (int(from_x), int(from_y)), "to": (int(to_x), int(to_y)),
                                "duration": span, "basis": _basis_note(), "hints": _hint_codes()})


@action(risk=True)
def _do_wait(seconds: float = 3, until_stable: bool = False) -> ActionResult:
    """等待：默认固定等待；until_stable=true 时等到界面稳定就提前返回（最多 seconds 秒）。"""
    s = max(0.5, min(float(seconds), 10))
    if until_stable:
        ms = _wait_stable(s)
        return ActionResult(ok=True, settle_ms=ms, detail={"seconds": s, "until_stable": True, "waited_ms": ms})
    _interruptible_sleep(s)
    return ActionResult(ok=True, settle_ms=int(s * 1000), detail={"seconds": s, "until_stable": False})


def _app_bundle_path(name: str) -> str | None:
    """把「备忘录 / 微信」这类本地化显示名解析成 .app 路径。

    `open -a` 只认英文注册名，中文显示名一律 exit 1 —— 而 macOS 的显示名存在
    Spotlight 里，所以查得到；查不到再退回到直接扫几个 Applications 目录。
    """
    safe = str(name).replace("\\", "").replace("'", "")
    query = f"kMDItemDisplayName == '{safe}' || kMDItemFSName == '{safe}.app'"
    try:
        out = subprocess.run(["mdfind", query], capture_output=True, text=True, timeout=10).stdout
        hits = [ln.strip() for ln in out.splitlines() if ln.strip().endswith(".app")]
    except Exception:
        hits = []
    roots = ["/Applications", str(Path.home() / "Applications"), "/System/Applications",
             "/Applications/Utilities", "/System/Applications/Utilities"]
    ranked = [h for r in roots for h in hits if h.startswith(r + "/")] + hits
    if ranked:
        return ranked[0]
    for root in roots:
        try:
            for app in Path(root).glob("*.app"):
                if app.stem.lower() == str(name).lower():
                    return str(app)
        except OSError:
            continue
    return None


@action(risk=True)
def _do_launch_app(name: str) -> ActionResult:
    """启动应用并验证进程真的活着（启动即崩溃要如实报告，不许反复重试）。"""
    name = str(name).strip()
    mark_sent()
    failure = ""
    try:
        subprocess.run(["open", "-a", name], check=True, capture_output=True, timeout=20)
    except subprocess.CalledProcessError as e:
        failure = (e.stderr or b"").decode("utf-8", "replace").strip() or f"exit {e.returncode}"
    except subprocess.TimeoutExpired:
        return ActionResult(ok=False, error="TIMEOUT", possibly_sent=True,
                            message=f"open -a {name} 超过 20 秒未返回（应用可能仍在启动）")
    except Exception as e:
        failure = f"{type(e).__name__}: {e}"
    resolved = None
    if failure:
        resolved = _app_bundle_path(name)
        if resolved:
            try:
                subprocess.run(["open", resolved], check=True, capture_output=True, timeout=20)
                failure = ""
            except Exception as e:
                failure = f"{type(e).__name__}: {e}"
    _interruptible_sleep(2.0)
    # 存活检查按「解析到的 bundle 路径」查：本地化显示名和真正的进程名往往不是一回事
    # （「备忘录」→ Notes、「计算器」→ Calculator），拿显示名 pgrep 会把启动成功误报成崩溃。
    probe = resolved or name
    alive = subprocess.run(["pgrep", "-x", probe], capture_output=True).returncode == 0
    if not alive:
        alive = subprocess.run(["pgrep", "-f", probe], capture_output=True).returncode == 0
    if alive:
        _note(f"🚀 启动 {name}")
        return ActionResult(ok=True, detail={"name": name, "via": resolved, "probe": probe})
    if failure:
        return ActionResult(
            ok=False, error="LAUNCH_FAILED", possibly_sent=True,
            message=(
                f"启动 {name} 失败：{failure}。`open -a` 需要英文注册名，中文名可再试一次（已自动按显示名查过 "
                "Spotlight），或用 list_apps 看正在运行的应用名、launch_app 传 bundle id（如 com.apple.Notes）。"
            ),
        )
    return ActionResult(
        ok=False, error="APP_CRASHED", possibly_sent=False,
        message=(
            f"open 命令成功，但 2 秒后检测不到名为 {name} 的进程——应用启动后立即自行退出了，"
            "是应用本身的问题，不是操作问题。直接把这个结论报告给用户即可：不要反复重试，"
            "也不要主动深入排查或尝试修复该应用，除非用户明确要求。"
        ),
    )


@action(risk=True)
def _do_run_shell(command: str) -> ActionResult:
    """跑一条 shell 命令（60 秒强杀线）。"""
    mark_sent()
    try:
        r = subprocess.run(str(command), shell=True, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return ActionResult(
            ok=False, error="TIMEOUT", possibly_sent=True,
            message="命令超过 60 秒被强杀。若是 GUI 程序/常驻进程，请改用 `open -a` 或 `nohup ... &` 后台运行，然后另行查看输出文件。",
        )
    return ActionResult(ok=True, detail={"exit": r.returncode, "stdout": r.stdout.strip()[:2500],
                                         "stderr": r.stderr.strip()[:1500]})


# ---------- 坐标路径的渲染层 ----------

def _render_click(r: ActionResult) -> str:
    if not r.ok:
        return render_error(r)
    d = r.detail
    what = "双击" if d.get("double") else f"{d.get('button', 'left')} 键单击"
    return (
        f"已在截图坐标 ({d.get('x')}, {d.get('y')}) 完成{what}。{d.get('basis', '')}。"
        f"{d.get('focus', '')}{effect_clause(r)}{tail(r)}"
    )


def _render_scroll(r: ActionResult) -> str:
    if not r.ok:
        return render_error(r)
    d = r.detail
    how = f"（语义动作 {d.get('element')}）" if d.get("strategy") == "a11y" else ""
    where = f"{d.get('basis')}。" if d.get("basis") else ""
    return f"已向{d.get('direction')}滚动 {d.get('amount')} 格{how}。{where}{effect_clause(r)}{tail(r)}"


def _render_type_text(r: ActionResult) -> str:
    if not r.ok:
        return render_error(r)
    return (
        f"已通过剪贴板粘贴 {r.detail.get('chars')} 个字符（原剪贴板内容已被覆盖）。"
        f"{r.detail.get('focus', '')}{effect_clause(r)}{tail(r)}"
    )


def _render_press_key(r: ActionResult) -> str:
    if not r.ok:
        return render_error(r)
    hold = r.detail.get("hold_seconds") or 0
    return (
        f"已按下 {r.detail.get('key')}" + (f"（按住 {hold} 秒后松开）" if hold else "") + "。"
        f"{r.detail.get('focus', '')}{dash(r)}{effect_clause(r)}{tail(r)}"
    )


def dash(r: ActionResult) -> str:
    """坐标基准那一行（只在有坐标含义的动作里出现）。"""
    basis = r.detail.get("basis")
    return f"{basis}。" if basis else ""


def _render_drag(r: ActionResult) -> str:
    if not r.ok:
        return render_error(r)
    d = r.detail
    return (
        f"已从 ({d['from'][0]},{d['from'][1]}) 拖拽到 ({d['to'][0]},{d['to'][1]})。{d.get('basis', '')}。"
        f"{effect_clause(r)}如需确认请 screenshot。{tail(r)}"
    )


def _render_wait(r: ActionResult) -> str:
    if not r.ok:
        return render_error(r)
    if r.detail.get("until_stable"):
        return f"已等待 {r.detail.get('waited_ms')} 毫秒（界面稳定后提前返回；上限 {r.detail.get('seconds')} 秒）。"
    return f"已等待 {r.detail.get('seconds')} 秒。"


def _render_launch(r: ActionResult) -> str:
    if not r.ok:
        return render_error(r)
    via = f"（按显示名解析到 {r.detail['via']}）" if r.detail.get("via") else ""
    return f"已启动应用 {r.detail.get('name')}{via}（进程运行中）。"


def _render_shell(r: ActionResult) -> str:
    if not r.ok:
        return render_error(r)
    d = r.detail
    out = f"exit code: {d.get('exit')}\nstdout: {d.get('stdout', '')}"
    if d.get("stderr"):
        out += f"\nstderr: {d['stderr']}"
    return out


_register(
    "click", _do_click, _render_click,
    "移动鼠标并点击。x/y 是最近一张截图上的像素坐标；button 可选 left/right；double=true 为双击。\n"
    "strategy: auto/event=真实指针点击（默认，坐标工具的本义）；a11y=改为「这一点上的元素 + AXPress 语义动作」，"
    "该元素没有语义动作时返回 ERROR[ACTION_UNAVAILABLE] 而**不降级**（不污染用户鼠标）。\n"
    "已绑定目标应用时，若这一下实际会落到别的窗口上，会返回 ERROR[NOT_FRONTMOST] 并且**不点**"
    "—— 先 focus_app / focus_window，或者确实要点那个窗口时带 force=true。",
)
_register(
    "scroll", _do_scroll, _render_scroll,
    "把鼠标移到指定位置并滚动滚轮。direction 为 up/down，amount 为滚动格数（1~20）。"
    "strategy=a11y 时改用元素的 AXScrollUp/Down 语义动作（后台应用也生效、不动用户鼠标），"
    "该元素没有这个动作就返回 ERROR[ACTION_UNAVAILABLE] 而**不降级**。",
)
_register(
    "type_text", _do_type_text, _render_type_text,
    "向当前聚焦的输入框输入文字（剪贴板粘贴实现，支持中文等任意字符）。输入前必须先点击输入框获得焦点。"
    "注意会覆盖当前剪贴板内容。strategy=a11y 只用 System Events（无障碍）通道，失败即报错；"
    "event 强制合成 CGEvent；auto（默认）优先 System Events、失败才降级。",
)
_register(
    "press_key", _do_press_key, _render_press_key,
    "按下单个按键或组合键。`window=` 指定按键发给哪扇窗：窗口标题（推荐，如 `window=\"下载\"`）"
    "或 get_app 里的序号；留空则沿用上一条指令的目标窗。序号会在抬窗后重排，多窗口时优先用标题。"
    "单个键如 return、esc、tab、space、delete、up/down/left/right；组合键用 + 连接，如 cmd+space、cmd+w、cmd+shift+t；"
    "hold_seconds=按住时长秒（0~5，长按场景用，只有合成事件路径）。"
    "strategy: auto=优先 System Events；event=强制合成事件；a11y=只用 System Events 且失败即报错。\n"
    "注意：部分应用会忽略合成的菜单快捷键——按键后如界面无变化（对照 effect 证据或截图），"
    "改用 click 点击界面上的对应按钮。",
)
_register(
    "drag", _do_drag, _render_drag,
    "按截图像素坐标拖拽（坐标约定同 click，同样带 NOT_FRONTMOST 保护）。返回 effect 提示。"
    "拖拽只有合成事件路径：strategy=a11y 会明确返回 ERROR[ACTION_UNAVAILABLE]。",
)
_register(
    "wait", _do_wait, _render_wait,
    "等待界面加载/应用响应，最多 10 秒。until_stable=true 时等到界面稳定就提前返回（不再固定睡满）。"
    "等待后应截图或 get_ax_state 确认。",
)
_register(
    "launch_app", _do_launch_app, _render_launch,
    "用 open -a 启动 macOS 应用（如 Notes、Safari、Finder、Qoder），并验证进程真的存活。"
    "若返回 APP_CRASHED（检测不到进程），说明应用启动即崩溃，不要反复重试。",
)
_register(
    "run_shell", _do_run_shell, _render_shell,
    "执行一条 shell 命令并返回输出（60 秒超时，超时会强杀进程）。"
    "不要前台运行 GUI 程序或常驻进程——它们不退出会白白卡满 60 秒；长任务用 `nohup ... > /tmp/x.log 2>&1 &` 放到后台，再单独查日志。"
    "也不要用它执行删除、付款、对外发送等不可逆操作。",
)

# ---------- MCP 工具定义 ----------

@mcp.tool()
def sequence(steps: list, on_unchanged: str = "continue", step_timeout_ms: int = 5000, sequence_timeout_ms: int = 20000, binding_id: str = ""):
    """一轮多步：按顺序执行一串动作（元素路径与坐标路径都可以），一次调用跑完整条链路。

    steps 每项形如：
      {"tool": "click_element", "args": {"index": 42}}
      {"tool": "type_text",     "args": {"text": "你好"}}
      {"tool": "press_key",     "args": {"key": "return"}}
      {"observe": {"diff": true, "screenshot": false}}      // 观察伪步：插入一次 get_ax_state
    可用 tool：除 sequence / stop / resume / release_lease / preview 之外的任何动作工具
    （click / click_element / type_text / press_key / scroll / drag / paste / set_value /
    perform_action / select_text / launch_app / focus_app / focus_window / wait / run_shell /
    get_app / get_ax_state / elements / list_apps / list_windows / screenshot）。

    规则：
    1. 元素索引自**最近一次观察**（get_ax_state / elements / observe 伪步）。批处理内部不插 observe
       时索引保持有效；一旦插了 observe，**之后的步骤必须用新索引**（结果里会提示）。
    2. on_unchanged="abort" 时，某步 effect=unchanged 就停下（默认 continue，只提示）。
    3. 失败默认停止：ELEMENT_UNAVAILABLE / STALE_STATE 会提示重新观察，不会拿旧索引硬点。
    4. possibly_sent=true 时立即停止 —— 那一步可能已经下发，必须先观察再决定（不要重复同一动作）。
    5. 已经执行过的步骤会**完整列出**：这个列表就是已经发生的副作用，别当成「什么都没做」。
    6. 每步返回真实 settle_ms；wait 步走「界面稳定即返回」，固定秒数只是上限。
    7. step_timeout_ms 是**每步的等待预算**（settle 不会超过它），sequence_timeout_ms 是总预算
       （在步与步之间检查）—— 单个已下发的 CGEvent/AX 调用不可抢占，所以这是预算而不是强杀线。
    8. 一次结果最多一张栅格图：只有最后一个 observe / screenshot 步骤会附图。
    9. binding_id=最近一次 get_ax_state / get_app 回显的绑定令牌（可选）：整批动作都打在同一个绑定上，
       带上它可以在别的会话改过绑定之后**在第一步之前**就拒绝整批，而不是照着旧索引点到别的应用上。"""
    stale = action_layer.check_binding(binding_id)
    if stale:
        return (f"ERROR[BINDING_CHANGED]: {stale}\n"
                "整批没有执行（0 步），先重新 get_app + get_ax_state，再用新索引重发。")
    return _run_sequence(steps, on_unchanged, step_timeout_ms, sequence_timeout_ms, binding_id)


#: 当前步骤的等待预算（毫秒）；`_settle` 读它。None = 用默认 ACTION_DELAY。
_SETTLE_BUDGET_MS = [None]


def _indent(text: str, prefix: str = "   ") -> str:
    return "\n".join(prefix + ln for ln in str(text).splitlines() if ln.strip())


def _seq_render(spec, res: ActionResult, is_last: bool) -> tuple:
    """渲染一步的结果 → (文本, 附图路径或 None)。非末步的图一律丢掉（§7 风险 5）。"""
    try:
        rendered = spec.render(res)
    except Exception as e:  # 渲染层自己炸了不能吃掉这一步的结果
        return f"（渲染失败 {type(e).__name__}: {e}）", None
    image = None
    if isinstance(rendered, Blocks):
        texts = []
        for item in rendered.items:
            if isinstance(item, Image):
                image = item.path
            elif item is not None:
                texts.append(str(item))
        text = "\n".join(texts)
    else:
        text = str(rendered)
    if image and not is_last:
        text += "\n（本步的截图已丢弃：一次结果最多一张栅格图，只有最后一步会附图）"
        image = None
    return text, image


def _seq_observe(step: dict, is_last: bool, total: int, index: int):
    """观察伪步：`{"observe": {"diff": true, "screenshot": false, "max_nodes": 400}}`。"""
    opts = step.get("observe")
    if not isinstance(opts, dict):
        opts = {}
    try:
        window = int(opts.get("window", -1))
        max_nodes = int(opts.get("max_nodes", 400))
    except (TypeError, ValueError):
        return ActionResult(ok=False, error="INVALID_ARGUMENT", possibly_sent=False,
                            message="observe 的 window / max_nodes 需要是整数"), None
    want_shot = bool(opts.get("screenshot", False))
    res = _do_observe(
        app=str(step.get("app") or ""),
        window=window,
        full=not bool(opts.get("diff", True)),
        max_nodes=max_nodes,
        screenshot=want_shot and is_last,
    )
    image = res.detail.get("image") if res.ok else None
    return res, image


def _required_args(fn) -> list:
    """动作层的必填参数（= 工具声明的 required）：批处理里缺参数要回 INVALID_ARGUMENT，
    而不是让 TypeError 落到 INTERNAL 里 —— 后者对模型来说不可自纠。"""
    return [p.name for p in inspect.signature(fn).parameters.values() if p.default is inspect.Parameter.empty]


def _run_sequence(steps, on_unchanged, step_timeout_ms, sequence_timeout_ms, binding_id: str = ""):
    if not isinstance(steps, list) or not steps:
        return (
            'ERROR[INVALID_ARGUMENT]: steps 必须是非空数组，每项形如 {"tool": "click_element", "args": {"index": 42}} '
            '或观察伪步 {"observe": {"diff": true}}。'
        )
    mode = str(on_unchanged or "continue").strip().lower()
    if mode not in ("continue", "abort"):
        return f"ERROR[INVALID_ARGUMENT]: on_unchanged 只能是 continue / abort，收到 {on_unchanged!r}"
    try:
        step_budget = max(200, min(int(step_timeout_ms), 30000))
        total_budget = max(500, min(int(sequence_timeout_ms), 120000))
    except (TypeError, ValueError):
        return "ERROR[INVALID_ARGUMENT]: step_timeout_ms / sequence_timeout_ms 需要是整数毫秒。"

    started = time.monotonic()
    deadline = started + total_budget / 1000.0
    total = len(steps)
    lines: list = []
    records: list = []
    stopped_at, stop_reason, image = None, "", None

    for i, raw in enumerate(steps, 1):
        if time.monotonic() > deadline:
            stopped_at, stop_reason = i, f"总预算 sequence_timeout_ms={total_budget} 已用完"
            break
        step = raw if isinstance(raw, dict) else {}
        is_last = i == total

        # ---- 观察伪步 ----
        if "observe" in step:
            res, img = _seq_observe(step, is_last, total, i)
            records.append({"i": i - 1, "observe": True, "ok": res.ok, "effect": res.effect})
            if not res.ok:
                lines.append(f"{i}. [observe] ✗ {render_error(res)}")
                stopped_at, stop_reason = i, "观察失败（先修好观察再继续动作）"
                break
            lines.append(f"{i}. [observe] ✓ 元素索引已按本次观察更新，后续步骤必须用新索引")
            lines.append(_indent(res.detail.get("state") or ""))
            if img:
                image = img
            elif bool((step.get("observe") or {}).get("screenshot")) and not is_last:
                lines.append("   （非末步的 observe 不附图：一次结果最多一张栅格图）")
            continue

        # ---- 普通动作步 ----
        name = str(step.get("tool") or "")
        args = step.get("args") if isinstance(step.get("args"), dict) else {}
        spec = _ACTIONS.get(name)
        if spec is None:
            lines.append(
                f"{i}. [{name}] ✗ ERROR[INVALID_ARGUMENT]: 未知工具，可用: {', '.join(sorted(_ACTIONS))}；已停止后续步骤。"
            )
            stopped_at, stop_reason = i, f"未知工具 {name!r}"
            break
        if name == "wait" and "until_stable" not in args:
            args = {**args, "until_stable": True}   # 批处理里的 wait 走稳定判定，不固定睡满
        # 顶层令牌下发给每一步：整批就是一个逻辑动作，它的绑定是同一份。顺带让每一步自己
        # 也过一遍绑定校验（纵深防御），并免掉每步那句「本次没带 binding_id」的噪声。
        if binding_id and "binding_id" not in args and "binding_id" in inspect.signature(spec.fn).parameters:
            args = {**args, "binding_id": binding_id}
        missing = [k for k in _required_args(spec.fn) if k not in args]
        if missing:
            res = ActionResult(ok=False, error="INVALID_ARGUMENT", possibly_sent=False,
                               message=f"{name} 缺少必填参数: {', '.join(missing)}",
                               detail={"hint": f"这一步的 args 要带齐 {_required_args(spec.fn)}。"})
            _SETTLE_BUDGET_MS[0] = None
        else:
            _SETTLE_BUDGET_MS[0] = step_budget
            try:
                res = spec.fn(**coerce_for(spec.fn, args))
            except BadArgument as e:
                res = ActionResult(ok=False, error="INVALID_ARGUMENT", message=str(e), possibly_sent=False)
            except killswitch.Killed as e:
                res = ActionResult(ok=False, error="KILLED", message=str(e), possibly_sent=False)
            except AXBridgeError as e:
                res = ActionResult(ok=False, error=e.code, message=e.detail, possibly_sent=False)
            except Exception as e:  # noqa: BLE001
                res = ActionResult(ok=False, error="INTERNAL", message=f"{type(e).__name__}: {e}", possibly_sent=False)
            finally:
                _SETTLE_BUDGET_MS[0] = None

        records.append({"i": i - 1, "tool": name, "ok": res.ok, "effect": res.effect,
                        "settle_ms": res.settle_ms, "possibly_sent": res.possibly_sent})
        lines.append(f"{i}. [{name}] {step_line(res)}")
        text, img = _seq_render(spec, res, is_last)
        if text.strip():
            lines.append(_indent(text))
        if img:
            image = img

        if res.effect == "unchanged":
            lines.append("   （未观察到界面变化：换一种方式或先观察，不要重复同一动作）")
        if res.error == "KILLED":
            stopped_at, stop_reason = i, "急停生效（kill switch）"
            break
        if res.possibly_sent:
            stopped_at, stop_reason = i, (
                "这一步可能已经下发（possibly_sent=true）：先 get_ax_state 确认实际状态，**不要重复同一动作**"
            )
            break
        if not res.ok:
            advice = "；建议重新 get_ax_state 拿新索引后再试" if res.error in ("ELEMENT_UNAVAILABLE", "STALE_STATE") else ""
            stopped_at, stop_reason = i, f"{res.error}{advice}"
            break
        if res.effect == "unchanged" and mode == "abort":
            stopped_at, stop_reason = i, "on_unchanged=abort：这一步没有产生界面变化"
            break

    elapsed = int((time.monotonic() - started) * 1000)
    if stopped_at is None:
        head = f"sequence 完成：{len(records)} 步全部执行，耗时 {elapsed}ms。"
    else:
        head = (
            f"sequence 在第 {stopped_at} 步停止（{stop_reason}），已执行 {len(records)} 步、耗时 {elapsed}ms。"
            "上面每一步的结果就是**实际已经发生**的副作用清单，不要当成「什么都没做」。"
        )
    body = "\n".join([head, *lines])
    return Blocks(body, Image(path=image)) if image else body


@mcp.tool()
def screenshot(display: int = 1, window_id: int = 0):
    """截取屏幕并返回图片。执行结果不确定时先截图观察；坐标以这张图为准。
    display=显示器序号（多显示器时用，清单见 screen_info，默认 1 主屏）；
    window_id=只截某个窗口（id 见 list_windows，坐标同样以这张截图为准）。"""
    return _render_screenshot(_do_screenshot(display=display, window_id=window_id))


@mcp.tool()
def screen_info() -> str:
    """返回显示器清单、坐标换算基准、macOS 权限状态、急停开关与控制器租约状态。
    点击/输入疑似没生效时先调它自查。"""
    sw, sh = cg.main_size()
    last = _last_shot["size"]
    last_txt = f"{last[0]}x{last[1]}" if last else "尚未截图"
    ax_ok = _ax_trusted()
    scr = _screen_recording_ok()
    bound = f"{_bound_app['name']}（pid={_bound_app['pid']}）" if _bound_app else "未绑定（用 get_app 绑定）"
    lines = [
        f"主屏逻辑尺寸: {sw}x{sh}（点）；最近截图尺寸: {last_txt}（像素）。"
        "click 等工具输入截图像素坐标，服务器按截图所属显示器/窗口自动换算成全局坐标。",
        "显示器: " + "; ".join(
            f"[{d['index']}] {d['size_pt'][0]}x{d['size_pt'][1]} 左上角@({d['topleft'][0]},{d['topleft'][1]})"
            for d in _displays()
        ),
        f"辅助功能权限（点击/键盘必需）: {'已授予' if ax_ok else '未授予（系统设置→隐私与安全性→辅助功能→添加 DeepSeek Harness）' if ax_ok is False else '未知'}",
        f"屏幕录制权限（截屏必需）: {'已授予' if scr else '未授予（系统设置→隐私与安全性→屏幕录制→添加 DeepSeek Harness）' if scr is False else '未知'}",
        f"已绑定应用: {bound}" + (f"（binding_id={_binding['id']}）" if _binding["id"] else ""),
        "会话隔离: ⚠️ 这个服务器进程被本 DSH 实例的**所有会话**共用，绑定/租约/观察器都是进程内共享状态；"
        "动作带 binding_id 才能在绑定被别人改掉时被拦住（ERROR[BINDING_CHANGED]）。",
        killswitch.state_line(),
        f"控制器租约: {lease.describe()}",
    ]
    return "\n".join(lines)


@mcp.tool()
def stop(reason: str = "") -> str:
    """急停（kill switch）：立刻停止一切动作、进入只读，直到 resume()。

    触发后所有**动作类**工具返回 ERROR[KILLED]（run_shell / launch_app / sequence 也在内），
    观察类工具（get_ax_state / screenshot / list_apps / screen_info …）仍然可用。
    正在跑的等待与批处理会在下一个可中断点（≤0.1 秒）退出；**已经下发的动作不会被撤回**。
    同时释放控制器租约，别的会话或用户可以立刻接管。
    用户侧还有两个等价入口：预览浮窗上的 ⏹ 键、host 半区的 /_computer-use/stop 路由。
    只在真有危险或用户明确要求时用它；用过之后要给用户一个明确的恢复说明。"""
    why = killswitch.stop(reason)
    if why.startswith("ERROR["):
        return why
    lease.release()
    return (
        f"已进入只读（急停）：{why}。动作类工具从此返回 ERROR[KILLED]，观察类工具照常可用；"
        "恢复用 resume()，或在预览浮窗上按 ▶。"
    )


@mcp.tool()
def resume() -> str:
    """解除急停（stop() 或浮窗 ⏹ 触发的只读状态），恢复动作类工具。"""
    err = killswitch.resume()
    if err:
        return err
    return "已解除急停：动作类工具恢复可用。（若刚才是用户按的停止键，先确认他确实要你继续，再往下做。）"


@mcp.tool()
def release_lease(binding_id: str = "") -> str:
    """显式释放控制器租约（「谁在操作这台电脑」的那份仲裁）。进程退出会自动释放。

    binding_id=你在 get_app / get_ax_state 里拿到的那枚令牌（可选）：服务器被多个会话共享，
    带上它就能保证你释放的是**自己那份**租约 —— 对不上会拒绝，而不是把另一个会话的控制器夺走。"""
    if binding_id and str(binding_id) != _binding["id"]:
        return (
            f"ERROR[BINDING_CHANGED]: 你带的是 {binding_id}，当前绑定是 {_binding['id']} {_binding_app()}。"
            "这份租约现在属于另一个会话，不能由你释放（否则会把它的控制器夺走）。"
            "如果你确实要操作这台电脑，先 get_app 重新绑定；确实要交还控制权就直接 release_lease() 不带令牌。"
        )
    held = lease.mine()
    if lease.release():
        return f"已释放控制器租约 {held.get('lease_id')}（目标 {held.get('target_name') or held.get('target_pid')}）。"
    return "当前没有持有控制器租约（可能是因为从没 get_app 绑定过应用）。"

# ---------------------------------------------------------------------------
# 无障碍（AX）元素级操作 —— 优先路径：后台可读可点，不占用用户的鼠标
# ---------------------------------------------------------------------------

_bound_app: dict | None = None

# 动作层的租约检查要知道「这一下打在哪个 pid 上」；动作层不该知道 _bound_app 在哪儿，
# 所以由这里把读法注册进去（依赖倒置）。
action_layer.bind_lease_target(lambda: int(_bound_app["pid"]) if _bound_app else None)


# ---- 绑定令牌（任务书 §4.2）---------------------------------------------------
# 这个服务器是**宿主级单例**：一个 DSH 实例里所有会话共用它，所以 `_bound_app` 天生是共享
# 状态，「会话 A 的动作打到会话 B 绑定的应用上」在结构上是可能的（真实事故，见任务书 §1）。
# 绑定令牌把这件事从静默变成可检测：
#   - `get_app`（以及任何改绑定的调用）换一个新令牌；
#   - 观察类工具把当前令牌回显给模型；
#   - 动作类工具可以带上它 —— 对不上就**拒绝**（BINDING_CHANGED），而不是照旧索引点到别的应用上。
# 它**不是**隔离：真正的隔离要「每个 agent 一个服务器进程」，见 README「多会话」一节。
_binding = {"id": "", "pid": None, "name": "", "seq": 0, "at": 0.0}


def _bind_app(info: dict) -> str:
    """把绑定指向 info 并**换一个新令牌**；返回新令牌。

    同时抬一次索引纪元（`ax_bridge.bump_index_epoch`）：换绑之后，之前发出去的索引在数值上
    就与新的那棵树不重叠了 —— 会话 A 的旧索引在会话 B 改过绑定之后只会得到 ELEMENT_UNAVAILABLE，
    而不是在新应用的树里命中同号元素（任务书 §2.4 那个「静默点错应用」）。
    """
    global _bound_app
    _bound_app = info
    try:
        ax.bump_index_epoch()
    except Exception:       # AX 层没加载/不可用也不能让绑定本身失败
        pass
    _binding["seq"] += 1
    _binding.update({
        "id": f"b{_binding['seq']}-{secrets.token_hex(3)}",
        "pid": int(info["pid"]),
        "name": info.get("name") or "",
        "at": time.time(),
    })
    return _binding["id"]


def _binding_app() -> str:
    return f"{_binding['name']}（pid={_binding['pid']}）" if _binding["id"] else "（还没绑定过应用）"


def _binding_line() -> str:
    """观察类工具回显的一行：模型要把这个令牌带在后续动作上。"""
    if not _binding["id"]:
        return ""
    return (
        f"\n🔗 binding_id={_binding['id']}（目标 {_binding_app()}）—— 后续动作请带上它；"
        "它一变就说明绑定被改过（同一 DSH 实例里的另一个会话 get_app 了），旧索引不能再用了。"
    )


def _check_binding(value) -> str | None:
    """动作前的绑定校验：对不上返回错误说明（渲染成 ERROR[BINDING_CHANGED]）。

    没带令牌（`""`）= 放行：这是**可选**参数，老会话与纯坐标路径不受影响；带上才有保护。
    """
    if value in (None, "", "-1"):
        return None
    if str(value) == _binding["id"]:
        return None
    return (
        f"你的 binding_id={value} 已失效：当前绑定是 {_binding['id']} {_binding_app()}。"
        "这个服务器被同一 DSH 实例的多个会话共享，另一个会话在这期间重新 get_app 了 —— "
        "你手里的元素索引已经不是那个应用的索引，照旧操作会点到别的应用上。请重新 get_app + get_ax_state 再动手。"
    )


def _binding_note(value) -> str:
    """没带令牌时的提醒（只在真的绑定过应用、也就是有索引可用时才加，避免无谓噪声）。"""
    if value not in (None, "", "-1") or not _binding["id"]:
        return ""
    return (
        f"（本次没带 binding_id：当前绑定 {_binding['id']} {_binding_app()}；"
        "带上它才能在绑定被别的会话改掉时被拦住。）"
    )


action_layer.bind_binding_checker(_check_binding)
action_layer.bind_binding_note(_binding_note)


def _resolve_app(spec: str = "", bind: bool = True) -> dict:
    """解析目标应用：显式 spec > 已绑定 > 前台应用。

    `bind=False` = 只解析不绑定（`list_windows(app=…)` 这种「按应用过滤」的只读查询）：
    既不动 `_bound_app`，也不换绑定令牌 —— 过滤不该有副作用。
    """
    if spec:
        info = ax.find_app(spec)
        if bind:
            _bind_app(info)
        return info
    if _bound_app:
        try:
            os.kill(int(_bound_app["pid"]), 0)  # 还在就不必重新解析（running_apps 要问 LaunchServices）
            return _bound_app
        except OSError:
            pass      # 应用没了：下面退回前台应用（并换令牌）
    info = ax.find_app("front")
    if bind:
        _bind_app(info)
    return info


def _lease_block(what: str, granted: dict) -> ActionResult:
    """租约被别人活着持有时的统一回包（§3.6：返回 CONTROLLER_BUSY + 持有者信息）。"""
    holder = granted.get("blocked_by") or {}
    return ActionResult(
        ok=False, error="CONTROLLER_BUSY", possibly_sent=False,
        message=(
            f"{lease.describe_holder(holder)} 正在操作这台电脑，"
            f"{what} 的动作会被仲裁拦住。你仍然可以 get_ax_state 观察。"
        ),
        detail={"hint": "等它收工（剩余时间见上）、换一个应用，或确认那个会话已经不用电脑时用 force=true 抢占（会写日志）。"},
    )


@action(risk=False)
def _do_list_apps() -> ActionResult:
    return ActionResult(ok=True, detail={"apps": ax.running_apps()})


def _render_list_apps(r: ActionResult) -> str:
    if not r.ok:
        return render_error(r)
    lines = [
        f"{a['name']}  |  bundle: {a['bundle'] or '-'}  |  pid: {a['pid']}" + ("  [前台]" if a["active"] else "")
        for a in r.detail.get("apps") or []
    ]
    return "\n".join(lines) or "(无 GUI 应用)"


@action(risk=False)
def _do_get_app(app: str = "", force: bool = False, focus: bool = False) -> ActionResult:
    """绑定应用（`focus=true` 顺带带到前台），并申请控制器租约。

    绑定本身是只读的：租约被别人占着时仍然绑定（观察可用），但返回 CONTROLLER_BUSY，
    后续动作会被仲裁拦住 —— 「能看不能动」比「连看都不让看」有用得多。
    """
    info = ax.find_app(app)
    binding = _bind_app(info)      # 换绑定令牌：之前的索引与令牌一起作废（任务书 §4.2）
    app_el = ax.Element.for_pid(info["pid"])
    try:
        ax.enable_chromium_accessibility(app_el)  # Electron/Chromium 首次需要强制开启 AX
        wins = ax.windows_of(app_el)
    finally:
        app_el.dispose()
    _publish_focus(pid=info["pid"])
    granted = lease.acquire(int(info["pid"]), target_name=info["name"], force=bool(force))
    detail = {"app": info, "windows": wins, "lease": granted, "binding_id": binding}
    if focus:
        detail["activated"] = _activate_pid(info["pid"])
    if not granted.get("ok"):
        return _with_detail(_lease_block(f"{info['name']}（pid={info['pid']}）", granted), detail)
    return ActionResult(ok=True, detail=detail)


def _with_detail(result: ActionResult, detail: dict) -> ActionResult:
    """给失败结果补上结构化细节（渲染层要拿窗口列表/租约信息说话）。"""
    result.detail = {**detail, **result.detail}
    return result


def _render_get_app(r: ActionResult) -> str:
    if not r.detail.get("app"):
        # 解析应用时就炸了（APP_NOT_FOUND / AMBIGUOUS_APP…）：detail 里没有窗口可列，
        # 原样把类型化错误交出去，别渲染成一个「已绑定 None」的假结果。
        return render_error(r)
    d = r.detail
    info = d.get("app") or {}
    lease_rec = d.get("lease") or {}
    head = f"已绑定 {info.get('name')}（pid={info.get('pid')}，bundle={info.get('bundle') or '-'}）"
    wlines = [
        f"  窗口[{w['index']}] {w['title']}"
        + (f"  @({w['pos'][0]},{w['pos'][1]}) {w['size'][0]}x{w['size'][1]}" if w["pos"] and w["size"] else "")
        for w in (d.get("windows") or [])
    ]
    body = "\n".join(wlines) or "  (无可见窗口)"
    if not r.ok:
        return (f"{head}，但动作能力被仲裁拦住：{r.message}\n窗口：\n{body}\n"
                f"{r.detail.get('hint', '')}{_binding_line()}{tail(r)}")
    out = f"{head}\n窗口：\n{body}\n{lease.describe()}"
    if lease_rec.get("warning"):
        out += f" ⚠️ {lease_rec['warning']}"
    if d.get("activated") is False:
        out += f"\n⚠️ 已发送激活指令但无法确认 {info.get('name')} 在前台（部分系统限制自动激活）；可手动点一下它的窗口再继续。"
    return out + _binding_line() + "\n现在可以 get_ax_state 观察它的界面元素。"


@action(risk=False)
def _do_observe(app: str = "", window: int = -1, full: bool = False, max_nodes: int = 400,
                screenshot: bool = False, binding_id: str = "") -> ActionResult:
    """观察界面：元素树（diff / 全量）+ 可选一张截图 —— 一次调用拿两样（§3.7）。

    `binding_id` 在这里**不做拦截**（观察是无害的，而且模型正需要看到当前这棵树才能重新对表），
    但带错了会在结果里醒目地说明「下面这棵树不是你要观察的那个应用」（任务书 §5.1 的精神：
    宁可啰嗦，也不要让模型以为自己在看计算器、实际看的是别的应用）。
    """
    info = _resolve_app(app)
    obs = ax.observer_for(info["pid"])
    root = ax.Element.for_pid(info["pid"])
    win_el = None
    try:
        ax.enable_chromium_accessibility(root)
        if int(window) >= 0:
            win_el = ax.window_element(root, int(window))
            if win_el is None:
                return ActionResult(
                    ok=False, error="WINDOW_NOT_FOUND", possibly_sent=False,
                    message=f"窗口索引 {window} 不存在（该应用共 {len(ax.windows_of(root))} 个窗口）",
                    detail={"hint": "用 get_app 看最新窗口清单（序号会随抬窗重排）。"},
                )
        try:
            state = obs.observe(full=bool(full), window=win_el, max_nodes=max(50, min(int(max_nodes), 2000)))
        finally:
            if win_el is not None:
                win_el.dispose()
    finally:
        root.dispose()
    detail = {"state": state, "app": info, "hints": _hint_codes()}
    if binding_id and str(binding_id) != _binding["id"]:
        detail["notice"] = (
            f"⚠️ 你带的 binding_id={binding_id} 已失效：当前绑定是 {_binding['id']} {_binding_app()}。"
            "**下面这棵元素树不是你要观察的那个应用**（同一 DSH 实例的另一个会话改过绑定）。"
            "要操作它请先重新 get_app 绑定，再按本次的 binding_id 重新观察。"
        )
    if screenshot:
        if _screen_recording_ok() is False:
            detail["notice"] = "⚠️ 屏幕录制权限未授予宿主 App，这次的附图拿不到（元素树是好的）。"
        else:
            try:
                wid = _cg_id_for_window(info["pid"], int(window)) if int(window) >= 0 else None
                detail["image"] = str(_capture_window_jpeg(int(wid)) if wid else _capture_jpeg())
                _note("📷 截屏")
            except Exception as e:  # noqa: BLE001 - 图拿不到不该让树也失败
                detail["notice"] = f"⚠️ 截图失败（{type(e).__name__}: {e}），元素树是好的。"
    return ActionResult(ok=True, detail=detail)


def _render_observe(r: ActionResult) -> object:
    if not r.ok:
        return render_error(r)
    path = r.detail.get("image")
    text = str(r.detail.get("state") or "") + _binding_line() + tail(r)
    return Blocks(text, Image(path=path) if path else None)


@action(risk=False)
def _do_elements(app: str = "", binding_id: str = "") -> ActionResult:
    """全量元素树（= get_ax_state(full=true) 的旧名，保留为别名）。"""
    return _do_observe(app=app, full=True, binding_id=binding_id)


@action(risk=False)
def _do_screenshot(display: int = 1, window_id: int = 0) -> ActionResult:
    if _screen_recording_ok() is False:
        return ActionResult(
            ok=False, error="PERMISSION_DENIED", possibly_sent=False,
            message="屏幕录制权限未授予宿主 App（系统设置→隐私与安全性→屏幕录制→添加 DeepSeek Harness），无法截屏。请让用户授权后重试。",
        )
    try:
        path = _capture_window_jpeg(int(window_id)) if window_id else _capture_jpeg(int(display))
    except ValueError as e:
        return ActionResult(ok=False, error="INVALID_ARGUMENT", possibly_sent=False, message=str(e))
    except subprocess.CalledProcessError as e:
        return ActionResult(ok=False, error="INTERNAL", possibly_sent=False, message=f"截屏失败（{e}）")
    _note("📷 截屏")
    return ActionResult(ok=True, detail={"path": str(path), "basis": _basis_note()})


def _render_screenshot(r: ActionResult) -> object:
    if not r.ok:
        return render_error(r)
    return Blocks(f"已截屏。{r.detail.get('basis', '')}（本图就是这个坐标系的基准）", Image(path=r.detail["path"]))


@action(risk=False)
def _do_list_windows(app: str = "") -> ActionResult:
    target = _resolve_app(app, bind=False) if app else None   # 过滤不该有副作用（不改绑定/不换令牌）
    out = []
    for w in ax.window_list():
        name = w["name"] or "(无标题)"
        # 只列普通层级窗口，避免菜单栏/通知中心之类刷屏；但自家浮窗是浮在所有应用之上的
        # 高层级面板，不豁免的话它就永远不出现在清单里，坐标路径只能靠猜（拖动/点 × 都无从下手）。
        if w["layer"] != 0 and name != PANEL_WINDOW_TITLE:
            continue
        if target and w["pid"] != target["pid"]:
            continue
        out.append(w)
    return ActionResult(ok=True, detail={"windows": out[:40]})


def _render_list_windows(r: ActionResult) -> str:
    if not r.ok:
        return render_error(r)
    lines = [
        f"window_id: {w['id']}  |  {w['owner'] or '?'} — {w['name'] or '(无标题)'}  |  @({w['x']},{w['y']}) {w['w']}x{w['h']}"
        for w in r.detail.get("windows") or []
    ]
    return "\n".join(lines) or "(屏幕上没有符合条件的窗口)"


@action(risk=True)
def _do_click_element(index: int, button: str = "left", double: bool = False, strategy: str = "auto", binding_id: str = "") -> ActionResult:
    """元素点击：auto = AXPress 优先、坐标兜底；a11y = 只用 AXPress（没有就报错）；event = 强制坐标。"""
    st = _strategy(strategy)
    info = _resolve_app()
    obs = ax.observer_for(info["pid"])
    node = obs.node_by_index(int(index))   # 元素没了/换了 → AXBridgeError，装饰器翻译成类型化错误码
    label = (f"{node.role} {node.title}".strip()) or f"#{index}"
    acts = node.el.action_names()
    if st == "a11y" and double:
        return ActionResult(
            ok=False, error="ACTION_UNAVAILABLE", possibly_sent=False,
            message=f"元素[{index}] {label}：a11y 策略没有「双击」这个语义动作",
            detail={"hint": "双击只能用合成事件：strategy=event（或 auto）。"},
        )
    pid, before = _baseline()
    if "AXPress" in acts and not double:
        mark_sent()
        err = node.el.perform("AXPress")
        if err == 0:
            settle = _settle()
            effect, ev = _evidence(pid, before)
            if effect == "unchanged" and node.el.get("AXFocused")[1] is True:
                effect, ev = "changed", "元素已获得焦点，界面树无差异"
            return ActionResult(
                ok=True, effect=effect, evidence=ev, settle_ms=settle,
                detail={"index": int(index), "element": label, "strategy": "AXPress", "hints": _hint_codes()},
            )
        if st == "a11y":
            return ActionResult(ok=False, error="ACTION_UNSUPPORTED", possibly_sent=False,
                                message=f"元素[{index}] {label} 的 AXPress 失败（{err_name(err)}）")
        # auto：语义动作失败 → 落到坐标兜底（下面继续）
    elif st == "a11y":
        return ActionResult(
            ok=False, error="ACTION_UNAVAILABLE", possibly_sent=False,
            message=f"元素[{index}] {label} 没有 AXPress 语义动作（可用: {', '.join(acts[:8]) or '无'}）",
            detail={"hint": "改用 strategy=auto（坐标兜底）或换一个可按的元素索引。"},
        )
    if not (node.pos and node.size):
        return ActionResult(
            ok=False, error="ACTION_UNAVAILABLE", possibly_sent=False,
            message=f"元素[{index}] {label} 既没有 AXPress 也没有位置信息",
            detail={"hint": "重新 get_ax_state 拿新索引。"},
        )
    cx, cy = node.pos[0] + node.size[0] / 2, node.pos[1] + node.size[1] / 2
    mark_sent()
    try:
        with _input():  # 坐标兜底同样要排队，别和正在进行的动作抢物理鼠标
            cg.click(cx, cy, button=button, double=bool(double))
    except cg.FailSafe as e:
        return ActionResult(ok=False, error="FAILSAFE", message=str(e), possibly_sent=False)
    settle = _settle()
    _mark_cursor()
    effect, ev = _evidence(pid, before)
    return ActionResult(
        ok=True, effect=effect, evidence=ev, settle_ms=settle,
        detail={"index": int(index), "element": label, "center": (round(cx), round(cy)),
                "strategy": "coord", "hints": _hint_codes()},
    )


def _render_click_element(r: ActionResult) -> str:
    if not r.ok:
        return render_error(r)
    d = r.detail
    if d.get("strategy") == "AXPress":
        return f"已语义点击（AXPress）元素[{d['index']}] {d['element']}。{effect_clause(r)}{tail(r)}"
    return f"已坐标点击元素[{d['index']}] {d['element']} 中心 {d.get('center')}。{effect_clause(r)}{tail(r)}"


@action(risk=True)
def _do_perform_action(index: int, action: str, binding_id: str = "") -> ActionResult:
    """对元素执行无障碍语义动作（AXPress / AXRaise / AXConfirm / AXCancel / AXPick …）。"""
    info = _resolve_app()
    obs = ax.observer_for(info["pid"])
    node = obs.node_by_index(int(index))
    pid, before = _baseline()
    mark_sent()
    err = node.el.perform(action)
    if err != 0:
        avail = ", ".join(node.el.action_names()[:8]) or "(未知)"
        return ActionResult(
            ok=False, error="ACTION_UNSUPPORTED", possibly_sent=False,
            message=f"元素[{index}] 不支持 {action}（{err_name(err)}）。可用: {avail}",
        )
    settle = _settle()
    effect, ev = _evidence(pid, before)
    return ActionResult(ok=True, effect=effect, evidence=ev, settle_ms=settle,
                        detail={"index": int(index), "action": action, "hints": _hint_codes()})


def _render_perform_action(r: ActionResult) -> str:
    if not r.ok:
        return render_error(r)
    return f"已对元素[{r.detail['index']}] 执行 {r.detail['action']}。{effect_clause(r)}{tail(r)}"


@action(risk=True)
def _do_set_value(index: int, value: str, strategy: str = "auto", binding_id: str = "") -> ActionResult:
    """直接写元素值（文本框/滑块），绕过剪贴板与键盘。"""
    st = _strategy(strategy)
    if st == "event":
        return ActionResult(
            ok=False, error="ACTION_UNAVAILABLE", possibly_sent=False,
            message="set_value 只有无障碍路径（直接写 AXValue），没有合成事件版本",
            detail={"hint": "要合成输入请用 click_element 聚焦 + type_text。"},
        )
    info = _resolve_app()
    obs = ax.observer_for(info["pid"])
    node = obs.node_by_index(int(index))
    mark_sent()
    err = node.el.set_attr("AXValue", str(value))
    if err != 0:
        return ActionResult(
            ok=False, error="NOT_SETTABLE", possibly_sent=False,
            message=f"元素[{index}] ({node.role}) 不支持写入 AXValue（{err_name(err)}）",
            detail={"hint": "改用 click_element + type_text。"},
        )
    settle = _settle()
    # 这类写入**不能靠整树指纹**证明：观察器缓存的元素引用与重新遍历得到的子元素不是同一个
    # AX 实例，实测写进搜索框后指纹一模一样 —— 于是成功也报 unchanged，模型会以为没生效而
    # 换一种方式重来一遍。直接读回该元素的值当证据。
    ok_read, after = node.el.get("AXValue", max_str=120)
    shown = "" if not ok_read or isinstance(after, tuple) else str(after)
    if shown == str(value):
        effect, ev = "changed", (f"读回 = {shown!r}" if shown else "已清空")
    else:
        effect, ev = "unknown", f"存疑：set 返回成功，但读回是 {shown!r}"
    return ActionResult(ok=True, effect=effect, evidence=ev, settle_ms=settle,
                        detail={"index": int(index), "role": node.role, "hints": _hint_codes()})


def _render_set_value(r: ActionResult) -> str:
    if not r.ok:
        return render_error(r)
    return f"已写入元素[{r.detail['index']}] ({r.detail.get('role')}) 的值。{effect_clause(r)}{tail(r)}"


@action(risk=True)
def _do_select_text(index: int, text: str = "", prefix: str = "", suffix: str = "", cursor: str = "", binding_id: str = "") -> ActionResult:
    """在文本元素里选一段文字或移动光标（读回选区当证据）。"""
    info = _resolve_app()
    obs = ax.observer_for(info["pid"])
    node = obs.node_by_index(int(index))
    ok, whole = node.el.get("AXValue", max_str=500000)
    if not ok or not isinstance(whole, str):
        return ActionResult(ok=False, error="ATTRIBUTE_UNSUPPORTED", possibly_sent=False,
                            message=f"元素[{index}] ({node.role}) 没有可读文本 AXValue")
    s = whole
    if prefix:
        loc = s.find(prefix)
        if loc < 0:
            return ActionResult(ok=False, error="NOT_SELECTABLE", possibly_sent=False,
                                message=f"prefix 在文本中找不到（前 80 字符: {s[:80]!r}）")
        start = loc + len(prefix)
    else:
        start = 0
    if suffix:
        end = s.find(suffix, start)
        if end < 0:
            return ActionResult(ok=False, error="NOT_SELECTABLE", possibly_sent=False,
                                message="suffix 在文本中找不到")
    else:
        end = len(s)
    if text:
        i = s.find(text, start, end + len(text))
        if i < 0:
            return ActionResult(ok=False, error="NOT_SELECTABLE", possibly_sent=False,
                                message=f"text 在指定范围内找不到（前 80 字符: {s[:80]!r}）")
        rng_loc, rng_len = (i, 0) if cursor == "before" else ((i + len(text), 0) if cursor == "after" else (i, len(text)))
    else:
        rng_loc, rng_len = start, max(0, end - start)
    mark_sent()
    err = node.el.set_selected_range(rng_loc, rng_len)
    if err != 0:
        return ActionResult(
            ok=False, error="NOT_SELECTABLE", possibly_sent=False,
            message=f"元素[{index}] 不支持设置选区（{err_name(err)}）",
            detail={"hint": "改用双击/坐标选择。"},
        )
    time.sleep(0.2)
    # 同样靠读回证明：整树指纹对选区这种焦点态变化不敏感，报 unchanged 只会误导重试
    ok_read, back_raw = node.el.get("AXSelectedTextRange")
    back = back_raw if ok_read and isinstance(back_raw, tuple) else None
    if back is None:
        effect, ev = "unknown", "该元素读不回选区，以返回值为准"
    elif tuple(back) == (int(rng_loc), int(rng_len)):
        effect, ev = "changed", f"读回 loc={back[0]} len={back[1]}"
    else:
        effect, ev = "unknown", (
            f"存疑：要求 loc={rng_loc} len={rng_len}，读回 loc={back[0]} len={back[1]}；"
            "输入框没有焦点时选区会被系统清掉，先 click_element 聚焦再选"
        )
    return ActionResult(ok=True, effect=effect, evidence=ev, settle_ms=200,
                        detail={"index": int(index), "loc": rng_loc, "len": rng_len, "hints": _hint_codes()})


def _render_select_text(r: ActionResult) -> str:
    if not r.ok:
        return render_error(r)
    d = r.detail
    return f"已在元素[{d['index']}] 设置选区 loc={d['loc']} len={d['len']}。{effect_clause(r)}{tail(r)}"


_register("list_apps", _do_list_apps, _render_list_apps,
          "列出当前所有有界面的应用（名称 / bundle id / pid / 是否前台）。用于 get_app 前确定目标。",
          risk=False, read_only=True)
_register("get_app", _do_get_app, _render_get_app,
          "绑定一个应用，作为后续 get_ax_state / elements / click_element 等工具的默认目标；"
          "同时申请控制器租约（多个会话/子代理同时操作这台电脑时的仲裁，见 lease）。\n"
          "app 可以是 名称、bundle id 或 pid；留空绑定当前前台应用。focus=true 顺带把它带到前台。"
          "返回应用信息、窗口列表与租约信息。\n"
          "若返回 ERROR[CONTROLLER_BUSY]：另一个会话正持有这个应用的租约（消息里有持有者与剩余时间）。"
          "你可以照常观察，但动作会被拦住 —— 等它收工、换应用，或确认那个会话已经不用电脑时带 force=true 抢占（会写日志）。",
          risk=False, read_only=True)
_register("get_ax_state", _do_observe, _render_observe,
          "观察应用的无障碍元素树（角色/标题/值/位置/尺寸），后台应用也能读。\n"
          "默认 diff 模式：只回新增和变化的行，未变化行省略但索引仍有效；full=true 全量。"
          "max_nodes=最多观察的元素数（大界面可调大或配合 window 只看一个窗口）。\n"
          "screenshot=true 时**同时**回一张截图（一次调用拿树 + 图，省一个回合）。\n"
          "返回的 [索引] 供 click_element / set_value / perform_action / select_text 使用；"
          "再次观察后索引会更新，之后的动作必须用新索引。\n"
          "Electron 应用首次观察前请先 get_app（会自动开启 AX 支持）。",
          risk=False, read_only=True)
_register("elements", _do_elements, _render_observe,
          "返回应用的全部无障碍元素（等价 get_ax_state(full=true)，旧名保留为别名）。"
          "大界面建议用 get_ax_state 的 diff 模式。",
          risk=False, read_only=True)
_register("list_windows", _do_list_windows, _render_list_windows,
          "列出屏幕上的窗口（window_id 可用于 screenshot 的窗口截取，跨全部显示器）。\n"
          "app 可选：按 名称/bundle/pid 过滤（留空=所有应用的前台层级窗口）。",
          risk=False, read_only=True)
_register("screenshot", _do_screenshot, _render_screenshot,
          "截取屏幕并返回图片。执行结果不确定时先截图观察；坐标以这张图为准。\n"
          "display=显示器序号（多显示器时用，清单见 screen_info，默认 1 主屏）；"
          "window_id=只截某个窗口（id 见 list_windows，坐标同样以这张截图为准）。",
          risk=False, read_only=True)
_register("click_element", _do_click_element, _render_click_element,
          "点击元素树中的一个元素（索引自最近一次 get_ax_state / elements）。\n"
          "strategy=auto（默认）优先 AXPress 语义动作（后台应用也生效、不占用鼠标），"
          "没有语义动作时回退点击元素中心；a11y 只用语义动作，没有就返回 ERROR[ACTION_UNAVAILABLE]"
          "（不污染用户鼠标）；event 强制走坐标点击。double=true 只能用坐标路径。返回 effect 证据。")
_register("perform_action", _do_perform_action, _render_perform_action,
          "对元素执行无障碍语义动作，如 AXPress、AXRaise（把窗口提到前台）、AXConfirm、AXCancel、AXPick。\n"
          "动作名用 AX 惯例（可按性元素多为 AXPress、勾选框 AXPress、下拉 AXShowMenu）。后台应用也生效。返回 effect 证据。")
_register("set_value", _do_set_value, _render_set_value,
          "直接写入元素值（文本框/滑块等），绕过剪贴板和键盘，对后台应用也生效。返回 effect 证据（读回值）。\n"
          "若返回 NOT_SETTABLE，说明该元素不支持写入，请改用 click_element 聚焦后 type_text。\n"
          "strategy=event 会明确报 ERROR[ACTION_UNAVAILABLE]（这个工具只有无障碍路径）。")
_register("select_text", _do_select_text, _render_select_text,
          "在文本元素中选择一段文字或移动光标（对后台应用也生效）。\n"
          "用法：text=要选中的文字；或 prefix+suffix 定位（选中两者之间）；"
          "cursor=before/after 表示只把光标放到文字前/后（配合 prefix 定位）。")

def _to_shot_point(x: float, y: float) -> tuple:
    """全局逻辑坐标（点）→ 最近一张截图的像素坐标，即 `click` / `drag` 吃的那套。

    浮窗报位置必须用这个空间，否则 Agent 拿到点坐标再喂给像素坐标的工具，
    在 Retina / 非整屏截图下会偏。
    """
    if _last_shot["size_pt"]:
        ox, oy = _last_shot["origin"]
        sw, sh = _last_shot["size_pt"]
    else:
        ox, oy = 0, 0
        sw, sh = cg.main_size()
    if _last_shot["size"]:
        iw, ih = _last_shot["size"]
        return round((x - ox) * iw / sw), round((y - oy) * ih / sh)
    return round(x - ox), round(y - oy)


def _pb_set_html(html: str, plain: str):
    """同时写入 HTML 和纯文本两种剪贴板风味，目标应用自选。
    注意两点（都是实测踩过的坑）：
    1. HTML 必须是完整文档（带 <html><head><meta charset="utf-8">），裸片段或
       charset 缺失会导致部分应用（如 Notes）拒绝粘贴或中文乱码；
    2. UTF-8 编码写入。"""
    from AppKit import NSPasteboard, NSPasteboardTypeHTML, NSPasteboardTypeString
    from Foundation import NSData

    low = html.lower()
    meta = '<meta charset="utf-8">'
    if "<html" not in low:
        html = f"<html><head>{meta}</head><body>{html}</body></html>"
    elif "<meta" not in low[:500]:
        html = html.replace("<head>", f"<head>{meta}", 1) if "<head>" in low else meta + html
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    raw = html.encode("utf-8")
    data = NSData.dataWithBytes_length_(raw, len(raw))
    pb.setData_forType_(data, NSPasteboardTypeHTML)
    pb.setString_forType_(plain, NSPasteboardTypeString)


@action(risk=True)
def _do_paste(text: str, format: str = "text", strategy: str = "auto", binding_id: str = "") -> ActionResult:
    """把内容放进剪贴板并粘贴到当前聚焦位置（format=html 时写富文本风味）。"""
    st = _strategy(strategy)
    body = str(text)
    if format in ("html", "rich"):
        plain = re.sub(r"<[^>]+>", "", body)
        try:
            _pb_set_html(body, plain)
        except Exception as e:  # noqa: BLE001 - pyobjc 缺失/剪贴板异常都要回成结构化错误
            return ActionResult(ok=False, error="INTERNAL", possibly_sent=False,
                                message=f"写入富文本剪贴板失败 {type(e).__name__}: {e}")
    elif format in ("text", "md", "plain"):
        try:
            subprocess.run(["pbcopy"], input=body.encode("utf-8"), check=True, timeout=10)
        except Exception as e:  # noqa: BLE001
            return ActionResult(ok=False, error="INTERNAL", possibly_sent=False,
                                message=f"写入剪贴板失败 {type(e).__name__}: {e}")
    else:
        return ActionResult(ok=False, error="INVALID_ARGUMENT", possibly_sent=False,
                            message=f"不支持的 format={format!r}（可用 text/md/html）")
    focus = _ensure_frontmost() + _aim_keys()
    pid, before = _baseline()
    detail = {"chars": len(body), "format": format, "strategy": st, "focus": focus, "hints": _hint_codes()}
    notice = _user_takeover()
    if notice:
        detail["notice"] = notice
    mark_sent()
    with _input():
        _hotkey(["command", "v"], strategy=st)
        _release_modifiers()
    settle = _settle()
    _mark_cursor()
    _note("📋 粘贴")
    effect, ev = _evidence(pid, before)
    return ActionResult(ok=True, effect=effect, evidence=ev, settle_ms=settle, detail=detail)


def _render_paste(r: ActionResult) -> str:
    if not r.ok:
        return render_error(r)
    return (
        f"已粘贴 {r.detail.get('chars')} 个字符（format={r.detail.get('format')}，剪贴板已被覆盖）。"
        f"{r.detail.get('focus', '')}{effect_clause(r)}{tail(r)}"
    )


@action(risk=True)
def _do_focus_app(app: str = "", binding_id: str = "") -> ActionResult:
    """把目标应用带到前台并聚焦（之后键盘/坐标输入就会落在它上面）。"""
    info = _resolve_app(app)
    granted = lease.acquire(int(info["pid"]), target_name=info["name"])
    if not granted.get("ok"):
        return _with_detail(_lease_block(f"{info['name']}（pid={info['pid']}）", granted),
                            {"app": info, "lease": granted})
    _note(f"🎯 聚焦 {info['name']}")
    mark_sent()
    if _activate_pid(info["pid"]):
        return ActionResult(ok=True, detail={"app": info, "activated": True})
    return ActionResult(
        ok=True, detail={"app": info, "activated": False,
                         "notice": f"已发送激活指令但无法确认 {info['name']} 在前台（部分系统限制自动激活）。可手动点一下它的窗口再继续。"},
    )


def _render_focus_app(r: ActionResult) -> str:
    if not r.detail.get("app"):
        return render_error(r)
    name = (r.detail.get("app") or {}).get("name") or "目标应用"
    if not r.ok:
        return render_error(r)
    if r.detail.get("activated"):
        return f"{name} 已在前台聚焦。{_binding_line()}{tail(r)}"
    return f"已发送激活指令但无法确认 {name} 在前台。{_binding_line()}{tail(r)}"


@action(risk=True)
def _do_focus_window(index: int, binding_id: str = "") -> ActionResult:
    """把绑定应用的指定窗口提到最前并聚焦（内部 = 激活应用 + AXRaise 该窗口）。"""
    info = _resolve_app()
    granted = lease.acquire(int(info["pid"]), target_name=info["name"])
    if not granted.get("ok"):
        return _with_detail(_lease_block(f"{info['name']}（pid={info['pid']}）", granted),
                            {"app": info, "lease": granted})
    root = ax.Element.for_pid(info["pid"])
    try:
        w = ax.window_element(root, int(index))
        if w is None:
            return ActionResult(ok=False, error="WINDOW_NOT_FOUND", possibly_sent=False,
                                message=f"窗口索引 {index} 不存在")
        try:
            _activate_pid(info["pid"])  # 激活失败不阻断 AXRaise
            _publish_focus(pid=info["pid"], window_id=_cg_id_for_window(info["pid"], int(index)))
            mark_sent()
            err = w.perform("AXRaise")
        finally:
            w.dispose()
    finally:
        root.dispose()
    if err != 0:
        # 部分原生应用（实测计算器）不实现 AXRaise；这时激活应用本身通常就够了
        if not _activate_pid(info["pid"]):
            return ActionResult(ok=False, error="ACTION_UNSUPPORTED", possibly_sent=False,
                                message=f"AXRaise 失败（{err_name(err)}），且激活应用也没成功")
        return ActionResult(ok=True, detail={"index": int(index), "raised": False, "name": info["name"],
                                             "notice": f"已激活 {info['name']}（该应用不支持 AXRaise，窗口未单独提升）。"})
    _interruptible_sleep(0.4)
    _note(f"🎯 聚焦窗口[{index}]")
    return ActionResult(ok=True, detail={"index": int(index), "raised": True, "name": info["name"],
                                         "aim": _aim_keys(int(index))})


def _render_focus_window(r: ActionResult) -> str:
    if not r.ok:
        return render_error(r)
    d = r.detail
    if not d.get("raised"):
        return f"{d.get('notice', '')}{tail(r)}"
    return f"已提升窗口[{d['index']}]并聚焦 {d.get('name')}。{d.get('aim', '')}{_binding_line()}{tail(r)}"


_register("paste", _do_paste, _render_paste,
          "把内容放入剪贴板并粘贴到当前聚焦位置（对齐 ZCode 的 paste）。\n"
          "format: text/md=按纯文本粘贴；html=text 作为富文本（HTML）粘贴，支持格式的应用会渲染样式。\n"
          "使用前先让目标位置获得焦点。注意会覆盖当前剪贴板。strategy 语义同 type_text。")
_register("focus_app", _do_focus_app, _render_focus_app,
          "把目标应用带到前台并聚焦（之后键盘/坐标输入就会落在它上面）。app=名称/bundle/pid；留空=当前绑定的应用。\n"
          "同时续期控制器租约；租约被别人持有时返回 ERROR[CONTROLLER_BUSY]。")
_register("focus_window", _do_focus_window, _render_focus_window,
          "把绑定应用的指定窗口提到最前并聚焦（窗口清单见 get_app）。内部=激活应用 + AXRaise 该窗口。")

#: v1 的名字，保留给 scripts/mcp_wire_check.py 与外部脚本：名字 → 动作层函数
_SEQUENCE_TOOLS = {name: spec.fn for name, spec in _ACTIONS.items()}

@mcp.tool()
def preview(enable: bool = True) -> str:
    """显示/隐藏 OS 级实时预览浮窗（唯一的预览面）。

    浮窗浮在所有应用之上、可以拖到桌面任意位置（含 dsh 窗口之外），本来就会在第一次
    电脑操作时自动出现、停手 2 分钟自己藏起来、下一次动作自己回来；这个开关用来强制
    显示（并把它的坐标报给你，便于拖动或点它的 ×）或明确收起。

    注意 enable=false 是**粘性**的：它写 panel.hidden=off，之后所有电脑操作都不再自动拉起
    浮窗，用户也看不到操作画面 —— 而用户侧没有恢复入口，只有 preview(enable=true) 能解除。
    所以只在用户明确说「以后别再弹」时才用它；用户只说「把窗口关了 / 关掉你开的窗口」时
    不要调它，浮窗上的 × 已经是「现在收起来」，自己会回来。
    """
    if not enable:
        _panel_remember("off")          # 粘性 opt-out：用户侧没有恢复入口，只能靠 preview(True)
        return (
            ("预览浮窗已收起。" if _panel_stop() else "当前没有正在显示的预览浮窗。")
            + " 注意这是粘性开关：之后不会再自动出现，用户也看不到操作画面，而用户侧没有恢复入口。"
              "用户再说「怎么不弹窗了 / 我想看它操作」时，用 preview(enable=true) 恢复。"
        )
    _panel_remember("on")               # 明确要开：清掉「用户关掉了」那个状态
    if not _panel_start():
        return "ERROR[INTERNAL]: 预览浮窗进程启动失败（检查 .venv 里是否装了 pyobjc）"
    # 浮窗起来后把自己的几何写进 panel.json：它是独立进程，不在 dsh 的 AX 树里，
    # 坐标路径必须拿到真实位置才能瞄到 × 和拖动手势。
    for _ in range(12):
        time.sleep(0.25)
        try:
            geo = json.loads(PANEL_GEOMETRY.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        gx, gy = _to_shot_point(geo["x"], geo["y"])
        gw, gh = _to_shot_point(geo["x"] + geo["width"], geo["y"] + geo["height"])
        cx, cy = _to_shot_point(*geo["close"])
        hx, hy = _to_shot_point(geo["x"] + geo["width"] / 2, geo["y"] + geo["height"] / 2)
        halt = geo.get("stop")
        halt_txt = f"，⏹ 急停键在 ({_to_shot_point(*halt)[0]},{_to_shot_point(*halt)[1]})" if halt else ""
        return (
            f"预览浮窗已拉起：截图坐标 ({gx},{gy}) {gw - gx}x{gh - gy}"
            f"（与 click/drag 同一坐标系，可直接用；它是插件自己的界面，落点保护对它不生效）。"
            f"拖动手柄=画面中心 ({hx},{hy})，× 在 ({cx},{cy}){halt_txt}。"
            "浮窗左上角的 ⏹ 是**用户侧急停开关**（与 stop() 等价，再按 ▶ 恢复）；"
            "用户说「停下来」而你想让他自己动手时，可以告诉他按这个键。"
        )
    return "预览浮窗已拉起（浮于所有应用之上），但没能读到它的几何信息。"


@mcp.tool()
def open_permission_settings() -> str:
    """打开 macOS 的辅助功能权限设置面板（当 screen_info 显示权限未授予时，引导用户到这里添加 DeepSeek Harness）。"""
    subprocess.run(
        ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"],
        check=True,
        timeout=15,
    )
    return "已打开 系统设置 → 隐私与安全性 → 辅助功能。请把 DeepSeek Harness 添加进去并打开开关。屏幕录制权限在「屏幕录制」一节。"


if __name__ == "__main__":
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    mcp.run()
