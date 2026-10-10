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
import re
import sys
import time
from pathlib import Path

#: 默认租约 TTL（秒）：太短会频繁失效，太长会卡住其他会话（§7 风险 6）
TTL = 60.0
#: 续期写盘的最小间隔（秒）；判据（别人的租约）每次动作都真扫，只是不必每次写盘
RENEW_MIN_S = 1.0
_DIR = "leases"
_LOG = "leases.log"

#: 本进程持有的租约，**按持有者键分开**（内存真相；写盘的副本供别的会话判断）。
#: 持有者键 = 调用方身份（DSH 里是 session id）。同一个服务器进程服务多个会话时，
#: 它们各自持有一份 —— 这正是「同一个 DSH 实例内也能仲裁」的前提（任务书多会话一节）。
_mine: dict[str, dict] = {}
_last_renew: dict[str, float] = {}


def holder_key(value: str = "") -> str:
    """把调用方身份收敛成一个安全的文件名/比较键；空 = 退回本进程 pid。

    旧版本只有「持有者 = 进程」这一种粒度，所以没有身份的调用方保持老语义（等价于以前的行为）。
    """
    raw = str(value or "").strip()
    if not raw:
        return str(os.getpid())      # 旧语义：持有者就是进程（文件名也保持 <pid>.json 不变）
    return re.sub(r"[^A-Za-z0-9._-]", "_", raw)[:64]


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


def _scan(me: str = "") -> list[dict]:
    """所有租约（含同一个进程里**别的会话**那些）；顺手回收死进程/过期留下的文件。

    `me` = 我自己的持有者键：只有它被跳过。同一个进程里别的会话（同一个 pid、不同键）
    必须算「别人」—— 否则同一个 DSH 实例内的多会话永远拿不到 CONTROLLER_BUSY。
    """
    out: list[dict] = []
    try:
        files = sorted(_dir().glob("*.json"))
    except OSError:
        return out
    now = time.time()
    mine_key = holder_key(me)
    for path in files:
        rec = _read_file(path)
        holder = rec.get("holder_pid")
        if not rec or not holder:
            _drop(path, "文件损坏")
            continue
        if holder_key(rec.get("holder_key") or str(holder)) == mine_key:
            out.append(rec)          # 我自己的：由 _mine 代表，这里只是别被当成别人
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


def _path_for(holder: str, holder_pid: int | None = None) -> Path:
    """租约文件：**一个持有者一份**（键 = 会话身份；没有身份就是 <pid>.json，与旧版同名）。"""
    return _dir() / f"{holder_key(holder)}.json"


def _write(rec: dict) -> None:
    path = _path_for(rec.get("holder_key") or str(rec["holder_pid"]), rec["holder_pid"])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        _log(f"租约写盘失败 {path}：{e}")


def _label(holder: str) -> str:
    """租约里给人看的持有者名字：有会话身份就写它（CONTROLLER_BUSY 要能分辨是谁）。"""
    if not holder:
        return holder_label()
    short = holder if len(holder) <= 26 else holder[:26] + "…"
    return f"会话 {short}"


def mine(holder: str = "") -> dict:
    """这个持有者当前持有的租约（空 dict = 没持有）。"""
    return dict(_mine.get(holder_key(holder)) or {})


def _remaining(rec: dict) -> float:
    return max(0.0, float(rec.get("renewed_at") or 0) + float(rec.get("ttl") or ttl()) - time.time())


