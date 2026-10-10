#!/usr/bin/env python3
"""dsh-computer-use 的 OS 级实时预览浮窗（画中画）—— **唯一的预览面**。

对齐 ZCode / Qoder 的 computer-use 观察窗形态：它是浮在所有应用之上的原生面板，
而不是宿主窗口里的一块 DOM —— 这样 Agent 在操作别的 app 时，你才看得见它在干什么，
而且它能拖到桌面的任何位置（包括 dsh 窗口之外）。原先那个「内嵌卡片」半区已退休
（`lib/client.js` 保留实现但不再声明 `dsh.client`，所以 dsh 不会再注入它）。

行为约定：
  - 画面 = /tmp/dsh-computer-use/latest.jpg，按 mtime 变化重载（250ms 观察一次）；
  - 等比缩放、不裁切（对齐 setPreservesAspectRatio / ResizeAspect）；
  - 整块画面可拖（对齐原生面板 setMovableByWindowBackground），不抢焦点、不进 Dock；
    拖完的位置记在 panel.pos 里，下次拉起还在原地（默认右下角）；
  - **完成勾与收起只看 agent 活动**（见 `_panel_state`）：这一轮还在跑就一直上屏；这一轮刚结束
    就立刻打绿色 ✓，再安静 `DSH_CUA_HIDE_S`（默认 25 秒）收起。判据里**没有视频帧的 mtime** ——
    帧只决定「有没有东西可看」。拿不到 host 半区的 `turn.json` 时退回纯时间判定
    （`DSH_CUA_DONE_S` / `DSH_CUA_DORMANT_S`）。收起时写 `panel.state=hidden`，host 半区据此停掉抓屏；
  - 右上角 × = 「现在收起来」，**不是**「以后别再出现」：只 orderOut，进程留着，等 Agent
    安静够 HIDE_QUIET_S（默认 2 分钟，说明上一轮活儿干完了）之后的下一次动作，它自己回来
    —— 用户不需要去找任何「重新打开」的入口（用户侧本来也没有：没有菜单栏图标、没有 Dock、
    没有 GUI 开关）。真正「以后别再自动出现」是另一件事，只有 Agent 的 preview(enable=False)
    能做：它写 panel.hidden=off，并在工具结果里明确给出恢复办法；
  - 左上角 ⏹ / ▶ = **急停开关**（设计任务书 §3.5 的第三档）：写的是与
    server/killswitch.py 同一个 `kill` 文件，MCP 服务器在下一个可中断点（≤0.1 秒）进入只读，
    动作类工具从此回 ERROR[KILLED]。急停期间浮窗**一直显示**（否则用户找不到恢复入口），
    再按一下 ▶ 解除 —— 它是这个功能唯一的用户侧入口，状态必须一直看得见；
  - 没人用了就自己藏起来（判据同上）→ orderOut，下一次动作自动回来；
    拉起它的 MCP 服务器退出 → 本进程一起退出，不留孤儿窗口；停手 IDLE_EXIT_S 之后整个进程退出。

由 server.py 在第一次电脑操作时自动拉起（`DSH_CUA_NO_PANEL=1` 可关掉）；本脚本自身不依赖 MCP。
"""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

import objc
from AppKit import (
	NSApplication,
	NSApplicationActivationPolicyAccessory,
	NSBackingStoreBuffered,
	NSButton,
	NSColor,
	NSAttributedString,
	NSForegroundColorAttributeName,
	NSFont,
	NSImage,
	NSImageScaleProportionallyUpOrDown,
	NSImageView,
	NSMakeRect,
	NSPanel,
	NSScreen,
	NSStatusWindowLevel,
	NSEvent,
	NSTextField,
	NSTimer,
	NSView,
	NSWindowCollectionBehaviorCanJoinAllSpaces,
	NSWindowCollectionBehaviorFullScreenAuxiliary,
	NSWindowStyleMaskBorderless,
	NSWindowStyleMaskNonactivatingPanel,
)

