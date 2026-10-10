#!/bin/sh
# server/*.py 的静态检查。pyflakes 专抓「函数删了、调用点还留着」这类问题 ——
# 观察窗、错误分支这些路径平时没人手动点，一跑就 NameError 等于直接废掉一个工具。
# 它是开发期工具，不是运行依赖：没装就跳过，别让 check 变成必须 pip install。
# 但下面的两条「曾经真炸过」的守则检查不依赖 pyflakes，必须无条件跑。
cd "$(dirname "$0")/.." || exit 1
PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python

"$PY" - <<'EOF'
import pathlib, re, sys

problems = []

# 借用引用必须 retain 后再交给会 CFRelease 的函数：`CFArrayGetValueAtIndex` 返回的是
# 数组持有的引用，多减一次计数会在之后任意一次释放时炸成 SIGSEGV/SIGTRAP
# （真实崩过 4 次，栈顶都是 __RELEASE_OBJECTS_IN_THE_ARRAY__）。
src = pathlib.Path("server/ax_bridge.py").read_text(encoding="utf-8")
bad = [ln.strip() for ln in src.splitlines() if "_cf_to_py(" in ln and "CFArrayGetValueAtIndex(" in ln]
if bad:
    problems.append("有地方把借用的数组元素直接交给会 release 的函数：" + bad[0][:110])

# 滚动单位必须是 kCGScrollEventUnitLine=1（Pixel=0 / Line=1 / Page=2）：写成 2 时
# Chromium 还认、原生 AppKit 完全不滚 —— 实测访达滚 30 格图标纹丝不动。
cg = pathlib.Path("server/cg_input.py").read_text(encoding="utf-8")
if not re.search(r"^_UNIT_LINE = 1$", cg, re.M):
    problems.append("滚动单位不是 Line=1，原生应用里 scroll 会失效")

if problems:
    for p in problems:
        print("✗ " + p)
    sys.exit(1)
print("✓ CF 借用引用与滚动单位两条守则都没回归")
EOF
[ $? -eq 0 ] || exit 1

if "$PY" -m pyflakes --version >/dev/null 2>&1; then
	"$PY" -m pyflakes server/*.py || exit 1
	echo "✓ pyflakes 干净"
else
	echo "· 跳过 pyflakes（$PY 里没装，pip install pyflakes 可启用）"
fi
