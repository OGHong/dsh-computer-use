#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""代码执行层（v3）：`run_code` —— 把动作注册表投影成进程内模块 `mu`。

动机与取舍见 `computer-use-v3-设计任务书.md`。一句话：`sequence` 是**声明式步骤表**，
步数在调用前就写死了；`run_code` 让模型写一段程序，步数取决于运行结果（循环 / 条件 / 校验）。

不变量（全部沿用现有动作层，不另建一套）：

  - 拿证：每个内层动作仍返回 `ActionResult`，账本逐条记录（异常路径也记）；
  - 防重放：任一步 `possibly_sent=true` → 整段脚本立刻停止，外层显式回 `possibly_sent=true`；
  - 绑定令牌：入口先校验一次，之后每个带 `binding_id` 的内层调用自动补上顶层令牌；
  - 控制器租约 / 急停：内层动作自己过 `@action` 的三道闸门，外层 `run_code` 也过一遍；
  - 一次最多一张图：脚本里截多次，只附最后一张；
  - 类型化错误：脚本 / 预检 / 参数 / 超时 / 预算各有码，不落 `INTERNAL`。

**边界声明（与 DESIGN.md 同一段，必须原样保留）**：本模块的受限执行**不是安全边界**。
in-process `exec` 在 CPython 里可以被走过（经典 `().__class__.__base__.__subclasses__()` 路径），
双下划线属性拦截只提高门槛。它挡的是手滑与幻觉；而「让模型在这台机器上跑任意命令」这个能力
本来就由 `run_shell` 这个独立工具承载，所以不构成提权。要真隔离只有子进程一条路（v3.1 评估）。
"""
from __future__ import annotations

import builtins
import inspect
import re
import sys
import time

import action_layer
import mcp_stdio
from mcp_stdio import BadArgument

# ---------------------------------------------------------------------------
# 静态预检的白名单
# ---------------------------------------------------------------------------

_DENY_NAMES = {
    "open", "eval", "exec", "compile", "__import__", "globals", "locals",
    "vars", "dir", "getattr", "setattr", "delattr", "breakpoint",
    "input", "help", "exit", "quit", "object", "type", "super",
}
_ALLOW_MODULES = {"json", "re", "math", "itertools", "collections", "textwrap"}
_BUILTIN_NAMES = (
    "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "format",
    "int", "isinstance", "len", "list", "map", "max", "min", "print", "pow", "range",
    "repr", "reversed", "round", "set", "slice", "sorted", "str", "sum", "tuple", "zip",
    # AttributeError 必须可用：文档给出的 mu.run_shell 检测方式就是 except AttributeError。
    # 但不放 BaseException —— ScriptHalt 靠它不可被脚本 `except Exception` 吞掉。
    "Exception", "AttributeError", "ValueError", "KeyError", "TypeError", "RuntimeError",
)
#: `mu` 上的非动作属性：`done` 设置最终结果，`log` 是 print 的等价糖
_META_ATTRS = {"done", "log"}
#: 只有 allow_shell=true 才注入；其余情况属性根本不存在（AttributeError 级拒绝）
_GATED_ATTRS = {"run_shell"}

_MAX_CODE = 20000
_STDOUT_CAP = 8192
_DETAIL_CAP = 2048
#: detail="full" 时观察文本的上限（附录允许实现方自定，只要不是无限）
_FULL_STATE_CAP = 16000
_COMPACT_NODES = 60

#: 由 server.py 在模块加载时注入（名字唯一来源是 _ACTIONS，这里不写第二份清单）
_api: dict = {}
_specs: dict = {}
_settle_budget: list = [None]
_last_shot_size_cb = None


def configure(api: dict, specs: dict, settle_budget: list, last_shot_size=None) -> None:
    """server.py 模块加载时调用一次；`api` 是 `{name: spec.fn}`，`specs` 是 `_ACTIONS`。

    `last_shot_size` 是读「最近一张截图像素尺寸」的回调。必须由 server.py 传进来：
    服务器以 `__main__` 启动时，`import server` 会加载第二份模块，读到的是另一个
    未初始化的 `_last_shot`（实测 `size_px` 因此恒为 null）。
    """
    global _api, _specs, _settle_budget, _last_shot_size_cb
    _api, _specs, _settle_budget = api, specs, settle_budget
    _last_shot_size_cb = last_shot_size


class ScriptHalt(BaseException):
    """脚本必须立刻停止（超时 / 超预算 / 可能已下发 / 急停）。

    刻意继承 BaseException（与 ``killswitch.Killed``、``mcp_stdio.Cancelled`` 同类）：
    动作路径里有大量 ``except Exception`` 兜底分支，而停止是**控制流**、不是「工具错误」——
    被它们吞成一条普通错误然后脚本继续跑完，墙钟与预算就形同虚设。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# 工具入口（动作层）
# ---------------------------------------------------------------------------