BASE = Path(os.environ.get("DSH_CUA_DIR", "/tmp/dsh-computer-use"))
SHOT = BASE / "latest.jpg"
STATUS = BASE / "status.txt"
HEARTBEAT = BASE / "heartbeat"
LOCK = BASE / "preview.lock"
GEOMETRY = BASE / "panel.json"
POS = BASE / "panel.pos"   # 拖动后的落点记忆（下次拉起还在原地）
HIDDEN = BASE / "panel.hidden"   # 开关状态（带时间戳：值变了才算新指令）。
                                 # "off:" = 明确不再自动出现（只有 preview(True) 能解）；
                                 # "hide:" = 用户点了 ×，本次活跃期内先收起来；
                                 # "on:" = 明确要开。server 只认 "off"。
KILL = BASE / "kill"   # 急停标志，格式必须与 server/killswitch.py 一致：
                       # "on:<毫秒时间戳>:<原因>" = 只读；文件不存在 = 正常
TURN = BASE / "turn.json"    # host 半区写：{agent, running, at} —— 这一轮还在不在跑
STATE = BASE / "panel.state"  # 本进程写："up:<ts>" / "hidden:<ts>"（host 半区据此决定是否抓屏）

WIDTH = 320
# 完成勾（画面中央的绿 ✓）= 「这一轮干完了」。判据是 **agent 的活动**，不是有没有新视频帧：
# 以前它挂在 latest.jpg 的 mtime 上，而 SCK 推流只要画面有变化（用户动一下鼠标、屏幕上任何
# 动画）就重写那一帧 → ✓ 被抹掉。现在只有 agent 状态能影响它（见 `_panel_state`）。
DONE_S = float(os.environ.get("DSH_CUA_DONE_S") or 8.0)     # 拿不到 agent 状态时的兜底：安静这么久 = 完成
HIDE_S = float(os.environ.get("DSH_CUA_HIDE_S") or 25.0)    # 这一轮结束 + 安静这么久 → 收起（原来是 120 秒，太长了）
DORMANT_S = float(os.environ.get("DSH_CUA_DORMANT_S") or 60.0)  # 拿不到 agent 状态时的兜底收起时间
HIDE_QUIET_S = 120.0   # 点了 × 之后：Agent 安静够这么久 = 上一轮活儿干完了，解除收起
IDLE_EXIT_S = 900.0   # 停手 15 分钟就整个退掉：任务完了不该留一个 ~35MB 的进程挂着，
                      # 下一次动作 server 会再把它拉起来（落点照 panel.pos 恢复）
MARGIN = 16
BOTTOM = 96

_QUIT = False  # 收到 SIGTERM/SIGINT 时由主循环自己收尾
_HIDE_AT: float | None = None  # 用户点 × 的时刻（None = 没在临时收起）

MIN_X = 1
MAX_X = 4
MIN_Y = 16
MAX_Y = 32


def _mtime(path: Path) -> float:
	try:
		return path.stat().st_mtime
	except OSError:
		return 0.0


def _agent_ts() -> float:
	"""最近一次 Agent 动作的时间（字幕 / 心跳取较新者），0 = 从没动过。"""
	return max(_mtime(STATUS), _mtime(HEARTBEAT))


def _turn_running() -> bool | None:
	"""这台电脑上还有 agent 在跑吗 —— host 半区每 2 秒把「最近一次 computer-use 调用属于哪个
	会话、它现在还在不在跑」写进 turn.json。

	`None` = 拿不到（老版本 host 半区、独立运行、文件过期）→ 退回纯时间判定。
	拿得到的时候才能做到「agent 在想事情的几十秒里窗不走、这一轮一结束就收起」。
	"""
	try:
		rec = json.loads(TURN.read_text(encoding="utf-8"))
		if time.time() * 1000 - int(rec.get("at") or 0) > 15000:   # host 半区每 4 秒续一次
			return None
		return bool(rec.get("running"))
	except (OSError, ValueError, TypeError):
		return None


