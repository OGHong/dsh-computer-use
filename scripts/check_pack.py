#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发版阻塞项 D7：打包完整性。

两道检查，都必须从 `npm pack` 的产物出发，不许只信源码树：

1. `npm pack --dry-run --json`：仓库 `server/*.py` 的每一个都必须出现在 files 白名单里；
2. 真打包 → 解压 → 从 `package/server/` 起服务器 → `tools/list` 必须是 29 个工具，
   且 `run_code` 的 `inputSchema.required == ["code"]`。

第 1 条防的是「下一个新模块又忘了写进 package.json files」；第 2 条防的是
「源码树能跑、装出来的包起不来」（server.py 的 import 链在包外断掉）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
EXPECTED_TOOLS = 29


def pack_json(*, dest: Path | None = None, dry_run: bool = False) -> dict:
    cmd = ["npm", "pack", "--json", "--ignore-scripts"]
    if dry_run:
        cmd.append("--dry-run")
    if dest is not None:
        cmd += ["--pack-destination", str(dest)]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"npm pack 失败（rc={proc.returncode}）：{proc.stderr[-2000:]}")
    try:
        return json.loads(proc.stdout)[0]
    except (ValueError, IndexError):
        raise SystemExit(f"npm pack 输出不是预期的 JSON：{proc.stdout[:1500]} {proc.stderr[:1500]}")


def main() -> int:
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(("✓ " if ok else "✗ ") + label + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(label)

    # 1) files 白名单必须覆盖 server/ 下每一个 .py
    repo_py = sorted(p.relative_to(ROOT).as_posix() for p in SERVER.glob("*.py"))
    dry = pack_json(dry_run=True)
    packed = {f["path"] for f in dry.get("files", [])}
    missing = [p for p in repo_py if p not in packed]
    check(
        "打包完整性：server/ 下每个 .py 都进包（新的模块不许漏）",
        not missing,
        f"缺失: {missing}" if missing else f"{len(repo_py)} 个 .py 全在包里",
    )

    # 2) 真打包 → 解压 → 从包里起服务器
    with tempfile.TemporaryDirectory(prefix="dsh-cua-pack.") as raw_tmp:
        tmp = Path(raw_tmp)
        info = pack_json(dest=tmp)
        tgz = tmp / info["filename"]
        with tarfile.open(tgz, "r:gz") as tar:
            try:
                tar.extractall(tmp, filter="data")
            except TypeError:  # Python 3.10 没有 filter 参数
                tar.extractall(tmp)

        pkg = tmp / "package"
        server_dir = pkg / "server"
        listing = sorted(p.name for p in server_dir.iterdir())
        print("package/server/ 完整列表: " + " ".join(listing))
        check("解压包里有 server/code_exec.py", (server_dir / "code_exec.py").is_file(),
              str(server_dir / "code_exec.py"))

        state = tmp / "state"
        state.mkdir()
        env = {
            **os.environ,
            "PYTHONPATH": str(server_dir),
            "DSH_CUA_DIR": str(state),
            "DSH_CUA_NO_REEXEC": "1",
            "DSH_CUA_NO_PANEL": "1",
            "DSH_CUA_STREAM": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        proc = subprocess.Popen(
            [sys.executable, str(server_dir / "server.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True, bufsize=1,
        )

        def send(msg: dict) -> None:
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()

        def recv(want: int, timeout: float = 60.0) -> dict:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                line = proc.stdout.readline()
                if not line:
                    raise RuntimeError("解压包服务器提前退出：" + (proc.stderr.read() or "")[-2500:])
                msg = json.loads(line)
                if msg.get("id") == want:
                    return msg
            raise TimeoutError(f"等 id={want} 超时")

        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                             "clientInfo": {"name": "check-pack", "version": "1"}}})
            recv(1)
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            tools = recv(2)["result"]["tools"]
            run_tool = next((t for t in tools if t["name"] == "run_code"), None)
            check("解压包起的服务器 tools/list = 29", len(tools) == EXPECTED_TOOLS, str(len(tools)))
            check("解压包起的服务器清单里有 run_code", run_tool is not None)
            if run_tool is not None:
                required = run_tool["inputSchema"].get("required")
                print("run_code: " + json.dumps(
                    {"name": run_tool["name"], "required": required}, ensure_ascii=False
                ))
                check("解压包里的 run_code.inputSchema.required = [code]", required == ["code"], str(required))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    print("全部通过 ✓" if not failures else f"失败 {len(failures)} 项: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
