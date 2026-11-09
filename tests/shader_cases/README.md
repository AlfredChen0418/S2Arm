# 合成 Maxwell 翻译器回归（T220）

入口：`bash tests/shader_cases_test.sh`。仅 Linux lavapipe（CPU Vulkan）；不需要游戏、真机、网络或维护者路径。缺依赖明确失败，不把跳过算绿。

## 环境

预先准备仓库 `global.json` 允许的官方 .NET 10 SDK、Python 3.12+、C 编译器、Vulkan loader/headers、Mesa lavapipe、`glslc` 和 `spirv-val`。用 `VK_DRIVER_FILES`（或 `VK_ICD_FILENAMES`）选择 lavapipe；执行器还核设备类型 CPU 和 llvmpipe 名称，拒绝拿 SwiftShader/真实 GPU 代替。`LP_NUM_THREADS=1` 可约束 CPU 资源。

`glslc`/`spirv-val` 从 PATH 或已安装 `ANDROID_NDK_HOME/shader-tools/linux-x86_64` 找；headers 可用 `VULKAN_HEADERS` 或同 NDK 自带的 Vulkan/vk_video 头（不把 Android libc include 到宿主）。测试不会下载、安装、写系统配置。`SHADER_CASES_OUT` 可指向一个新的独立目录保留全部二进制/IR/覆盖率证据；默认临时目录完成即清理。

## 用例合同 v1

`<族>/<名>.json`，`schema:1`、唯一 `id`、`stage`（fragment/compute）、可读 `instructions` 字符串数组、`inputs`、`expected`、`rules`、独立 `reference`。规则名只供读者理解，**不计覆盖率**。

- fragment：R0–R3 为浮点输出；`expected` 是 RGBA8 四个整数，使用显式 Vulkan originUpperLeft 状态。独立 `harness.vert` 画全屏三角形并提供 location0 的常量 vec4(0.25,0.5,0.75,1)。真正受测的片元程序全部由合成 Maxwell 经过当前真实翻译器生成，辅助 VS 不是受测翻译结果；不宣称已测 Maxwell VS。
- compute：单 invocation，`inputs.storage0` 和 `expected` 都恰64个 uint32。手写 LDC64 地址识别模式，翻译器真实 GlobalToStorage 降到 set1/binding0 存储缓冲；读取后逐值比较，包括所有不应变化的 guard。C0 输入固定为零填充64KiB（guest地址base=0），真实 uniform descriptor 位于 set0/binding1，覆盖clamp辅助函数实际读取；这不是从期望输出构造输入，其他常量输入尚不支持。当前格式没有泛化纹理上传/任意 descriptor 配置，扩充时同步执行器和用例。
- 当前可读编码：`MOV32I Rd bits`、`IADD32I Rd Ra imm`、`FADD32I Rd Ra float`、`ALD Rd location component`、`LDC64 Rd slot offset`、`LDG32 Rd Ra byteOffset`、`STG32 Rs Ra byteOffset`、`EXIT`；T221 整数逻辑另有 `LOP.<AND|OR|XOR|PASS_B> Rd [~]Ra [~]B`、`LOP32I.<op> Rd [~]Ra [~]imm32`、`LOP3.LUT Rd Ra B Rc lut`，B 为 `Rn`、有符号 20 位立即数或 `c[slot][字节偏移]`；移位 `SHL[.W] Rd Ra B`、`SHR.<U32|S32>[.W][.BREV] Rd Ra B`、`SHF.<L|R>[.U64|.S64][.W] Rd Ra Rb|imm6 Rc`；位域 `BFE.<U32|S32>[.BREV] Rd Ra B`、`BFI Rd Ra B Rc|c[][]`、`POPC Rd [~]B`、`FLO.<U32|S32>[.SH] Rd [~]B`；加法与最值 `IADD[.PO] Rd [-]Ra [-]B`、`IADD3 Rd [-]Ra [-]B [-]Rc`、`ISCADD[.PO] Rd [-]Ra [-]B shift`、`ISCADD32I Rd Ra imm32 shift`、`IMNMX.<U32|S32>.<MIN|MAX> Rd Ra B`；乘加 `XMAD[.<U16|S16>.<U16|S16>][.CLO|.CHI|.CBCC][.PSL][.MRG] Rd Ra[.H1] B[.H1] C`（B 为 16 位立即数时是 I 形式，B / C 为 `c[][]` 时是 C / Rc 形式）（见 [integer/README.md](integer/README.md)）。RZ=255；编码按当前 `Decoders/InstTable.cs` / `InstDecoders.cs`。生成器插入软件 decoder 跳过的调度字，尾槽填 EXIT；不声称能直接喂 Maxwell 硬件。
- 四族：integer / float / memory（T223 子集）/ attribute（T224 子集），各2例。T221–T224 后续可扩充同合同，不代表目前覆盖整个指令族。

## 真值与负控

每个输出有 `reference` 的独立算术推导；不从翻译结果反推金样。SPIR-V 必须通过 `spirv-val --target-env vulkan1.1`，然后真正提交 CPU Vulkan、等待 fence、映射读回、比较。

