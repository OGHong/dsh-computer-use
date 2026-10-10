#!/usr/bin/env python3
"""最小 MCP stdio 服务器 —— 只用 stdlib，不引官方 SDK。

为什么要自己写这一段（都是实测出来的）：

  - `import mcp.server.fastmcp` 一个动作就 +42MB 常驻、+290ms 启动：它是 **全套 web 栈**
    （anyio + httpx + httpx-sse + jsonschema + pydantic + pydantic-settings + uvicorn），
    而我们只用其中「stdin/stdout 上一行一条 JSON-RPC」这一件事；
  - 工具服务器是 dsh 每次会话都拉起来常驻的进程，这 42MB 谁都用不上也躲不掉。

协议面（对着官方 SDK 的真实回包逐字段对拍过，见 scripts/mcp_wire_check.py）：
`initialize` / `notifications/*` / `tools/list` / `tools/call` / `ping`，
未知方法回 `-32601`，异常回 `isError: true` 而不是把连接打断。
`notifications/cancelled` 由读线程就地处理（主线程这时正卡在动作里，没人看队列），
被取消的动作在下一个可中断点让出、并且**不回包**（见 `pause`）。
读 stdin 放在守护线程里：主线程进到动作里时 EOF 也得有人看见（见 `pause`）。
"""
from __future__ import annotations

import base64
import inspect
import json
import queue
import sys
import threading
import time
import typing

PROTOCOL_FALLBACK = "2024-11-05"
KNOWN_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}
_JSON_BY_PY = {int: "integer", float: "number", bool: "boolean", str: "string", list: "array", dict: "object"}

#: 宿主断开（stdin 读到 EOF）时由读线程置位
host_gone = threading.Event()


class Cancelled(BaseException):
    """调用方发来 `notifications/cancelled`：立刻停止动作，并且**不回包**。

    刻意继承 BaseException（与 SystemExit 同类）：工具实现里有大量 `except Exception`
    兜底分支，而取消是控制流、不是「工具错误」——不能被它们吞成一条 isError 再回给一个
    早就不听的调用方。dsh 的 mcp-client 会给每次调用带上取消信号，宿主按停止键时发的
    就是这条通知。
    """


#: 正在执行的请求 id 与其取消信号（主线程登记，读线程比对）
_inflight_lock = threading.Lock()
_inflight_id: str | None = None
_cancelled = threading.Event()
#: 取消比请求先被读到时先记在这里 —— 请求还排在队列里、主线程正忙上一条，这是常态
_cancelled_early: set[str] = set()


def _id_key(value: object) -> str:
    """JSON-RPC id 的稳定键：id 只会是字符串/数字，repr 足够且不会把 1 与 True 混为一谈。"""
    return repr(value)


def request_cancelled(request_id: object) -> None:
    """读线程收到 `notifications/cancelled` 时就地调用（线程安全）。"""
    key = _id_key(request_id)
    with _inflight_lock:
        if _inflight_id == key:
            _cancelled.set()
            return
        # 还没开始跑（或已经跑完）：先记账，等它真开始跑时立刻让出。
        # 上限只是防一条坏连接的无限增长；正常会话里 id 单调递增，旧账自然过期。
        if len(_cancelled_early) > 64:
            _cancelled_early.clear()
        _cancelled_early.add(key)


def _begin_call(msg_id: object) -> bool:
    """登记正在执行的请求；返回 True = 它在开始前就已被取消。"""
    global _inflight_id
    key = _id_key(msg_id)
    with _inflight_lock:
        _inflight_id = key
        _cancelled.clear()
        if key in _cancelled_early:
            _cancelled_early.discard(key)
            _cancelled.set()
            return True
        return False


def _end_call() -> None:
    global _inflight_id
    with _inflight_lock:
        _inflight_id = None
        _cancelled.clear()


def pause(seconds: float) -> None:
    """可被「宿主已经断开」或「调用方取消了这次请求」打断的停顿 —— 动作里所有较长的等待都走这里。

    为什么需要：主线程一进到动作里（wait / sequence / 每步 settle）就没人看 stdin 了，
    宿主这时把连接关掉、或者发一条 notifications/cancelled，我们还原地睡。实测这样滞留过
    一批 python 进程（每个都占 20~37MB，还连带让抓屏 helper 不退）。睡完发现宿主没了就直接
    退出，不再回包（也没人收）；被取消则抛 Cancelled，由 tools/call 分支吞掉且不回包。

    注意：**按键长按**（cg_input.hold / _hold_combo）不走这里 —— 那种等待被中途打断会
    把按键留在按下状态，比多等几秒糟得多。
    """
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        if host_gone.is_set():
            raise SystemExit(0)
        if _cancelled.is_set():
            raise Cancelled("the caller cancelled this request")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        # 50ms 一探：Event.wait 阻塞着不烧 CPU，对取消的反应也足够快（动作的等待从 0.12s 起）
        host_gone.wait(min(0.05, remaining))


