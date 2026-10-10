#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""控制器租约（controller lease）—— 用仲裁取代「禁止子代理用电脑」的一刀切。

设计任务书 §3.6。要解决的问题不是「一个动作占用物理输入」（那个 ``threading.RLock``
已经管住了），而是**两个会话/子代理轮流操作同一台电脑**：锁是瞬时的，谁先抢到谁动，
于是 A 在填表单、B 把前台抢走点了别处 —— 两边都以为自己在操作那个应用。

形态：**一个持有者一个文件**（``leases/<holder_pid>.json``）。为什么不是一份全局文件：
租约是「谁在操作哪个应用」，本来就可能有多个持有者（各自盯着不同的 app），一份文件会把
它们挤成互相覆盖。目录扫描是廉价的（通常 0~2 个文件），每个动作前扫一次完全付得起。

四个动作：

  - ``acquire(target_pid, force)`` —— get_app / focus_app 时申请；同目标被别人活着持有 →
    返回 ``CONTROLLER_BUSY``（带持有者与剩余 TTL），``force=True`` 可抢占并写日志；
  - ``renew()`` —— 每个动作续期（写盘节流，判据仍按文件走）；
  - ``release()`` —— 显式释放 / 急停 / 进程退出（atexit）自动释放；
  - ``guard(target_pid)`` —— 动作前的检查：自己的租约被别人抢走了就返回阻塞信息。