def _panel_state(now: float, agent_ts: float, running: bool | None) -> tuple[bool, bool]:
	"""纯决策：(该不该上屏, 该不该打完成勾)。

	**输入里没有视频帧的 mtime** —— 帧只决定「有没有东西可看」，不该决定「该不该显示/完成没完成」。
	（这条就是那个 bug：用户一碰鼠标，SCK 推流重写 latest.jpg，✓ 就没了。）

	  * agent 这一轮还在跑（running is True）→ 一直上屏、不打勾；
	  * 这一轮刚结束（running is False）→ 立刻打勾（1.5 秒地板，免得连轴转时闪），
	    再安静 HIDE_S 秒就收起；
	  * 拿不到状态（None）→ 退回时间判定：安静 DONE_S 秒打勾、DORMANT_S 秒收起。
	"""
	if agent_ts <= 0 or not SHOT.exists():
		return False, False
	quiet = now - agent_ts
	if running is True:
		return True, False
	limit = HIDE_S if running is not None else DORMANT_S
	up = quiet <= limit
	done = up and quiet >= 1.5 and (running is False or quiet >= DONE_S)
	return up, done


def _save_panel_state(up: bool) -> None:
	"""把「在屏上 / 收起了」写进 panel.state —— host 半区据此决定还要不要为画面抓屏。

	抓屏进程活着 = 系统录屏灯亮着。以前只要浮窗**进程**还在（哪怕早就收起了）就继续抓，
	一停手还能白抓两分钟；现在收起就停（host 半区读这个文件，见 lib/index.js 的 panelWatching）。
	"""
	try:
		tmp = STATE.with_suffix(".state.tmp")
		tmp.write_text(f"{'up' if up else 'hidden'}:{int(time.time() * 1000)}", encoding="utf-8")
		os.replace(tmp, STATE)
	except OSError:
		pass


def _hide_expired(hide_at: float | None, agent_ts: float, now: float) -> bool:
	"""「现在收起来」该解除了吗：Agent 已经安静 HIDE_QUIET_S。

	为什么不是「下一次动作就回来」：用户点 × 往往正是为了在 Agent 连续操作时把它挪开，
	动作一来就弹回去等于没关。安静够 2 分钟说明那一轮活儿结束了，这时解除收起，
	用户下一次让 Agent 干活时浮窗自己回来 —— 不需要任何恢复入口。"""
	return hide_at is not None and now - agent_ts >= HIDE_QUIET_S


def _request_hide() -> None:
	"""用户点了 ×：立刻收起（下一拍 tick 里 orderOut），并记下时刻。

	不写 "off"（那是 preview(False) 的语义，会让 server 永远不再自动拉起），
	只写 "hide:" 便于外部观察状态。"""
	global _HIDE_AT  # noqa: PLW0603
	_HIDE_AT = time.time()
	_save_hidden("hide")


def _kill_on() -> bool:
	"""急停标志是不是已置上（与 killswitch.py 同一个文件、同一套格式）。"""
	try:
		return KILL.read_text(encoding="utf-8").strip().startswith("on")
	except OSError:
		return False


def _set_kill(on: bool, reason: str) -> None:
	"""置上/清掉急停标志。

	**必须原子写**：MCP 服务器每 120ms 就可能读一次这个文件（_settle 的可中断点），
	让它在半个文件上做判断，就是把「停止键」变成一个看运气的开关。
	"""
	try:
		if not on:
			KILL.unlink(missing_ok=True)
			return
		BASE.mkdir(parents=True, exist_ok=True)
		tmp = KILL.with_suffix(".kill.tmp")
		tmp.write_text(f"on:{int(time.time() * 1000)}:{reason}", encoding="utf-8")
		os.replace(tmp, KILL)
	except OSError:
		pass


def _release_hide() -> None:
	"""解除临时收起：回到自动模式（下一次动作按活跃度自己上屏），并把状态写回文件 ——
	否则 panel.hidden 里会一直留着一条已经失效的 "hide:"，外部排查时看着像还收着。"""
	global _HIDE_AT  # noqa: PLW0603
	_HIDE_AT = None
	_save_hidden("auto")


