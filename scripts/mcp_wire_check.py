#!/usr/bin/env python3
"""真的用 stdio 跟工具服务器握一次手 —— 手写协议层最怕的就是「看着对、发出去不对」。

覆盖：initialize（回显 protocolVersion / capabilities 形状 / serverInfo）、
notifications 不该有回包、tools/list 的 28 个工具与每个工具的 name/description/inputSchema、
tools/call 的文本与图片两种 content、缺参数、未知方法、ping、参数类型转换、
以及三条生命周期：Dock 上不留图章、宿主断开后不滞留、notifications/cancelled 真的打断动作且不回包。

v2 新增（设计任务书 §3.1~§3.6，全部**不发真实输入**）：
  - sequence v2：未知工具 / 空数组 / on_unchanged 取值 / 失败即停 / 观察伪步 / wait 步走稳定判定；
  - strategy 三态：a11y 不静默降级（拖拽、set_value(event)、非法取值）；
  - possibly_sent 的三条判定规则（参数错 false、已下发后超时 true、正常返回 false）；
  - 急停三档：环境变量 / 急停文件（= 浮窗 ⏹ 与 host 路由走的那条）/ stop-resume 工具；
  - 控制器租约：并发申请 CONTROLLER_BUSY、force 抢占、持有者崩溃后自动回收、退出自动释放；
  - 动作层契约：每个注册动作都是 ActionResult、渲染层不返回自然语言对象。
"""
import inspect
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server" / "server.py"
GOLDEN_TOOLS = 28
#: 这个检查会真的跑起服务器：给它一个自己的目录，别碰真实会话的 state
CHECK_DIR = Path(os.environ.get("DSH_CUA_CHECK_DIR") or f"/tmp/dsh-cua-wire.{os.getpid()}")


def _pid_path(pid: int) -> str:
    """进程真正的可执行文件（内核对它的看法）—— 用来看「有没有换进自建 bundle」。"""
    import ctypes
    try:
        libc = ctypes.CDLL(None)
        buf = ctypes.create_string_buffer(4096)
        n = libc.proc_pidpath(int(pid), buf, len(buf))
    except Exception:
        return ""
    return buf.value.decode("utf-8", "replace") if n else ""


