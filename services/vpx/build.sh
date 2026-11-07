#!/bin/bash
# libvpx（Google VP8 / VP9 参考实现，BSD）静态库：只含 VP8 与 VP9 解码器（S2Arm 不带 FFmpeg）。VP8 给 Nvdec 的 VP8 用（暗黑2 片头，垫片 s2a_vp8.c）；
#   VP9 给 Switch 2 直连档接管的 nn::codec 视频解码器用（graphics/nvn/gfx/nvn/nvn2_codec.c，垫片 s2a_vp9.c；火纹 Fortune's Weave 片头 VP9 WebM）。两路同一份 libvpx。
# 用法：bash services/vpx/build.sh [osx-arm64|osx-x64|ios-arm64] → 最后一行输出 libs2a_vpx.a 路径（libvpx + C 垫片 s2a_vp8.c / s2a_vp9.c）。按版本 / 垫片哈希缓存在 ~/.s2arm/run/s2arm/cache/vpx，命中秒过。
# 源码按固定版本 + sha256 取，不进仓库；源码包按 sha256 缓存在 ${S2A_BUILD_CACHE:-~/.cache/s2arm-build}/sources/（android/tools/fetch_source.sh）。
set -euo pipefail
RID=${1:-osx-arm64}
VER=1.17.0; SHA=1020f184046187baa2985dbde38e0691f49c44088bca7a1842b0236c6081dc0a
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
source "$ROOT/android/tools/fetch_source.sh"   # 源码包按 sha256 进本机共享缓存（S2A_BUILD_CACHE/sources），命中不联网；下载按进展判挂死
TCE=$ROOT/tools/toolchain.env; MMAC=$(sed -n 's/^MACOSX_DEPLOYMENT_TARGET=//p' "$TCE"); MIOS=$(sed -n 's/^IPHONEOS_DEPLOYMENT_TARGET=//p' "$TCE")   # 产物下限只取统一工具链那两行
[ -n "$MMAC" ] && [ -n "$MIOS" ] || { echo "🔴 $(basename "$(dirname "$0")")：$TCE 里没有部署目标" >&2; exit 1; }
# 构建键：版本 + 本脚本与系统下限 + 垫片，只按内容算、不含路径（发版机与玩家机引擎路径不同；缓存目录、预编库都按它认）；--key 只打印它（发版打包 bundle_engine.py 记进 build.txt）
H1=$( { cat "$0"; echo "$MMAC $MIOS"; } | shasum -a 256 | cut -c1-12); K=$(cat "$ROOT/services/vpx/s2a_vp8.c" "$ROOT/services/vpx/s2a_vp9.c" "$0" | shasum -a 256 | cut -c1-12); KEY=$VER-$H1-$K
[ "${2:-}" = --key ] && { echo "$KEY"; exit 0; }
# 出包作业（S2A_PREBUILT_TOOLS）用 App 里的预编库，不构建（services/prebuilt_lib.sh）
source "$ROOT/services/prebuilt_lib.sh"; s2a_prebuilt_lib vpx "$RID" "$KEY" libs2a_vpx.a
C=${S2A_VPX_CACHE:-${S2A_RUN_ROOT:-$HOME/.s2arm/run}/s2arm/cache/vpx}; mkdir -p "$C"   # 整机级缓存（10-20 出包提速）：键 = 本脚本内容（含版本与 sha256）+ 平台，各工作树共用
OUT=$C/$RID-$VER-$H1
until mkdir "$C/.lock" 2> /dev/null; do p=$(cat "$C/.lock/pid" 2> /dev/null || true); [ -n "$p" ] && ! kill -0 "$p" 2> /dev/null && rm -rf "$C/.lock"; sleep 0.5; done
echo $$ > "$C/.lock/pid"; trap 'rm -rf "$C/.lock"' EXIT   # 几棵树同时编同一键时排队（.tmp 名字不撞）
ARCH=arm64
case $RID in
  osx-arm64) TARGET=arm64-darwin20-gcc; MIN=-mmacosx-version-min=$MMAC; SDK=macosx ;;
  osx-x64) TARGET=generic-gnu; ARCH=x86_64; MIN=-mmacosx-version-min=$MMAC; SDK=macosx ;;
  ios-arm64) TARGET=arm64-darwin-gcc; MIN=-miphoneos-version-min=$MIOS; SDK=iphoneos ;;
  *) echo "🔴 vpx：不认识的平台 $RID" >&2; exit 1 ;;
