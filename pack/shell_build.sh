#!/bin/bash
# 出预编壳（阶段 0，11-04 Windows 出包线）：pack/shell_build.sh <壳目录>
#   与游戏无关的 iOS 壳：make ios（主程序不含任何游戏镜像，不要任何游戏产物）→ 拷渲染器内部辅助库 gal_helpers（含 m3/）→ pack/ship_ext.py shell
#   （剥 / 核按款文件、写随包表清单、主程序 ad-hoc 签名带 pack/app.entitlements、出壳门、封壳清单）。
#   输出 <壳目录>/root/（壳本体）与 <壳目录>/manifest.json（壳清单，壳编号 = 壳输入表 pack/stage_inputs/shell.tsv 的指纹 + 直连链号 + iOS 最低版本）；布局同发版缓存 pack/shell/cache.py。
#   壳输入闭包（11-04 维护者）：make 前把仓库文件访问时间设旧、出壳后核「这次读到的仓库文件都在输入表里」，有表外的报红并删掉刚出的壳（pack/shell/closure.py）。
#   S2A_SHELL_CLOSURE_SUGGEST=<文件>：只把这次读到的收成建议表写进该文件、不判红（首次生成 / 大改后重生成输入表用，要冷出：缓存都指空目录）
#   用法：发版（启动器随版，gate-host 自动编）与内部测试（按提交本地编）同一个入口；出包时 S2A_SHELL=<壳目录>（pack/ipa.sh）或 ship_ext.py build --shell <壳目录>。
#   只在 Mac 上跑（要 Xcode 编宿主、codesign 做 ad-hoc 签名）。构建相关目录有未提交改动时不出（壳编号按提交号算，对不上源码）。
set -u
OUT=${1:?壳目录}
H=$(cd "$(dirname "$0")" && pwd); R=$(cd "$H/.." && pwd)
[ -e "$OUT" ] && { echo "【红】$OUT 已存在，不覆盖"; exit 1; }
# 输入表「入」的文件有未提交改动就不出（壳编号按 HEAD 上的对象号算）；这一步必须在设旧访问时间之前：设旧会改文件状态时间，之后的 git status 会把全部文件重读一遍
IN=$(LC_ALL=C awk -F'\t' '$1 == "入" { print $2 }' "$R/pack/stage_inputs/shell.tsv" 2> /dev/null) && [ -n "$IN" ] || { echo "【红】读不到壳输入表 pack/stage_inputs/shell.tsv"; exit 1; }
# shellcheck disable=SC2086
[ -z "$(git -C "$R" status --porcelain -- $IN pack/stage_inputs/shell.tsv)" ] || { echo "【红】壳输入表里的文件有未提交改动，壳编号对不上源码，不出"; exit 1; }
source "$R/tools/toolchain.sh" || exit 2
# 壳编号里记了 iOS SDK 主版本（tools/toolchain.env，审计 P1-124）：实际 SDK 对不上就不出，免得同一个编号对应两种链接 SDK
SDKV=$(xcrun --sdk iphoneos --show-sdk-version 2> /dev/null) || { echo "【红】读不出 iOS SDK 版本（xcrun --sdk iphoneos）"; exit 1; }
SDKM=$(awk -F= '$1 == "IPHONEOS_SDK_MAJOR" { print $2 }' "$R/tools/toolchain.env")
[ "${SDKV%%.*}" = "$SDKM" ] || { echo "【红】这台机器的 iOS SDK 是 ${SDKV}，tools/toolchain.env 钉的主版本是 ${SDKM}：换 Xcode 大版本要同时改 IPHONEOS_SDK_MAJOR"; exit 1; }
source "$R/tools/cores.sh"; s2a_cores || exit 1
# 引擎树里的 iOS 编译中间产物（渲染器 / 直连层静态库、gal_helpers 含 m3、宿主目标文件缓存）按修改时间增量，不认编译器（审计 #1534 P1-107）：
#   工具链版本行与上次出壳记的不同就整份清掉重编（tools/toolchain.sh s2a_tc_sweep），只在真换了 Xcode / Metal 组件时发生
s2a_tc_sweep "$R/build/.ios_toolchain" "$R/build/gal/ios" "$R/build/nvn/ios" "$R/build/ios_objcache" || { echo "【红】清不掉旧工具链编出的 iOS 中间产物（$R/build）"; exit 1; }
# 引擎在带空格的目录（启动器：~/Library/Application Support）时 make 按空格拆路径（10-25 启动器实测 Hades）：交给 make 的路径与宿主库缓存一律用无空格别名（tools/nospace.sh）
source "$R/tools/nospace.sh"; RN=$(s2a_nospace "$R") || { echo "【红】建不了无空格别名（${R}）"; exit 1; }
for v in S2A_CS_CACHE S2A_VPX_CACHE S2A_OPUS_CACHE S2A_DAV1D_CACHE; do [ -n "${!v:-}" ] || continue; mkdir -p "${!v}" && a=$(s2a_nospace "${!v}") && export "$v=$a" || { echo "【红】建不了 $v 的无空格别名"; exit 1; }; done
# GNU Make ≥ 4（tools/gmake.sh；系统 /usr/bin/make 3.81 的 -j 令牌会卡死，11-06 docs/坑记录.md）：找不到就报红，不悄悄退回 3.81
source "$R/tools/gmake.sh"; MK=$(s2a_gmake) || exit 1
B=$(mktemp -d "${TMPDIR:-/tmp}/s2a-shell.XXXXXX") || exit 1
trap 'rm -rf "${B:?}"' EXIT
# 设旧访问时间会改文件状态时间：出壳流程里任何 git 命令（取提交号、看改动）都会因此把整棵树重读核对一遍、闭包核不了（11-04 实测一次 git status 读 9635/9638 个）。
#   本次出壳的子进程一律让 git 不信状态时间（只看修改时间 / 大小 / inode），只作用于本次出壳、不改仓库配置
n=${GIT_CONFIG_COUNT:-0}; export "GIT_CONFIG_KEY_$n=core.trustctime" "GIT_CONFIG_VALUE_$n=false" GIT_CONFIG_COUNT=$((n + 1))
"${S2A_PY:-python3}" "$H/shell/closure.py" mark "$R" || { echo "【红】设不了仓库文件访问时间（壳输入闭包）"; exit 1; }
t0=$SECONDS
# 游戏库界面（runtime/build.mk S2A_SHELLUI）开不开只看壳输入表：有「入 shell」行才编进壳。不收外部开关——表与表里的目录都进壳编号，
#   开关与库界面源码一变壳编号必变，同一壳编号不会对应带 / 不带库界面两种壳
SHELLUI=0; printf '%s\n' "$IN" | grep -qx 'shell' && SHELLUI=1
"$MK" -C "$RN" ROOT="$RN" -j"$S2A_JOBS" -s ios S2A_CS=1 S2A_SHELLUI=$SHELLUI IOS_BUNDLE=dev.s2arm.shell IOS_DISPLAY=S2Arm BUILD="$B/build" IOS_OBJ_CACHE="$RN/build/ios_objcache" > "$B/make.log" 2>&1 \
  || { echo "【红】make ios 失败：$(grep -a -m1 -E 'error:|🔴|【红】' "$B/make.log" | cut -c1-200)"; tail -20 "$B/make.log"; exit 1; }