@action_layer.action(risk=True)
def run_code(
    code: str,
    *,
    binding_id: str = "",
    dry_run: bool = False,
    allow_shell: bool = False,
    wall_timeout_ms: int = 20000,
    max_actions: int = 50,
    max_observations: int = 10,
    step_timeout_ms: int = 5000,
    detail: str = "compact",
) -> action_layer.ActionResult:
    """执行一段程序；预检失败 / 脚本异常 / 超时都回类型化错误码，并保留完整账本。"""
    started = time.monotonic()
    try:
        wall_ms = max(1000, min(int(wall_timeout_ms), 120000))
        max_act = max(1, min(int(max_actions), 200))
        max_obs = max(0, min(int(max_observations), 50))
        step_ms = max(200, min(int(step_timeout_ms), 30000))
    except (TypeError, ValueError):
        return _fail("INVALID_ARGUMENT", "wall_timeout_ms / max_actions / max_observations / step_timeout_ms 需要是整数。", started)
    mode = str(detail or "compact").strip().lower()
    if mode not in ("compact", "full"):
        return _fail("INVALID_ARGUMENT", 'detail 只能是 "compact" 或 "full"。', started)
    if not isinstance(code, str):
        code = str(code)
    if len(code) > _MAX_CODE:
        return _fail("INVALID_ARGUMENT", f"code 长度 {len(code)} 超过上限 {_MAX_CODE} 字符。", started)

    # dry_run 只做静态预检：不执行、不碰电脑、不需要绑定。
    # 外层 @action 仍会做急停/租约/绑定三道闸门（run_code 本身是 risk=true），
    # 所以带错令牌时连预检都不会跑 —— 这与「不需要绑定」不冲突：不传即可。
    if not dry_run:
        stale = action_layer.check_binding(binding_id)
        if stale:
            return _fail(
                "BINDING_CHANGED", stale, started,
                hint="重新 get_app 绑定目标，再 get_ax_state 拿新索引；不要用旧索引操作。",
            )

    api = _api
    specs = _specs
    if not api or not specs:
        return _fail("INTERNAL", "代码执行层未初始化（server.py 没有调用 code_exec.configure）。", started)

    pre = _precheck(code, api, specs, allow_shell)
    if pre["error"]:
        elapsed = _elapsed(started)
        return action_layer.ActionResult(
            ok=False, error=pre["error"], message=pre["message"], possibly_sent=False,
            settle_ms=elapsed,
            detail={
                "records": [], "stdout": "", "elapsed_ms": elapsed, "step": 0, "executed": 0,
                "dry_run": dry_run, "precheck": pre["report"], "precheck_failed": True,
                "wall_timeout_ms": wall_ms,
            },
        )
    if dry_run:
        elapsed = _elapsed(started)
        return action_layer.ActionResult(
            ok=True, possibly_sent=False, settle_ms=elapsed,
            detail={
                "records": [], "stdout": "", "elapsed_ms": elapsed, "step": 0, "executed": 0,
                "dry_run": True, "precheck": pre["report"], "precheck_failed": False,
                "wall_timeout_ms": wall_ms,
            },
        )
    return _execute(
        code=code, tree=pre["tree"], api=api, specs=specs, binding_id=binding_id,
        allow_shell=allow_shell, wall_ms=wall_ms, max_act=max_act, max_obs=max_obs,
        step_ms=step_ms, mode=mode, warnings=pre["warnings"],
    )


def render_run_code(r: action_layer.ActionResult) -> object:
    """把 run_code 的 ActionResult 渲染成「头行 + stdout + 账本 + result」。"""
    d = r.detail or {}
    if d.get("dry_run") or d.get("precheck_failed"):
        report = str(d.get("precheck") or "")
        if d.get("precheck_failed") and not d.get("dry_run"):
            report += "\n没有执行任何动作（预检未通过）。"
        return report

    records = d.get("records") or []
    elapsed = int(d.get("elapsed_ms") or 0)
    stdout = str(d.get("stdout") or "")
    lines: list[str] = []

    if r.ok:
        lines.append(
            f"run_code 完成：执行 {int(d.get('actions_ok') or 0)} 个动作、"
            f"{int(d.get('observations_ok') or 0)} 次观察，耗时 {elapsed}ms"
            f"（预算 {int(d.get('wall_timeout_ms') or 0)}ms）。"
        )
    else:
        step = int(d.get("step") or len(records))
        executed = int(d.get("executed") or sum(1 for x in records if x.get("ok")))
        short = _first_line(r.message)
        code = r.error or "SCRIPT_ERROR"
        pre_reject = {"BINDING_CHANGED", "CONTROLLER_BUSY", "KILLED", "INVALID_ARGUMENT"}
        if step <= 0 and code in pre_reject:
            head = f"run_code 在开始前被拒（{code}：{short}），已执行 0 步、耗时 {elapsed}ms。"
        elif step <= 0:
            head = f"run_code 在脚本执行中停止（{code}：{short}），已执行 0 步、耗时 {elapsed}ms。"
        else:
            head = f"run_code 在第 {step} 步之后停止（{code}：{short}），已执行 {executed} 步、耗时 {elapsed}ms。"
        lines.append(head)
        err = f"ERROR[{code}]" + (f": {r.message}" if r.message else "")
        hint = d.get("hint")
        if hint:
            err += f" {hint}"
        lines.append(err)

    lines.append("--- stdout ---")
    lines.append(stdout if stdout.strip() else "（无输出）")
    lines.append("--- 账本 ---")
    if records:
        lines.extend(_ledger_lines(records))
    elif r.ok:
        lines.append("（账本为空：这段程序没有调用任何动作）")
    else:
        lines.append("（账本为空：整批没有执行，一步都没有下发）")

    if not r.ok and records:
        lines.append("上面每一条就是**实际已经发生**的副作用清单，不要当成「什么都没做」。")
    if int(d.get("image_count") or 0) > 1:
        lines.append(
            f"（脚本内共产生 {int(d['image_count'])} 张图，只附最后一张；"
            "非末步的 observe 不附图：一次结果最多一张栅格图）"
        )
    if d.get("done_set"):
        lines.append("--- result ---")
        lines.append(str(d.get("done") or "None"))

    extra = action_layer.tail(r)
    if extra:
        lines.append(extra.strip())

    body = "\n".join(lines)
    image = d.get("image")
    if image:
        return mcp_stdio.Blocks(body, mcp_stdio.Image(path=str(image)))
    return body


