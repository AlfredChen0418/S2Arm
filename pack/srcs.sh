#!/bin/bash
# 游戏原始包按内容认（tools/game_icons.sh、pack/stage_aoc.sh 共用；被 source）：
#   prog_id <产物根>           现行产物根的程序号（unpack.tsv 的 title_id；没有就读 main.npdm 的 ACI0），大写 16 位十六进制
#   src_ids                    原始包目录（S2A_GAME_SRC，缺省 ~/Downloads）里每个 xci / nsp：「路径 大小 修改时间 程序号」；
#                              程序号按内容读控制元数据取（nx_unpack --nacp-only），读不出（单独的更新 / DLC 包没有控制分区）记「-」。
#                              按 nx_unpack 退出码分：1 = 读了文件、按内容下了结论（单独的更新 / DLC 包「基础程序应恰好 1 个，实际 0」、缺密钥、
#                              源文件校验不过；Program.cs 的 SourceError 等都回 1）→ 照旧记「-」；其余非 0 = 工具本身没跑成（131 没有 .NET 运行时、
#                              126 / 127 起不来、128+ 被信号杀、2 用法错）→ 机器原因：那个文件不出行，打一行「【红】机器原因：原件探测出错…」带
#                              nx_unpack 原话，其余照列，最后返回 1——调用方必须认这个返回码，不能当成「没有这个程序号的包」
#                              （11-05 after_r2 v4：没 .NET 运行时探测全回 131，15 款被记成游戏「失败 解包」）
#                              一个文件里有多个本体（11-05 索尼克×夏特：卡带里游戏 + 艺术集&原声集两个程序号）：--nacp-only 不带 --title-id 回 1「基础程序应恰好 1 个，实际 N」
#                              （N ≥ 2）→ 改用 nx_unpack --inspect 按内容清单列出全部本体，每个程序号各出一行（同一路径多行，按程序号排序）、都进缓存；
#                              调用方按程序号筛行，往 nx_unpack 传 --title-id=<程序号> 只取这一款。--inspect 读不出（退出 3）照旧记「-」不进缓存
#                              结果按「路径 大小 修改时间（纳秒）」缓存在 S2A_SRC_IDS（缺省 run/s2arm/icons/.ids.tsv）；文件名一概不看（10-20 维护者定）
# nx_unpack：S2A_NX_UNPACK 给了就用；没给按本树源码哈希取 run/s2arm/bin/nx_unpack_<哈希>/nx_unpack，没有就现编（编到 .tmp 再 mv，内容寻址、只读使用）——
#   共享的旧 bin/nx_unpack 不认新参数（--aoc-only），合入后不重编就会让刷新全红
_srcs_h=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# .NET 运行时由本入口自己保证，不靠起任务的人记得 export（11-05：after_r2 v4 由 launchctl submit 起，环境只有系统 PATH、没有 DOTNET_ROOT，
#   nx_unpack 启动壳找不到运行时秒退 rc=131，15 款被记成游戏「失败 解包」）。nx_unpack 是 apphost 启动壳：只认 DOTNET_ROOT 或微软默认位置
#   /usr/local/share/dotnet；现编要 PATH 上有 dotnet。没设 DOTNET_ROOT 时按 DOTNET → PATH 上的 dotnet → ~/.dotnet 取第一个带 host/fxr 的根补上
#   （同 pack/oneclick/oneclick.sh 的找法），PATH 上没有 dotnet 就把这个根补进 PATH。都找不到不在这里停：探测 / 解包会带着启动壳的原话报红
if [ -z "${DOTNET_ROOT:-}" ]; then
  for _srcs_dn in "${DOTNET:-}" "$(command -v dotnet 2> /dev/null)" "$HOME/.dotnet/dotnet"; do
    [ -n "$_srcs_dn" ] && [ -x "$_srcs_dn" ] || continue
    _srcs_dn=$(readlink -f "$_srcs_dn" 2> /dev/null || echo "$_srcs_dn")   # Homebrew 等把 dotnet 软链进 bin：运行时根在真身所在目录
    [ -d "${_srcs_dn%/*}/host/fxr" ] && { export DOTNET_ROOT=${_srcs_dn%/*}; break; }
  done; unset _srcs_dn
fi
[ -n "${DOTNET_ROOT:-}" ] && [ -x "$DOTNET_ROOT/dotnet" ] && ! command -v dotnet > /dev/null 2>&1 && export PATH="$DOTNET_ROOT:$PATH"
nxu_path() {
  [ -n "${S2A_NX_UNPACK:-}" ] && { echo "$S2A_NX_UNPACK"; return; }
  local h b; h=$(cat "$_srcs_h"/nx_unpack/*.cs "$_srcs_h"/nx_unpack/*.csproj | "${S2A_PY:-python3}" -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()[:12])'); b=${S2A_RUN_ROOT:-$HOME/.s2arm/run}/s2arm/bin/nx_unpack_$h   # 摘要不用 shasum（Windows Git Bash 没有）
  if [ ! -x "$b/nx_unpack" ]; then
    mkdir -p "$(dirname "$b")"   # 新机器上 run/s2arm/bin 还不存在：不建的话编译日志写不进、编译一行都没跑（10-23 Spare-host 全新机器实测）
    rm -rf "$b.tmp.$$"; dotnet build -c Release "$_srcs_h/nx_unpack" -o "$b.tmp.$$" > "$b.build.log" 2>&1 && mv "$b.tmp.$$" "$b" 2>/dev/null || rm -rf "$b.tmp.$$"
  fi
  echo "$b/nx_unpack"
}
SRC_N=$(nxu_path); SRC_K=${S2A_KEYS:-$HOME/.switch/prod.keys}
# 原件进键（审计 P1-54，11-03 #1417：一键解包键、追加内容源键原先是「路径 大小 修改时间」，保住这三样换了原件就沿用旧解包 / 旧追加内容）：
#   src_sample <文件>…  一行一个：「大小<TAB>首尾各 64 KiB 的 SHA-256」——内容抽样（与资源抽样键 pack/resource_input_key.py 同口径，不读整个几十 GB），不含路径（挪位置不重解）
#   src_ident <文件>…   不读内容的身份：「大小 修改时间 ctime inode」——换了文件（哪怕保住大小和修改时间）ctime / inode 必变；原件在机械盘（软链）上时用它，
#                       不为算键去读机械盘（读机械盘要走读锁）。拷动过原件也会变：多重解一次，不会装旧的
src_sample() { python3 - "$@" <<'PY'
import hashlib, os, sys
for p in sys.argv[1:]:
    n = os.path.getsize(p); h = hashlib.sha256()
    with open(p, 'rb') as f:
        h.update(f.read(65536))
        if n > 131072: f.seek(-65536, 2)
        h.update(f.read(65536))
    print(f'{n}\t{h.hexdigest()}')
PY
}
src_ident() { python3 -c 'import os,sys
for p in sys.argv[1:]: s = os.stat(p); print(s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_ino)' "$@"; }   # 纳秒级：同一秒里原地改写（inode 不变）也认得出
SRC_DIR=${S2A_GAME_SRC:-$HOME/Downloads}; SRC_IDS=${S2A_SRC_IDS:-${S2A_ICONS:-${S2A_RUN_ROOT:-$HOME/.s2arm/run}/s2arm/icons}/.ids.tsv}
prog_id() {
  local id; id=$(awk -F'\t' '$1 == "title_id" {print toupper($2)}' "$1/unpack.tsv" 2>/dev/null)
  # 没有 unpack.tsv：按 main.npdm 格式取（META +0x70 指的 ACI0 +0x10），与 tools/addr_need.py npdm_program 同一份实现；不 find 第一处 ACI0（标题名 / ACID 签名里可能碰巧有这四个字节）
  [ -n "$id" ] || id=$(python3 -c "import sys;sys.path.insert(0,sys.argv[1]);import addr_need;print(addr_need.npdm_program(sys.argv[2]).upper())" "$_srcs_h/../tools" "$1/exefs/main.npdm" 2>/dev/null)
  echo "$id"
}
src_ids() {
  local f k id ids t ok e m rc=0; mkdir -p "$(dirname "$SRC_IDS")"; touch "$SRC_IDS"
  while IFS= read -r f; do
    # stat -L：软链取目标的大小 / 修改时间，原件换了键跟着变。GNU（-c）与 BSD（-f）都试：GNU 的 -f 是文件系统统计、退出码也不可靠（#824 F-pack-stat-bsd），
    #   所以 -c 在前、BSD 不认 -c 才落到 -f；两样都读不出就停，不拿空键认包
    k="$f	$(stat -L -c '%s	%.9Y' "$f" 2> /dev/null || stat -L -f '%z	%.9Fm' "$f" 2> /dev/null)" || { echo "【红】src_ids 读不出 $f 的大小 / 修改时间（stat）" >&2; return 1; }
    ids=$(awk -F'\t' -v k="$k" '$1"\t"$2"\t"$3 == k && !n[$4]++ {print $4}' "$SRC_IDS")   # 多本体的文件缓存里有多行：全取（并发重复写入的同号去重）
    # 退出 1（按内容的结论：更新 / DLC 包、缺密钥…）记「-」但不进缓存，下次重探；只有退出 0 的才进缓存；其余非 0 报机器原因（见头注）。
    #   10-26 自检 pack_stages_test 在 gate-host 偶发红：并行的失败用例先把 base.nsp 记成「-」，同批另一路读到就判成「没有这个程序号的包」
    if [ -z "$ids" ]; then t=$(mktemp -d); "$SRC_N" "$SRC_K" "$t" --nacp-only "$f" > "$t.log" 2>&1 && ok=0 || ok=$?   # 原话写在输出目录外
      # 多本体（见头注）：--inspect 列全部本体。stdout 只有一行 JSON；退出 3 = 容器读不出（按内容的结论，照旧「-」）
      m=--nacp-only
      if [ $ok = 1 ] && grep -aEq '基础程序应恰好 1 个.*实际 ([2-9]|[1-9][0-9]+)' "$t.log"; then
        m=--inspect; "$SRC_N" "$SRC_K" --inspect "$f" > "$t.json" 2> "$t.log" && ok=0 || ok=$?
        [ $ok = 3 ] && ok=1
        [ $ok = 0 ] && { ids=$(python3 -c 'import json, re, sys
b = sorted({p["titleId"].upper() for p in json.load(open(sys.argv[1], encoding="utf-8"))["packages"] if p.get("type") == "base"})
assert len(b) >= 2 and all(re.fullmatch("[0-9A-F]{16}", x) for x in b)
print("\n".join(b))' "$t.json" 2> /dev/null) || ok=1; }   # 清单与 --nacp-only 的结论对不上（不足 2 个本体 / 程序号不合法）：按读不出记「-」
      else ids=$(awk -F'\t' '$1 == "title_id" {print toupper($2)}' "$t/unpack.tsv" 2>/dev/null); fi
      if [ $ok != 0 ] && [ $ok != 1 ]; then   # 工具没跑成（机器原因）：不出行、不进缓存（下次重探），带 nx_unpack 最后两行原话
        e=$(grep -a . "$t.log" 2> /dev/null | tail -2 | tr '\t\n' '  '); rm -rf "$t" "$t.log" "$t.json"; rc=1
        echo "【红】机器原因：原件探测出错（nx_unpack ${m} 退出 ${ok}，不是游戏的问题）：${e:0:240}（${f}）" >&2; continue; fi
      rm -rf "$t" "$t.log" "$t.json"; ids=${ids:--}
      [ $ok = 1 ] || for id in $ids; do printf '%s\t%s\n' "$k" "$id"; done >> "$SRC_IDS"; fi
    for id in $ids; do printf '%s\t%s\n' "$k" "$id"; done
  # 系统 / 隐藏目录不进（11-02 体验线三单：整块机械盘当源目录，Windows 回收站 $RECYCLE.BIN 里的 $I*.xci 元数据 / $R*.xci 删掉的原件被当原件探测）
  done < <(find -L "$SRC_DIR" -maxdepth 4 \( -type d ! -path "$SRC_DIR" \( -name '$RECYCLE.BIN' -o -name 'System Volume Information' -o -name '.?*' \) -prune \) -o \( -iname '*.xci' -o -iname '*.nsp' -o -iname '*.nsz' -o -iname '*.xcz' \) -type f -print 2>/dev/null | sort)   # -L：软链目录也进去，指向文件的软链按目标认（10-26 原件挪外置盘留软链）
  return $rc
}