APP=$B/build/ios/S2Arm.app
HL=$R/build/gal/ios/gal_helpers
{ [ -f "$HL/.stamp" ] || "$MK" -C "$RN" ROOT="$RN" -s "$RN/build/gal/ios/gal_helpers/.stamp" >> "$B/make.log" 2>&1; } \
  && mkdir -p "$APP/gal_helpers/m3" && cp "$HL"/*.metallib "$APP/gal_helpers/" && cp "$HL"/m3/*.metallib "$APP/gal_helpers/m3/" \
  || { echo "【红】渲染器内部辅助库拷不进壳（${HL}）"; exit 1; }
TABLES=$("$MK" -C "$RN" -s print-app-tables) || { echo "【红】取不到随包表清单"; exit 1; }
"${S2A_PY:-python3}" "$H/ship_ext.py" shell --app "$APP" --out "$OUT" --tables "$TABLES" || exit 1
if [ -n "${S2A_SHELL_CLOSURE_SUGGEST:-}" ]; then "${S2A_PY:-python3}" "$H/shell/closure.py" suggest "$R" > "$S2A_SHELL_CLOSURE_SUGGEST" || exit 1
else "${S2A_PY:-python3}" "$H/shell/closure.py" check "$R" || { rm -rf "${OUT:?}"; exit 1; }; fi
echo "🟢 出壳用时 $((SECONDS - t0)) 秒"