# ---------------------------------------------------------------------------
# 静态预检（AST）
# ---------------------------------------------------------------------------

def _precheck(code: str, api: dict, specs: dict, allow_shell: bool) -> dict:
    import ast

    try:
        tree = ast.parse(code, filename="<run_code>", mode="exec")
    except SyntaxError as e:
        line = e.lineno or 1
        msg = e.msg or str(e)
        report = f"预检失败 ERROR[SCRIPT_SYNTAX]: line {line}: {msg}"
        return {"tree": None, "error": "SCRIPT_SYNTAX", "line": line,
                "message": f"line {line}: {msg}", "report": report,
                "actions": 0, "observations": 0, "warnings": []}

    def loop_has_break(node) -> bool:
        """这个循环体里有没有一个能退出**本层**循环的 break（嵌套循环里的不算）。"""
        if isinstance(node, ast.Break):
            return True
        if isinstance(node, (ast.While, ast.For, ast.AsyncFor, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            return False
        return any(loop_has_break(child) for child in ast.iter_child_nodes(node))

    class Checker(ast.NodeVisitor):
        def __init__(self):
            self.error = None
            self.actions = 0
            self.observations = 0
            self.warnings: list[str] = []
            self._warned: set = set()

        def fail(self, code: str, line: int, message: str) -> None:
            if self.error is None:
                self.error = (code, line, message)

        def visit_Import(self, node) -> None:
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in _ALLOW_MODULES:
                    self.fail("SCRIPT_FORBIDDEN", node.lineno,
                              f"禁止 import {alias.name}（只允许 {', '.join(sorted(_ALLOW_MODULES))}）")
                if alias.asname in {"mu", "print"}:
                    self.fail("SCRIPT_FORBIDDEN", node.lineno, f"不许用 import 覆盖 {alias.asname}")
            self.generic_visit(node)

        def visit_ImportFrom(self, node) -> None:
            if node.level:
                self.fail("SCRIPT_FORBIDDEN", node.lineno, "禁止相对 import")
            module = node.module or ""
            if module.split(".")[0] not in _ALLOW_MODULES:
                self.fail("SCRIPT_FORBIDDEN", node.lineno,
                          f"禁止 import {module or '.'}（只允许 {', '.join(sorted(_ALLOW_MODULES))}）")
            for alias in node.names:
                if alias.asname in {"mu", "print"}:
                    self.fail("SCRIPT_FORBIDDEN", node.lineno, f"不许用 import 覆盖 {alias.asname}")
            self.generic_visit(node)

        def visit_Name(self, node) -> None:
            if node.id in _DENY_NAMES:
                self.fail("SCRIPT_FORBIDDEN", node.lineno, f"禁用名字 {node.id}")
            self.generic_visit(node)

        def visit_Attribute(self, node) -> None:
            attr = node.attr
            if attr.startswith("__") or attr.endswith("__"):
                self.fail("SCRIPT_FORBIDDEN", node.lineno, f"禁止访问双下划线属性 {attr}")
            elif isinstance(node.value, ast.Name) and node.value.id == "mu":
                if attr not in _META_ATTRS and attr not in api:
                    self.fail("SCRIPT_UNKNOWN_ACTION", node.lineno, f"mu.{attr} 不是可用的动作。")
                elif attr in _GATED_ATTRS and not allow_shell:
                    key = (node.lineno, attr)
                    if key not in self._warned:
                        self._warned.add(key)
                        self.warnings.append(
                            f"⚠️ 第 {node.lineno} 行 `mu.{attr}`：allow_shell=false 时该属性不存在，"
                            "运行时会 AttributeError。"
                        )
            self.generic_visit(node)

        def visit_Call(self, node) -> None:
            f = node.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "mu":
                spec = specs.get(f.attr)
                if spec is not None:
                    if spec.risk:
                        self.actions += 1
                    elif spec.read_only:
                        self.observations += 1
            self.generic_visit(node)

        def visit_Global(self, node) -> None:
            self.fail("SCRIPT_FORBIDDEN", node.lineno, "禁止 global")

        def visit_Nonlocal(self, node) -> None:
            self.fail("SCRIPT_FORBIDDEN", node.lineno, "禁止 nonlocal")

        def visit_AsyncFunctionDef(self, node) -> None:
            self.fail("SCRIPT_FORBIDDEN", node.lineno, "不支持 async（没有事件循环）")

        def visit_Await(self, node) -> None:
            self.fail("SCRIPT_FORBIDDEN", node.lineno, "不支持 await（没有事件循环）")

        def visit_AsyncFor(self, node) -> None:
            self.fail("SCRIPT_FORBIDDEN", node.lineno, "不支持 async for（没有事件循环）")

        def visit_AsyncWith(self, node) -> None:
            self.fail("SCRIPT_FORBIDDEN", node.lineno, "不支持 async with（没有事件循环）")

        def _check_target(self, target, line: int) -> None:
            for node in ast.walk(target):
                if (isinstance(node, ast.Name) and node.id in {"mu", "print"}
                        and isinstance(node.ctx, (ast.Store, ast.Del))):
                    self.fail("SCRIPT_FORBIDDEN", line, f"不许覆盖注入的 {node.id}")
                elif (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                      and node.value.id == "mu"):
                    self.fail("SCRIPT_FORBIDDEN", line, "不许覆盖注入的 mu")

        def visit_Assign(self, node) -> None:
            for target in node.targets:
                self._check_target(target, node.lineno)
            self.generic_visit(node)

        def visit_AnnAssign(self, node) -> None:
            self._check_target(node.target, node.lineno)
            self.generic_visit(node)

        def visit_AugAssign(self, node) -> None:
            self._check_target(node.target, node.lineno)
            self.generic_visit(node)

        def visit_For(self, node) -> None:
            self._check_target(node.target, node.lineno)
            self.generic_visit(node)

        def visit_With(self, node) -> None:
            for item in node.items:
                if item.optional_vars is not None:
                    self._check_target(item.optional_vars, node.lineno)
            self.generic_visit(node)

        def visit_NamedExpr(self, node) -> None:
            self._check_target(node.target, node.lineno)
            self.generic_visit(node)

        def visit_Delete(self, node) -> None:
            for target in node.targets:
                self._check_target(target, node.lineno)
            self.generic_visit(node)

        def visit_FunctionDef(self, node) -> None:
            if node.name in {"mu", "print"}:
                self.fail("SCRIPT_FORBIDDEN", node.lineno, f"不许覆盖注入的 {node.name}")
            self.generic_visit(node)

        def visit_ClassDef(self, node) -> None:
            if node.name in {"mu", "print"}:
                self.fail("SCRIPT_FORBIDDEN", node.lineno, f"不许覆盖注入的 {node.name}")
            self.generic_visit(node)

        def visit_While(self, node) -> None:
            test = node.test
            if isinstance(test, ast.Constant) and bool(test.value) and not any(
                loop_has_break(stmt) for stmt in node.body
            ):
                src = ast.get_source_segment(code, test) or "True"
                self.warnings.append(
                    f"⚠️ 第 {node.lineno} 行 `while {src}` 没有 break：到 wall_timeout_ms 会被硬停。"
                )
            self.generic_visit(node)

    checker = Checker()
    checker.visit(tree)
    if checker.error:
        code_name, line, message = checker.error
        if code_name == "SCRIPT_UNKNOWN_ACTION":
            report = (
                f"预检失败 ERROR[SCRIPT_UNKNOWN_ACTION]: line {line} {message}\n"
                f"可用：{', '.join(sorted(api))}"
            )
        elif code_name == "SCRIPT_SYNTAX":
            report = f"预检失败 ERROR[SCRIPT_SYNTAX]: {message}"
        else:
            report = f"预检失败 ERROR[{code_name}]: line {line}: {message}"
        return {"tree": None, "error": code_name, "line": line, "message": message,
                "report": report, "actions": checker.actions,
                "observations": checker.observations, "warnings": checker.warnings}

    report = (
        f"预检通过：可执行。动作 {checker.actions} 个、观察 {checker.observations} 次、"
        "用到的动作名合法。"
    )
    if checker.warnings:
        report += "\n" + "\n".join(checker.warnings)
    return {"tree": tree, "error": None, "line": None, "message": "",
            "report": report, "actions": checker.actions,
            "observations": checker.observations, "warnings": checker.warnings}


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

class _CappedStdout:
    """stdout 累计上限；超出部分丢弃并记总数，避免一段 print 循环撑爆结果。"""

    def __init__(self, cap: int):
        self.cap = cap
        self.parts: list[str] = []
        self.size = 0
        self.total = 0
        self.truncated = False

    def write(self, text) -> int:
        s = text if isinstance(text, str) else str(text)
        raw = s.encode("utf-8")
        self.total += len(raw)
        if self.truncated:
            return len(s)
        remain = self.cap - self.size
        if len(raw) > remain:
            keep = raw[:max(0, remain)].decode("utf-8", "ignore")
            self.parts.append(keep)
            self.size += len(keep.encode("utf-8"))
            self.truncated = True
        else:
            self.parts.append(s)
            self.size += len(raw)
        return len(s)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def getvalue(self) -> str:
        out = "".join(self.parts)
        if self.truncated:
            out += f"\n… [stdout 已截断，共 {self.total} 字节]"
        return out


class _Mu:
    """注入对象：动作属性**只**从传入的 api（= `_ACTIONS` 投影）生成，没有第二份名字表。"""

    def __init__(self, api: dict, allow_shell: bool, invoke, done):
        self._invoke = invoke
        self._done = done
        self._names = tuple(sorted(
            name for name in api if name not in _GATED_ATTRS or allow_shell
        ))
        for name in self._names:
            setattr(self, name, self._action(name))

    def _action(self, name: str):
        def call(*args, **kwargs):
            line = sys._getframe(1).f_lineno
            return self._invoke(name, args, kwargs, line)

        call.__name__ = name
        return call

    def __getattr__(self, item: str):
        if item in _GATED_ATTRS:
            raise AttributeError(
                f"mu.{item} 不可用：本次 run_code 没有带 allow_shell=true（{item} 默认不注入）。"
            )
        raise AttributeError(f"mu.{item} 不是可用的动作。可用：{', '.join(self._names)}")

    def done(self, value) -> None:
        self._done(value)

    def log(self, *args, **kwargs) -> None:
        print(*args, **kwargs)


def _execute(*, code, tree, api, specs, binding_id, allow_shell, wall_ms,
             max_act, max_obs, step_ms, mode, warnings) -> action_layer.ActionResult:
    import contextlib
    import importlib
    import linecache
    import signal

    # 让 traceback 能取到用户脚本的源码行（compile 的 filename 不在 sys.path 里）
    linecache.cache["<run_code>"] = (len(code), None, code.splitlines(True), "<run_code>")
    started = time.monotonic()
    records: list[dict] = []
    state = {
        "done": None, "done_set": False,
        "images": [], "last_image": None,
        "actions": 0, "observations": 0,
    }
    prev_budget = _settle_budget[0]

    def remember_image(path: str) -> None:
        state["images"].append(path)
        state["last_image"] = path

    def done(value) -> None:
        try:
            text = repr(value)
        except Exception:  # noqa: BLE001 - repr 失败不该把脚本带走
            text = "<unrepresentable>"
        state["done"] = text[:500]
        state["done_set"] = True

    def invoke(name: str, args: tuple, kwargs: dict, line: int):
        spec = specs.get(name)
        if spec is None:
            msg = f"mu.{name} 不是可用的动作。可用：{', '.join(sorted(api))}"
            records.append({
                "tool": name, "kind": "other", "ok": False, "error": "SCRIPT_UNKNOWN_ACTION",
                "message": msg, "possibly_sent": False, "settle_ms": 0, "line": line,
            })
            raise ScriptHalt("SCRIPT_UNKNOWN_ACTION", msg)

        kind = "action" if spec.risk else ("observation" if spec.read_only else "other")
        call_started = time.monotonic()
        dispatched = False
        try:
            # 每个内层注入调用的入口：宿主断开 / 调用方取消在下一个可中断点让出
            mcp_stdio.pause(0)
            signature = inspect.signature(spec.fn)
            params = signature.parameters
            try:
                bound = signature.bind(*args, **kwargs)
            except TypeError as e:
                raise ScriptHalt("SCRIPT_ARGUMENT", _arg_message(spec, e))

            # 顶层令牌下发给每一步：整段程序是一个逻辑动作，绑定是同一份。
            # 脚本显式传了就尊重它（包括传空串 = 主动放弃这层保护）。
            if binding_id and "binding_id" in params and "binding_id" not in bound.arguments:
                bound.arguments["binding_id"] = binding_id

            call_kwargs = {
                k: v for k, v in bound.arguments.items()
                if k in params and params[k].kind not in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.VAR_POSITIONAL,
                    inspect.Parameter.VAR_KEYWORD,
                )
            }
            try:
                coerced = mcp_stdio.coerce_for(spec.fn, call_kwargs)
            except BadArgument as e:
                raise ScriptHalt("SCRIPT_ARGUMENT", _arg_message(spec, e))

            if spec.risk:
                if state["actions"] >= max_act:
                    raise ScriptHalt(
                        "SCRIPT_BUDGET",
                        f"动作数超过 max_actions={max_act}（第 {len(records) + 1} 次动作调用未下发）。",
                    )
                state["actions"] += 1
            elif spec.read_only:
                if state["observations"] >= max_obs:
                    raise ScriptHalt(
                        "SCRIPT_BUDGET",
                        f"观察数超过 max_observations={max_obs}（第 {len(records) + 1} 次观察未执行）。",
                    )
                state["observations"] += 1

            _settle_budget[0] = step_ms
            dispatched = True
            res = spec.fn(**coerced)
        except BaseException as exc:  # 账本在异常路径也记；记完原样抛回，绝不吞
            records.append({
                "tool": name, "kind": kind, "ok": False,
                "error": _err_code(exc), "message": str(exc)[:2000],
                "possibly_sent": action_layer.was_sent() if dispatched else False,
                "settle_ms": int((time.monotonic() - call_started) * 1000),
                "line": line,
            })
            raise
        finally:
            _settle_budget[0] = prev_budget

        duration = res.settle_ms if res.settle_ms is not None else int((time.monotonic() - call_started) * 1000)
        record = {
            "tool": name, "kind": kind, "ok": bool(res.ok), "effect": res.effect,
            "settle_ms": duration, "possibly_sent": bool(res.possibly_sent),
            "error": res.error, "message": str(res.message or "")[:2000],
            "obs": None, "line": line,
        }
        # 先记账再投影：投影层再出意外也不能让「已经发生的那一步」从账本里消失
        records.append(record)
        try:
            projected, obs = _project_result(res, mode, state, call_kwargs, remember_image)
            record["obs"] = obs
        except Exception:  # noqa: BLE001 - 投影失败退化成最小 dict，动作本身已经发生
            projected = {
                "ok": bool(res.ok), "effect": res.effect, "error": res.error,
                "message": str(res.message or "")[:300], "detail": {},
            }

        if res.possibly_sent:
            raise ScriptHalt(
                "POSSIBLY_SENT",
                f"第 {len(records)} 步 possibly_sent=true（{name}）：已停止后续调用，先观察再决定。",
            )
        if res.error == "KILLED":
            raise ScriptHalt("KILLED", f"急停已生效（{name}）：已停止后续调用。")
        return projected

    mu = _Mu(api, allow_shell, invoke, done)

    modules = {}
    for name in sorted(_ALLOW_MODULES):
        try:
            modules[name] = importlib.import_module(name)
        except Exception:  # noqa: BLE001 - 预加载失败就让脚本里的 import 自己报
            continue

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level:
            raise ImportError("相对 import 被禁用")
        top = str(name).split(".")[0]
        if top not in _ALLOW_MODULES:
            raise ImportError(
                f"import {name} 被禁用（只允许 {', '.join(sorted(_ALLOW_MODULES))}）"
            )
        importlib.import_module(name)
        return importlib.import_module(name if fromlist else top)

    safe_builtins = {name: getattr(builtins, name) for name in _BUILTIN_NAMES if hasattr(builtins, name)}
    safe_builtins["__import__"] = guarded_import
    globals_dict = {"__builtins__": safe_builtins, "mu": mu, **modules}

    capped = _CappedStdout(_STDOUT_CAP)
    halt = None
    failure = None

    old_handler = None
    old_timer = None
    try:
        old_handler = signal.getsignal(signal.SIGALRM)

        def on_alarm(signum, frame):  # noqa: ARG001 - 信号处理器里只做 raise，不做 IO
            raise ScriptHalt(
                "SCRIPT_TIMEOUT",
                f"超过 wall_timeout_ms={wall_ms}ms 未完成，已硬停（纯 CPU 脚本也停得住）。",
            )

        signal.signal(signal.SIGALRM, on_alarm)
        old_timer = signal.setitimer(signal.ITIMER_REAL, wall_ms / 1000.0)
    except (ValueError, OSError) as e:
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
            if old_handler is not None:
                signal.signal(signal.SIGALRM, old_handler)
        except Exception:  # noqa: BLE001 - 复位失败不掩盖真正的安装错误
            pass
        return _finish_error(
            started, records, state, "", "SCRIPT_ERROR",
            f"无法安装墙钟定时器：{type(e).__name__}: {e}", wall_ms, warnings,
        )

    try:
        try:
            with contextlib.redirect_stdout(capped):
                exec(compile(tree, "<run_code>", "exec"), globals_dict)
        except ScriptHalt as e:
            halt = e
        except Exception as e:  # noqa: BLE001 - 脚本自己的异常，翻译成 SCRIPT_ERROR
            failure = e
    finally:
        # 这个进程是宿主级单例：漏一次复位就会打歪下一个请求
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer and old_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, old_timer[0], old_timer[1])
        _settle_budget[0] = prev_budget

    elapsed = _elapsed(started)
    stdout = capped.getvalue()
    possibly_sent = _batch_possibly_sent(records)
    detail = _result_detail(records, state, elapsed, stdout, wall_ms, warnings)

    if halt is not None:
        if halt.code == "POSSIBLY_SENT":
            possibly_sent = True
        detail["line"] = _user_line(halt)
        return action_layer.ActionResult(
            ok=False, error=halt.code, message=halt.message,
            possibly_sent=possibly_sent, settle_ms=elapsed, detail=detail,
        )
    if failure is not None:
        line, message = _script_error(failure)
        detail["line"] = line
        return action_layer.ActionResult(
            ok=False, error="SCRIPT_ERROR", message=message,
            possibly_sent=possibly_sent, settle_ms=elapsed, detail=detail,
        )
    return action_layer.ActionResult(
        ok=True, possibly_sent=possibly_sent, settle_ms=elapsed, detail=detail,
    )


