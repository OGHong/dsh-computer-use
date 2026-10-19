#!/usr/bin/env python3
"""真的用 stdio 跟工具服务器握一次手 —— 手写协议层最怕的就是「看着对、发出去不对」。

覆盖：initialize（回显 protocolVersion / capabilities 形状 / serverInfo）、
notifications 不该有回包、tools/list 的 29 个工具与每个工具的 name/description/inputSchema、
tools/call 的文本与图片两种 content、缺参数、未知方法、ping、参数类型转换、
以及三条生命周期：Dock 上不留图章、宿主断开后不滞留、notifications/cancelled 真的打断动作且不回包。

v2 新增（设计任务书 §3.1~§3.6，全部**不发真实输入**）：
  - sequence v2：未知工具 / 空数组 / on_unchanged 取值 / 失败即停 / 观察伪步 / wait 步走稳定判定；
  - strategy 三态：a11y 不静默降级（拖拽、set_value(event)、非法取值）；
  - possibly_sent 的三条判定规则（参数错 false、已下发后超时 true、正常返回 false）；
  - 急停三档：环境变量 / 急停文件（= 浮窗 ⏹ 与 host 路由走的那条）/ stop-resume 工具；
  - 控制器租约：并发申请 CONTROLLER_BUSY、force 抢占、持有者崩溃后自动回收、退出自动释放；
  - 动作层契约：每个注册动作都是 ActionResult、渲染层不返回自然语言对象。

v3 新增（设计任务书 §8/§9，全部**不发送输入**）：
  - run_code 预检三码（语法 / 禁用名字 / 未知动作名）；
  - 账本在异常路径完整、possibly_sent 按整批计算、超时不被 @action 吞掉；
  - 纯 CPU 墙钟、观察 compact 投影、单图、绑定令牌零下发、run_shell 默认不存在；
  - step_timeout_ms 结束后复位、code_exec 无第三方依赖。
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
GOLDEN_TOOLS = 29
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
    instructions = r.get("instructions", "")
    check("mcp.instructions 写清 sequence vs run_code 的分工",
          "run_code" in instructions and "sequence" in instructions and "≤5 步" in instructions,
          [ln for ln in instructions.split("程序化批处理")[1:2]][0][:70] if "程序化批处理" in instructions else instructions[:60])
    check("mcp.instructions 不把 run_code 描述成沙箱/隔离环境",
          not any(w in instructions for w in ("沙箱", "sandbox", "隔离", "受限执行")))

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
    run_tool = next(t for t in tools if t["name"] == "run_code")
    run_props = run_tool["inputSchema"]["properties"]
    check("run_code 的 inputSchema 与实现签名一致（9 个参数，接口名照任务书）",
          set(run_props) == {"code", "binding_id", "dry_run", "allow_shell", "wall_timeout_ms",
                             "max_actions", "max_observations", "step_timeout_ms", "detail"},
          str(sorted(run_props)))
    check("run_code 必填只有 code", run_tool["inputSchema"].get("required") == ["code"],
          str(run_tool["inputSchema"].get("required")))
    check("run_code 描述写明 allow_shell 默认关、dry_run 可预检，且没有沙箱话术",
          "allow_shell" in run_tool["description"] and "dry_run" in run_tool["description"]
          and not any(w in run_tool["description"] for w in ("沙箱", "sandbox", "隔离", "受限执行")),
          run_tool["description"][:80])

    info = recv_call(send, recv, "screen_info", {})
    check("tools/call 文本结果", info["content"][0]["type"] == "text" and "主屏逻辑尺寸" in info["content"][0]["text"],
          info["content"][0]["text"][:28].replace("\n", " "))
    check("没有 caller.json 时如实报告「会话身份未启用」（降级到旧的共享行为，而不是假装隔离）",
          "会话身份: 未启用" in info["content"][0]["text"],
          [ln for ln in info["content"][0]["text"].splitlines() if "会话身份" in ln][0][:70])
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

    def call_code(code, **kwargs):
        res = recv_call(send, recv, "run_code", {"code": code, **kwargs})
        text = "".join(b.get("text", "") for b in res.get("content") or [] if b.get("type") == "text")
        images = [b for b in res.get("content") or [] if b.get("type") == "image"]
        return text, images

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

    # --- 多会话串扰：一个服务器进程 + 两个逻辑调用者（任务书 §4.2 / §5.3） ----------
    # 生产拓扑是「一个 DSH 实例 → 一个宿主级 server.py → N 个会话共用它」。从服务器这一侧看，
    # 那就是「一条 stdio 连接上交替来两组调用」—— 正是下面这段。真起两个 DSH 会话也复现不出
    # 更多东西：服务器根本无法区分调用者，这正是缺陷本身。
    import re as _re

    def binding_of(text: str) -> str:
        m = _re.search(r"binding_id=(\S+?)[）（(]", text + "（")
        return m.group(1) if m else ""

    apps = [ln for ln in call_text("list_apps", {})[0].splitlines() if " pid: " in ln]
    pids = [_re.search(r"pid: (\d+)", ln).group(1) for ln in apps]
    if len(pids) < 2:
        skip("多会话串扰（绑定令牌）", f"当前只有 {len(pids)} 个有界面的应用，需要两个")
    else:
        a_txt, _ = call_text("get_app", {"app": pids[0]})      # 会话 A 绑定 app1
        b_a = binding_of(a_txt)
        check("get_app 回显绑定令牌（会话 A）", bool(b_a), a_txt.splitlines()[-2][:100] if a_txt else "")
        # 用 full 观察：diff 在界面没变化时一行都不回，这条断言要的是「索引区间」
        seen, _ = call_text("get_ax_state", {"full": True, "binding_id": b_a})
        check("get_ax_state 回显同一枚令牌（索引和令牌必须一起给模型）",
              binding_of(seen) == b_a, f"观察里是 {binding_of(seen)!r}")
        # 会话 B 在同一个服务器进程里绑另一个应用 —— 就是生产里发生的那一下
        b_txt, _ = call_text("get_app", {"app": pids[1]})
        b_b = binding_of(b_txt)
        check("另一个调用者绑定别的应用 → 换了一枚新令牌", bool(b_b) and b_b != b_a, f"{b_a} → {b_b}")
        b_seen, _ = call_text("get_ax_state", {"full": True, "binding_id": b_b})
        nums_a = [int(x) for x in _re.findall(r"\[(\d+)\]", seen)]
        nums_b = [int(x) for x in _re.findall(r"\[(\d+)\]", b_seen)]
        check("换绑即抬索引纪元：两棵树的索引区间不重叠（旧索引不可能命中新树的同号元素）",
              bool(nums_a) and bool(nums_b) and min(nums_b) > max(nums_a),
              f"A 索引 {min(nums_a) if nums_a else '-'}~{max(nums_a) if nums_a else '-'} / "
              f"B 索引 {min(nums_b) if nums_b else '-'}~{max(nums_b) if nums_b else '-'}")
        # A 带着旧令牌去观察：不拦（观察无害），但必须醒目说明「这不是你要看的应用」
        stale_view, _ = call_text("get_ax_state", {"full": True, "binding_id": b_a})
        check("拿旧令牌观察会明确警告「这棵树不是你要观察的应用」（而不是让模型误会）",
              "不是你要观察的那个应用" in stale_view, stale_view.splitlines()[0][:100])
        # A 带着旧令牌动作：必须在**下发之前**被拦住
        stale, _ = call_text("drag", {"from_x": 0, "from_y": 0, "to_x": 1, "to_y": 1,
                                      "strategy": "a11y", "binding_id": b_a})
        check("串改后 A 的动作被拒（BINDING_CHANGED，早于 a11y 分支，一次都没下发）",
              "ERROR[BINDING_CHANGED]" in stale, stale[:110])
        batch, _ = call_text("sequence", {"steps": [{"tool": "drag", "args": {"from_x": 0, "from_y": 0, "to_x": 1, "to_y": 1, "strategy": "a11y"}}],
                                          "binding_id": b_a})
        check("整批动作也拦（第一步之前就拒，0 步执行）",
              "ERROR[BINDING_CHANGED]" in batch and "0 步" in batch, batch[:100])
        cur, _ = call_text("drag", {"from_x": 0, "from_y": 0, "to_x": 1, "to_y": 1,
                                    "strategy": "a11y", "binding_id": b_b})
        check("当前令牌放行（走到动作层自己的 a11y 分支，证明拦的是令牌不是动作）",
              "ACTION_UNAVAILABLE" in cur and "BINDING_CHANGED" not in cur, cur[:90])
        noid, _ = call_text("drag", {"from_x": 0, "from_y": 0, "to_x": 1, "to_y": 1, "strategy": "a11y"})
        check("不带令牌照旧放行（可选参数：不打断老会话与坐标路径）",
              "ACTION_UNAVAILABLE" in noid and "BINDING_CHANGED" not in noid, noid[:90])
        held, _ = call_text("release_lease", {"binding_id": b_a})
        check("A 的 release_lease 不能释放 B 的租约（带旧令牌被拒）",
              "ERROR[BINDING_CHANGED]" in held, held[:100])
        freed, _ = call_text("release_lease", {"binding_id": b_b})
        check("当前令牌可以正常释放自己的租约", "已释放控制器租约" in freed, freed[:80])
        # 症状本身（§1.1）仍在：令牌把它从「静默点错应用」变成「可拦住」，不是隔离
        info_txt, _ = call_text("screen_info", {})
        line = next((ln for ln in info_txt.splitlines() if "已绑定应用" in ln), "")
        check("如实报告：绑定仍是进程内共享的（screen_info 里点名了这条限制）",
              "所有会话" in info_txt and f"binding_id={b_b}" in line, line[:90])

    # --- 会话级隔离（任务书 §5.1 四项，协议级复现：一个服务器进程 + 两个会话身份） ------
    # host 半区在 tools/pre-execute 里把 exec.agent.id 写进 caller.json（host-smoke 已验），
    # 服务器每次 tools/call 读它。这里直接扮演那个文件，验的就是服务器侧的行为。
    caller_file = CHECK_DIR / "caller.json"

    def as_session(agent: str) -> None:
        tmp = caller_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"agent": agent, "tool": "probe", "seq": 1, "at": int(time.time() * 1000)}),
                       encoding="utf-8")
        os.replace(tmp, caller_file)

    apps2 = [ln for ln in call_text("list_apps", {})[0].splitlines() if " pid: " in ln]
    pids2 = [_re.search(r"pid: (\d+)", ln).group(1) for ln in apps2]
    if len(pids2) < 2:
        skip("会话级隔离（§5.1）", f"当前只有 {len(pids2)} 个有界面的应用，需要两个")
    else:
        as_session("session-AAA")
        a_txt, _ = call_text("get_app", {"app": pids2[0]})
        b_a2 = binding_of(a_txt)
        call_text("get_ax_state", {"full": True})
        as_session("session-BBB")
        call_text("get_app", {"app": pids2[1]})
        call_text("get_ax_state", {"full": True})
        # §5.1-1 并发绑定隔离：A 再观察，拿到的必须还是 A 那个应用的树
        as_session("session-AAA")
        a_again, _ = call_text("get_ax_state", {"full": True})
        check("§5.1 并发绑定隔离：B 绑定别的应用之后，A 拿到的仍是**自己**那个应用的树",
              f"pid={pids2[0]}" in a_again and b_a2 in a_again and f"pid={pids2[1]}" not in a_again,
              [ln for ln in a_again.splitlines() if "binding_id" in ln][0][:90] if "binding_id" in a_again else a_again[:80])
        as_session("session-AAA")
        a_info, _ = call_text("screen_info", {})
        check("§5.1 每个会话各有一份绑定（screen_info 里能看出 A 没被 B 改掉）",
              ("会话身份: 已启用" in a_info) and (f"binding_id={b_a2}" in a_info),
              [ln for ln in a_info.splitlines() if "已绑定应用" in ln][0][:80])
        as_session("session-AAA")
        a_get, _ = call_text("get_app", {"app": pids2[0]})
        b_a2 = binding_of(a_get)   # 重新绑定会换令牌：后续断言用最新那枚
        check("§5.1 get_app 当场就能看到**自己**那份租约（不是进程级那份，也不是空）",
              "控制器租约 l-session-AAA" in a_get, [ln for ln in a_get.splitlines() if "租约" in ln][0][:80])
        # §5.1-2 跨会话 CONTROLLER_BUSY：B 去抢 A 正持有的应用
        as_session("session-BBB")
        busy, _ = call_text("get_app", {"app": pids2[0]})
        check("§5.1 跨会话 CONTROLLER_BUSY，且持有者信息里能分辨是哪个会话",
              "CONTROLLER_BUSY" in busy and "session-AAA" in busy, busy[:110])
        # §5.1-4 release_lease 只释放自己那份
        as_session("session-AAA")
        rel, _ = call_text("release_lease", {"binding_id": b_a2})
        left = sorted(q.name for q in (CHECK_DIR / "leases").glob("*.json"))
        check("§5.1 A 的 release_lease 只释放自己那份（B 的租约文件还在）",
              "已释放" in rel and "session-AAA" in rel and "session-BBB.json" in left,
              f"{rel[:60]} | 剩余租约 {left}")
        # §5.1-3 动作不误伤：A 用自己的旧索引，解析的仍是 A 的应用
        as_session("session-AAA")
        mine, _ = call_text("get_ax_state", {"full": True})
        idx = _re.findall(r"\[(\d+)\]", mine)
        as_session("session-BBB")
        call_text("get_ax_state", {"full": True})          # B 也观察（索引落在 B 的纪元里）
        as_session("session-AAA")
        safe, _ = call_text("select_text", {"index": int(idx[0]) if idx else 0, "text": "\u0000不存在的串"})
        check("§5.1 动作不误伤：A 的索引在 A 自己的绑定下解析（不受 B 的绑定影响）",
              "ERROR[" in safe and "BINDING_CHANGED" not in safe, safe[:90])
        caller_file.unlink(missing_ok=True)
        shared_again, _ = call_text("screen_info", {})
        check("删掉 caller.json 之后这次调用回落到共享桶（身份没了就退回旧行为，不炸）",
              "当前调用属于 shared" in shared_again,
              [ln for ln in shared_again.splitlines() if "会话身份" in ln][0][:80])

    # --- run_code v3（任务书 §8/§9；全部不发送输入） --------------------------------
    dry_ok, _ = call_code("mu.list_apps()\nmu.get_ax_state(full=True)\n", dry_run=True)
    check("run_code dry_run 只做预检、不碰电脑（动作/观察计数正确）",
          "预检通过" in dry_ok and "动作 0 个、观察 2 次" in dry_ok, dry_ok[:80])

    syn, _ = call_code("def f(:\n    pass\n", dry_run=True)
    check("SCRIPT_SYNTAX 命中且带行号", "ERROR[SCRIPT_SYNTAX]" in syn and "line 1" in syn, syn[:90])
    forb, _ = call_code('open("/tmp/x")\n', dry_run=True)
    check("SCRIPT_FORBIDDEN 命中且带行号 + 被禁名字",
          "ERROR[SCRIPT_FORBIDDEN]" in forb and "line 1" in forb and "open" in forb, forb[:90])
    unk, _ = call_code("mu.click_elment(index=1)\n", dry_run=True)
    check("SCRIPT_UNKNOWN_ACTION 命中且列出可用名",
          "ERROR[SCRIPT_UNKNOWN_ACTION]" in unk and "line 1" in unk
          and "mu.click_elment" in unk and "可用：" in unk, unk[:110])

    ledger, _ = call_code("mu.list_apps()\nmu.list_apps()\nmu.get_ax_state(max_nodes='abc')\n")
    check("账本在异常路径也完整（3 条、含错误码、头行已执行 2 步）",
          "已执行 2 步" in ledger and "1. [list_apps]" in ledger and "2. [list_apps]" in ledger
          and "3. [get_ax_state]" in ledger and "ERROR[SCRIPT_ARGUMENT]" in ledger,
          ledger.splitlines()[0][:110] if ledger else "")
    check("SCRIPT_ARGUMENT 报出合法参数表（缺参/多参绝不静默丢弃）",
          "合法参数" in ledger and "max_nodes" in ledger, ledger[-300:])

    binding_bad, _ = call_code("mu.list_apps()\n", binding_id="bogus")
    check("绑定令牌带错 → BINDING_CHANGED 且账本为空（一步都没下发）",
          "ERROR[BINDING_CHANGED]" in binding_bad and "账本为空" in binding_bad
          and "0 步" in binding_bad and "[list_apps]" not in binding_bad, binding_bad[:110])

    shell_off, _ = call_code('mu.run_shell("id")\n')
    check("allow_shell=False 时 mu.run_shell 根本不存在（AttributeError，不是执行 shell）",
          "AttributeError" in shell_off and "allow_shell" in shell_off and "uid=" not in shell_off,
          shell_off.splitlines()[0][:110] if shell_off else "")
    shell_caught, _ = call_code(
        'try:\n    mu.run_shell("id")\nexcept AttributeError:\n    mu.done("caught")\n'
    )
    caught_line = next((ln for ln in shell_caught.splitlines() if "caught" in ln), "") if shell_caught else ""
    check("AttributeError 在白名单里（文档给出的 except AttributeError 检测方式真的可用）",
          "SCRIPT_ERROR" not in shell_caught and "caught" in shell_caught and "uid=" not in shell_caught,
          caught_line or shell_caught[-120:])

    cpu_started = time.monotonic()
    cpu, _ = call_code("while True:\n    pass\n", wall_timeout_ms=1000)
    cpu_ms = int((time.monotonic() - cpu_started) * 1000)
    check("纯 CPU 死循环被墙钟硬停（SCRIPT_TIMEOUT，不是 INTERNAL）",
          "ERROR[SCRIPT_TIMEOUT]" in cpu and "INTERNAL" not in cpu and cpu_ms < 5000, f"{cpu_ms}ms")
    after_cpu, _ = call_code("mu.list_apps()\n", dry_run=True)
    check("墙钟超时之后仍能服务下一个请求（定时器与 handler 已复位）",
          "预检通过" in after_cpu, after_cpu[:60])

    mutex, _ = call_code("mu.wait(seconds=5)\n", wall_timeout_ms=1500)
    check("墙钟在动作中间触发 → SCRIPT_TIMEOUT（异常没有被 @action 吞成 INTERNAL）",
          "ERROR[SCRIPT_TIMEOUT]" in mutex and "INTERNAL" not in mutex, mutex.splitlines()[0][:110])
    check("超时的那一步也在账本里（wait 记录 + SCRIPT_TIMEOUT）",
          "1. [wait]" in mutex and "SCRIPT_TIMEOUT" in mutex.split("--- 账本 ---")[-1],
          mutex.split("--- 账本 ---")[-1][:90])

    obs, _ = call_code(
        "r = mu.get_ax_state(full=True)\n"
        "print('OBS', r.get('total'), r.get('returned'), r.get('truncated'), len(r.get('nodes') or []))\n"
        "print('HINT', r.get('hint') or '')\n"
    )
    obs_m = _re.search(r"OBS (\d+) (\d+) (True|False) (\d+)", obs)
    if not obs_m or int(obs_m.group(1)) <= 60:
        skip("观察 compact 投影（total/returned/truncated）", "当前前台应用不足 60 个元素或没有可观察应用")
    else:
        total_n, returned_n = int(obs_m.group(1)), int(obs_m.group(2))
        kept = int(obs_m.group(4))
        check("detail=compact 时观察有 total/returned/truncated，且只回 60 行",
              obs_m.group(3) == "True" and returned_n == 60 and kept == 60 and total_n >= 60,
              f"{returned_n}/{total_n}, kept={kept}, truncated={obs_m.group(3)}")
        check("compact 结果带「要全量用 detail=full」的提示",
              'detail="full"' in obs and "前 60 行" in obs, obs[obs.find("HINT"):][:90])

    shots, shots_imgs = call_code("mu.screenshot()\nmu.screenshot()\nmu.screenshot()\n")
    if not shots_imgs and ("PERMISSION_DENIED" in shots or "INVALID_ARGUMENT" in shots or "共 0 个" in shots):
        skip("单图（脚本内截图 3 次只附 1 张）", "当前没有可截取的显示器或屏幕录制权限")
    else:
        check("脚本内截图 3 次 → content 里恰好 1 个 image 块",
              len(shots_imgs) == 1, f"images={len(shots_imgs)}")
        check("文本说明只附最后一张（一次结果最多一张栅格图）",
              "一次结果最多一张栅格图" in shots, shots[-160:])

    ps, _ = call_code(
        'mu.launch_app("DSH-CUA-NoSuchApp-XYZ")\n'
        "mu.list_apps()\n"
        'mu.done("unreachable")\n',
        wall_timeout_ms=15000,
    )
    check("整批防重放：第 1 步 possibly_sent 后立刻停止，第 2 步与 mu.done 都没跑",
          "possibly_sent=true" in ps and "1. [launch_app]" in ps
          and "2. [list_apps]" not in ps and "unreachable" not in ps,
          ps.splitlines()[0][:110] if ps else "")
    check("整体 possibly_sent 为 true，并带 action_layer.POSSIBLY_SENT 指引",
          ps.count("possibly_sent=true") >= 2 and "不要重复同一动作" in ps, ps[-220:])

    nested, _ = call_text("sequence", {
        "steps": [{"tool": "run_code", "args": {"code": "mu.wait(seconds=5)\n",
                                                "wall_timeout_ms": 1000}}],
        "sequence_timeout_ms": 10000,
    })
    check("sequence 可以嵌套 run_code：内层超时被包住、外层副作用清单完整、无协议错误",
          "sequence 在第 1 步停止" in nested and "[run_code]" in nested
          and "SCRIPT_TIMEOUT" in nested and "实际已经发生" in nested,
          nested[:180].replace("\n", " | "))

    stopped = call_text("stop", {"reason": "wire-check run_code"})[0]
    check("急停已生效（为 run_code 的 A11 做准备）",
          "已进入只读" in stopped and "急停" in stopped, stopped[:70])
    killed_code, _ = call_code("mu.list_apps()\n")
    check("急停期间 run_code 返回 ERROR[KILLED]",
          "ERROR[KILLED]" in killed_code, killed_code.splitlines()[0][:100] if killed_code else "")
    still_observes, _ = call_text("get_ax_state", {})
    check("急停期间观察类工具仍可用（get_ax_state 不被 KILLED）",
          "KILLED" not in still_observes, still_observes[:80])
    call_text("resume", {})

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
    run_declared = set(next(t for t in tools if t["name"] == "run_code")["inputSchema"]["properties"])
    run_sig = set(inspect.signature(server_mod._ACTIONS["run_code"].fn).parameters)
    check("run_code 的 inputSchema 与实现签名逐个一致（双向）", run_declared == run_sig,
          f"多余声明={run_declared - run_sig} 未声明={run_sig - run_declared}")

    sent_algo = server_mod.code_exec._batch_possibly_sent
    check("整批 possibly_sent 从账本算：第一条 true、最后一条 false 仍为 true（不取最后一次内层调用）",
          sent_algo([{"possibly_sent": True}, {"possibly_sent": False}]) is True)
    check("反向顺序同样成立；全 false 才是 false",
          sent_algo([{"possibly_sent": False}, {"possibly_sent": True}]) is True
          and sent_algo([{"possibly_sent": False}, {"possibly_sent": False}]) is False)
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

    # --- run_code：step_timeout_ms 压住内层 settle，结束后复位（A10） ----------------
    @action_layer.action(risk=False)
    def _probe_settle():
        ms = server_mod._settle()
        return _AR(ok=True, detail={"ms": ms})

    server_mod._ACTIONS["probe_settle"] = action_layer.ActionSpec(
        name="probe_settle", fn=_probe_settle, render=lambda r: "probe", description="", risk=False
    )
    server_mod._CODE_API["probe_settle"] = _probe_settle
    try:
        probe = server_mod._ACTIONS["run_code"].fn(code="mu.probe_settle()\n", step_timeout_ms=300)
        probe_rec = (probe.detail.get("records") or [{}])[0]
        check("run_code 的 step_timeout_ms 真的压住内层 _settle（300ms 预算）",
              probe.ok and (probe_rec.get("settle_ms") or 9999) <= 500,
              f"ok={probe.ok} settle_ms={probe_rec.get('settle_ms')} err={probe.error}")
        check("run_code 结束后 _SETTLE_BUDGET_MS 已复位（不会把后续动作永久压在旧值）",
              server_mod._SETTLE_BUDGET_MS[0] is None, repr(server_mod._SETTLE_BUDGET_MS[0]))
    finally:
        server_mod._ACTIONS.pop("probe_settle", None)
        server_mod._CODE_API.pop("probe_settle", None)

    # --- D3：桩化 possibly_sent=True，锁住「立刻停 + 第二条没跑」 ----------------
    @action_layer.action(risk=False)
    def _probe_sent():
        return _AR(ok=True, possibly_sent=True)

    server_mod._ACTIONS["probe_sent"] = action_layer.ActionSpec(
        name="probe_sent", fn=_probe_sent, render=lambda r: "probe", description="", risk=False
    )
    server_mod._CODE_API["probe_sent"] = _probe_sent
    try:
        sent_res = server_mod._ACTIONS["run_code"].fn(code="mu.probe_sent()\nmu.list_apps()\n")
        sent_recs = sent_res.detail.get("records") or []
        check("桩化 possibly_sent=True 后 run_code 立刻停止（账本只 1 条，第二条没跑）",
              sent_res.possibly_sent is True and sent_res.error == "POSSIBLY_SENT"
              and len(sent_recs) == 1 and sent_recs[0].get("tool") == "probe_sent",
              f"sent={sent_res.possibly_sent} err={sent_res.error} records={len(sent_recs)}")
    finally:
        server_mod._ACTIONS.pop("probe_sent", None)
        server_mod._CODE_API.pop("probe_sent", None)

    # --- A13：code_exec 只依赖 stdlib + 本仓库模块 ---------------------------------
    import ast as _ast_mod
    src = (ROOT / "server" / "code_exec.py").read_text(encoding="utf-8")
    mods = set()
    for node in _ast_mod.walk(_ast_mod.parse(src)):
        if isinstance(node, _ast_mod.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, _ast_mod.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    local = {"action_layer", "mcp_stdio", "killswitch", "server"}
    third = sorted(m for m in mods if m not in sys.stdlib_module_names and m not in local)
    check("code_exec 只依赖 stdlib + 本仓库模块（没有新第三方依赖）", not third, str(third))

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

    # --- 绑定令牌（任务书 §4.2）的进程内部分：可选性、提醒、只读查询不该换令牌 ---------
    saved_binding = dict(server_mod._binding)
    try:
        server_mod._binding.update({"id": "", "pid": None, "name": "", "seq": 0})
        check("没有绑定时不提醒（不给坐标路径刷屏）", server_mod._binding_note("") == "")
        server_mod._bind_app({"pid": 4242, "name": "探针应用"})
        first = server_mod._binding["id"]
        check("每个动作都填 settle_ms 之外，绑定令牌也每次都换（否则拦不住旧索引）",
              bool(first) and server_mod._binding["pid"] == 4242)
        second = server_mod._bind_app({"pid": 4243, "name": "另一个探针"})
        check("重新绑定 → 新令牌，旧令牌判失效",
              second != first and server_mod._check_binding(first) and server_mod._check_binding(second) is None,
              f"{first} → {second}")
        check("不带令牌（可选参数）永远放行：老会话与坐标路径不受影响",
              server_mod._check_binding("") is None and server_mod._check_binding(None) is None)
        check("没带令牌时给一句提醒（绑定过应用、有索引可用时才加）",
              "没带 binding_id" in server_mod._binding_note(""), server_mod._binding_note("")[:60])
        before = server_mod._binding["id"]
        server_mod._resolve_app("", bind=False)
        check("list_windows 那种只读过滤不换令牌（查询不该有副作用）",
              server_mod._binding["id"] == before)

        # 提醒真的会挂到 ok 的结果尾部（用合成动作，不碰真实输入）
        @action_layer.action(risk=True)
        def _probe_ok(binding_id: str = ""):   # 和真实动作一样声明这个可选参数（否则批处理里会被丢弃）
            return _AR(ok=True, effect="changed")

        server_mod._ACTIONS["probe_ok"] = action_layer.ActionSpec(
            name="probe_ok", fn=_probe_ok, render=lambda r: "probe" + action_layer.tail(r),
            description="", risk=True
        )
        out = server_mod._run_sequence([{"tool": "probe_ok", "args": {}}], "continue", 5000, 20000)
        check("动作结果尾部带「没带 binding_id」提醒（模型能自我纠正）",
              "没带 binding_id" in out, out.splitlines()[1][:80])
        with_id = server_mod._run_sequence([{"tool": "probe_ok", "args": {"binding_id": server_mod._binding["id"]}}],
                                           "continue", 5000, 20000)
        check("带上当前令牌就没有那句提醒（按指引做就不吵）",
              "没带 binding_id" not in with_id, with_id.splitlines()[1][:80])
        top = server_mod._run_sequence([{"tool": "probe_ok", "args": {}}], "continue", 5000, 20000,
                                       server_mod._binding["id"])
        check("sequence 顶层令牌下发给每一步（不带出每步一句噪声）",
              "没带 binding_id" not in top, top.splitlines()[1][:80])
        del server_mod._ACTIONS["probe_ok"]
    finally:
        server_mod._binding.update(saved_binding)

    # --- 索引纪元：服务器被多会话共用时，「旧索引 → 新绑定」必须响亮失败（任务书 §2.4/§5.1） ---
    saved_app = server_mod._bound_app
    saved_bind = dict(server_mod._binding)
    try:
        apps = server_mod.ax.running_apps()
        if len(apps) < 2:
            skip("索引纪元（旧索引不得命中新绑定）", "当前不足两个有界面的应用")
        else:
            pid_a, pid_b = apps[0]["pid"], apps[1]["pid"]
            server_mod._bind_app({"pid": pid_a, "name": "会话 A 的应用"})
            obs_a = server_mod.ax.observer_for(pid_a)
            obs_a.observe(full=True)
            idx_a = max((n.idx for n in obs_a.nodes.values()), default=None)
            server_mod._bind_app({"pid": pid_b, "name": "会话 B 的应用"})
            obs_b = server_mod.ax.observer_for(pid_b)
            obs_b.observe(full=True)
            idx_b = min((n.idx for n in obs_b.nodes.values()), default=None)
            check("换绑即抬纪元：新树的最小索引大于旧树的最大索引（数值上不可能撞号）",
                  idx_a is not None and idx_b is not None and idx_b > idx_a, f"A 最大 {idx_a} → B 最小 {idx_b}")
            stale = ""
            try:
                obs_b.node_by_index(idx_a)
            except server_mod.AXBridgeError as e:
                stale = e.code
            check("旧索引在新绑定里查不到（ELEMENT_UNAVAILABLE/STALE_STATE，而不是命中同号元素）",
                  stale in ("ELEMENT_UNAVAILABLE", "STALE_STATE"), f"{stale or '居然查到了'}")
            try:
                obs_a.node_by_index(idx_a)
                alive = True
            except server_mod.AXBridgeError:
                alive = False
            check("同一个绑定内的索引仍然有效（纪元只切断换绑前后的重叠，不误伤正常流程）", alive)
    finally:
        server_mod._bound_app = saved_app
        server_mod._binding.update(saved_bind)

    # --- 观察器生命周期：退出应用留下的观察器必须被回收（forget() 一度是死代码 → 真泄漏） ---
    live_pids = [a["pid"] for a in server_mod.ax.running_apps()]
    if not live_pids:
        skip("观察器回收", "当前没有有界面的应用")
    else:
        pid = live_pids[0]
        obs = server_mod.ax.observer_for(pid)
        obs.observe(full=True)
        first = next(iter(obs.nodes.values()), None)
        check("观察器建起来之后确实持有 AX 元素引用（这正是泄漏的东西）",
              first is not None and first.el.p != 0, f"nodes={len(obs.nodes)}")
        check("显式回收：节点引用被释放、注册表不再留着它",
              server_mod.ax.forget_observer(pid) is True
              and pid not in server_mod.ax._OBSERVERS
              and (first is None or first.el.p == 0),
              f"el.p={first.el.p if first else '-'}")
        check("回收是幂等的（再回收一次返回 False，不炸）", server_mod.ax.forget_observer(pid) is False)

        # 死进程：不用等谁显式回收，下一次取观察器时顺手扫掉
        victim = subprocess.Popen(["sleep", "30"], stdout=subprocess.DEVNULL)
        dead_pid = victim.pid
        server_mod.ax.observer_for(dead_pid)
        victim.kill()
        victim.wait(timeout=5)
        server_mod.ax.observer_for(live_pids[0])          # 触发一次 prune
        check("已退出的应用留下的观察器会被自动回收（不用等下一次观察）",
              dead_pid not in server_mod.ax._OBSERVERS, f"残留 pid={dead_pid}")
        check("活着的应用不受影响（回收只针对已经退出的）",
              live_pids[0] in server_mod.ax._OBSERVERS)

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
