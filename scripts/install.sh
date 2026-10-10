#!/bin/sh
# 换一台 Mac 之后的一条命令：把这台机器上"每台各做一次"的事都做完。
#
#   sh scripts/install.sh
#
# 做五件事（都可重复执行，已经做过的会跳过）：
#   1. 建 .venv 并装 server/requirements.txt（浮窗要的 pyobjc 就在里面）；
#   2. 构建 Host 半区（src/index.ts → lib/index.js；lib/ 是产物，不进版本库）；
#   3. 把 cordis.patch.yml 里 command/args 的绝对路径改成本机插件目录
#      （这是插件里唯一写死机器路径的地方，换电脑必须改，脚本替你改）；
#   4. 没有 bin/cua_capture 就试着编一个（要 Xcode Command Line Tools；
#      编不出来也不阻塞：Host 半区会自动退回 screencapture，~2.5fps）；
#   5. 打印还差的手工步骤（profile 声明 + 两项系统权限）。
#
# 不需要做的事（脚本顺带说明，免得你以为要拷）：runtime/DshComputerUse.app 是
# 首次启动时按本机 Python 自建的；/tmp/dsh-computer-use 里的状态是临时的。
set -eu

DIR=$(cd "$(dirname "$0")/.." && pwd)
printf '插件目录: %s\n\n' "$DIR"

# ---- 1. venv + 依赖 --------------------------------------------------------
PY=""
for cand in python3.12 python3.13 python3.11 python3.10 python3; do
	command -v "$cand" >/dev/null 2>&1 || continue
	# 代码里用了 `int | None` 这类 3.10+ 的注解写法（server.py 没走 postponed 求值），
	# 3.9 及更早会在 import 时就炸 —— 直接跳过，别挑中它。
	"$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null || continue
	PY=$cand
	break
done
if [ -z "$PY" ]; then
	echo "✗ 找不到 Python 3.10+（实测 3.12 最稳）。装一个再来：https://www.python.org/downloads/macos/" >&2
	echo "  提示：python.org 的 framework 版最好 —— Dock 上那枚 Python 图章的自建 bundle 就是从它拷解释器的。" >&2
	exit 1
fi
printf '1) Python: %s (%s)\n' "$PY" "$("$PY" -V 2>&1)"
if [ ! -x "$DIR/.venv/bin/python" ]; then
	"$PY" -m venv "$DIR/.venv"
	echo "   建好 .venv"
else
	echo "   .venv 已存在，跳过创建"
fi
"$DIR/.venv/bin/python" -m pip install -q --upgrade pip >/dev/null 2>&1 || true
if "$DIR/.venv/bin/python" -m pip install -q -r "$DIR/server/requirements.txt"; then
	echo "   依赖已就绪（含预览浮窗要的 pyobjc）"
else
	echo "   ⚠ 依赖安装失败：工具仍可用（纯标准库），但预览浮窗起不来" >&2
fi
if ! "$DIR/.venv/bin/python" -c 'import objc' >/dev/null 2>&1; then
	echo "   ⚠ venv 里没有 pyobjc → 预览浮窗（唯一的预览面）不会出现" >&2
fi

# ---- 2. Host 半区（TypeScript → lib/index.js）-------------------------------
printf '\n2) 构建 Host 半区（src/index.ts → lib/index.js）\n'
if command -v npm >/dev/null 2>&1; then
	if (cd "$DIR" && npm install --omit=peer --no-audit --no-fund >/dev/null 2>&1 && npm run build >/dev/null 2>&1); then
		echo "   构建完成（先删掉 lib/index.js 再跑本脚本可强制重建）"
	else
		echo "   ⚠ 构建失败：profile 会加载不到 lib/index.js（观测窗的推流路由就没了）" >&2
	fi
elif [ -f "$DIR/lib/index.js" ]; then
	echo "   没有 npm，沿用已有的 lib/index.js（改过 src/ 就必须自己重建）"
else
	echo "   ⚠ 没有 npm 也没有 lib/index.js → 先用 Node 22+ 跑一次 npm run build" >&2
fi

# ---- 3. patch 里的绝对路径 -------------------------------------------------
printf '\n3) 把 cordis.patch.yml 的路径指向 %s\n' "$DIR"
"$DIR/.venv/bin/python" - "$DIR" <<'PY'
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
patch = root / "cordis.patch.yml"
text = patch.read_text(encoding="utf-8")
# command: <任意路径>/.venv/bin/python  与  - <任意路径>/server/server.py
new = re.sub(r"^(?P<indent>\s*command:\s*).*?/\.venv/bin/python\s*$",
             lambda m: f"{m.group('indent')}{root}/.venv/bin/python", text, flags=re.M)
new = re.sub(r"^(?P<indent>\s*-\s*).*?/server/server\.py\s*$",
             lambda m: f"{m.group('indent')}{root}/server/server.py", new, flags=re.M)
if new == text:
    print("   路径已经是本机目录（或 patch 结构变了，没动它）")
else:
    patch.write_text(new, encoding="utf-8")
    print("   已改写 command / args")
for line in new.splitlines():
    if "/.venv/bin/python" in line or "/server/server.py" in line:
        print("   " + line.strip())
PY

# ---- 4. 抓屏 helper --------------------------------------------------------
printf '\n4) 抓屏 helper\n'
if [ -x "$DIR/bin/cua_capture" ]; then
	printf '   已有 bin/cua_capture（%s，最低 macOS %s）' \
		"$(lipo -archs "$DIR/bin/cua_capture" 2>/dev/null || echo '?')" \
		"$(otool -l "$DIR/bin/cua_capture" 2>/dev/null | awk '/minos/{print $2; exit}')"
	echo " —— 通用二进制，Intel / Apple Silicon 都能直接用"
elif command -v xcrun >/dev/null 2>&1; then
	echo "   没有预编译产物，尝试用 Xcode CLT 编一个…"
	if sh "$DIR/scripts/build_capture.sh" >/dev/null 2>&1; then
		echo "   编好了"
	else
		echo "   ⚠ 编译失败 → 会自动退回 screencapture（~2.5fps），功能不受影响" >&2
	fi
else
	echo "   没装 Xcode CLT → 自动退回 screencapture（~2.5fps），功能不受影响"
fi

# ---- 5. 还差的手工步骤 -----------------------------------------------------
cat <<'TXT'

5) 还差两步手工（都在 dsh 里点几下）：
   a. 把插件加进 profile：插件页安装本包（或在 profile 的 package.json 里
      声明依赖 + 加进 dsh.profile.bundles），然后重启 DeepSeek Harness。
   b. 系统设置 → 隐私与安全性 → 给 DeepSeek Harness 打开
      「辅助功能」（点击/键盘）和「屏幕录制」（截屏），否则截图黑屏、点击无效。

自检：npm run verify（typecheck + 构建 + check）；check 里是 Host 路由冒烟 +
退休卡片冒烟 + pyflakes + MCP 线格式对拍（含取消）。这些开发脚本不在发布包里，
要在源码树里跑。
预览浮窗默认在第一次操作电脑时自动出现；DSH_CUA_NO_PANEL=1 可关掉它。
TXT