esac
# 本机缓存跟编译器走（审计 #1534 P1-107）：构建键不含编译器（发版机与玩家机的预编库按它认，不能带本机版本），本机缓存目录名另带 clang 版本行的哈希，toolchain.txt 记原文备查
TCV=$(xcrun --sdk $SDK clang --version 2> /dev/null | sed -n 1p) || TCV=
[ -n "$TCV" ] || { echo "🔴 vpx：取不到 $SDK clang 的版本行（xcrun 失败），不出库" >&2; exit 1; }
# 编译器进本机缓存目录名（11-05 服务层单测偶发 rc=134）：原先同一目录记 toolchain.txt、版本行不同就 rm -rf 整个目录重编；本机两套 Xcode
#   （门禁经 tools/toolchain.sh 用 S2A_DEVELOPER_DIR 那套、直接跑用 xcode-select 那套）版本行不同，轮流互删，把别的树单测正要 dlopen 的动态库删掉。
#   按编译器分目录后已出好的目录不再被改写；构建键（--key / 预编库）仍不含编译器
OUT=$OUT-$(printf '%s' "$TCV" | shasum -a 256 | cut -c1-8)
if [ ! -s "$OUT/libvpx.a" ]; then
  TGZ=$(s2a_source_fetch "libvpx-$VER.tar.gz" "https://github.com/webmproject/libvpx/archive/refs/tags/v$VER.tar.gz" "$SHA") || { echo "🔴 vpx：取不到源码包" >&2; exit 1; }
  W=$OUT.tmp; rm -rf "$W"; mkdir -p "$W/src" "$W/obj"
  tar xzf "$TGZ" -C "$W/src" --strip-components 1
  # 缓存在带空格目录（启动器：~/Library/Application Support）时 configure / make 拆路径：走无空格别名（tools/nospace.sh）
  source "$ROOT/tools/nospace.sh"; WN=$(s2a_nospace "$W")
  ( cd "$WN/obj" &&
    # Intel 只用于出包机上的服务测试；通用 C 后端不要求额外安装汇编器。
    if [ "$RID" = osx-x64 ]; then export CC="xcrun --sdk macosx clang -arch x86_64" CXX="xcrun --sdk macosx clang++ -arch x86_64"; fi
    CFLAGS="$MIN -ffile-prefix-map=$WN/src=libvpx" CXXFLAGS="$MIN -ffile-prefix-map=$WN/src=libvpx" "$WN/src/configure" --target=$TARGET \
      --disable-vp8-encoder --enable-vp8-decoder --disable-vp9-encoder --enable-vp9-decoder --disable-examples --disable-tools --disable-docs \
      --disable-unit-tests --disable-webm-io --disable-libyuv --enable-pic --enable-static --disable-shared --size-limit=8192x8192 \
      > "$W/configure.log" 2>&1 && make -j8 > "$W/make.log" 2>&1 ) || { echo "🔴 vpx：$RID 构建失败（$W/*.log）" >&2; tail -5 "$W"/*.log >&2; exit 1; }
  mkdir -p "$W/out/include"; cp "$W/obj/libvpx.a" "$W/out/"; cp -R "$W/src/vpx" "$W/out/include/"
  printf '%s\n' "$TCV" > "$W/out/toolchain.txt"
  rm -rf "$OUT"; mv "$W/out" "$OUT"; rm -rf "$W"
fi
# 垫片与 libvpx 合成一个静态库（DirectPInvoke s2a_vpx 链它）
L=$OUT/s2a-$K/libs2a_vpx.a
if [ ! -s "$L" ]; then
  mkdir -p "$OUT/s2a-$K.tmp"
  for c in vp8 vp9; do xcrun --sdk $SDK clang -arch "$ARCH" $MIN -O2 -fPIC -I"$OUT/include" -c "$ROOT/services/vpx/s2a_$c.c" -o "$OUT/s2a-$K.tmp/s2a_$c.o"; done
  libtool -static -o "$OUT/s2a-$K.tmp/libs2a_vpx.a" "$OUT/libvpx.a" "$OUT/s2a-$K.tmp/s2a_vp8.o" "$OUT/s2a-$K.tmp/s2a_vp9.o" 2>/dev/null
  # Mac 另出一份动态库：服务层单测（JIT 跑，不静态链）经它调同一套代码
  [[ $RID = osx-* ]] && xcrun clang -arch "$ARCH" $MIN -dynamiclib -o "$OUT/s2a-$K.tmp/libs2a_vpx.dylib" "$OUT/s2a-$K.tmp/s2a_vp8.o" "$OUT/s2a-$K.tmp/s2a_vp9.o" "$OUT/libvpx.a"
  rm -rf "$OUT/s2a-$K"; mv "$OUT/s2a-$K.tmp" "$OUT/s2a-$K"
fi
touch "$OUT"   # 取用即更新目录时间：tools/gc.sh 每平台留 2 份、30 分钟内动过的不删，在用的这份不被别的键挤掉
echo "$L"
