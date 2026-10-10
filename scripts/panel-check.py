#!/usr/bin/env python3
"""观察窗「什么时候收起 / 什么时候打完成勾」的回归闸门。

这一段以前埋在一个 250ms 的 tick 里，只有肉眼看窗口才能发现问题，而它真的踩了两个坑：

  1. **完成勾挂在视频帧的 mtime 上**：SCK 推流只要有画面变化就重写 latest.jpg，于是用户动一下
     鼠标（或屏幕上任何动画）就把绿 ✓ 抹掉了。判据必须是 **agent 的活动**，不是有没有新帧。
  2. **收起时间写死 120 秒**：任务早结束了，窗还杵在那儿两分钟。

现在两侧都可以单独验：
  * 浮窗侧是一个**纯函数** `_panel_state(now, agent_ts, running)` —— 输入里根本没有帧 mtime；
  * host 侧写 `turn.json`（这一轮还在不在跑）、读 `panel.state`（在屏上才继续抓屏）——
    由 `scripts/host-smoke.mjs` 断言。

用法：.venv/bin/python scripts/panel-check.py
"""
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
      list(sig.parameters) == ["now", "agent_ts", "running"], str(list(sig.parameters)))

# panel.state：host 半区据此停流（录屏灯）
pw.STATE.unlink(missing_ok=True)
pw._save_panel_state(True)
check("panel.state 写 up（host 半区据此继续抓屏）",
      pw.STATE.read_text(encoding="utf-8").startswith("up:"), pw.STATE.read_text(encoding="utf-8"))
pw._save_panel_state(False)
check("panel.state 写 hidden（host 半区据此停流，录屏灯不再白亮）",
      pw.STATE.read_text(encoding="utf-8").startswith("hidden:"), pw.STATE.read_text(encoding="utf-8"))
pw.STATE.unlink(missing_ok=True)
pw.SHOT.unlink(missing_ok=True)

os.environ.pop("DSH_CUA_DIR", None)
shutil.rmtree(CHECK_DIR, ignore_errors=True)
print(("全部通过 ✓ " if not fails else f"失败 {len(fails)} 项: {fails}"))
raise SystemExit(1 if fails else 0)