def acquire(target_pid: int, target_name: str = "", force: bool = False, holder: str = "") -> dict:
    """申请（或续期）``target_pid`` 的租约，持有者是 ``holder``。

    ``holder`` = 调用方身份（DSH 里是会话 id）；留空 = 本进程（旧语义：持有者就是进程）。
    返回 ``{"ok", "lease_id", "remaining_s", "warning", "blocked_by"}``：
    ``ok=False`` 且带 ``blocked_by`` = 别人正持有同一目标且活着（CONTROLLER_BUSY）。
    目标不同但别人也在操作时仍然放行，只带一句 warning。
    """
    key = holder_key(holder)
    label = _label(holder)
    now = time.time()
    ttl_s = ttl()
    target = int(target_pid)
    blocked: dict | None = None
    warning = ""
    for rec in _scan(key):
        if holder_key(rec.get("holder_key") or str(rec.get("holder_pid"))) == key:
            continue
        same = int(rec.get("target_pid") or 0) == target
        left = _remaining(rec)
        if same and not force:
            blocked = rec
            break
        if same and force:
            _log(
                f"抢占：{rec.get('lease_id')}（{rec.get('holder')}，目标 {rec.get('target_name') or rec.get('target_pid')}）"
                f" → {label}（目标 {target_name or target}）"
            )
            # 抢占必须**真的**把对方的记录收掉：留着它，被抢的一方下次 guard() 仍然看得见
            # 自己那份、抢占方也会被自己刚抢来的文件挡住 —— 两边一起卡死。
            _drop(_path_for(rec.get("holder_key") or str(rec.get("holder_pid")), rec.get("holder_pid")),
                  f"被 {label} 抢占")
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
    cur = _mine.get(key)
    if not cur or int(cur.get("target_pid") or 0) != target:
        _mine[key] = {
            "lease_id": f"l-{key}-{int(now * 1000)}",
            "holder_key": key,
            "holder_pid": os.getpid(),
            "holder": label,
            "target_pid": target,
            "target_name": target_name or "",
            "acquired_at": now,
            "renewed_at": now,
            "ttl": ttl_s,
        }
    else:
        cur["renewed_at"] = now
        cur["ttl"] = ttl_s
        cur["target_name"] = target_name or cur.get("target_name") or ""
    rec = _mine[key]
    _write(rec)
    _last_renew[key] = now
    return {
        "ok": True,
        "lease_id": rec["lease_id"],
        "remaining_s": _remaining(rec),
        "warning": warning,
        "blocked_by": None,
    }


def renew(holder: str = "") -> None:
    """续期（写盘节流；内存里的 renewed_at 每次都更新，写盘至少间隔 RENEW_MIN_S）。"""
    key = holder_key(holder)
    rec = _mine.get(key)
    if not rec:
        return
    now = time.time()
    rec["renewed_at"] = now
    if now - _last_renew.get(key, 0.0) >= RENEW_MIN_S:
        _write(rec)
        _last_renew[key] = now


def guard(target_pid: int | None = None, holder: str = "") -> dict | None:
    """动作前的租约检查：``None`` = 放行；否则是给模型的阻塞信息（CONTROLLER_BUSY）。

    自己的租约被别人 ``force`` 抢走后，这里必须挡住 —— 否则「抢占」只是写了一条日志。
    """
    key = holder_key(holder)
    rec_mine = _mine.get(key)
    if not rec_mine:
        return None                       # 没申请过租约（纯坐标路径）→ 不介入
    if target_pid is not None and int(rec_mine.get("target_pid") or 0) != int(target_pid):
        return None                       # 换了目标应用：get_app 会重新申请
    for rec in _scan(key):
        if holder_key(rec.get("holder_key") or str(rec.get("holder_pid"))) == key:
            continue
        if int(rec.get("target_pid") or 0) == int(rec_mine.get("target_pid") or 0):
            return {
                "ok": False,
                "error": "CONTROLLER_BUSY",
                "blocked_by": rec,
                "remaining_s": _remaining(rec),
            }
    renew(holder)
    return None


def release(holder: str = "") -> bool:
    """释放这个持有者的租约（幂等）。返回 True = 确实释放了一份。"""
    key = holder_key(holder)
    rec = _mine.pop(key, None)
    _last_renew.pop(key, None)
    if not rec:
        return False
    try:
        _path_for(key, rec.get("holder_pid")).unlink(missing_ok=True)
    except OSError:
        pass
    return True


def release_all() -> int:
    """本进程持有的全部租约一起放掉（急停 / 进程退出）。返回释放的份数。"""
    count = 0
    for key in list(_mine):
        if release(key):
            count += 1
    return count


def describe(holder: str = "") -> str:
    """给模型看的一行租约状态（get_app / CONTROLLER_BUSY 都复用它）。"""
    rec = _mine.get(holder_key(holder))
    if not rec:
        return "当前没有持有控制器租约（动作不会被仲裁拦住）"
    return (
        f"控制器租约 {rec.get('lease_id')}（持有者 {rec.get('holder')}，"
        f"目标 {rec.get('target_name') or rec.get('target_pid')}，"
        f"剩余 {_remaining(rec):.0f} 秒，每次动作自动续期）"
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


atexit.register(release_all)
