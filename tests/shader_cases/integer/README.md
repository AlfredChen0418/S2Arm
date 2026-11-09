# T221 第一片：整数逻辑

Part of #1159；使用 #1157 / #1168 v1 合同。框架原有的 `wrap32`、`carry-exponent` 两例不动；本片 6 个原创 compute 用例都叫 `integer/logic_*`，由可读指令构成，不含游戏代码、提取的着色器或游戏数据。

## 覆盖

| 用例 | emitter | 内容 |
|---|---|---|
| `logic_lop_r` | `LopR` | AND / OR / XOR / PASS_B，取反 A / B / 两者，RZ 与同一寄存器作两个源 |
| `logic_lop_i` | `LopI` | 有符号 20 位立即数：最大 `0x7ffff`、`-1`、最小 `-0x80000`，取反立即数 |
| `logic_lop32i` | `Lop32i` | 32 位立即数，四种运算与取反 |
| `logic_lop3_r` | `Lop3R` | LUT 0x00 / 0xff / 单源 / 与 / 或 / 异或 / 多数 / 选择 / 混合，RZ 与重复源 |
| `logic_lop3_i` | `Lop3I` | B 为有符号 20 位立即数 |
| `logic_cbuf_zero` | `LopC`、`Lop3C` | 常量缓冲形式；框架 C0 恒为零，只证常量源取 0 时的运算 |

输入：word 0–7 是固定操作数（0、全 1、0x55555555、0xaaaaaaaa、0x80000001、0x12345678、0x0f0f00ff、0xdeadbeef），其余 word 用 flow 片同一 guard `((i+1)*0x9e3779b9+0x31415926) mod 2^32`。结果从 word 8 起顺序写；没写到的 guard 必须原样读回。

## 独立期望

`python3 tests/shader_cases/integer/logic_reference_test.py`（已接进 `tests/shader_cases_test.sh`）按 ISA 语义解释用例的可读指令，不导入编码器、翻译器、SPIR-V 或实际输出：

- `LOP` / `LOP32I`：带 `~` 的源先按位取反，再 AND / OR / XOR；PASS_B 取（取反后的）B。
- `LOP` / `LOP3` 的立即数是有符号 20 位，符号扩展到 32 位。
- `LOP3.LUT`：结果位 = `(lut >> (a*4 + b*2 + c)) & 1`，即 PTX `lop3` 约定（A=0xF0、B=0xCC、C=0xAA）。
- `c[0][*]` 读框架的零 C0；RZ = 0。

它逐例重算全部 64 word。每个结果 word 翻一位的错误期望都必须被拒。另有几条手算锚点，防止解释器自己写错；越界的 20 位立即数和 LUT 也要拒。每个 JSON 的 `reference` 逐 word 写了公式和十六进制结果，可以手核。

## 射程

只覆盖这 7 个 emitter 的寄存器结果。谓词输出（`PredicateOp` / 目标谓词）、条件码（`.CC` / `.X`）、非零常量缓冲内容不在本片。整数加法 / 比较 / 最值 / 移位 / 位域 / XMAD / 视频族、`ConstantFolding` / `Simplification` / `XmadOptimizer` 遍、补丁归因也不在本片，仍归 #1159 后续片。不是 Metal、真机、游戏或性能验收。

# T221 第二片：移位

Part of #1159。4 个原创 compute 用例 `integer/shift_*`，输入与第一片相同，移位量用 `MOV32I` 设。

| 用例 | emitter | 内容 |
|---|---|---|
| `shift_shl` | `ShlR`、`ShlI`、`ShlC` | n = 0 / 1 / 4 / 31 / 32 / 33 / 63，默认钳位与 `.W` 回绕 |
| `shift_shr` | `ShrR`、`ShrI`、`ShrC` | `.U32` / `.S32`、正负 A、钳位与 `.W`、`.BREV` |
| `shift_shf_r` | `ShfLR`、`ShfRR` | {C:A} 漏斗移位，32 位与 `.U64` / `.S64` 上限，钳位与 `.W` |
| `shift_shf_i` | `ShfLI`、`ShfRI` | 6 位立即数形式 |

