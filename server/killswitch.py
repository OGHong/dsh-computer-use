#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""急停开关（kill switch）—— 三档入口，一份状态。

设计任务书 §3.5：三档优先级递增

  1. 环境变量 ``DSH_CUA_DISABLE=1`` —— 进程启动即只读，本进程内无法解除；
  2. 工具 ``stop(reason)`` / ``resume()`` —— Agent 或调用方自己停；
  3. 预览浮窗上的「停止」键 / host 半区的 ``/_computer-use/stop`` 路由 —— 用户肉眼看得见
     的那一下，经**文件**把状态传进来（浮窗是独立进程，工具服务器这时可能正卡在一个动作里，
     走不了工具调用那条路）。

为什么状态落在文件里而不是内存里：三档入口分属三个进程（MCP 服务器 / 浮窗 / Node 半区），
而「立刻中止当前动作」要求正在跑的那个动作**在下一个可中断点**就能看见 —— 文件 + 200ms
缓存是唯一不需要额外 IPC 通道的做法（对齐 ``mcp_stdio.pause`` 的可中断点设计）。

状态文件格式（``kill``）::

    on:<毫秒时间戳>:<原因文本>      停止
    （文件不存在）                  正常

刻意**不**在服务器启动时清掉它：用户按下停止之后，Agent 换一个会话又自己动起来是最糟的
结果。代价是它粘得住 —— 所以要能一眼看见（``screen_info`` / 浮窗上的 ▶ 键 / ``resume()``
的返回文案都写了怎么恢复）。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

#: 环境变量档：与 lib/index.js / preview_window.py 共用同一个目录约定
ENV_VAR = "DSH_CUA_DISABLE"
_FILE = "kill"
#: 文件读缓存：`_settle` 每 120ms 探一次，没必要每次都开文件
_CACHE_S = 0.2


class Killed(BaseException):
    """已进入只读（急停）：当前动作必须立刻中止。

    刻意继承 BaseException（与 ``mcp_stdio.Cancelled`` / SystemExit 同类）：动作路径里有
    大量 ``except Exception`` 兜底分支，而急停是**控制流**、不是「工具错误」—— 被它们吞成
    一条普通错误然后接着往下点，正是这个开关要防的事。
    """


def _path() -> Path:
    return Path(os.environ.get("DSH_CUA_DIR", "/tmp/dsh-computer-use")) / _FILE


def env_locked() -> bool:
    """这一档是不是由环境变量锁死的（本进程内无法 resume）。"""
    return os.environ.get(ENV_VAR, "").strip().lower() in {"1", "true", "yes", "on"}


_cache = [0.0, False, ""]   # [检查时刻, 是否已停, 原因]


def _state() -> tuple[bool, str]:
    """(是否已停, 原因)；200ms 缓存。"""
    if env_locked():
        return True, f"环境变量 {ENV_VAR}=1（服务器启动即只读；本进程内无法解除）"
    now = time.monotonic()
    if now - _cache[0] < _CACHE_S:
        return _cache[1], _cache[2]
    raw = ""
    try:
        raw = _path().read_text(encoding="utf-8").strip()
    except OSError:
        raw = ""
    _cache[0] = now
    if raw.startswith("on"):
        parts = raw.split(":", 2)
        why = parts[2].strip() if len(parts) > 2 else ""
        _cache[1], _cache[2] = True, why or "已停止（未写原因）"
        return _cache[1], _cache[2]
    _cache[1], _cache[2] = False, ""
    return False, ""


def stopped() -> bool:
    return _state()[0]


def reason() -> str:
    """已停时的原因（未停 = 空串）。"""
    return _state()[1]


def guard() -> None:
    """已停止就抛 ``Killed`` —— 每个动作的入口、每个等待循环的可中断点都调它。"""
    hit, why = _state()
    if hit:
        raise Killed(why)


def stop(why: str = "") -> str:
    """进入只读（幂等）。返回实际记下的原因文本。"""
    text = (why or "").strip() or "未写原因（stop() 被调用）"
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".kill.tmp")
        tmp.write_text(f"on:{int(time.time() * 1000)}:{text}", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        return f"ERROR[INTERNAL]: 无法写入急停标志 {path}（{e}）"
    _cache[0], _cache[1], _cache[2] = 0.0, True, text
    return text


def resume() -> str:
    """解除急停。返回空串 = 成功；否则是给模型看的错误说明。"""
    if env_locked():
        return (
            f"ERROR[KILLED]: 只读由环境变量 {ENV_VAR}=1 锁定（服务器启动时就生效），"
            "本进程内无法恢复；请去掉该变量后重开会话。"
        )
    path = _path()
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        return f"ERROR[INTERNAL]: 无法删除急停标志 {path}（{e}）"
    _cache[0], _cache[1], _cache[2] = 0.0, False, ""
    return ""


def state_line() -> str:
    """给 screen_info / stop 回包用的一行状态。"""
    hit, why = _state()
    if not hit:
        return "急停开关: 未触发（动作工具可用）"
    src = f"环境变量 {ENV_VAR}" if env_locked() else "stop() 或浮窗停止键"
    return (
        f"急停开关: **已停止**（来源: {src}）—— {why}；"
        "动作类工具一律回 ERROR[KILLED]，观察类工具仍可用，恢复用 resume()。"
    )