def _parent_alive() -> bool:
	"""拉起我的 MCP 服务器还在吗？没设 DSH_CUA_PARENT 时视为独立运行，不自杀。"""
	pid = os.environ.get("DSH_CUA_PARENT", "")
	if not pid:
		return True
	try:
		os.kill(int(pid), 0)
	except (OSError, ValueError):
		return False
	return True


def _drop_lock() -> None:
	for path in (LOCK, GEOMETRY, STATE):
		try:
			path.unlink()
		except OSError:
			pass


def _report_geometry(panel, done: bool = False) -> None:
	"""把面板几何写进 panel.json —— 它是独立进程的浮层，不在 dsh 的 AX 树里，
	Agent 的坐标路径只有拿到真实位置才能瞄到 × 和拖动手势。坐标一律用
	「全局、左上角原点、y 向下」（与 CGWindowBounds 同一套），收起时删掉文件。"""
	if panel is None:
		try:
			GEOMETRY.unlink()
		except OSError:
			pass
		return
	frame = panel.frame()
	# AppKit 的全局坐标原点在主屏左下角，换算成 y 向下要减去主屏总高。
	top = NSScreen.screens()[0].frame().size.height - frame.origin.y - frame.size.height
	btn = panel.contentView().close.frame()
	halt = panel.contentView().stop.frame()
	payload = {
		# 完成勾是不是正显示着（Agent 与测试都据此判断「这一轮画完了没有」）
		"done": bool(done),
		"x": round(frame.origin.x),
		"y": round(top),
		"width": round(frame.size.width),
		"height": round(frame.size.height),
		"close": [round(frame.origin.x + btn.origin.x + btn.size.width / 2),
		          round(top + frame.size.height - btn.origin.y - btn.size.height / 2)],
		# 急停键的位置：Agent 要能告诉用户「按左上角那个 ⏹」（用户侧唯一的恢复入口）
		"stop": [round(frame.origin.x + halt.origin.x + halt.size.width / 2),
		         round(top + frame.size.height - halt.origin.y - halt.size.height / 2)],
	}
	tmp = GEOMETRY.with_suffix(".json.tmp")
	try:
		tmp.write_text(json.dumps(payload), encoding="utf-8")
		os.replace(tmp, GEOMETRY)
	except OSError:
		pass


class _ImageView(NSImageView):  # noqa: N801
	"""铺满画面的截图视图。必须让命中测试穿透：NSImageView 自己会消费 mouseDown
	（它支持把图拖出去），那样 setMovableByWindowBackground_ 永远收不到拖动事件。"""

	def hitTest_(self, point):  # noqa: N802
		return None


class _Passthrough(NSView):  # noqa: N801
	"""同上：完成徽章只是装饰，不能挡住拖拽。"""

	def hitTest_(self, point):  # noqa: N802
		return None


