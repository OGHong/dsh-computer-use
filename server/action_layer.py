#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""动作层与渲染层的公共契约（设计任务书 §3.1）。

三层分工，越界即回归：

  动作层 ``_do_*``（server.py）
      执行 + 采证，**只**返回 :class:`ActionResult`，一句自然语言都不许有
      （少一个字段，sequence 就做不了步间决策）；
  渲染层 ``_render_*``（server.py）
      把 ActionResult 变成给模型看的自然语言；
  注册表 :class:`ActionSpec`
      把两者绑成一个 MCP 工具，同时登记进 ``_ACTIONS`` 供 ``sequence`` 按名字复用 ——
      「工具能做的」和「批处理能做的」从此是同一份清单（v1 的白名单只有坐标工具，
      最该批处理的元素路径反而一步一回合）。

本模块只放不依赖具体工具的公共部分：数据结构、异常翻译、证据/尾注渲染。
"""
from __future__ import annotations

import functools
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable

import killswitch
import lease
from ax_error import AXBridgeError
from mcp_stdio import BadArgument


@dataclass
class ActionResult:
    """一次动作的结构化结果。

    ``possibly_sent`` 是**防重放语义**（§3.4），判定规则写死在这里，不允许各工具自己发挥：

      - 参数校验失败、元素查不到、租约被占、急停 → ``False``（肯定没下发，改参数重试是安全的）；
      - 动作已发出后超时 / 连接中断 / 子进程被杀 / 被急停打断 → ``True``（**可能已经点下去了**，
        重复同一动作就是重复提交）；
      - 动作正常返回（无论 effect） → ``False``。

    ``settle_ms`` 是 ``_settle()`` 实际等了多久，每个动作都必须有。
    """

    ok: bool = True
    effect: str | None = None       # changed / unchanged / unknown / None（无树哈希可言）
    evidence: str = ""              # 证据（读回值、元素已获焦等）；空则用默认解释
    settle_ms: int | None = None    # 由 _settle() 填；None = 由装饰器按总耗时兜底
    possibly_sent: bool | None = None   # None = 由装饰器按「有没有 mark_sent」判定
    error: str | None = None        # 类型化错误码，如 ELEMENT_UNAVAILABLE
    message: str = ""               # 错误说明（不含 "ERROR[...]" 前缀）
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ActionSpec:
    """一个动作型工具的完整定义：动作层 + 渲染层 + 给模型看的描述。"""

    name: str
    fn: Callable[..., ActionResult]
    render: Callable[[ActionResult], object]
    description: str
    #: True = 会动这台电脑（占用物理输入/改变系统状态）→ 受急停与租约约束
    risk: bool = True
    #: True = 只读观察（get_ax_state / list_apps …），急停期间仍然可用
    read_only: bool = False


# ---------------------------------------------------------------------------
# 「动作已下发」的标记
# ---------------------------------------------------------------------------

_sent = [False]


def mark_sent() -> None:
    """动作**即将**真的下发时调用（CGEvent 投递 / osascript / 子进程启动）。

    写在调用之前、而不是拿到回包之后：超时/被杀断在中间时，我们不知道它有没有落地，
    这正是 ``possibly_sent=true`` 要表达的东西。调用里能明确判定「没送出去」的分支
    （比如 AXPress 返回错误码）请显式写 ``possibly_sent=False`` 覆盖。
    """
    _sent[0] = True


def was_sent() -> bool:
    return _sent[0]


#: 「这次动作打在哪个 pid 上」—— 由 server.py 注册（动作层不该知道绑定应用在哪儿）。
#: 返回 None = 没有绑定应用（纯坐标路径）→ 租约不介入。
_lease_target = lambda: None  # noqa: E731


def bind_lease_target(fn) -> None:
    """server.py 在模块加载时把 `_bound_app` 的读法注册进来（依赖倒置，避免环形 import）。"""
    global _lease_target  # noqa: PLW0603
    _lease_target = fn


#: 「调用方带的绑定令牌还对得上吗」—— 同样由 server.py 注册（任务书 §4.2）。
#: 传进来的是动作工具的可选参数 `binding_id`；返回错误说明串 = 拒绝这次动作。
_binding_check = lambda _value: None  # noqa: E731


def bind_binding_checker(fn) -> None:
    """注册绑定校验（依赖倒置）。它必须只读且便宜：每个 risk 动作前都会走一次。"""
    global _binding_check  # noqa: PLW0603
    _binding_check = fn


def check_binding(value) -> str:
    """给不走 `@action` 装饰器的入口用（`sequence` 要先整体校验一次）。"""
    return _binding_check(value) or ""


#: 「没带 binding_id」时的一句提醒 —— 同样由 server.py 注册（没有绑定就返回空串，不刷屏）。
_binding_note = lambda _value: ""  # noqa: E731


def bind_binding_note(fn) -> None:
    global _binding_note  # noqa: PLW0603
    _binding_note = fn


# ---------------------------------------------------------------------------
# 动作装饰器：异常 → ActionResult；补 settle_ms / possibly_sent
# ---------------------------------------------------------------------------

KILL_HINT = (
    "急停已生效：本次动作**没有下发**，也不需要重试。观察类工具（get_ax_state / screenshot / "
    "list_apps 等）仍可用；确认可以继续后调 resume() 恢复，或让用户在预览浮窗上按 ▶。"
)


def _finish(result: object, started: float) -> ActionResult:
    if not isinstance(result, ActionResult):
        raise TypeError(f"动作层必须返回 ActionResult，收到 {type(result).__name__}（自然语言请交给渲染层）")
    if result.settle_ms is None:
        result.settle_ms = int((time.monotonic() - started) * 1000)
    if result.possibly_sent is None:
        result.possibly_sent = False if result.ok else bool(_sent[0])
    return result


def action(risk: bool = True):
    """把一个 ``_do_*`` 变成「永远返回 ActionResult」的动作层函数。

    ``risk=True`` 的动作在入口依次查：急停 → 控制器租约 → 绑定令牌。三者都在**一次点击发出去
    之前**返回类型化错误（``KILLED`` / ``CONTROLLER_BUSY`` / ``BINDING_CHANGED``），所以「急停期间
    一次都不会点下去」「租约被别人拿着时不会互相踩」「另一个会话改过绑定之后不会照着旧索引点到
    别的应用上」都是结构上成立的，不靠各工具自觉。
    异常一律翻译成类型化错误码 —— ``ELEMENT_UNAVAILABLE`` / ``STALE_STATE`` 这类码模型要能
    直接分支，不能被挤进 INTERNAL 的文案里。
    """

    def deco(fn):
        @functools.wraps(fn)
        def run(*args, **kwargs):
            started = time.monotonic()
            _sent[0] = False
            try:
                if risk:
                    killswitch.guard()
                    blocked = lease.guard(_lease_target())
                    if blocked:
                        return _finish(
                            ActionResult(
                                ok=False, error="CONTROLLER_BUSY", possibly_sent=False,
                                message=(
                                    f"{lease.describe_holder(blocked.get('blocked_by') or {})} "
                                    "正在操作这台电脑，本动作已被仲裁拦住（没有下发）。"
                                ),
                                detail={"hint": "等它收工、换一个应用，或确认对方已经不用电脑时用 get_app(force=true) 抢占（会写日志）。"},
                            ),
                            started,
                        )
                    stale = _binding_check(kwargs.get("binding_id"))
                    if stale:
                        return _finish(
                            ActionResult(
                                ok=False, error="BINDING_CHANGED", possibly_sent=False, message=stale,
                                detail={"hint": "重新 get_app 绑定目标，再 get_ax_state 拿新索引；不要用旧索引操作。"},
                            ),
                            started,
                        )
                result = fn(*args, **kwargs)
                if risk and result.ok:
                    note = _binding_note(kwargs.get("binding_id"))
                    if note:
                        result.detail["notice"] = f"{result.detail.get('notice', '')} {note}".strip()
            except killswitch.Killed as e:
                lease.release()   # 急停即交出控制器：别的会话/用户可以立刻接管
                return _finish(
                    ActionResult(ok=False, error="KILLED", message=str(e), possibly_sent=False,
                                 detail={"hint": KILL_HINT}),
                    started,
                )
            except BadArgument as e:
                return _finish(
                    ActionResult(ok=False, error="INVALID_ARGUMENT", message=str(e), possibly_sent=False),
                    started,
                )
            except AXBridgeError as e:
                return _finish(ActionResult(ok=False, error=e.code, message=e.detail), started)
            except subprocess.TimeoutExpired as e:
                return _finish(
                    ActionResult(
                        ok=False,
                        error="TIMEOUT",
                        message=f"{' '.join(e.cmd) if isinstance(e.cmd, list) else e.cmd} 超过 {e.timeout} 秒未返回，进程已被杀掉",
                        possibly_sent=True,
                    ),
                    started,
                )
            except Exception as e:  # noqa: BLE001 - 动作层不许把异常抛给协议层
                return _finish(
                    ActionResult(ok=False, error="INTERNAL", message=f"{type(e).__name__}: {e}"),
                    started,
                )
            return _finish(result, started)

        return run

    return deco


# ---------------------------------------------------------------------------
# 渲染层的公共件
# ---------------------------------------------------------------------------

#: 权限/观察窗这类「静默失败」的固定提示（按码取，动作层只负责给码）。
#: 两条都是「Agent 以为一切正常、用户其实什么都看不到」：辅助功能权限没给时点击/键盘静默无效；
#: 浮窗被 preview(enable=false) 明确关掉之后，用户的监督面就没了 —— 而 off 状态在 v1 里只在
#: `_ensure_panel()` 里默默 return，Agent 完全不知道。
HINTS = {
    "AX_PERMISSION": " ⚠️ 辅助功能权限未授予宿主 App（系统设置→隐私与安全性→辅助功能→添加 DeepSeek Harness），本次操作很可能没有生效。",
    "PANEL_HIDDEN": " ℹ️ 预览浮窗已被明确关闭，用户看不到这次操作的画面；用户想看画面时用 preview(enable=true) 恢复。",
}

#: §3.4 的模型侧指引：不能只加字段，还要把「别重复点」说清楚
POSSIBLY_SENT = (
    " ⚠️ possibly_sent=true：这一步**可能已经下发**（超时/中断/被中止），"
    "不要重复同一动作 —— 先 get_ax_state 观察当前状态，再决定下一步；重复一个可能已生效的点击会造成重复提交。"
)

_EFFECT_WHY = {
    "changed": "界面树已变化",
    "unchanged": "界面树逐字节一致",
    "unknown": "无操作前基线",
}


def effect_clause(r: ActionResult) -> str:
    """``effect: changed（界面树已变化）。`` —— 每个动作结果里都必须有的那半句。"""
    if not r.effect:
        return ""
    why = r.evidence or _EFFECT_WHY.get(r.effect, "")
    return f"effect: {r.effect}（{why}）。" if why else f"effect: {r.effect}。"


def tail(r: ActionResult) -> str:
    """统一尾巴：possibly_sent 指引 + 权限/浮窗提示 + 自由 notice。"""
    out = POSSIBLY_SENT if r.possibly_sent else ""
    for code in r.detail.get("hints") or []:
        out += HINTS.get(code, "")
    notice = r.detail.get("notice")
    if notice:
        out += f" {notice}"
    return out


def render_error(r: ActionResult) -> str:
    head = f"ERROR[{r.error}]" + (f": {r.message}" if r.message else "")
    hint = r.detail.get("hint")
    return (f"{head} {hint}" if hint else head) + tail(r)


def step_line(r: ActionResult) -> str:
    """sequence 里每一步的紧凑行（详细信息由渲染层另行缩进给出）。"""
    if not r.ok:
        return "✗ " + render_error(r)
    bits = [f"effect: {r.effect}"] if r.effect else []
    bits.append(f"{r.settle_ms}ms")
    if r.possibly_sent:
        bits.append("possibly_sent=true")
    return "✓ " + " ".join(bits)
