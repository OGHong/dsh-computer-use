#!/bin/sh
# 换一台 Mac 之后的一条命令：把这台机器上"每台各做一次"的事都做完。
#
#   sh scripts/install.sh                 # 默认接进 desktop profile
#   sh scripts/install.sh --profile=web   # 指定 profile
#   sh scripts/install.sh --dry-run       # 只打印要做什么，一个字节都不改
#
# 做六件事（都可重复执行，已经做过的会跳过）：
#   1. 建 .venv 并装 server/requirements.txt（浮窗要的 pyobjc 就在里面）；
#   2. 构建 Host 半区（src/index.ts → lib/index.js；lib/ 是产物，不进版本库）；
#   3. 把 cordis.patch.yml 里 command/args 的绝对路径改成本机插件目录
#      （这是插件里唯一写死机器路径的地方，换电脑必须改，脚本替你改）；
#   4. 没有 bin/cua_capture 就试着编一个（要 Xcode Command Line Tools；
#      编不出来也不阻塞：Host 半区会自动退回 screencapture，~2.5fps）；
#   5. 接进 dsh profile：package.json 里加 link: 依赖 + 加进 dsh.profile.bundles
#      + 建 node_modules 符号链接（改前先备份 package.json）；
#   6. 打开「系统设置 → 隐私与安全性」里那两项权限面板（--no-open-settings 可跳过）。
#
# 之后只剩两件谁也替不了你的事：重启 DeepSeek Harness（宿主启动时才扫描插件），
# 以及在系统设置里点开那两项开关（macOS 的 TCC 权限不允许程序自己给自己授权）。
#
# 不需要做的事（脚本顺带说明，免得你以为要拷）：runtime/DshComputerUse.app 是
# 首次启动时按本机 Python 自建的；/tmp/dsh-computer-use 里的状态是临时的。
set -eu

DRY_RUN=0
OPEN_SETTINGS=1
PROFILE_NAME=""
while [ $# -gt 0 ]; do
	case "$1" in
		--dry-run) DRY_RUN=1 ;;
		--no-open-settings) OPEN_SETTINGS=0 ;;
		--profile) PROFILE_NAME="${2:-}"; shift ;;
		--profile=*) PROFILE_NAME="${1#*=}" ;;
		-h|--help)
			sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
			exit 0 ;;
		*)
			echo "未知参数：$1（-h 看用法）" >&2
			exit 2 ;;
	esac
	shift
done

DIR=$(cd "$(dirname "$0")/.." && pwd)
printf '插件目录: %s\n' "$DIR"
[ "$DRY_RUN" = 1 ] && printf '模式: --dry-run（只打印，不改任何文件）\n'
echo

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
	if [ "$DRY_RUN" = 1 ]; then
		echo "   [dry-run] 会建 .venv 并装依赖"
	else
		"$PY" -m venv "$DIR/.venv"
		echo "   建好 .venv"
	fi
else
	echo "   .venv 已存在，跳过创建"
fi
if [ "$DRY_RUN" = 0 ]; then
	"$DIR/.venv/bin/python" -m pip install -q --upgrade pip >/dev/null 2>&1 || true
	if "$DIR/.venv/bin/python" -m pip install -q -r "$DIR/server/requirements.txt"; then
		echo "   依赖已就绪（含预览浮窗要的 pyobjc）"
	else
		echo "   ⚠ 依赖安装失败：工具仍可用（纯标准库），但预览浮窗起不来" >&2
	fi
	if ! "$DIR/.venv/bin/python" -c 'import objc' >/dev/null 2>&1; then
		echo "   ⚠ venv 里没有 pyobjc → 预览浮窗（唯一的预览面）不会出现" >&2
	fi
fi

# ---- 2. Host 半区（TypeScript → lib/index.js）-------------------------------
printf '\n2) 构建 Host 半区（src/index.ts → lib/index.js）\n'
if [ "$DRY_RUN" = 1 ]; then
	echo "   [dry-run] 会跑 npm install + npm run build"
elif command -v npm >/dev/null 2>&1; then
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
if [ "$DRY_RUN" = 1 ]; then
	echo "   [dry-run] 会把 command / args 改写成 $DIR/.venv/bin/python 与 $DIR/server/server.py"
else
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
fi

# ---- 4. 抓屏 helper --------------------------------------------------------
printf '\n4) 抓屏 helper\n'
if [ -x "$DIR/bin/cua_capture" ]; then
	printf '   已有 bin/cua_capture（%s，最低 macOS %s）' \
		"$(lipo -archs "$DIR/bin/cua_capture" 2>/dev/null || echo '?')" \
		"$(otool -l "$DIR/bin/cua_capture" 2>/dev/null | awk '/minos/{print $2; exit}')"
	echo " —— 通用二进制，Intel / Apple Silicon 都能直接用"
elif [ "$DRY_RUN" = 1 ]; then
	echo "   [dry-run] 会尝试编译 bin/cua_capture（失败则退回 screencapture）"
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

