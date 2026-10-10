#!/usr/bin/env python3
"""把工具服务器自己搬进一个 LSUIElement 的自建 app bundle 里再跑。

**为什么需要（macOS 27.2 实测，A/B 记录见 README「Dock 上不留 Python 火箭」一节）**

framework 版解释器（python.org / Homebrew 的 .pkg 版）主程序就在
`Resources/Python.app` 里，进程一旦和窗口服务器接上，LaunchServices 就按这个 bundle 把
进程 —— **以及它 spawn 的子进程**（osascript / screencapture / sips / lsappinfo…）——
登记成一个应用，Dock 上于是多出一枚 Python 火箭，并且一直留到进程退出。dsh 每次拉起
电脑控制都会攒一枚。

试过并否掉的替代方案（都实测过）：

  - `TransformProcessType(kProcessTransformToUIElementApplication)`：只改得动
    `lsappinfo` 里的 type，**拦不住 Dock 那颗图块**（macOS 27 上 plist 与运行时策略是两回事）；
  - 把解释器拷出 bundle 跑：Dock 立刻把图块记到最近的上级应用（dsh）头上，变成第二枚
    「DeepSeek Harness」图标 —— 换汤不换药；
  - 只替换激活应用那一条路径（osascript → AX）：截图走的 `screencapture` 一样会把图块带出来。

真正管用的是 **plist 级的 `LSUIElement`**：让进程住进一个 `LSUIElement=true` 的、
我们自己的 bundle，Dock 就再也不把它（和它的子进程）算成一个应用。实测同一条
「AX 树深读 → 子进程 osascript → focus_app」流程：住 framework 的 Python.app 时出图块，
住这个 bundle 时全程干净。

**做法**：启动最早期（进 MCP 循环之前）如果发现自己没住进这样的 bundle，就把解释器本体
拷进去、补上指向 `.venv` 的 `pyvenv.cfg` / `lib` 符号链接、ad-hoc 签名，然后 `os.execv`
换过去 —— stdio 管道（fd）原样保留、pid 不变，dsh 的 mcp-client 完全无感。

**失败即放弃**：任何一步出错都返回 False、继续用原解释器跑。最坏表现是 Dock 上多一枚
图章，绝不是「工具起不来」。要整条关掉：`DSH_CUA_NO_REEXEC=1`。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

BUNDLE_NAME = "DshComputerUse.app"
BUNDLE_ID = "local.dsh.computer-use"
BUNDLE_EXE = "Python"

MARKER_ENV = "DSH_CUA_REEXEC"       # 已经换过一轮：防止 exec 失败后无限打转
OFF_ENV = "DSH_CUA_NO_REEXEC"       # 用户主动关掉（排障用）

_INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>CFBundleExecutable</key><string>{exe}</string>
\t<key>CFBundleIdentifier</key><string>{bid}</string>
\t<key>CFBundleName</key><string>dsh-computer-use</string>
\t<key>CFBundlePackageType</key><string>APPL</string>
\t<key>CFBundleShortVersionString</key><string>1.0</string>
\t<key>CFBundleVersion</key><string>1</string>
\t<key>LSUIElement</key><true/>
</dict>
</plist>
"""


def _plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _bundle_path() -> Path:
    return _plugin_root() / "runtime" / BUNDLE_NAME


def _bundle_python() -> Path:
    return _bundle_path() / "Contents" / "MacOS" / BUNDLE_EXE


def _source_python() -> Path | None:
    """要拷进 bundle 的解释器本体 —— 必须是**住在 .app 里的那一份**。

    坑（实测踩过，Dock 上照样出图块）：python.org 的 framework 里有两份不同的二进制 ——
    `Resources/Python.app/Contents/MacOS/Python`（151584 字节，Python.app 这个 GUI 应用的
    主机程序）和 `bin/python3.12`（152624 字节，CLI 用的那一份）。`sys._base_executable`
    指的是后者；把后者塞进自建 bundle、plist 写得好好的也**不管用**，Dock 依旧按
    Python.app 算图块。所以先找 framework 的 app 主机程序，找不到就干脆不换
    （返回 None = 保持现状，最坏也只是 Dock 上多一枚图章）。
    """
    base = Path(sys.base_prefix)
    cands = [
        base / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python",
        Path(os.path.realpath(sys.executable)),
        Path(os.path.realpath(getattr(sys, "_base_executable", "") or sys.executable)),
    ]
    for p in cands:
        try:
            if p.is_file() and ".app/Contents/MacOS/" in str(p):
                return p
        except OSError:
            continue
    return None