class _Panel(NSView):  # noqa: N801
	"""圆角底 + 画面 + 完成徽章 + 关闭键 + 急停键。子视图顺序即层叠顺序，画面必须在最下。"""

	badge = None
	close = None
	stop = None
	_killed = None

	def initWithFrame_(self, frame):  # noqa: N802
		self = objc.super(_Panel, self).initWithFrame_(frame)
		if self is None:
			return None
		self.setWantsLayer_(True)
		self.layer().setCornerRadius_(10)
		self.layer().setMasksToBounds_(True)
		self._bg = NSColor.colorWithRed_green_blue_alpha_(16 / 255, 16 / 255, 20 / 255, 1)
		self.layer().setBackgroundColor_(self._bg.CGColor())

		self.image = _ImageView.alloc().initWithFrame_(NSMakeRect(0, 0, frame.size.width, frame.size.height))
		self.image.setImageScaling_(NSImageScaleProportionallyUpOrDown)  # 等比适配，不裁切
		self.image.setEditable_(False)
		self.image.setImageFrameStyle_(0)
		self.addSubview_(self.image)

		side = 52
		badge = _Passthrough.alloc().initWithFrame_(
			NSMakeRect((frame.size.width - side) / 2, (frame.size.height - side) / 2, side, side)
		)
		badge.setWantsLayer_(True)
		badge.layer().setCornerRadius_(side / 2)
		badge.layer().setBackgroundColor_(
			NSColor.colorWithRed_green_blue_alpha_(46 / 255, 184 / 255, 84 / 255, 0.92).CGColor()
		)
		tick = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 11, side, 30))
		tick.setStringValue_("✓")
		tick.setBezeled_(False)
		tick.setDrawsBackground_(False)
		tick.setEditable_(False)
		tick.setSelectable_(False)
		tick.setAlignment_(1)  # 居中
		tick.setFont_(NSFont.boldSystemFontOfSize_(26))
		tick.setTextColor_(NSColor.whiteColor())
		badge.addSubview_(tick)
		badge.setHidden_(True)
		badge.setAutoresizingMask_(MIN_X | MAX_X | MIN_Y | MAX_Y)  # 随窗口保持居中
		self.addSubview_(badge)
		self.badge = badge

		close = NSButton.buttonWithTitle_target_action_("×", self, "onClose:")
		close.setBordered_(False)
		close.setAttributedTitle_(
			NSAttributedString.alloc().initWithString_attributes_(
				"×", {NSForegroundColorAttributeName: NSColor.whiteColor()}
			)
		)
		close.setFrame_(NSMakeRect(frame.size.width - 26, frame.size.height - 26, 26, 26))
		close.setAutoresizingMask_(MIN_X | MAX_Y)  # 钉在右上（AppKit 的 y 轴向上）
		self.addSubview_(close)
		self.close = close

		# 左上角：急停 / 恢复。它是这个功能唯一的用户侧入口，所以放在最显眼、最不容易
		# 误触的位置（与 × 隔着整条上边），并且状态直接写在按钮上（⏹ 停 / ▶ 恢复）。
		stop = NSButton.buttonWithTitle_target_action_("⏹", self, "onStop:")
		stop.setBordered_(False)
		stop.setAttributedTitle_(
			NSAttributedString.alloc().initWithString_attributes_(
				"⏹", {NSForegroundColorAttributeName: NSColor.whiteColor()}
			)
		)
		stop.setFrame_(NSMakeRect(2, frame.size.height - 26, 30, 26))
		stop.setAutoresizingMask_(MAX_X | MAX_Y)
		self.addSubview_(stop)
		self.stop = stop
		self._refresh_stop(False)
		return self

	def _refresh_stop(self, killed: bool) -> None:
		"""按钮标题与底色跟着急停状态走（状态变了才动，别每 250ms 抖一次窗口）。"""
		if self._killed == killed:
			return
		self._killed = killed
		self.stop.setAttributedTitle_(
			NSAttributedString.alloc().initWithString_attributes_(
				"▶" if killed else "⏹", {NSForegroundColorAttributeName: NSColor.whiteColor()}
			)
		)
		self.layer().setBackgroundColor_(
			NSColor.colorWithRed_green_blue_alpha_(120 / 255, 32 / 255, 36 / 255, 1).CGColor() if killed
			else self._bg.CGColor()
		)

	def onStop_(self, sender):  # noqa: N802
		"""⏹ = 急停，▶ = 解除。"""
		killed = _kill_on()
		_set_kill(not killed, "用户点了预览浮窗上的停止键")
		self._refresh_stop(not killed)

	def onClose_(self, sender):  # noqa: N802
		# × = 「现在收起来」：只收起，进程留着，Agent 安静够了下一次动作自己回来。
		# 「以后别再自动出现」不在这里 —— 那是 Agent 的 preview(False)，用户随手一点
		# 不该造成一个他再也找不到开关去恢复的状态（这条是真踩过的坑）。
		_request_hide()

	# ---- 按住画面任意处拖动（对齐原生面板 setMovableByWindowBackground 的手感）。
	# 这里自己算：AppKit 的背景拖拽对合成事件（CGEvent / 脚本发的按住移动）不总是生效，
	# 而观察窗恰恰主要被脚本操作。 ----
	def acceptsFirstMouse_(self, event):  # noqa: N802
		# 面板是 nonactivating 的 borderless 窗口，从来不是 key window；默认第一下点击
		# 会被系统当成「激活窗口」而吞掉，mouseDown_ 根本收不到 → 拖不动。
		# （× 是 NSButton，它本来就接受首次点击，所以只有它能点动。）
		return True

	def mouseDown_(self, event):  # noqa: N802
		self._grab = event.locationInWindow()

	def mouseDragged_(self, event):  # noqa: N802
		win = self.window()
		if win is None or getattr(self, "_grab", None) is None:
			return
		point = NSEvent.mouseLocation()  # 屏幕坐标（y 向上）
		win.setFrameOrigin_((point.x - self._grab.x, point.y - self._grab.y))

	def mouseUp_(self, event):  # noqa: N802
		self._grab = None
		_save_origin(self.window())   # 拖完记住落点，下次拉起还在原地


