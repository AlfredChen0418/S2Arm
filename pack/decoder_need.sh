# 抽取要不要游戏自带的解码器（Oodle / LZ5：在 Mac 宿主的工具模式里跑游戏自己的解压代码，要 build/s2arm-host 与改写后的模块 main.fixups.tsv，
#   见 translator/uepak.py oodle_host_find）。RomFS 里有下面任一种容器就要：
#   Unreal 包 .pak / .pkz / .utoc；id Tech 资源包 .resources（DOOM Eternal 的 gameresources*.resources 条目 Oodle 压缩，graphics/tools/shbake/idtech_idcl.py）。
#   Trinity 的 .trpfs 同样需要游戏自带的 Oodle：抽取前必须等 main.fixups.tsv 等改写产物就绪（#470）。
#   11-07 质量线：原先只认 Unreal 三种，DOOM Eternal 在 Mac 本机出包时宿主没编、抽取又跑在改写之前（dfd90808c 依赖重排），两路抽取都是 0 支。
#   判据只此一处：pack/stage_extract.sh（要不要编 Mac 宿主）与 pack/refresh.sh 快路（抽取等不等改写）都用它
S2A_DECODER_EXTS=".pak .pkz .utoc .resources .trpfs"
decoder_need() { "${PY:-python3}" "$1/graphics/nvn/tools/romfs_list.py" has-ext "$2" $S2A_DECODER_EXTS 2> /dev/null; }   # $1 = 仓库根，$2 = RomFS 目录；要 → 退 0