def _batch_possibly_sent(records: list[dict]) -> bool:
    """整批防重放：任一条记录可能已下发就是 True。

    刻意**不**取「最后一个内层调用」的值：`action.run()` 每次进入都会重置 `_sent[0]`，
    后一个正常观察会把最后一个内层调用的 `possibly_sent` 刷回 False。这个函数是
    「整批」这条不变量的唯一算法，wire check 有一条不依赖 halt 顺序的纯单测钉住它。
    """
    return any(bool(rec.get("possibly_sent")) for rec in records)


def _result_detail(records, state, elapsed, stdout, wall_ms, warnings) -> dict:
    actions_ok = sum(1 for rec in records if rec.get("ok") and rec.get("kind") == "action")
    observations_ok = sum(1 for rec in records if rec.get("ok") and rec.get("kind") == "observation")
    return {
        "records": records,
        "elapsed_ms": elapsed,
        "stdout": stdout,
        "done": state["done"],
        "done_set": state["done_set"],
        "image": state["last_image"],
        "image_count": len(state["images"]),
        "step": len(records),
        "executed": sum(1 for rec in records if rec.get("ok")),
        "actions_ok": actions_ok,
        "observations_ok": observations_ok,
        "wall_timeout_ms": wall_ms,
        "warnings": warnings,
        "dry_run": False,
        "precheck_failed": False,
    }