def main() -> int:
    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "server"), "DSH_CUA_STREAM": "0",
           "DSH_CUA_NO_PANEL": "1", "DSH_CUA_DIR": str(CHECK_DIR),
           "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.Popen(
        [sys.executable, str(SERVER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=env, text=True, bufsize=1,
    )

    def send(msg):
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def recv(msg_id, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                raise SystemExit(f"服务器提前退出：rc={proc.poll()} stderr=" + (proc.stderr.read() or "(空)"))
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("id") == msg_id:
                return m
        raise SystemExit(f"等 id={msg_id} 超时")

    fails = []

    def check(label, ok, detail=""):
        print(("✓ " if ok else "✗ ") + label + (f" — {detail}" if detail else ""))
        if not ok:
            fails.append(label)

    def skip(label, why):
        """环境不具备时跳过：屏幕休眠这类情况让断言假失败，比不测更误导。"""
        print(f"… 跳过 {label} — {why}")

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "wire-check", "version": "1"}}})
    init = recv(1)
    r = init.get("result", {})
    check("initialize 顶层字段与官方 SDK 同形",
          sorted(r) == ["capabilities", "instructions", "protocolVersion", "serverInfo"], str(sorted(r)))
    check("protocolVersion 回显客户端版本", r.get("protocolVersion") == "2024-11-05")
    check("capabilities 齐备", sorted(r.get("capabilities", {})) == ["experimental", "prompts", "resources", "tools"])
    check("serverInfo.name", r.get("serverInfo", {}).get("name") == "computer-use")

    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    time.sleep(0.2)

    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tools = recv(2)["result"]["tools"]
    check(f"tools/list 数量 = {GOLDEN_TOOLS}", len(tools) == GOLDEN_TOOLS, str(len(tools)))
    check("每个工具都有 name/description/inputSchema",
          all(sorted(t) == ["description", "inputSchema", "name"] for t in tools))
    check("inputSchema 都是 object 且 properties 齐",
          all(t["inputSchema"].get("type") == "object" and "properties" in t["inputSchema"] for t in tools))
    click = next(t for t in tools if t["name"] == "click")
    check("必填参数被标出（click.x/y）", click["inputSchema"].get("required") == ["x", "y"], str(click["inputSchema"].get("required")))
    check("可选参数带默认值（screenshot.display=1）",
          next(t for t in tools if t["name"] == "screenshot")["inputSchema"]["properties"]["display"].get("default") == 1)

    info = recv_call(send, recv, "screen_info", {})
    check("tools/call 文本结果", info["content"][0]["type"] == "text" and "主屏逻辑尺寸" in info["content"][0]["text"],
          info["content"][0]["text"][:28].replace("\n", " "))
    shot = recv_call(send, recv, "screenshot", {})
    blocks = shot.get("content", [])
    texts = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    pics = [b for b in blocks if b.get("type") == "image"]
    # 屏幕休眠时显示器列表是空的，截图会如实回 INVALID_ARGUMENT —— 那是环境，不是回归。
    if "共 0 个" in texts:
        skip("tools/call 图片结果", "当前没有活动显示器（屏幕已休眠），这一条需要真实屏幕")
    else:
        check("tools/call 图片结果（文本 + 图两块 content，一次调用拿两样）",
              bool(pics) and len(pics[0].get("data", "")) > 1000 and "坐标系" in texts,
              f'{len(pics[0].get("data", "")) if pics else 0} base64 字符, mime={pics[0].get("mimeType") if pics else "-"}')
    err = recv_call(send, recv, "click", {}, expect_error=True)
    check("缺参数回 isError 而不是断连", err["isError"] and "缺少参数" in err["content"][0]["text"], err["content"][0]["text"][:24])

    # 参数通道：官方 SDK 的 pydantic 会顺手把字符串转成声明类型，手写层必须做同样的事，
    # 否则模型发 {"x":"500"} 就会在坐标换算里炸成 TypeError。
    coerced = recv_call(send, recv, "wait", {"seconds": "1"})
    check("字符串数字被按声明类型转换", not coerced["isError"] and "已等待 1.0" in coerced["content"][0]["text"],
          coerced["content"][0]["text"][:26])
    badtype = recv_call(send, recv, "click", {"x": "abc", "y": 1})
    check("坏类型回 INVALID_ARGUMENT（不是 INTERNAL）", badtype["isError"] and "INVALID_ARGUMENT" in badtype["content"][0]["text"],
          badtype["content"][0]["text"][:40])
    typed = recv_call(send, recv, "get_app", {"app": "a"})
    check("类型化错误码原样透出（AMBIGUOUS_APP 不被挤成 INTERNAL）",
          "AMBIGUOUS_APP" in typed["content"][0]["text"] and "INTERNAL" not in typed["content"][0]["text"],
          typed["content"][0]["text"][:34])
    extra = recv_call(send, recv, "screen_info", {"junk": 1})
    check("多余参数被忽略而不是崩", not extra["isError"], extra["content"][0]["text"][:22])

    send({"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}})
    check("ping 回空结果", "result" in recv(3))
    for method, key in (("prompts/list", "prompts"), ("resources/list", "resources"), ("resources/templates/list", "resourceTemplates")):
        send({"jsonrpc": "2.0", "id": 4, "method": method, "params": {}})
        got = recv(4)["result"]
        check(f"{method} 回空集合", got == {key: []}, str(got))
    send({"jsonrpc": "2.0", "id": 5, "method": "logging/setLevel", "params": {"level": "debug"}})
    check("logging/setLevel 不报错", "result" in recv(5))
    send({"jsonrpc": "2.0", "id": 6, "method": "totally/unknown", "params": {}})
    check("未知方法回 -32601", recv(6).get("error", {}).get("code") == -32601)

    # --- 没被真调用过的工具是静默错的高发区 ---------------------------------------
    # 会真的动鼠标/键盘的那些绝不能在这里跑（会把用户的鼠标点走、把字打到别人窗口里），
    # 所以下面挑的都是**不碰机器**的路径：只读观察、无害 echo、以及一批「本来就会报错」
    # 的分支（未知工具、非法 strategy、a11y 没有语义动作、失败即停）。
    def call_text(name, arguments):
        res = recv_call(send, recv, name, arguments)
        return "".join(b.get("text", "") for b in res.get("content") or []), res

    windows = json.dumps(recv_call(send, recv, "list_windows", {}), ensure_ascii=False)
    check("list_windows 在真机上走通（只读）", "ERROR[" not in windows, windows[:70])

    shell = json.dumps(recv_call(send, recv, "run_shell", {"command": "echo wire-check"}), ensure_ascii=False)
    check("run_shell 能跑一条无害命令并回文本", "wire-check" in shell and "ERROR[" not in shell, shell[:70])

    # --- strategy 三态：a11y 不静默降级（这几条都不会发出任何输入） -----------------
    sv, _ = call_text("set_value", {"index": 0, "value": "x", "strategy": "event"})
    check("strategy=event 在只有无障碍路径的 set_value 上明确报错（不假装成功）",
          "ACTION_UNAVAILABLE" in sv, sv[:80])
    bad, _ = call_text("click", {"x": 1, "y": 1, "strategy": "bogus"})
    check("非法 strategy 在参数校验阶段就被拒（不会点到任何地方，也不会标 possibly_sent）",
          "INVALID_ARGUMENT" in bad and "possibly_sent" not in bad, bad[:90])
    dr, _ = call_text("drag", {"from_x": 0, "from_y": 0, "to_x": 1, "to_y": 1, "strategy": "a11y"})
    check("strategy=a11y 拖拽直接报 ACTION_UNAVAILABLE（有合成事件可用也不降级）",
          "ACTION_UNAVAILABLE" in dr, dr[:80])

    # --- sequence v2：全部动作工具 + observe 伪步 + 失败即停 -------------------------
    txt, _ = call_text("sequence", {"steps": [{"tool": "no-such-tool", "args": {}}]})
    check("sequence 的未知工具分支被走到（不是静默跳过）", "未知工具" in txt, txt[:70])

    txt, _ = call_text("sequence", {"steps": []})
    check("sequence 空数组回 INVALID_ARGUMENT", "INVALID_ARGUMENT" in txt, txt[:60])

    txt, _ = call_text("sequence", {"steps": [{"tool": "wait", "args": {"seconds": 1}}], "on_unchanged": "nope"})
    check("on_unchanged 只认 continue/abort", "INVALID_ARGUMENT" in txt and "on_unchanged" in txt, txt[:70])

    txt, _ = call_text("sequence", {"steps": [{"tool": "set_value", "args": {"index": 0, "value": "x", "strategy": "event"}}]})
    check("sequence 跑得动元素路径工具（v1 白名单里根本没有 set_value）",
          "[set_value]" in txt and "ACTION_UNAVAILABLE" in txt, txt[:90])
    check("失败默认停止，并报出停在哪一步 + 已执行的副作用清单",
          "在第 1 步停止" in txt and "副作用清单" in txt, txt[:120])

    txt, _ = call_text("sequence", {
        "steps": [{"tool": "drag", "args": {"from_x": 0, "from_y": 0, "to_x": 1, "to_y": 1, "strategy": "a11y"}}],
        "on_unchanged": "abort",
    })
    check("on_unchanged=abort 被接住（合法取值不报 INVALID_ARGUMENT）",
          "INVALID_ARGUMENT" not in txt and "ACTION_UNAVAILABLE" in txt, txt[:80])

    seq_missing, _ = call_text("sequence", {"steps": [{"tool": "set_value", "args": {"index": 0}}]})
    check("批处理里缺必填参数回 INVALID_ARGUMENT（不是 INTERNAL，模型能自纠）",
          "INVALID_ARGUMENT" in seq_missing and "value" in seq_missing, seq_missing[:90])

    txt, _ = call_text("sequence", {"steps": [{"tool": "wait", "args": {"seconds": 1}}]})
    check("wait 步走稳定判定并回真实 settle_ms", "sequence 完成" in txt and "ms" in txt, txt.replace("\n", " ")[:90])

    obs, _ = call_text("sequence", {"steps": [{"observe": {"diff": True}}]})
    if "APP_NOT_FOUND" in obs or "PERMISSION_DENIED" in obs:
        skip("observe 伪步（索引不变量）", "当前没有可观察的前台应用")
    else:
        check("observe 伪步可用，并显式声明索引已更新（索引不变量）",
              "[observe]" in obs and "索引已按本次观察更新" in obs, obs.splitlines()[-1][:90])

    # 主连接到这里就功成身退：下面的生命周期检查各用独立进程（Dock 图章要新进程，
    # 断连要能关掉一条连接的 stdin）。**之后不要再往 send/recv 上加检查**——它们会读到
    # EOF 并报成「服务器提前退出」，而真正的原因只是这条连接被主动关掉了。
    proc.terminate()

    # --- 生命周期：Dock 图章 + 宿主断开后的滞留 ---------------------------------
    # 两条都是实机踩出来的：前者「每开一轮电脑操作，Dock 就多一枚 Python 火箭，并且
    # 一直留到进程结束」；后者「dsh 在长动作中途退场，服务器却把最后几秒睡完才发现自己
    # 成了孤儿」—— 今天真的攒下过一批这样的进程。
    def spawn_side(extra_env=None):
        q = subprocess.Popen([sys.executable, str(SERVER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, env={**env, **(extra_env or {})}, text=True, bufsize=1)
        q.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "c", "version": "1"}}}) + "\n")
        q.stdin.flush()
        q.stdout.readline()
        return q

    def call_side(q, msg_id, name, arguments=None):
        q.stdin.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
                                  "params": {"name": name, "arguments": arguments or {}}}) + "\n")
        q.stdin.flush()

    def recv_from(q, msg_id, timeout=30):
        """从任意一条连接上等一条指定 id 的回包；超时返回 None（用 select，不阻塞死）。"""
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([q.stdout], [], [], left)[0]:
                return None
            line = q.stdout.readline()
            if not line:
                return None
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("id") == msg_id:
                return m

    def silent_for(q, seconds):
        """seconds 秒内一条整行都没来 → True。"""
        return not select.select([q.stdout], [], [], seconds)[0]

    side = spawn_side()
    call_side(side, 2, "list_apps")
    side.stdout.readline()  # 走 AX 的那一下，正是会被 LaunchServices 登记成应用的时机
    listed = subprocess.run(["lsappinfo", "list"], capture_output=True, text=True).stdout
    kind = next((x for line in listed.splitlines() if f"pid = {side.pid} " in line
                 for x in line.split() if x.startswith('type="')), 'type="未登记"')
    # 真正的判据是「进程有没有换进自建 UIElement bundle」：Dock 只认 plist 级的 LSUIElement，
    # 运行时 TransformProcessType 在 macOS 27 上拦不住图块（见 README 那一节）。换不了的环境
    # （非 framework 解释器 / DSH_CUA_NO_REEXEC=1）退回旧判据：至少别被登记成 Foreground。
    exe = _pid_path(side.pid)
    in_bundle = "DshComputerUse.app" in exe
    check("第一次 AX 调用后不留 Dock 图章",
          in_bundle or "Foreground" not in kind,
          f"bundle={in_bundle} exe={exe or '?'} {kind}")
    side.kill()

    leak = spawn_side()
    call_side(leak, 3, "wait", {"seconds": 30})
    time.sleep(0.6)
    leak.stdin.close()  # 宿主在动作中途断开
    try:
        rc = leak.wait(timeout=3)
        check("动作中途宿主断开 → 立刻退出，不滞留", rc == 0, f"rc={rc}")
    except subprocess.TimeoutExpired:
        check("动作中途宿主断开 → 立刻退出，不滞留", False, "3 秒了还在")
        leak.kill()

    # --- 协作式取消：notifications/cancelled 必须真的打断动作，而且不回包 -------------
    # dsh 的 mcp-client 给每次调用都带取消信号：宿主按停止键、或调用超时，发的就是这条
    # 通知。以前它被当成「未知通知」丢掉 —— 动作照跑满 10 秒，回包也没人收，表现是
    # 「按了停止，鼠标还在自己动」。
    cancel = spawn_side()
    call_side(cancel, 7, "wait", {"seconds": 10})
    time.sleep(0.7)  # 让它真的进到 pause 里
    cancel.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                   "params": {"requestId": 7, "reason": "wire-check"}}) + "\n")
    cancel.stdin.flush()
    started = time.monotonic()
    quiet = silent_for(cancel, 1.5)
    check("被取消的调用不回包（规范：停止处理且不响应）", quiet,
          "" if quiet else f"还是回了：{cancel.stdout.readline()[:80]!r}")
    check("取消是真的打断了动作，而不是等它自然跑完", time.monotonic() - started < 2.5,
          f"耗时 {time.monotonic() - started:.2f}s")
    call_side(cancel, 8, "screen_info", {})
    after = recv_from(cancel, 8, timeout=15)
    check("取消之后照常服务下一个请求（连接与协议状态都没坏）",
          after is not None and "主屏逻辑尺寸" in json.dumps(after, ensure_ascii=False),
          json.dumps(after, ensure_ascii=False)[:60] if after else "没有回包")
    cancel.kill()

    # --- 急停三档 ----------------------------------------------------------------
    # 第 1 档：环境变量。这是唯一「进程内解除不了」的一档，必须真的拒到底。
    locked = spawn_side({"DSH_CUA_DISABLE": "1"})
    call_side(locked, 11, "drag", {"from_x": 0, "from_y": 0, "to_x": 1, "to_y": 1, "strategy": "a11y"})
    r11 = recv_from(locked, 11, timeout=20)
    txt11 = json.dumps(r11, ensure_ascii=False)
    check("急停档1 环境变量：DSH_CUA_DISABLE=1 时动作一律 ERROR[KILLED]（连参数都没轮到）",
          "ERROR[KILLED]" in txt11, txt11[:90])
    call_side(locked, 12, "resume", {})
    r12 = recv_from(locked, 12, timeout=20)
    check("急停档1：环境变量锁定无法在进程内解除（resume 明确报错而不是假装成功）",
          "无法恢复" in json.dumps(r12, ensure_ascii=False), json.dumps(r12, ensure_ascii=False)[:90])
    locked.kill()

    # 第 2 档：浮窗 ⏹ / host 路由写的那份**急停文件**（跨进程，且正在跑的动作也能看见）。
    kill_file = CHECK_DIR / "kill"
    kill_file.write_text(f"on:{int(time.time() * 1000)}:wire-check", encoding="utf-8")
    killed = spawn_side()
    time.sleep(0.3)
    call_side(killed, 13, "drag", {"from_x": 0, "from_y": 0, "to_x": 1, "to_y": 1, "strategy": "a11y"})
    r13 = recv_from(killed, 13, timeout=20)
    check("急停档2 浮窗/路由的急停文件：动作工具回 ERROR[KILLED]",
          "ERROR[KILLED]" in json.dumps(r13, ensure_ascii=False), json.dumps(r13, ensure_ascii=False)[:80])
    call_side(killed, 14, "screen_info", {})
    r14 = json.dumps(recv_from(killed, 14, timeout=20), ensure_ascii=False)
    check("急停期间观察类工具仍可用（screen_info 照常回答并报出已停止）",
          "主屏逻辑尺寸" in r14 and "已停止" in r14, r14[:90])
    call_side(killed, 15, "resume", {})
    r15 = json.dumps(recv_from(killed, 15, timeout=20), ensure_ascii=False)
    check("急停档3 resume() 能在**另一个进程**里解除文件档（跨进程一致）",
          "已解除急停" in r15, r15[:80])
    call_side(killed, 16, "drag", {"from_x": 0, "from_y": 0, "to_x": 1, "to_y": 1, "strategy": "a11y"})
    r16 = json.dumps(recv_from(killed, 16, timeout=20), ensure_ascii=False)
    check("解除之后动作工具重新入队（a11y 分支的报错路径证明没被急停拦住）",
          "KILLED" not in r16 and "ACTION_UNAVAILABLE" in r16, r16[:90])
    killed.kill()
    kill_file.unlink(missing_ok=True)

    print(("全部通过 ✓ " if not fails else f"失败 {len(fails)} 项: {fails}"))
    # --- 进程内：动作层契约 / possibly_sent 三条规则 / 急停优先级 / 租约仲裁 ---------
    # 这些都来自「一个字段没填，上层就做不了决策」这类真实事故：sequence 的内层实现与工具
    # schema 参数名漂移（参数被静默丢弃）、落点保护在窗口列表读失败时静默放行（fail-open）、
    # possibly_sent 判反（模型重复点击）…
    sys.path.insert(0, str(SERVER.parent))
    os.environ.setdefault("DSH_CUA_NO_REEXEC", "1")  # 只读导入，别把测试进程换掉
    os.environ["DSH_CUA_DIR"] = str(CHECK_DIR)       # 进程内这套也只用隔离目录
    import action_layer  # noqa: E402
    from action_layer import ActionResult as _AR  # noqa: E402
    import killswitch  # noqa: E402
    import lease  # noqa: E402
    import server as server_mod  # noqa: E402

    # 动作层 ↔ 工具 schema：同一份签名，一个参数都不许漂
    for name, spec in server_mod._ACTIONS.items():
        declared = set(next(t for t in tools if t["name"] == name)["inputSchema"]["properties"])
        dropped = declared - set(inspect.signature(spec.fn).parameters)
        check(f"动作 {name} 的形参覆盖工具声明（否则参数被静默丢弃）", not dropped,
              f"会被丢弃: {sorted(dropped)}")
    check("动作注册表覆盖了 sequence 文档里承诺的全部动作工具",
          {"click_element", "set_value", "perform_action", "select_text", "get_ax_state"} <= set(server_mod._ACTIONS))
    check("每个动作都经 action 装饰器（否则不返回 ActionResult，sequence 做不了步间决策）",
          all(hasattr(spec.fn, "__wrapped__") for spec in server_mod._ACTIONS.values()))
    check("flow 分层：动作层不返回自然语言（_do_* 的返回类型就是 ActionResult）",
          server_mod._do_click.__wrapped__.__annotations__.get("return") is _AR)

    waited = server_mod._do_wait(0.5)
    check("每个动作都填 settle_ms（wait 0.5 秒 → ≈500ms，且是实际等待）",
          isinstance(waited, _AR) and 450 <= (waited.settle_ms or 0) <= 1200,
          f"settle_ms={waited.settle_ms}")
    settled = server_mod._settle(budget_ms=200)
    check("_settle 返回实际等待毫秒（并受预算约束）", isinstance(settled, int) and 150 <= settled <= 500,
          f"settle={settled}ms")

    # possibly_sent 的三条判定规则（§3.1 写死的那三条）
    @action_layer.action(risk=False)
    def _sent_then_timeout():
        action_layer.mark_sent()
        raise subprocess.TimeoutExpired("probe", 1)

    @action_layer.action(risk=False)
    def _arg_fail():
        raise server_mod.BadArgument("坏参数")

    @action_layer.action(risk=False)
    def _sent_then_ok():
        action_layer.mark_sent()
        return _AR(ok=True, effect="changed")

    r = _sent_then_timeout()
    check("possibly_sent：动作已发出后超时 → true（模型必须先去观察）",
          r.possibly_sent is True and r.error == "TIMEOUT", f"{r.error} sent={r.possibly_sent}")
    r = _arg_fail()
    check("possibly_sent：参数校验失败 → false（改参数重试是安全的）",
          r.possibly_sent is False and r.error == "INVALID_ARGUMENT", f"{r.error} sent={r.possibly_sent}")
    r = _sent_then_ok()
    check("possibly_sent：动作正常返回（哪怕界面真的变了）→ false",
          r.possibly_sent is False and r.effect == "changed", f"sent={r.possibly_sent}")
    r = server_mod._do_click(0, 0, strategy="bogus")
    check("真动作上的同一套规则：非法 strategy 不点也不标 sent",
          r.error == "INVALID_ARGUMENT" and r.possibly_sent is False, f"{r.error} sent={r.possibly_sent}")

    # on_unchanged=abort：真的会停在「界面没变化」的那一步（用合成动作验，不碰真实输入）
    @action_layer.action(risk=False)
    def _probe_unchanged():
        return _AR(ok=True, effect="unchanged", evidence="界面树逐字节一致")

    server_mod._ACTIONS["probe_unchanged"] = action_layer.ActionSpec(
        name="probe_unchanged", fn=_probe_unchanged, render=lambda r: "probe", description="", risk=False
    )
    steps = [{"tool": "probe_unchanged", "args": {}}, {"tool": "wait", "args": {"seconds": 1}}]
    cont = server_mod._run_sequence(steps, "continue", 5000, 20000)
    check("on_unchanged=continue（默认）：unchanged 只提示、继续往下跑",
          "2 步全部执行" in cont and "未观察到界面变化" in cont, cont.splitlines()[0][:80])
    aborted = server_mod._run_sequence(steps, "abort", 5000, 20000)
    check("on_unchanged=abort：unchanged 当场停在第 1 步（后面的 wait 没跑）",
          "在第 1 步停止" in aborted and "on_unchanged=abort" in aborted and "2 步全部执行" not in aborted,
          aborted.splitlines()[0][:90])
    del server_mod._ACTIONS["probe_unchanged"]

    # 每步的等待预算真的被 _settle 读到（step_timeout_ms 不是摆设）
    server_mod._SETTLE_BUDGET_MS[0] = 300
    budgeted = server_mod._settle()
    server_mod._SETTLE_BUDGET_MS[0] = None
    check("step_timeout_ms 通过 _SETTLE_BUDGET_MS 传进 _settle（等待被预算压住）",
          budgeted <= 400, f"预算 300ms → 实际 {budgeted}ms")

    # 急停优先级：KILLED 必须比「a11y 没有语义动作」更早命中，且一次都不下发
    killswitch.stop("wire-check 进程内演习")
    r = server_mod._do_drag(0, 0, 1, 1, strategy="a11y")
    check("急停优先级：动作在入口被 KILLED 拦住（早于 a11y 的 ACTION_UNAVAILABLE）",
          r.error == "KILLED" and r.possibly_sent is False, f"{r.error}")
    r = server_mod._do_click(0, 0, strategy="bogus")
    check("急停优先级：连参数错误都轮不到（一次都不会点下去）", r.error == "KILLED", f"{r.error}")
    killswitch.resume()
    r = server_mod._do_click(0, 0, strategy="bogus")
    check("resume() 之后动作层重新可入（回到正常的参数校验）", r.error == "INVALID_ARGUMENT", f"{r.error}")

    # 控制器租约：并发申请 / force 抢占 / 崩溃回收 / 释放
    leases_dir = CHECK_DIR / "leases"
    leases_dir.mkdir(parents=True, exist_ok=True)
    other = subprocess.Popen(["sleep", "60"], stdout=subprocess.DEVNULL)
    try:
        (leases_dir / f"{other.pid}.json").write_text(json.dumps({
            "lease_id": "l-fake-1", "holder_pid": other.pid, "holder": "另一个会话",
            "target_pid": 4242, "target_name": "假应用",
            "acquired_at": time.time(), "renewed_at": time.time(), "ttl": 60,
        }), encoding="utf-8")
        granted = lease.acquire(4242, "假应用")
        check("租约：同一目标被活着持有 → CONTROLLER_BUSY（带持有者与剩余时间）",
              granted.get("ok") is False and granted.get("error") == "CONTROLLER_BUSY"
              and "另一个会话" in lease.describe_holder(granted.get("blocked_by") or {}),
              json.dumps(granted, ensure_ascii=False, default=str)[:90])
        granted = lease.acquire(4242, "假应用", force=True)
        check("租约：force=true 可抢占", granted.get("ok") is True, json.dumps(granted, ensure_ascii=False)[:80])
        check("租约：抢占后对方的记录被真的收掉（否则两边一起卡死）",
              not (leases_dir / f"{other.pid}.json").exists())
        check("租约：自己持有后 guard 放行", lease.guard(4242) is None)
        check("租约日志留痕（谁把谁踢了要查得出来）", (CHECK_DIR / "leases.log").exists())
    finally:
        other.kill()
        other.wait(timeout=5)

    # 崩溃的持有者不能把别人永久挡在门外
    (leases_dir / "99999999.json").write_text(json.dumps({
        "lease_id": "l-dead", "holder_pid": 99999999, "holder": "崩掉的会话",
        "target_pid": 4242, "renewed_at": time.time(), "ttl": 60,
    }), encoding="utf-8")
    check("租约：死进程留下的租约自动回收（不留永久路障）",
          lease.guard(4242) is None and not (leases_dir / "99999999.json").exists())

    # 自己的租约被别人抢走 → 动作必须被拦住（否则「抢占」只是写了一条日志）
    robber = subprocess.Popen(["sleep", "60"], stdout=subprocess.DEVNULL)
    try:
        (leases_dir / f"{robber.pid}.json").write_text(json.dumps({
            "lease_id": "l-robber", "holder_pid": robber.pid, "holder": "抢占者",
            "target_pid": 4242, "renewed_at": time.time(), "ttl": 60,
        }), encoding="utf-8")
        blocked = lease.guard(4242)
        check("租约：自己的租约被抢走后动作被拦住（返回 CONTROLLER_BUSY）",
              bool(blocked) and blocked.get("error") == "CONTROLLER_BUSY",
              json.dumps(blocked, ensure_ascii=False, default=str)[:80])
    finally:
        (leases_dir / f"{robber.pid}.json").unlink(missing_ok=True)
        robber.kill()
        robber.wait(timeout=5)
    check("租约：release() 释放，且进程退出时 atexit 也会释放",
          lease.release() is True and not (leases_dir / f"{os.getpid()}.json").exists())

    # 落点保护：读不到窗口列表时必须 fail-closed，而不是静默放行
    saved_bound = server_mod._bound_app
    try:
        server_mod._bound_app = {"pid": 1, "name": "probe"}
        refused = server_mod._aim_guard(10, 10, False, server_mod._WINDOW_QUERY_FAILED)
        check("窗口列表读不到时落点保护拒绝执行（不是静默放行）", refused.startswith("ERROR[TARGET_UNKNOWN]"), refused[:48])
        check("force=true 仍是逃生口", server_mod._aim_guard(10, 10, True, server_mod._WINDOW_QUERY_FAILED) == "")
        other_win = {"id": 9, "pid": 42, "owner": "Mail", "name": "收件箱", "layer": 0, "x": 0, "y": 0, "w": 9, "h": 9}
        check("挡住的是别的应用时仍然拒绝", server_mod._aim_guard(10, 10, False, other_win).startswith("ERROR[NOT_FRONTMOST]"))
    finally:
        server_mod._bound_app = saved_bound

    import shutil as _shutil
    _shutil.rmtree(CHECK_DIR, ignore_errors=True)
    return 1 if fails else 0


def recv_call(send, recv, name, arguments, expect_error=False):
    send({"jsonrpc": "2.0", "id": 99, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    out = recv(99)
    res = out.get("result") or (out.get("error", {}).get("data") or {})
    if "content" not in res:
        raise SystemExit(f"{name} 回包没有 content: {out}")
    return res


if __name__ == "__main__":
    raise SystemExit(main())