def _link_venv(contents: Path) -> None:
    """让 bundle 里的解释器继续用插件自己的 venv。

    框架版解释器靠 `pyvenv.cfg` 认 venv（找 `<exe 上级目录>/pyvenv.cfg`），所以把
    venv 的 `pyvenv.cfg` 与 `lib` **软链**进 `Contents/`：sys.prefix 变成 Contents，
    site-packages 顺着 `lib` 指回真 venv —— 一个包都不用重装。

    必须是符号链接而不是拷贝：bundle 里多出普通文件会让 `codesign` 拒绝签名
    （实测报 "In subcomponent: …/pyvenv.cfg"），签不上名的解释器一跑就被 SIGKILL。
    """
    venv = _plugin_root() / ".venv"
    if not (venv / "pyvenv.cfg").is_file():
        return
    for name in ("pyvenv.cfg", "lib"):
        src = venv / name
        dst = contents / name
        if not src.exists():
            continue
        rel = os.path.relpath(src, contents)          # 相对链接：插件目录整体搬走也还有效
        if dst.is_symlink() and os.readlink(dst) == rel:
            continue
        if dst.is_symlink() or dst.exists():
            dst.unlink()
        dst.symlink_to(rel)


def _sign(bundle: Path) -> None:
    """ad-hoc 签名。没有 codesign 就跳过（能不能跑交给下一步的真跑一次验证）。"""
    try:
        subprocess.run(
            ["/usr/bin/codesign", "--force", "--sign", "-", str(bundle)],
            check=True, capture_output=True, timeout=20,
        )
    except Exception:
        pass


def _stamp(source: Path) -> str:
    st = source.stat()
    return f"{st.st_size}:{st.st_mtime_ns}"


def _build(bundle: Path, source: Path) -> None:
    """重建 bundle。

    `shutil.copy`（不是 copy2）：元数据/xattr 一概不继承，出来的东西只取决于源码二进制。
    标记文件写在 **bundle 外面** —— bundle 里多一个文件就会让 codesign 的封条失效，
    下次启动的解释器会被系统直接 SIGKILL。
    """
    contents = bundle / "Contents"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    shutil.copy(source, _bundle_python())
    os.chmod(_bundle_python(), 0o755)
    plist = contents / "Info.plist"
    text = _INFO_PLIST.format(exe=BUNDLE_EXE, bid=BUNDLE_ID)
    if not plist.is_file() or plist.read_text(encoding="utf-8") != text:
        plist.write_text(text, encoding="utf-8")
    _link_venv(contents)
    _sign(bundle)
    if not _runs(_bundle_python()):
        # 签不上名 / 架构不合的解释器是「一 exec 就被 SIGKILL」，绝不能把服务器换上去。
        raise RuntimeError("自建 bundle 的解释器跑不起来")
    _stamp_file().write_text(_stamp(source), encoding="utf-8")


def _stamp_file() -> Path:
    return _plugin_root() / "runtime" / ".DshComputerUse.source"


def _runs(target: Path) -> bool:
    """真跑一次再交付 —— 签名失败的二进制是「一 exec 就被 SIGKILL」，跑一次最省事。"""
    try:
        r = subprocess.run([str(target), "-c", "import sys"], capture_output=True, timeout=20)
    except Exception:
        return False
    return r.returncode == 0


def ensure_ui_element_reexec(argv: list[str] | None = None) -> bool:
    """需要的话把自己换进自建 bundle 里跑。返回是否真的 exec 了（正常返回 False = 不用换）。"""
    if os.environ.get(OFF_ENV) or os.environ.get(MARKER_ENV) == "1":
        return False
    source = _source_python()
    if source is None:
        return False
    target = _bundle_python()
    try:
        if target.is_file() and os.path.realpath(target) == os.path.realpath(sys.executable):
            return False                                   # 已经在自己的 bundle 里
        fresh = False
        if target.is_file():
            try:
                fresh = _stamp_file().read_text(encoding="utf-8") == _stamp(source)
            except OSError:
                fresh = False
        if not fresh:
            _build(_bundle_path(), source)                 # 内含「跑一次」验证
    except Exception as e:
        print(f"computer-use: 自建 UIElement bundle 失败：{e}（继续用原解释器，Dock 上可能多一枚图章）",
              file=sys.stderr)
        return False
    args = [str(target), *(argv if argv is not None else sys.argv)]
    env = dict(os.environ)
    env[MARKER_ENV] = "1"
    # venv 的启动器会把它塞进来；留着会让新进程的 sys.executable 指回 venv，孩子（观察窗）
    # 就又跑在 framework 的 Python.app 里了。
    env.pop("__PYVENV_LAUNCHER__", None)
    try:
        os.execve(str(target), args, env)
    except Exception as e:
        print(f"computer-use: 换到 UIElement 解释器失败：{e}", file=sys.stderr)
        return False
    return True
