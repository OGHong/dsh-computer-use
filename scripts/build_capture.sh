#!/bin/sh
# 编译观察窗的 SCK 抓帧 helper → bin/cua_capture
#
# 为什么要编译产物而不是脚本跑：SCK 的 delegate 回调链要主 runloop 喂，Swift 脚本
# （xcrun swift）虽然能跑，但每次启动都要过一遍 JIT/类型检查，且没法随插件分发。
#
# 没有 Swift 工具链的机器：跳过本脚本即可，Host 半区发现 bin/cua_capture 不存在就
# 自动退回 `screencapture` 轮询那条 ~2.5fps 的路（功能不缺，只是不流畅）。
#
# 部署目标与架构必须显式钉死：swiftc 默认按**当前这台机器**的系统和架构编，实测
# 产物是 minos=27.2 + 单 arm64 —— 那种二进制换台 Mac（Intel，或任何旧一点的系统）
# 直接 dyld 报错启动不了。所以这里出 arm64 + x86_64 的通用二进制，最低 macOS 13。
set -e
cd "$(dirname "$0")/.."
mkdir -p bin
if ! command -v xcrun >/dev/null 2>&1; then
  echo "找不到 xcrun（需要 Xcode Command Line Tools）；跳过构建，观察窗会退回 screencapture 模式" >&2
  exit 0
fi
TARGETS="arm64-apple-macos13 x86_64-apple-macos13"
BUILT=""
for t in $TARGETS; do
  if xcrun swiftc -O -target "$t" -o "bin/.cua_capture.$t" capture/cua_capture.swift 2>/dev/null; then
    BUILT="$BUILT bin/.cua_capture.$t"
  else
    echo "  · $t 编不出来（该架构的 SDK 没装），跳过" >&2
  fi
done
[ -n "$BUILT" ] || { echo "一个架构都没编出来，保留 screencapture 回落" >&2; exit 0; }
# shellcheck disable=SC2086
if [ "$(echo $BUILT | wc -w)" -gt 1 ]; then
  lipo -create $BUILT -output bin/cua_capture
  rm -f $BUILT
else
  mv $BUILT bin/cua_capture
fi
# 未签名的本地二进制：ad-hoc 签名可以免掉一部分 Gatekeeper 拦截（分发仍需正式签名 + 公证）
codesign --force --sign - bin/cua_capture 2>/dev/null || true
ls -l bin/cua_capture