正常用例之后，同一执行器检查错误期望必须被拒；隔离测试源码副本只编译一次，在真实 FADD32I emitter 注入测试专用 `S2A_TEST_FADD32I_MUTATION`（只接受0/1）；正例子进程明确0走原立即数，负控独立子进程1把立即数源改0，使用同一DLL重执行 float/add，必须与独立期望不同。原工作树和生产翻译器不被改写。负控要求实际读回差异。

## 自动覆盖率与证据边界

`coverage.json` 有三种分母和完整未覆盖列表：
1. 实际 InstTable 注册的去重 emitter 方法（另列编码行数、InstName数）；工具按真实 opcode 优先规则解码用例二进制计数。这是**已解码存在**，不是逐指令动态执行覆盖。
2. 注册的 Transform passes 与 Optimizer 实际调用的优化 pass；只消费真实测试副本的 IR before/after 观察。没有变化不能计覆盖。当前注入 ConstantFolding/Simplification 操作快照，以及 XmadOptimizer 的块快照（它按块跑，before / after 是整块的操作序列，#1159 第七片）；其余遍即使被调用也不冒称覆盖。
3. `compat/patches/series` 中生效且触及 Shader 的补丁。首个归因项是 `20261102-0640-shader-iadd-plus-one-1339.patch` 的 IADD/ISCADD `.PO` 结果加一语义。其余补丁列为未覆盖。

归因复用 `integer/add_po` 的独立期望。用同一 DLL 重译这个用例时，独立子进程用 `S2A_TEST_IADD_PO_MUTATION=1` 在隔离副本里跳过 `PlusOne` 的加一处理。两版 SPIR-V 均须通过验证并由 lavapipe 执行。正常版须匹配全部 64 字，负控须在第 8–18 字（从 0 起）各少 1（按 uint32 回绕），其余 53 字相同。满足这些条件后，`coverage.json` 的 `patches.evidence` 按补丁名记录用例 ID、逐字差异、补丁和两版 SPIR-V 的 SHA256。该用例的证据范围是结果值。

`integer/add_po_carry` 单独验证寄存器形式的进位。`IADD.PO.CC` 写条件码，`IADD.X` 以两个 `RZ` 为操作数读出 `CF`。同一 DLL 在独立子进程中用 `S2A_TEST_IADD_PO_CARRY_MUTATION=1` 跳过 `SetPlusOneCarry`，保留 `PlusOne` 的结果加一。正常版逐字匹配独立期望。负控须让第 9 字（从 0 起）从 1 变成 0，其余 63 字逐字匹配。逐字差异与两版 SPIR-V 的 SHA256 写入 `iadd-po-carry-proof.json`，与 `coverage.json` 的结果值归因分开记录。

观察只注入隔离副本，不改生产输出路径。保存实际 IR 文本和SHA256；验证 hash/source/case 一致。`coverage_test.py` 对伪声明、同 IR、损坏hash、未知opcode、错误source做拒绝负控。

## 当前射程

仅原创建模指令、Linux CPU Vulkan图形/计算读回与工具覆盖；非Metal、Apple ARM64、真机、游戏、帧率/能耗证据。产物不含游戏来源内容。

## 尚红的独立0339探针（不计覆盖）

`SHADER_CASES_PROBE_CLAMP=1 bash tests/shader_cases_test.sh` 在同compute输入另译 host-window-clamp=true。main286a18cc上真实 GenerateCall抛 AstOperation→AstOperand InvalidCastException，尚未到SPIR-V验证/CPU执行；因此不把此探针算补丁通过，基础8例使用明确clamp=false。保留真实异常和已生成finalIR，独立已知红见 [#1166](https://github.com/jhaizhou-ops/S2Arm/issues/1166)；直连维护者已授权独立分片限0339辅助Call参数局部复制，未改GenerateCall/结构化内联规则。这个显式诊断开关不是绕过已登记失败：它是基础8例之外、为补足补丁归因新加入的探针，完整T220补丁归因仍未验收。

## macOS 门禁环境

macOS 自动走[共享 Linux Vulkan 镜像](../vulkan/README.md)，与 Android `tests/vk/run.sh` 共用；先在门禁外显式准备。环境未就绪返回75，Linux 原生路径不变。

### Logical-device feature contract

The fixed eight cases do not use fragment invocation interlock. The test accessor
must describe the features actually enabled by `vk_device.c`, not the interface's
default assumption or capabilities merely advertised by a physical GPU. That
logical device does not enable `VK_EXT_fragment_shader_interlock`; therefore the
test accessor reports interlock unavailable, and every actual generated SPIR-V
module is checked for incompatible interlock capabilities/extensions/instructions
before validation and execution. This does not alter production translation or
enable interlock-dependent future cases. Such cases require a separately verified
device-feature and fixture contract. The check supplements, never replaces or
filters, the shared container's Vulkan validation-layer errors.

### Cold-build watchdog versus execution watchdog

The CLI build has a 900-second stuck-process ceiling; translation, validation,
compilation of the native helper and each execution retain the 55-second ceiling.
`tests/shader_cases_test.sh` declares 1200 seconds in its first five lines for the
whole gate. These are watchdogs, not performance acceptance targets. The final
report separates build wall time from the remaining pipeline time (the latter
includes translation/validation/readback, not just GPU execution); a successful
cold build is no longer rejected solely because total wall time exceeds 60 seconds.
Numerical, SPIR-V, VVL and negative-control checks are unchanged.