独立期望：`python3 tests/shader_cases/integer/shift_reference_test.py`（已接进 `tests/shader_cases_test.sh`）。

- `SHL` / `SHR` 默认 n ≥ 32 得 0（`.S32` 得符号位），`.W` 取 `n & 31`；`.BREV` 先把 A 位反转。
- `SHF` 按 PTX `shf` 文档：`.L` 取 `({C:A} << n) mod 2^64` 的高 32 位，`.R` 取 `{C:A} >> n` 的低 32 位（`.S64` 为算术移位）。上限 32，`.U64` / `.S64` 为 64；默认钳到上限，`.W` 取模。

**已知红 #1223**：`SHF` 移位量 0、32 位钳位 n ≥ 32、`.S64` 右移 n ≥ 64 三类，当前翻译器与 ISA 不符。绿用例避开这些输入，复现程序和逐 word 期望 / 实际见 #1223，修好后把那里的 JSON 加进本目录。

射程补充：Mesa NIR 的移位按位宽取模，lavapipe 上 `a << 32` 本身就等于 `a << 0`。所以「`.W` 少了 `& 31` 掩码」这类错误在本执行器上看不出来，需要 Metal 或真 GPU 区分。`.X` / `.CC` 扩展精度、`.HI` 不在本片。

# T221 第三片：位域

Part of #1159。4 个原创 compute 用例 `integer/bitfield_*`，输入与前两片相同，控制字（pos | len << 8）用 `MOV32I` 设。

| 用例 | emitter | 内容 |
|---|---|---|
| `bitfield_bfe` | `BfeR`、`BfeI` | `.U32` / `.S32`、`.BREV`、长度 0、字段恰好到 bit 31、全宽 |
| `bitfield_bfi` | `BfiR`、`BfiI` | 字段在中间 / 低半 / 恰好到 bit 31、长度 0、全宽、只插最高位 |
| `bitfield_popc_flo` | `PopcR`、`PopcI`、`FloR`、`FloI` | `~B`、全 0 / 全 1、`.S32` 负数输入、`.SH`、找不到得 0xffffffff |
| `bitfield_cbuf_zero` | `BfeC`、`BfiC`、`BfiRc`、`PopcC`、`FloC` | 常量缓冲形式（含 BFI 的 Rc 形式：控制取寄存器、底取常量缓冲），对零 C0 |

独立期望：`python3 tests/shader_cases/integer/bitfield_reference_test.py`（已接进 `tests/shader_cases_test.sh`），按 SASS 直接对应的 PTX `bfe` / `bfi` / `popc` / `bfind` 语义算。

**已知红 #1278**：两类，翻译器与 ISA 不符：
- `BFE` / `BFI` 字段越过 bit 31（pos + len > 32）时，翻译器把 pos / len 原样交给 SPIR-V `OpBitField*`，SPIR-V 规定结果未定义，Mesa 把 pos 32 当 0；
- `FLO.S32.SH` 对负数不看符号。

绿用例只用 pos + len ≤ 32 的字段与非负的 `FLO.S32.SH` 输入；复现程序与逐 word 期望 / 实际见 #1278。`.CC` / `.X` 不在本片。

# T221 第四片：加法与最值

Part of #1159。7 个原创 compute 用例 `integer/add_*`。特殊值可由 `MOV32I` 设置，进位用例的操作数从存储缓冲载入。