class Image:
    """工具返回的图片：读文件 + 猜 mime，和 SDK 的 Image 用法同名同签名。"""

    def __init__(self, path: str):
        self.path = path

    def content(self) -> dict:
        suffix = self.path.rsplit(".", 1)[-1].lower()
        mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif"}.get(suffix, "image/jpeg")
        with open(self.path, "rb") as fh:
            return {"type": "image", "data": base64.b64encode(fh.read()).decode(), "mimeType": mime}


class Blocks:
    """一次工具调用回多块 content：一段文本 + 最多一张图。

    为什么需要：`get_ax_state(screenshot=true)` 与 `sequence` 的收尾 observe 都是
    「结论 + 画面」一起回 —— 一次调用拿两样，正是要省掉的那一个回合。栅格图仍然只允许
    一张（§7 风险 5：一次结果多张图会撑爆上下文），这条约束在工具层自己守。
    """

    def __init__(self, *items):
        self.items = items

    def content(self) -> list[dict]:
        out: list[dict] = []
        for item in self.items:
            if item is None:
                continue
            if isinstance(item, Image):
                out.append(item.content())
            else:
                out.append({"type": "text", "text": item if isinstance(item, str) else json.dumps(item, ensure_ascii=False, default=str)})
        return out


class ToolError(Exception):
    """工具自己抛的业务错误 —— 回成 isError 文本，不打断会话。"""


def _schema(fn) -> dict:
    """从函数签名生成 JSON Schema（工具参数只有标量/列表，不需要校验库）。"""
    hints = typing.get_type_hints(fn)
    props, required = {}, []
    for name, param in inspect.signature(fn).parameters.items():
        ann = hints.get(name)
        if ann in (inspect._empty, None):
            ann = str
        origin = typing.get_origin(ann)
        if origin is typing.Union or str(origin) == "types.UnionType":
            args = [a for a in typing.get_args(ann) if a is not type(None)]
            ann = args[0] if args else str
        kind = _JSON_BY_PY.get(ann, "string")
        props[name] = {"title": name.replace("_", " ").title(), "type": kind}
        if param.default is inspect.Parameter.empty:
            required.append(name)
        elif isinstance(param.default, bool):
            props[name]["default"] = param.default
        elif isinstance(param.default, (int, float, str)):
            props[name]["default"] = param.default
    out = {"properties": props, "type": "object"}
    if required:
        out["required"] = required
    return out


class BadArgument(Exception):
    """参数类型对不上 —— 回成 `ERROR[INVALID_ARGUMENT]` 让模型自己能改，而不是 INTERNAL。"""


def coerce_args(props: dict, args: dict) -> dict:
    """按声明的 JSON Schema 类型把参数过一遍。

    模型经常把数字发成字符串（`{"x": "500"}`），而官方 SDK 的 pydantic 会在入口处顺手
    转换；换成手写协议层后这一步没人做了，`x * sw / iw` 就会变成 `'500' * 1512 / 1512`
    那种 TypeError。这里补回等价行为，并且**未知参数直接丢掉**（原来会炸成
    "unexpected keyword argument"）。嵌套在 `sequence` 里的 args 走同一个函数。
    """
    out = {}
    for key, value in (args or {}).items():
        spec = props.get(key)
        if spec is None:
            continue
        kind = spec.get("type")
        try:
            if kind == "integer" and not isinstance(value, bool):
                out[key] = int(str(value).strip()) if isinstance(value, str) else int(value)
            elif kind == "number" and not isinstance(value, bool):
                out[key] = float(str(value).strip()) if isinstance(value, str) else float(value)
            elif kind == "boolean":
                if isinstance(value, str):
                    out[key] = value.strip().lower() in {"1", "true", "yes", "on"}
                else:
                    out[key] = bool(value)
            elif kind == "string":
                out[key] = value if isinstance(value, str) else str(value)
            else:
                out[key] = value
        except (TypeError, ValueError):
            raise BadArgument(f"参数 {key} 需要 {kind}，收到 {value!r}")
    return out


def coerce_for(fn, args: dict) -> dict:
    """给不在注册表里的可调用对象（如 sequence 的内层动作）现算一次 schema。"""
    props = _schema(fn).get("properties", {})
    return coerce_args(props, args)