# ---- 5. 接进 dsh profile ---------------------------------------------------
printf '\n5) 接进 dsh profile\n'
DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
if [ -z "$PROFILE_NAME" ]; then
	# 优先挑已经装了本插件的 profile（多半就是你在用的那个），否则退回 desktop。
	for cand in "$DSH_HOME"/profiles/*/; do
		[ -f "$cand/package.json" ] || continue
		if grep -q '"dsh-computer-use"' "$cand/package.json" 2>/dev/null; then
			PROFILE_NAME=$(basename "$cand")
			break
		fi
	done
	[ -n "$PROFILE_NAME" ] || PROFILE_NAME=desktop
fi
PROFILE_DIR="$DSH_HOME/profiles/$PROFILE_NAME"
if [ ! -f "$PROFILE_DIR/package.json" ]; then
	echo "   ⚠ 没有 $PROFILE_DIR/package.json —— 跳过（用 --profile=<名字> 指定，或手动安装）" >&2
else
	echo "   profile: ${PROFILE_NAME}（${PROFILE_DIR}）"
	if [ "$DRY_RUN" = 1 ]; then
		echo "   [dry-run] 会在 package.json 里加："
		echo "             dependencies.dsh-computer-use = link:$DIR"
		echo "             dsh.profile.bundles += dsh-computer-use"
		echo "           并建符号链接 node_modules/dsh-computer-use → $DIR"
	else
		"$DIR/.venv/bin/python" - "$PROFILE_DIR" "$DIR" <<'PY'
import json
import os
import shutil
import sys
import time
from pathlib import Path

profile, plugin = Path(sys.argv[1]), Path(sys.argv[2])
pkg_path = profile / "package.json"
pkg = json.loads(pkg_path.read_text(encoding="utf-8"))

dep_value = f"link:{plugin}"
changed = []
if pkg.setdefault("dependencies", {}).get("dsh-computer-use") != dep_value:
    pkg["dependencies"]["dsh-computer-use"] = dep_value
    changed.append("dependencies")
bundles = pkg.setdefault("dsh", {}).setdefault("profile", {}).setdefault("bundles", [])
if "dsh-computer-use" not in bundles:
    bundles.append("dsh-computer-use")
    changed.append("dsh.profile.bundles")

if changed:
    backup = pkg_path.with_name(f"package.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(pkg_path, backup)
    pkg_path.write_text(json.dumps(pkg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"   已写入 package.json（{'、'.join(changed)}），备份在 {backup.name}")
else:
    print("   package.json 里已经有了，跳过")

link = profile / "node_modules" / "dsh-computer-use"
link.parent.mkdir(parents=True, exist_ok=True)
# npm 的 link: 装出来的是相对链接，这里按解析后的真实路径比较，免得把等价链接误判成不一致。
if link.is_symlink() and os.path.realpath(link) == os.path.realpath(plugin):
    print("   符号链接已就绪")
elif link.exists() and not link.is_symlink():
    print(f"   ⚠ {link} 已存在且不是符号链接，没动它 —— 请手动确认", file=sys.stderr)
else:
    if link.is_symlink():
        link.unlink()
    link.symlink_to(plugin)
    print(f"   已建符号链接 node_modules/dsh-computer-use → {plugin}")
PY
	fi
fi

# ---- 6. 系统权限面板 -------------------------------------------------------
printf '\n6) 系统权限\n'
if [ "$DRY_RUN" = 1 ]; then
	echo "   [dry-run] 会打开「辅助功能」与「屏幕录制」两个设置面板"
elif [ "$OPEN_SETTINGS" = 1 ]; then
	echo "   正在打开「系统设置 → 隐私与安全性 → 辅助功能」…"
	open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility" >/dev/null 2>&1 || true
	echo "   屏幕录制面板："
	echo "     open 'x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture'"
else
	echo "   已跳过（--no-open-settings）。手动打开："
	echo "     open 'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility'"
	echo "     open 'x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture'"
fi

cat <<'TXT'

还差两件谁也替不了的事：
   a. 重启 DeepSeek Harness —— 宿主启动时才扫描插件（客户端半区同理）。
   b. 在刚打开的「系统设置」里给 DeepSeek Harness 打开两项开关：
      「辅助功能」（点击 / 键盘）和「屏幕录制」（截屏）。
      这是 macOS 的 TCC 权限，任何程序都不能自己给自己授权，只能你点。
      没授权时的表现：截图黑屏、点击无效 —— screen_info 会如实报告。

自检：npm run verify（typecheck + 构建 + check）；check 里是 Host 路由冒烟 +
退休卡片冒烟 + pyflakes + MCP 线格式对拍（含取消）。这些开发脚本不在发布包里，
要在源码树里跑。
预览浮窗默认在第一次操作电脑时自动出现；DSH_CUA_NO_PANEL=1 可关掉它。
TXT