def _finish_error(started, records, state, stdout, code, message, wall_ms, warnings) -> action_layer.ActionResult:
    elapsed = _elapsed(started)
    detail = _result_detail(records, state, elapsed, stdout, wall_ms, warnings)
    return action_layer.ActionResult(
        ok=False, error=code, message=message, possibly_sent=False,
        settle_ms=elapsed, detail=detail,
    )


def _project_result(res, mode, state, call_kwargs, remember_image):
    """ActionResult → mu 返回值；返回 (投影 dict, 观察元数据)。

    动作名在这里**不出现**：投影按结果的数据形状分派（`state` = 元素树观察，
    `path` + `basis` = 截图，其余 detail 的小字段原样提为顶层），所以 `mu` 的名字
    来源仍然只有 `_ACTIONS` 一处。
    """
    base = {
        "ok": bool(res.ok),
        "effect": res.effect,
        "evidence": _clip(res.evidence or "", _DETAIL_CAP),
        "settle_ms": res.settle_ms,
        "possibly_sent": res.possibly_sent,
        "error": res.error,
        "message": _clip(res.message or "", _DETAIL_CAP),
        # 设计任务书 §3.2：detail 只保留 hint / notice / hints（大块数据另走顶层字段）
        "detail": _cap_detail(res.detail or {}),
    }
    obs = None

    if res.ok and "state" in res.detail:
        raw = str(res.detail.get("state") or "")
        total, returned, truncated, nodes = _parse_state(raw, mode)
        obs = (total, returned)
        if mode == "full":
            base["state"] = _cap_full_state(raw)
            base["total"] = total
            base["returned"] = returned
            base["truncated"] = truncated
        else:
            base["total"] = total
            base["returned"] = returned
            base["nodes"] = nodes
            base["truncated"] = truncated
            if truncated:
                base["hint"] = (
                    '只回了前 60 行；要全量用 run_code(detail="full") 或单独调 get_ax_state'
                )
        image = res.detail.get("image")
        if image:
            base["image"] = image
            remember_image(str(image))
    elif res.ok and "path" in res.detail and "basis" in res.detail:
        path = res.detail.get("path")
        base["path"] = path
        base["size_px"] = _last_shot_size()
        base["display"] = call_kwargs.get("display", 1)
        if path:
            remember_image(str(path))

    # 其余 detail 是动作的主数据（apps / windows / binding_id / stdout / exit / label …）：
    # 提到顶层给脚本按数据分支，`detail` 本身仍只放 hint/notice/hints。
    for key, value in res.detail.items():
        if key in {"state", "hint", "notice", "hints"}:
            continue
        base[key] = _cap_value(value)
    return base, obs