存活判据是「持有者进程还在 + 没超时」：持有者崩了（DSH 被强杀、进程 OOM）留下的租约
会在下一次扫描时被回收并写进日志，不会把别的会话永久挡在门外。
"""
from __future__ import annotations

import atexit
import json
import os
import sys
import time
from pathlib import Path

#: 默认租约 TTL（秒）：太短会频繁失效，太长会卡住其他会话（§7 风险 6）
TTL = 60.0
#: 续期写盘的最小间隔（秒）；判据（别人的租约）每次动作都真扫，只是不必每次写盘
RENEW_MIN_S = 1.0
_DIR = "leases"
_LOG = "leases.log"

#: 本进程持有的租约（内存真相；写盘的副本供别的会话判断）
_mine: dict = {}
_last_renew = [0.0]


def _base() -> Path:
    return Path(os.environ.get("DSH_CUA_DIR", "/tmp/dsh-computer-use"))


def _dir() -> Path:
    return _base() / _DIR


def _log(line: str) -> None:
    """抢占/回收都留痕：一次「谁把谁踢了」必须事后查得出来。"""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    sys.stderr.write(f"computer-use lease: {line}\n")
    try:
        _dir().mkdir(parents=True, exist_ok=True)
        with (_base() / _LOG).open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {line}\n")
    except OSError:
        pass


def holder_label() -> str:
    """本进程在租约里的身份（多会话时用来分辨「是谁」）。"""
    return os.environ.get("DSH_CUA_LABEL") or f"computer-use pid {os.getpid()}"


def ttl() -> float:
    try:
        return max(5.0, float(os.environ.get("DSH_CUA_LEASE_TTL") or TTL))
    except ValueError:
        return TTL


def _alive(pid) -> bool:
    """持有者进程还在吗。

    EPERM 必须当成**活着**：别的用户（或 root）的进程 kill(pid,0) 会返回 EPERM，把它读成
    「已退出」就会去回收一份其实还活着的租约 —— 那是这个模块最不能出的错。
    """
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False
    return True


def _read_file(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _scan() -> list[dict]:
    """所有**活着**的租约；顺手回收死进程/过期留下的文件。"""
    out: list[dict] = []
    try:
        files = sorted(_dir().glob("*.json"))
    except OSError:
        return out
    now = time.time()
    for path in files:
        rec = _read_file(path)
        holder = rec.get("holder_pid")
        if not rec or not holder:
            _drop(path, "文件损坏")
            continue
        if int(holder) == os.getpid():
            out.append(rec)          # 自己的：由 _mine 代表，这里只是别被当成别人
            continue
        if not _alive(holder):
            _drop(path, f"持有者 pid {holder} 已不在（崩溃/强杀），自动回收")
            continue
        if now - float(rec.get("renewed_at") or 0) >= float(rec.get("ttl") or ttl()):
            _drop(path, f"租约 {rec.get('lease_id')} 超过 TTL 未续期，自动回收")
            continue
        out.append(rec)
    return out


def _drop(path: Path, why: str) -> None:
    try:
        path.unlink(missing_ok=True)
        _log(f"回收 {path.name}：{why}")
    except OSError:
        pass


def _path_for(holder_pid: int) -> Path:
    return _dir() / f"{int(holder_pid)}.json"


def _write(rec: dict) -> None:
    path = _path_for(rec["holder_pid"])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        _log(f"租约写盘失败 {path}：{e}")


def mine() -> dict:
    """本进程当前持有的租约（空 dict = 没持有）。"""
    return dict(_mine)


def _remaining(rec: dict) -> float:
    return max(0.0, float(rec.get("renewed_at") or 0) + float(rec.get("ttl") or ttl()) - time.time())


def acquire(target_pid: int, target_name: str = "", force: bool = False) -> dict:
    """申请（或续期）``target_pid`` 的租约。

    返回 ``{"ok", "lease_id", "remaining_s", "warning", "blocked_by"}``：
    ``ok=False`` 且带 ``blocked_by`` = 别人正持有同一目标且活着（CONTROLLER_BUSY）。
    目标不同但别人也在操作时仍然放行，只带一句 warning —— 用户可能确实要两个会话各干各的，
    但要让他知道「前台会被抢」这件事。
    """
    now = time.time()
    ttl_s = ttl()
    target = int(target_pid)
    blocked: dict | None = None
    warning = ""
    for rec in _scan():
        if int(rec.get("holder_pid") or 0) == os.getpid():
            continue
        same = int(rec.get("target_pid") or 0) == target
        left = _remaining(rec)
        if same and not force:
            blocked = rec
            break
        if same and force:
            _log(
                f"抢占：{rec.get('lease_id')}（{rec.get('holder')}，目标 {rec.get('target_name') or rec.get('target_pid')}）"
                f" → {holder_label()}（目标 {target_name or target}）"
            )
            # 抢占必须**真的**把对方的记录收掉：留着它，被抢的一方下次 guard() 仍然看得见
            # 自己那份、抢占方也会被自己刚抢来的文件挡住 —— 两边一起卡死。
            _drop(_path_for(int(rec.get("holder_pid"))), f"被 {holder_label()} 抢占")
        elif not same:
            warning = (
                f"另一个会话（{rec.get('holder') or rec.get('holder_pid')}）正在操作 "
                f"{rec.get('target_name') or rec.get('target_pid')}（租约剩余 {left:.0f} 秒）："
                "两边同时动鼠标/键盘会互相抢前台与焦点，必要时用 force=true 或等它收工。"
            )
    if blocked is not None:
        return {
            "ok": False,
            "error": "CONTROLLER_BUSY",
            "blocked_by": blocked,
            "remaining_s": _remaining(blocked),
            "warning": warning,
        }
    if not _mine or int(_mine.get("target_pid") or 0) != target:
        _mine.clear()
        _mine.update(
            {
                "lease_id": f"l-{os.getpid()}-{int(now * 1000)}",
                "holder_pid": os.getpid(),
                "holder": holder_label(),
                "target_pid": target,
                "target_name": target_name or "",
                "acquired_at": now,
                "renewed_at": now,
                "ttl": ttl_s,
            }
        )
    else:
        _mine["renewed_at"] = now
        _mine["ttl"] = ttl_s
        _mine["target_name"] = target_name or _mine.get("target_name") or ""
    _write(_mine)
    _last_renew[0] = now
    return {
        "ok": True,
        "lease_id": _mine["lease_id"],
        "remaining_s": _remaining(_mine),
        "warning": warning,
        "blocked_by": None,
    }


def renew() -> None:
    """续期（写盘节流；内存里的 renewed_at 每次都更新，写盘至少间隔 RENEW_MIN_S）。"""
    if not _mine:
        return
    now = time.time()
    _mine["renewed_at"] = now
    if now - _last_renew[0] >= RENEW_MIN_S:
        _write(_mine)
        _last_renew[0] = now


def guard(target_pid: int | None = None) -> dict | None:
    """动作前的租约检查：``None`` = 放行；否则是给模型的阻塞信息（CONTROLLER_BUSY）。

    自己的租约被别人 ``force`` 抢走后，这里必须挡住 —— 否则「抢占」只是写了一条日志。
    """
    if not _mine:
        return None                       # 没申请过租约（纯坐标路径）→ 不介入
    if target_pid is not None and int(_mine.get("target_pid") or 0) != int(target_pid):
        return None                       # 换了目标应用：get_app 会重新申请
    for rec in _scan():
        if int(rec.get("holder_pid") or 0) == os.getpid():
            continue
        if int(rec.get("target_pid") or 0) == int(_mine.get("target_pid") or 0):
            return {
                "ok": False,
                "error": "CONTROLLER_BUSY",
                "blocked_by": rec,
                "remaining_s": _remaining(rec),
            }
    renew()
    return None


def release() -> bool:
    """显式释放（幂等）。返回 True = 确实释放了一份。"""
    if not _mine:
        return False
    try:
        _path_for(os.getpid()).unlink(missing_ok=True)
    except OSError:
        pass
    _mine.clear()
    _last_renew[0] = 0.0
    return True


def describe() -> str:
    """给模型看的一行租约状态（get_app / CONTROLLER_BUSY 都复用它）。"""
    if not _mine:
        return "当前没有持有控制器租约（动作不会被仲裁拦住）"
    return (
        f"控制器租约 {_mine.get('lease_id')}（目标 {_mine.get('target_name') or _mine.get('target_pid')}，"
        f"剩余 {_remaining(_mine):.0f} 秒，每次动作自动续期）"
    )


def describe_holder(rec: dict) -> str:
    """拦路的那份租约长什么样（CONTROLLER_BUSY 的文案素材）。"""
    if not rec:
        return "未知持有者"
    return (
        f"{rec.get('holder') or rec.get('holder_pid')}"
        f"（租约 {rec.get('lease_id')}，目标 {rec.get('target_name') or rec.get('target_pid')}，"
        f"剩余 {_remaining(rec):.0f} 秒）"
    )


atexit.register(release)
