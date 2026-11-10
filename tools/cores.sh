#!/bin/bash
# 出包并行度（source 后调 s2a_cores）：入口（一键出包、pack/refresh.sh、pack/ipa.sh）开头算一次，导出给全程各阶段，各步骤不再自己读核数。调用方给了就用调用方的：
#   S2A_JOBS      本单可用核数 = 本机核数 ÷ 同机道数，下限 1（11-06 起不再扣「别人在用的核」，改为整条链降优先级 nice 10，见 s2a_cores）。make -j、文件级并行这类不吃大内存的步骤用
#                 同机道数 = max(S2A_LANES（出包队列传）, 本机正在出包的道数)：普查 / 快测 / 手动出包不传 S2A_LANES，原先每道都当整台是自己的
#                 （11-01 Spare-host 8 核多道冷出包，Metal 编译进程叠到 19 个、1 分钟负载 95）。每道算并行度时在 S2A_LANE_DIR（缺省 ~/.s2a/lanes）
#                 按入口进程号登记，数仍活着的登记（含自己）；进程没了的登记顺手删。只在本单第一次算时登记（嵌套入口继承导出的 S2A_JOBS，不重复算）
#   S2A_CPU_BAKE  每路吃内存的重活（改写、抽取）的路数 = min(S2A_JOBS, 本单可用内存 ÷ 1.5 GB)；本单可用内存 = 物理内存 × 0.75 ÷ S2A_LANES
#   S2A_DIRECT_J  直连库编译路数（pack/refresh.sh 出直连库用；11-04 玩家路径 J）= min(S2A_JOBS, 1 + (本单可用内存 − dsh 主进程 − 1 个 metal 最大峰值) ÷ metal p90)，下限 1。
#                 原先直连库也用 S2A_CPU_BAKE（每路按 1.5 GB），8 GB 玩家机只开 4 路；实测直连库编译内存 = dsh 主进程 1.7 GB + 每个 metal 编译进程
#                 p90 0.41 GB / 最大 1.04 GB（11-04 Sifu J=2 冷编 667 个 metal 进程），留一个最大峰值、其余按 p90 → 8 GB / 8 核开满 8 路。
#                 Sifu 直连库 J=2 → 18：3186 → 851 秒（11-04 本机）。三个量可用 S2A_DIRECT_MAIN_MB / S2A_DIRECT_METAL_PEAK_MB / S2A_DIRECT_METAL_P90_MB 覆盖
# 出包链进程的 malloc 不留大块空闲缓存（10-30 Spare-host 16 GB 实测：Python 已释放的大块被 libmalloc 留着不还，4 路抽取整树 14.6 GB；
#   MallocLargeCache=0 后 2.2 GB、产物逐文件相同、墙钟在噪声内）
export MallocLargeCache=${MallocLargeCache:-0}
s2a_lanes() {   # 本机正在出包的道数（含本道）：登记本道进程号（S2A_LANE_PID：外层把出包 + 跑局整段当一道时给，如快测 one_game.sh；没给 = 本入口），数活着的登记
  local d=${S2A_LANE_DIR:-$HOME/.s2a/lanes} n=0 f p
  mkdir -p "$d" 2> /dev/null && : > "$d/${S2A_LANE_PID:-$$}" 2> /dev/null || { echo 1; return; }
  for f in "$d"/*; do
    p=${f##*/}; case $p in ''|*[!0-9]*) continue ;; esac
    # 已进跑局阶段的道（one_game 出包完写「mac」）不再摊核：它排 Mac 局槽时不吃 CPU，跑局吃的 CPU 已由下面「别人实际在用的核」扣掉
    #   （11-02 普查本机：同机登记 9–11 单、多数在排 / 跑 Mac 局，在冷翻译的单只分到 2 核，机器负载 14–16 / 18 核空着）
    if kill -0 "$p" 2> /dev/null; then [ "$(cat "$f" 2> /dev/null)" = mac ] || n=$((n + 1)); else rm -f "$f"; fi
  done
  echo $(( n > 0 ? n : 1 ))
}
s2a_mem_gb() {   # 本机物理内存（GB，取整），不经 Python：macOS sysctl hw.memsize，Linux /proc/meminfo；都读不到输出空，由调用方报红
  #   原先用 PATH 上的 python3 调 os.sysconf("SC_PHYS_PAGES")，anaconda 3.9 不认这个名字，整单停在并行度（#447，11-10 Intel Mac 实走）
  local b k
  b=$(sysctl -n hw.memsize 2> /dev/null)
  [ "$b" -gt 0 ] 2> /dev/null && { echo $(( b >> 30 )); return; }
  k=$(awk '/^MemTotal:/ { print $2; exit }' "${S2A_PROC_MEMINFO:-/proc/meminfo}" 2> /dev/null)   # S2A_PROC_MEMINFO：单测在 Mac 上模拟 Linux 用
  [ "$k" -gt 0 ] 2> /dev/null && echo $(( k >> 20 ))
}
s2a_cores() {
  local nc gb ln mj live
  # 出包整条链降优先级（子进程继承）：只往低调、不往高调（已更低的不动；调高要 root）。S2A_NICE 改档，0 = 不调
  local nv=${S2A_NICE:-10} cur; cur=$(ps -o nice= -p $$ 2> /dev/null | tr -d ' ')
  [ "$nv" -gt "${cur:-0}" ] 2> /dev/null && renice "$nv" -p $$ > /dev/null 2>&1
  nc=${S2A_NCPU:-$(getconf _NPROCESSORS_ONLN 2> /dev/null)}; [ "$nc" -ge 1 ] 2> /dev/null || { echo "【红】并行度：读不出本机核数（getconf _NPROCESSORS_ONLN）" >&2; return 1; }
  ln=${S2A_LANES:-1}; [ "$ln" -ge 1 ] 2> /dev/null || ln=1
  if [ -z "${S2A_JOBS:-}" ]; then
    live=$(s2a_lanes); [ "$live" -gt "$ln" ] && ln=$live
    # 不再按「别人在用的核」扣（11-06 出包速度线，维护者：发布后几乎都是玩家冷出包）：原先入口 iostat 采 3 秒就把整单核数扣掉，
    #   之后 20 分钟一直按它跑——玩家机上 Spotlight 给刚导入的游戏建索引、浏览器开着，整单只剩一半核（Spare-host 实例：别人约 5 核 → 本单 3 / 8 核）。
    #   改成核数按道数分满，出包整条链降优先级（函数开头 renice），CPU 由系统调度器按实时忙闲分：别人要用时让给别人，空出来立刻吃满
    local j=$(( nc / ln )); [ "$j" -lt 1 ] && j=1
    export S2A_JOBS=$j
  fi
  if [ -z "${S2A_CPU_BAKE:-}" ] || [ -z "${S2A_DIRECT_J:-}" ]; then
    gb=${S2A_MEM_TOTAL_GB:-$(s2a_mem_gb)}
    [ "$gb" -ge 1 ] 2> /dev/null || { echo "【红】并行度：读不出物理内存（sysctl -n hw.memsize、/proc/meminfo 都没读到）；可用 S2A_MEM_TOTAL_GB=<GB> 手动指定" >&2; return 1; }
  fi
  if [ -z "${S2A_CPU_BAKE:-}" ]; then
    mj=$(( gb * 3 / 4 / ln * 2 / 3 )); [ "$mj" -ge 1 ] || mj=1
    export S2A_CPU_BAKE=$(( S2A_JOBS < mj ? S2A_JOBS : mj ))
  fi
  if [ -z "${S2A_DIRECT_J:-}" ]; then
    local am=$(( gb * 1024 * 3 / 4 / ln )) dj
    dj=$(( 1 + (am - ${S2A_DIRECT_MAIN_MB:-1740} - ${S2A_DIRECT_METAL_PEAK_MB:-1065}) / ${S2A_DIRECT_METAL_P90_MB:-420} ))
    [ "$dj" -gt "$S2A_JOBS" ] && dj=$S2A_JOBS; [ "$dj" -ge 1 ] || dj=1
    export S2A_DIRECT_J=$dj
  fi
  [ -n "${S2A_CORES_QUIET:-}" ] || echo "并行度：本机 ${nc} 核、同机 ${ln} 单、出包优先级 nice $(ps -o nice= -p $$ | tr -d ' ')；本单 ${S2A_JOBS} 核，重活 ${S2A_CPU_BAKE} 路、直连库编译 ${S2A_DIRECT_J} 路"
}