def _parse_state(raw: str, mode: str):
    """从 get_ax_state 的文本里恢复 total / returned / truncated / 节点行。"""
    node_lines = [ln for ln in raw.splitlines() if re.match(r"^\[\d+\]", ln)]
    match = re.search(r"注册表共\s*(\d+)\s*元素", raw) or re.search(r"共\s*(\d+)\s*个元素", raw)
    total = int(match.group(1)) if match else len(node_lines)
    if mode == "full":
        return total, len(node_lines), ("截断=是" in raw), None
    truncated = ("截断=是" in raw) or len(node_lines) > _COMPACT_NODES
    nodes = node_lines[:_COMPACT_NODES]
    return total, len(nodes), truncated, nodes


def _last_shot_size():
    try:
        if _last_shot_size_cb is not None:
            size = _last_shot_size_cb()
        else:
            import server
            size = server._last_shot.get("size")
        return list(size) if size else None
    except Exception:  # noqa: BLE001 - 拿不到尺寸不该影响 screenshot 本身的返回
        return None


def _cap_full_state(raw: str) -> str:
    text = str(raw)
    raw_bytes = text.encode("utf-8")
    if len(raw_bytes) <= _FULL_STATE_CAP:
        return text
    kept = raw_bytes[:_FULL_STATE_CAP].decode("utf-8", "ignore")
    return kept + f"\n… [state 已截断，共 {len(raw_bytes)} 字节；请分段观察或用 window/max_nodes]"