class Server:
    def __init__(self, name: str, version: str = "", instructions: str = ""):
        self.name, self.version, self.instructions = name, version, instructions
        self.tools: dict[str, dict] = {}

    def tool(self, **_kw):
        """与 FastMCP 同名的装饰器：`@mcp.tool()`。"""

        def deco(fn):
            # 官方 SDK 直接把 __doc__ 原文塞进 description（带着 4 空格缩进）；
            # getdoc 会去掉缩进，模型读到的内容一致但更干净。
            doc = inspect.getdoc(fn) or fn.__name__
            self.tools[fn.__name__] = {"name": fn.__name__, "description": doc, "inputSchema": _schema(fn), "fn": fn}
            return fn

        return deco

    # ---- 报文处理 ----

    def _reply(self, msg_id, result=None, error=None) -> dict:
        if error is not None:
            return {"jsonrpc": "2.0", "id": msg_id, "error": error}
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def dispatch(self, msg: dict) -> dict | None:
        method, msg_id = msg.get("method"), msg.get("id")
        params = msg.get("params") or {}
        if msg_id is None:  # 通知：没有回复这回事
            return None
        try:
            if method == "initialize":
                asked = str(params.get("protocolVersion") or "")
                return self._reply(
                    msg_id,
                    {
                        "protocolVersion": asked if asked in KNOWN_PROTOCOLS else PROTOCOL_FALLBACK,
                        "capabilities": {"experimental": {}, "prompts": {}, "resources": {}, "tools": {}},
                        "serverInfo": {"name": self.name, "version": self.version},
                        **({"instructions": self.instructions} if self.instructions else {}),
                    },
                )
            if method == "ping":
                return self._reply(msg_id, {})
            if method == "tools/list":
                tools = [
                    {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
                    for t in self.tools.values()
                ]
                return self._reply(msg_id, {"tools": tools})
            if method == "tools/call":
                tool = self.tools.get(str(params.get("name")))
                if tool is None:
                    return self._reply(msg_id, {"content": [{"type": "text", "text": f"未知工具 {params.get('name')!r}"}], "isError": True})
                args = params.get("arguments") or {}
                missing = [k for k in tool["inputSchema"].get("required", []) if k not in args]
                if missing:
                    return self._reply(msg_id, {"content": [{"type": "text", "text": f"缺少参数: {', '.join(missing)}"}], "isError": True})
                try:
                    args = coerce_args(tool["inputSchema"].get("properties", {}), args)
                except BadArgument as e:
                    return self._reply(msg_id, {"content": [{"type": "text", "text": f"ERROR[INVALID_ARGUMENT]: {e}"}], "isError": True})
                if _begin_call(msg_id):
                    _end_call()
                    sys.stderr.write("computer-use: 请求在开始前就被取消，未执行\n")
                    return None
                try:
                    result = tool["fn"](**args)
                except Cancelled:
                    # MCP 规范：收到 notifications/cancelled 之后必须停止处理，且**不回包**
                    sys.stderr.write("computer-use: 动作被调用方取消，已中断\n")
                    return None
                except (ToolError, BadArgument) as e:
                    return self._reply(msg_id, {"content": [{"type": "text", "text": str(e)}], "isError": True})
                except Exception as e:  # 工具炸了不能让会话断掉
                    # 带 .code 的类型化错误（AXBridgeError 那一族）必须原样透出，
                    # 调用方要按 ERROR[AMBIGUOUS_APP] 这类码分支，不能被挤进 INTERNAL 的文案里。
                    code = getattr(e, "code", None)
                    text = str(e) if code else f"ERROR[INTERNAL]: {type(e).__name__}: {e}"
                    return self._reply(msg_id, {"content": [{"type": "text", "text": text}], "isError": True})
                finally:
                    _end_call()
                if isinstance(result, Image):
                    return self._reply(msg_id, {"content": [result.content()], "isError": False})
                if isinstance(result, Blocks):
                    return self._reply(msg_id, {"content": result.content(), "isError": False})
                text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
                return self._reply(msg_id, {"content": [{"type": "text", "text": text}], "isError": False})
            # 声明了 capabilities 里的 prompts/resources 之后，客户端有权来问一遍；
            # 回 -32601 会让 dsh 的 MCP 面板出现一个失败区块，所以这些只读方法回空集合。
            empty = {
                "prompts/list": {"prompts": []},
                "resources/list": {"resources": []},
                "resources/templates/list": {"resourceTemplates": []},
                "logging/setLevel": {},  # 我们没有日志级别，但这是设置类调用，回成功即可
            }
            if method in empty:
                return self._reply(msg_id, empty[method])
            return self._reply(msg_id, None, {"code": -32601, "message": f"method not found: {method}"})
        except Exception as e:  # 解析/构造阶段的问题也要有回包
            return self._reply(msg_id, None, {"code": -32603, "message": str(e)})

    def run(self) -> None:
        """stdin 逐行读、stdout 逐行写；stdout 只能有 JSON-RPC，日志一律走 stderr。

        读放在守护线程里喂队列：主线程进到动作里的时候，EOF 也得有人看见（见 pause）。
        """
        lines: queue.Queue = queue.Queue()

        def pump() -> None:
            for line in sys.stdin:
                # 取消通知必须**就地**处理：此刻主线程正卡在动作里，没人会看队列 ——
                # 「取消能打断动作」的全部意义就在这一步。
                text = line.strip()
                if text.startswith("{") and "notifications/cancelled" in text:
                    try:
                        note = json.loads(text)
                    except ValueError:
                        note = None
                    if isinstance(note, dict) and note.get("method") == "notifications/cancelled":
                        request_cancelled((note.get("params") or {}).get("requestId"))
                        continue
                lines.put(line)
            host_gone.set()
            lines.put(None)

        threading.Thread(target=pump, daemon=True).start()
        while True:
            line = lines.get()
            if line is None:  # 宿主关掉了 stdin
                return
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}) + "\n")
                sys.stdout.flush()
                continue
            reply = self.dispatch(msg) if isinstance(msg, dict) else None
            if reply is not None:
                sys.stdout.write(json.dumps(reply, ensure_ascii=False, default=str) + "\n")
                sys.stdout.flush()