def _saved_origin() -> tuple[float, float] | None:
	"""上次拖到的位置（AppKit 坐标：原点在主屏左下角、y 向上）。越界就当没有。

	要检查「和某块屏幕的可见区域有交集」：换了显示器/拔了外接屏之后，旧坐标可能落在
	屏幕之外，照搬会把窗拉到看不见的地方。"""
	try:
		d = json.loads(POS.read_text(encoding="utf-8"))
		x, y = float(d["x"]), float(d["y"])
	except (OSError, ValueError, KeyError, TypeError):
		return None
	for screen in NSScreen.screens():
		f = screen.visibleFrame()
		if (x + WIDTH > f.origin.x and x < f.origin.x + f.size.width
				and y + 40 > f.origin.y and y < f.origin.y + f.size.height):
			return x, y
	return None


def _save_origin(win) -> None:
	"""原子写：读侧（下一次拉起）永远看到完整 JSON。"""
	if win is None:
		return
	try:
		frame = win.frame()
		tmp = POS.with_suffix(".pos.tmp")
		tmp.write_text(json.dumps({"x": round(frame.origin.x), "y": round(frame.origin.y)}), encoding="utf-8")
		os.replace(tmp, POS)
	except OSError:
		pass


def _save_hidden(state: str) -> None:
	"""记下「用户关掉了」/「明确要开」——server 侧据此决定要不要自动拉起。

	和 card.txt 同一套 nonce 语义：带毫秒时间戳，只有值真的变了才算一次新指令，
	否则用户点过 × 之后，Agent 再写一次同样的 off/on 没有任何信号。"""
	try:
		tmp = HIDDEN.with_suffix(".hidden.tmp")
		tmp.write_text(f"{state}:{int(time.time() * 1000)}", encoding="utf-8")
		os.replace(tmp, HIDDEN)
	except OSError:
		pass


def _panel_height() -> float:
	"""按截屏宽高比定高：等比、不裁切。"""
	ratio = 1039 / 1600
	try:
		size = NSImage.alloc().initWithContentsOfFile_(str(SHOT)).size()
		if size and size.width > 0:
			ratio = size.height / size.width
	except Exception:
		pass
	return round(WIDTH * max(0.3, min(ratio, 2.0)), 1)