def _cap_detail(detail: dict) -> dict:
    out = {}
    for key in ("hint", "notice", "hints"):
        value = detail.get(key)
        if not value:
            continue
        if isinstance(value, list):
            out[key] = value[:20]
        else:
            out[key] = _clip(value, _DETAIL_CAP)
    return out


def _cap_value(value):
    if isinstance(value, str):
        return _clip(value, _DETAIL_CAP)
    if isinstance(value, list):
        return value[:200]
    if isinstance(value, dict):
        return {
            k: (_clip(v, 512) if isinstance(v, str) else v)
            for k, v in value.items()
        }
    return value


# ---------------------------------------------------------------------------
# 错误/渲染小件
# ---------------------------------------------------------------------------

def _arg_message(spec, exc: Exception) -> str:
    signature = inspect.signature(spec.fn).replace(return_annotation=inspect.Signature.empty)
    return (
        f"{spec.name} 的参数不对：{exc}。"
        f"合法参数：{signature}"
    )


def _err_code(exc: BaseException) -> str:
    if isinstance(exc, ScriptHalt):
        return exc.code
    if isinstance(exc, BadArgument):
        return "SCRIPT_ARGUMENT"
    try:
        import killswitch
        if isinstance(exc, killswitch.Killed):
            return "KILLED"
    except Exception:  # noqa: BLE001
        pass
    try:
        if isinstance(exc, mcp_stdio.Cancelled):
            return "CANCELLED"
    except Exception:  # noqa: BLE001
        pass
    return type(exc).__name__


