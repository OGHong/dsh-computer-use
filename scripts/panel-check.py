#!/usr/bin/env python3
"""观察窗「什么时候收起 / 什么时候打完成勾」的回归闸门。

这一段以前埋在一个 250ms 的 tick 里，只有肉眼看窗口才能发现问题，而它真的踩了两个坑：

  1. **完成勾挂在视频帧的 mtime 上**：SCK 推流只要有画面变化就重写 latest.jpg，于是用户动一下
     鼠标（或屏幕上任何动画）就把绿 ✓ 抹掉了。判据必须是 **agent 的活动**，不是有没有新帧。
  2. **收起时间写死 120 秒**：任务早结束了，窗还杵在那儿两分钟。
  3. **用户按下的 × 被自己解除**（2026-10-11 用户报「观察窗又无法手动关闭了」）：解除收起原本
     也挂在「安静 HIDE_QUIET_S」上，而安静只说明 agent 没在碰电脑、不说明这一轮结束了 ——
     于是长回合里点 × ，窗要么当场收起又拉起（实测 0.30 秒），要么过两分钟自己回来。现在解除
     只看**下一轮开始了没有**（`turn.json` 的 running 由否转是），安静多久与它无关。

现在两侧都可以单独验：
  * 浮窗侧是两个**纯函数** `_panel_state(now, agent_ts, running)` 与
    `_hide_expired(hide_at, agent_ts, now, running, prev_running)` —— 输入里根本没有帧 mtime；
  * host 侧写 `turn.json`（这一轮还在不在跑）、读 `panel.state`（在屏上才继续抓屏）——
    由 `scripts/host-smoke.mjs` 断言。

用法：.venv/bin/python scripts/panel-check.py
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# **必须**先隔离状态目录再导入浮窗模块：这个脚本会写 latest.jpg / panel.state，
# 而真实会话正在往默认目录里写帧 —— 绝不能碰现场（还要保证 panel.state 不被我们改脏）。
CHECK_DIR = Path(os.environ.get("DSH_CUA_CHECK_DIR") or f"/tmp/dsh-cua-panel-check.{os.getpid()}")
os.environ["DSH_CUA_DIR"] = str(CHECK_DIR)
CHECK_DIR.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "server"))

try:
    import preview_window as pw
except ImportError as e:  # pyobjc 没装：这个环境本来就起不了浮窗
    print(f"… 跳过观察窗判据检查（{e}）")
    raise SystemExit(0)

fails = []


def check(label, ok, detail=""):
    print(("✓ " if ok else "✗ ") + label + (f" — {detail}" if detail else ""))
    if not ok:
        fails.append(label)


# 决策要跑得起来：得有帧、得有「agent 动过」的时间戳
pw.SHOT.write_bytes(b"\xff\xd8\xff\xd9")
NOW = time.time()
AGENT = NOW - 1.0


def state(quiet, running, agent_ts=None, now=None):
    ts = agent_ts if agent_ts is not None else (now or NOW) - quiet
    return pw._panel_state(now or NOW, ts, running)


up, done = state(0.5, True)
check("这一轮还在跑 → 上屏、不打勾", (up, done) == (True, False), f"up={up} done={done}")

up, done = state(2.0, False)
check("轮次刚结束 → 立刻打完成勾（不用等 8 秒）", (up, done) == (True, True), f"up={up} done={done}")

up, done = state(1.0, False)
check("轮次结束但有 1.5 秒地板（连轴转时不闪勾）", (up, done) == (True, False), f"up={up} done={done}")

up, done = state(pw.HIDE_S + 1.0, False)
check(f"轮次结束 + 安静超过 {pw.HIDE_S:.0f} 秒 → 收起（原来是 120 秒）",
      (up, done) == (False, False), f"up={up} done={done}")

up, done = state(5.0, None)
check("拿不到 agent 状态 → 退回时间判定：安静 5 秒还不打勾", (up, done) == (True, False), f"up={up} done={done}")

up, done = state(pw.DONE_S + 1.0, None)
check(f"拿不到 agent 状态 → 安静超过 {pw.DONE_S:.0f} 秒打勾", (up, done) == (True, True), f"up={up} done={done}")

up, done = state(pw.DORMANT_S + 1.0, None)
check(f"拿不到 agent 状态 → 安静超过兜底 {pw.DORMANT_S:.0f} 秒收起", (up, done) == (False, False), f"up={up} done={done}")

up, done = pw._panel_state(NOW, 0.0, True)
check("从没动过电脑（没有 agent 时间戳）→ 不上屏", (up, done) == (False, False), f"up={up} done={done}")

# ★ 用户按下的 × 什么时候解除（2026-10-11 实测「点了 × 没反应」）：
#   语义 = 「这一轮别给我看，下一轮开始照常弹回来」，判据是**轮次**，不是安静时长。
#   以前 _hide_expired 只看「安静够 120 秒」，而安静只说明 agent 没在碰电脑 —— 于是用户在长回合里
#   点完 × ，窗要么当场被自己收起又拉起，要么过两分钟自己回来。
def hide_expired(quiet, running, prev, clicked=True):
    return pw._hide_expired((NOW - 5.0) if clicked else None, NOW - quiet, NOW, running, prev)


check("从没点过 × → 永远不解除", hide_expired(3600.0, True, True, clicked=False) is False)

check("这一轮还在跑 → 安静 10 秒不解除", hide_expired(10.0, True, True) is False)
check("这一轮还在跑 → 安静 1 小时也不解除（思考久 ≠ 收工，这是那个 bug 的正中）",
      hide_expired(3600.0, True, True) is False)
check("上一拍还在跑、这一拍还在跑 → 不解除（不是「每一拍都算新一轮」）",
      hide_expired(0.4, True, True) is False)
check("这一轮结束了也不立刻解除（还在等下一轮）", hide_expired(5.0, False, True) is False)
check("下一轮开始（running 由否转是）→ 解除，浮窗照常自己上屏",
      hide_expired(0.4, True, False) is True)
check("「拿不到状态」变成「在跑」不算新一轮开始（那只是刚看得到，不是沿）→ 不解除",
      hide_expired(0.4, True, None) is False)
check("拿不到轮次状态 → 退回时间判定：安静 119 秒不解除",
      hide_expired(pw.HIDE_QUIET_S - 1.0, None, None) is False)
check(f"拿不到轮次状态 → 安静 {pw.HIDE_QUIET_S:.0f} 秒才解除",
      hide_expired(pw.HIDE_QUIET_S + 1.0, None, None) is True)

sig = __import__("inspect").signature(pw._hide_expired)
check("解除判据必须同时拿到 running 与上一拍 running（否则没法判「新一轮开始」）",
      list(sig.parameters) == ["hide_at", "agent_ts", "now", "running", "prev_running"],
      str(list(sig.parameters)))

# ★ 这两条就是用户报的那个 bug：帧在变（鼠标一动，SCK 就重写 latest.jpg），
#   但 agent 没动过 —— 结论必须一模一样。
before = state(2.0, False)
pw.SHOT.write_bytes(b"\xff\xd8\xff\xd9" + b"\x00" * 64)   # 新的一帧（mtime = 现在）
pw.SHOT.touch()
after = state(2.0, False)
check("鼠标移动（不断有新帧）不会改变完成勾/上屏的结论 —— 判据只看 agent 活动",
      before == after == (True, True), f"before={before} after={after}")

up, done = state(pw.HIDE_S + 1.0, False)
check("同上：帧再新也照样到点收起（帧只决定「有没有东西可看」）",
      (up, done) == (False, False), f"up={up} done={done}")

sig = __import__("inspect").signature(pw._panel_state)
check("判据函数的输入里没有帧 mtime（结构性保证，不是靠自觉）",
      list(sig.parameters) == ["now", "agent_ts", "running", "hide_s"], str(list(sig.parameters)))

# ★ 设置页（Settings → 电脑操控）写进来的偏好：收起延时。
#   它走 panel.conf（浮窗每拍重读），所以改设置**不需要重启任何进程**。
def state_conf(quiet, running, hide_s=None):
    return pw._panel_state(NOW, NOW - quiet, running, hide_s)


up, done = state_conf(13.0, False, 60.0)
check("设置页把收起延时改成 60 秒 → 安静 13 秒还在（默认 12 秒时早收了）",
      (up, done) == (True, True), f"up={up} done={done}")

up, done = state_conf(61.0, False, 60.0)
check("同一设置下安静 61 秒才收起", (up, done) == (False, False), f"up={up} done={done}")

up, done = state_conf(pw.DORMANT_S + 1.0, None, 45.0)
check("拿不到轮次状态时，设置里的延时同样生效（安静 61 秒 > 45 秒 → 收起）",
      (up, done) == (False, False), f"up={up} done={done}")

# panel.conf：设置页写的偏好镜像。读法必须容错（文件不在 / 坏了 = 全部默认行为），
# 并且 nonce 语义要对：值没变就不算新指令，否则 250ms 的 tick 会把收起时刻每拍重置。
pw.CONF.unlink(missing_ok=True)
pw._conf_cache["value"] = None
check("没有 panel.conf → 收起延时/收起请求全部走默认",
      (pw._conf_hide_seconds(), pw._conf_hide_request()) == (None, ("", 0.0)))

pw.CONF.write_text(json.dumps({"autoHideSeconds": 30,
                               "hideRequest": "hide", "hideRequestAt": 1791734836370}), encoding="utf-8")
pw._conf_cache["value"] = None
check("读 panel.conf：收起延时 30 秒", pw._conf_hide_seconds() == 30.0)
check("读 panel.conf：收起请求带方向与 nonce", pw._conf_hide_request() == ("hide", 1791734836370.0))

pw.CONF.write_text(json.dumps({"autoHideSeconds": 0}), encoding="utf-8")
pw._conf_cache["value"] = None
check("收起延时被夹住（0 → 1 秒，别让窗闪一下就没）", pw._conf_hide_seconds() == 1.0)

pw.CONF.write_text("{ 坏 json", encoding="utf-8")
pw._conf_cache["value"] = None
check("panel.conf 坏了 → 退回默认（绝不抛）",
      (pw._conf_hide_seconds(), pw._conf_hide_request()) == (None, ("", 0.0)))
pw.CONF.unlink(missing_ok=True)

# 粘性 off 不能被 × 覆盖：用户刚在设置页关掉观察窗，随手点一下浮窗的 ×（或下一轮开始时
# 那次自动解除）不该把它偷偷打开 —— 他会发现「我明明关了它怎么又出来了」。
pw.HIDDEN.write_text("off:1", encoding="utf-8")
pw._save_hidden("hide")
check("×（hide）不会覆盖设置页写的粘性 off", pw.HIDDEN.read_text(encoding="utf-8").startswith("off:"))
pw._save_hidden("auto")
check("自动解除（auto）也不会覆盖 off", pw.HIDDEN.read_text(encoding="utf-8").startswith("off:"))
pw._save_hidden("on")
check("明确要开（on）可以覆盖 off", pw.HIDDEN.read_text(encoding="utf-8").startswith("on:"))
pw.HIDDEN.unlink(missing_ok=True)

# panel.state：host 半区据此停流（录屏灯）
pw.STATE.unlink(missing_ok=True)
pw._save_panel_state(True)
check("panel.state 写 up（host 半区据此继续抓屏）",
      pw.STATE.read_text(encoding="utf-8").startswith("up:"), pw.STATE.read_text(encoding="utf-8"))
pw._save_panel_state(False)
check("panel.state 写 hidden（host 半区据此停流，录屏灯不再白亮）",
      pw.STATE.read_text(encoding="utf-8").startswith("hidden:"), pw.STATE.read_text(encoding="utf-8"))
pw.STATE.unlink(missing_ok=True)

# turn.json 的读法：三种状态必须分得开。host 半区在拿不到 agents 服务时写的是 "running": null，
# 那是「不知道」—— 读成「没在跑」会让浮窗以为一轮结束了（提前打勾、提前收起、提前解除 ×）。
def write_turn(value, age_ms=0):
    pw.TURN.write_text(
        json.dumps({"agent": "s1", "running": value, "at": int(time.time() * 1000) - age_ms}),
        encoding="utf-8")


write_turn(None)
check("turn.json 里 running=null（host 拿不到 agents 服务）→ 读成「拿不到」，不是「没在跑」",
      pw._turn_running() is None)
write_turn(True)
check("turn.json 里 running=true → True", pw._turn_running() is True)
write_turn(False)
check("turn.json 里 running=false → False", pw._turn_running() is False)
write_turn(True, age_ms=20_000)
check("turn.json 20 秒没续（host 半区每 4 秒一次）→ 过期，退回「拿不到」", pw._turn_running() is None)
pw.TURN.unlink(missing_ok=True)
pw.SHOT.unlink(missing_ok=True)

os.environ.pop("DSH_CUA_DIR", None)
shutil.rmtree(CHECK_DIR, ignore_errors=True)
print(("全部通过 ✓ " if not fails else f"失败 {len(fails)} 项: {fails}"))
raise SystemExit(1 if fails else 0)