| 用例 | emitter | 内容 |
|---|---|---|
| `add_iadd` | `IaddR`、`IaddI` | `-A` / `-B`、无符号回绕 0xffffffff + 1、有符号溢出 0x7fffffff + 1、20 位立即数边界 |
| `add_iadd3` | `Iadd3R`、`Iadd3I` | 三个源各自取负的组合、回绕、立即数 |
| `add_iscadd` | `IscaddR`、`IscaddI`、`Iscadd32i` | 移位 0 / 1 / 4 / 31、`-A` / `-B`、回绕 |
| `add_imnmx` | `ImnmxR`、`ImnmxI` | `.U32` / `.S32` × MIN / MAX：负数对正数、0 对全 1、相等、有符号立即数 |
| `add_cbuf_zero` | `IaddC`、`Iadd3C`、`IscaddC`、`ImnmxC` | 常量缓冲形式，对零 C0 |
| `add_po` | `IaddR` / `IaddI` / `IaddC`、`IscaddR` / `IscaddI` / `IscaddC` 的 `.PO` | 第五片（#1339 裁定后补）：三种形式的低字结果、无符号回绕、有符号溢出 |
| `add_po_carry` | `IaddR` | `IADD.PO.CC` 写入 `CF`，`IADD.X Rd RZ RZ` 消费 `CF` 并写入高字 |

独立期望：`python3 tests/shader_cases/integer/add_reference_test.py`（已接进 `tests/shader_cases_test.sh`），按补码 mod 2^32 算；`IMNMX` 按 `.U32` / `.S32` 比较。进位用例按 33 位无符号和取高低字。

**`.PO`（#1339）**：直连线裁定 `.PO`（取负两位同时置位）= 加一、两个源都不取负，即 `IADD.PO = A + B + 1`、`ISCADD.PO = (A << s) + B + 1`。依据是 nvdisasm 显示不带取负标记，以及 Mesa NAK `sm50.rs` 把「两源都取负」编成 `iadd.po`。翻译器修复在 main `75bd1f2b3`。参考模型按裁定算，`.PO` 与 `-` 同用判域外；`add_po` 在修复前的翻译器上红（见第五片 PR）。`.SAT` 与 `IADD3` 的半字选择和左右移另计。

`add_po_carry` 依次读回 `0 + 0xffffffff + 1`、`0xffffffff + 0 + 1` 和 `0x12345678 + 5 + 1`。这三组分别检验 `B + 1` 回绕的补偿进位、普通进位和随后清除 `CF`，读回的（低字，进位）依次为 `(0, 1)`、`(0, 1)`、`(0x1234567e, 0)`。测试助记符 `IADD.PO.CC Rd Ra Rb` 和 `IADD.X Rd Ra Rb` 限于寄存器形式，按 `InstIaddR` 的 `AvgMode`、`WriteCC` 和 `X` 字段编码。

# T221 第六片：XMAD

Part of #1159。6 个原创 compute 用例 `integer/xmad_*`，输入与前几片相同，覆盖 `XmadR`、`XmadI`、`XmadC`、`XmadRc` 4 个 emitter。编码按 `InstDecoders.cs` 的 `InstXmad*`，另用 envytools `gm107.c`（`f102b82381f3f11cee113d16374c87091db039d9`，第 1457 / 1465 / 1580 / 2031 / 2050 / 2064 / 2149 行）逐位核过。

| 用例 | emitter | 内容 |
|---|---|---|
| `xmad_r` | `XmadR` | A / B 各取低 / 高半字（`.H1`）、各自 `.U16` / `.S16`；C 的 CFULL / `.CLO` / `.CHI` / `.CBCC`；`.PSL`、`.MRG`；回绕 |
| `xmad_i` | `XmadI` | B 为无符号 16 位立即数（`.S16` 时按 16 位符号扩展），A 的 `.H1`，C 模式、`.PSL`、`.MRG`、`.CBCC` |
| `xmad_cbuf_zero` | `XmadC`、`XmadRc` | 常量缓冲形式，对零 C0：C 形式 B = `c[][]`，Rc 形式 C = `c[][]` |
| `xmad_mul32` | `XmadR` | 常见的三条指令 32 位乘法：`XMAD`；`XMAD.MRG`（B.H1）；`XMAD.PSL.CBCC`（A.H1、MRG 结果的高半字）。期望直接核成 `a × b mod 2^32` |
| `xmad_mad32` | `XmadR` | Mesa 24.0.2 nouveau 的乘加降级（`handleMULMAD`）：`XMAD b,a,c`；`XMAD.MRG b,a.H1,0`；`XMAD.PSL.CBCC b.H1,t1.H1,t0`。期望核成 `a × b + c mod 2^32` |
| `xmad_muli16` | `XmadI` | Mesa 24.0.2 nouveau 乘 16 位立即数：`XMAD a,imm,c`；`XMAD.PSL a.H1,imm,t`。期望核成 `a × imm + c mod 2^32` |
| `xmad_mul32_split` / `xmad_mad32_split` / `xmad_muli16_split` | `XmadR`、`XmadI` | 同上三段，每个中间值存进各自的暂存字（w40–w51；`xmad_muli16_split` 只用其中 w40、w42 … w50 六个）再读回：`XmadOptimizer` 认不出、不改写，结果由逐条模式的 emitter 算；期望同样核成普通整数算术（第七片） |