def _user_line(exc: BaseException):
    tb = exc.__traceback__
    line = None
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == "<run_code>":
            line = tb.tb_lineno
        tb = tb.tb_next
    return line


def _script_error(exc: Exception):
    import traceback

    frames = [
        f for f in traceback.extract_tb(exc.__traceback__)
        if f.filename == "<run_code>"
    ]
    line = frames[-1].lineno if frames else None
    head = f"line {line}: {type(exc).__name__}: {exc}" if line else f"{type(exc).__name__}: {exc}"
    rows = [
        f"  line {f.lineno} in {f.name}: {f.line or ''}"
        for f in frames[-8:]
    ]
    return line, head + ("\n" + "\n".join(rows) if rows else "")


def _ledger_lines(records: list[dict]) -> list[str]:
    out = []
    for i, rec in enumerate(records, 1):
        tool = rec.get("tool") or "?"
        if rec.get("ok"):
            bits = [
                f"effect: {rec.get('effect') or 'unknown'}",
                f"{int(rec.get('settle_ms') or 0)}ms",
            ]
            if rec.get("possibly_sent"):
                bits.append("possibly_sent=true")
            line = f"{i}. [{tool}] ✓ " + " ".join(bits)
            if rec.get("obs"):
                total, returned = rec["obs"]
                line += (
                    f"  （{returned}/{total} 行；"
                    "元素索引已按本次观察更新，后续步骤必须用新索引）"
                )
        else:
            code = rec.get("error") or "INTERNAL"
            msg = _clip(rec.get("message") or "", 300)
            line = f"{i}. [{tool}] ✗ ERROR[{code}]" + (f": {msg}" if msg else "")
            line += f" {int(rec.get('settle_ms') or 0)}ms"
            if rec.get("possibly_sent"):
                line += " possibly_sent=true"
        out.append(line)
    return out


def _fail(code: str, message: str, started: float, hint: str = "") -> action_layer.ActionResult:
    elapsed = _elapsed(started)
    detail = {
        "records": [], "stdout": "", "elapsed_ms": elapsed, "step": 0, "executed": 0,
        "dry_run": False, "precheck_failed": False,
    }
    if hint:
        detail["hint"] = hint
    return action_layer.ActionResult(
        ok=False, error=code, message=message, possibly_sent=False,
        settle_ms=elapsed, detail=detail,
    )


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _first_line(text: str) -> str:
    s = str(text or "").strip().splitlines()
    return _clip(s[0] if s else "", 300)


def _clip(text, limit: int) -> str:
    s = str(text or "")
    raw = s.encode("utf-8")
    if len(raw) <= limit:
        return s
    return raw[:limit].decode("utf-8", "ignore") + "…"