def main():
	BASE.mkdir(parents=True, exist_ok=True)
	app = NSApplication.sharedApplication()
	app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # 不进 Dock、不抢焦点

	height = _panel_height()
	visible = NSScreen.mainScreen().visibleFrame()
	origin = _saved_origin()
	if origin:                                   # 用户拖过就待在原地
		x, y = origin
	else:                                        # 默认：右下角（避开 dsh 的输入框那一带）
		x = visible.origin.x + visible.size.width - WIDTH - MARGIN
		y = visible.origin.y + BOTTOM

	panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
		NSMakeRect(x, y, WIDTH, height),
		NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
		NSBackingStoreBuffered,
		False,
	)
	panel.setLevel_(NSStatusWindowLevel)  # 浮在所有应用之上
	panel.setOpaque_(False)
	panel.setBackgroundColor_(NSColor.clearColor())
	panel.setHasShadow_(True)
	# 拖动由 _Panel.mouseDragged_ 自己实现，别让 AppKit 再叠一层
	panel.setMovable_(True)
	panel.setCollectionBehavior_(
		NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary
	)
	panel.setHidesOnDeactivate_(False)
	panel.setReleasedWhenClosed_(False)
	panel.setTitle_("computer-use 实时预览")

	content = _Panel.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, height))
	panel.setContentView_(content)
	panel.makeKeyAndOrderFront_(None)
	panel.orderFrontRegardless()

	state = {"shown": None, "up": False}

	def tick(timer):  # noqa: ARG001 - NSTimer block 签名
		if not _parent_alive() or _QUIT:
			_drop_lock()
			NSApplication.sharedApplication().terminate_(None)
			return
		now = time.time()
		agent_ts = _agent_ts()
		if _hide_expired(_HIDE_AT, agent_ts, now):
			_release_hide()      # 上一轮活儿干完了：回到自动模式，下一次动作浮窗自己回来
		killed = _kill_on()
		content._refresh_stop(killed)
		up, done = _panel_state(now, agent_ts, _turn_running())
		# 只在「藏 ↔ 显」切换时动窗口层级：每 250ms 反复 orderFront 是无谓的窗口服务
		# 抖动（用户实测期间出现过两次 Mission Control，先把这个嫌疑源去掉）。
		# 急停期间**一直显示**：▶ 是用户侧唯一的恢复入口，把它藏起来等于让用户没法恢复。
		if killed or (_HIDE_AT is None and up):
			if not state["up"]:
				panel.orderFrontRegardless()
				state["up"] = True
				_save_panel_state(True)
		else:
			if state["up"]:
				panel.orderOut_(None)
				state["up"] = False
				_save_panel_state(False)
			_report_geometry(None)
			# 藏久了就整个退出（「任务完自动关闭」）：进程 ~35MB，没必要一直挂着；
			# 下一次动作 server 会重新拉起，位置按 panel.pos 恢复。
			if now - agent_ts > IDLE_EXIT_S:
				_drop_lock()
				NSApplication.sharedApplication().terminate_(None)
			return
		_report_geometry(panel, done)
		shot = _mtime(SHOT)
		if shot and state["shown"] != shot:  # mtime 变了才解码，别每秒白读四次盘
			image = NSImage.alloc().initWithContentsOfFile_(str(SHOT))
			if image is not None:
				content.image.setImage_(image)
				state["shown"] = shot
		# 完成勾只跟 agent 状态走 —— 绝不掺视频帧的 mtime（一掺，用户动一下鼠标就把它抹掉）
		content.badge.setHidden_(not done)

	NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.25, True, tick)
	app.run()


if __name__ == "__main__":
	BASE.mkdir(parents=True, exist_ok=True)
	LOCK.write_text(str(os.getpid()), encoding="utf-8")

	def _term(signum, frame):  # noqa: ARG001 - 信号处理器签名
		# 只置标志：从信号处理器里抛 SystemExit 会被 pyobjc 的 block 边界吞掉，
		# 直接调 AppKit 又不安全；真正的退出交给下一次 tick（主线程、run loop 内）。
		global _QUIT  # noqa: PLW0603
		_QUIT = True
		_drop_lock()

	signal.signal(signal.SIGTERM, _term)
	signal.signal(signal.SIGINT, _term)
	try:
		main()
	finally:
		_drop_lock()
		try:
			GEOMETRY.unlink()
		except OSError:
			pass