独立期望：`python3 tests/shader_cases/integer/xmad_reference_test.py`（已接进 `tests/shader_cases_test.sh`）。
- 语义按模 2^32：a / b = 低半字或高半字（`.H1`），零扩展（`.U16`）或符号扩展（`.S16`）；积 = a × b，`.PSL` 再左移 16；C 项为 C、C & 0xffff（`.CLO`）、C >> 16（`.CHI`）或 C + (B << 16)（`.CBCC`）；结果 = 积 + C 项，`.MRG` 把高半字换成 B 的低半字。
- **期望的来源，照实写**：envytools 只给了字段与修饰名。上面逐条的读法（`.H1` / `.S16` 扩展、`.PSL`、`.CLO` / `.CHI`、`.CBCC`、`.MRG`）是**读过被测翻译器的 `EmitXmad` 之后**写的。
  - 所以单条模式的三例（`xmad_r`、`xmad_i`、`xmad_cbuf_zero`）的期望**不独立于翻译器**：能抓翻译器回退（见 PR 里的突变），抓不到两边共同的误读。
  - 独立的是三个组合用例（`xmad_mul32`、`xmad_mad32`、`xmad_muli16`）：每个结果是一段指令序列，期望逐 word 核成普通整数算术，不经逐条读法。其中两段取自 Mesa nouveau（独立编译器，在 Maxwell 及以后用这些序列做整数乘 / 乘加；`nv50_ir_peephole.cpp`，Mesa 24.0.2，VirtualBox 内置副本 `c826979b27a75c3ca8fbc28f60cbb38c8a789ba8` 第 1030–1036、2491–2532 行）。
  - 这几段合起来用到了 `.H1`、`.PSL`、`.MRG`、`.CBCC`、CFULL 与 I 形式；`.CLO` / `.CHI`、`.S16` 和 C / Rc 形式只在单条模式的用例里，没有独立依据。第七片起覆盖统计观察 `XmadOptimizer`。它把 `xmad_mul32` / `xmad_mad32` / `xmad_muli16` 三例改写成乘法 / 乘法 + 加数，单条模式的用例和三个 `*_split` 没被改。
  - **所以那三例核的是优化遍改写后的结果，不是逐条模式的 emitter**（#1399 原先写它们独立核对各模式，说多了）。逐条模式改错、改变了 IR 形状时，改写不再发生，它们也会红；但 emitter 与优化遍的匹配一起读错时看不出。
  - 独立核对各模式的是三个 `*_split`：中间值经暂存字读回，`XmadOptimizer` 不改写，结果由 emitter 算，期望仍是普通整数算术。
  - 但**独立的只有结果 word 8–13**。暂存字（w40–w51，`xmad_muli16_split` 是其中 6 个）存的是中间值，它们的期望来自逐条模式模型（读过 `EmitXmad` 后写的），不独立。
  - 两个中间值共用一个暂存字会撞上已知红 #1414（只用一次的读被推到之后对同一地址的写后面），所以用互不相同的暂存字。

`.CSFU`（语义没有公开依据）、`.X` / `.CC`、非零常量缓冲内容不在本片，参考模型判为域外。

