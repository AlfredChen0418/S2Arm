# Windows 出包环境只读自检（#1100）

`winenv.py` 是 Windows 启动器可调用的只读清单入口，不依赖 Bash、MSYS2 或 Mac。
它不下载安装、不启动服务、不登录、不自动选择公共anisette、不配对或装机，不执行出包。
驱动就绪后会向本机usbmux只读列举设备；用户/集成方应在执行真实设备检查前明确告知。
WSL 可验证解析和注入夹具，不能替代 Windows 驱动或设备验证。

## 调用与返回

```powershell
python pack/jobs/winenv.py --json
python pack/jobs/winenv.py --request request.json
```

Python 接口：`check(request, *, probe=None)`。默认 `NativeProbe` 获取真实宿主读数；
`probe(kind, **options)` 只用于注入原始观测，不接受调用者提供 `ready` 等判绿结论。
普通 CLI 入口无需 `-I`，输出固定 UTF-8；解释器子探测使用所选 Python 的 `-I -B`。

请求为 schema 1 的 JSON 对象，未知字段、重复 JSON 键、错误类型直接拒绝：

| 字段 | 含义 |
|---|---|
| `schema` | 可省略；提供时必须为整数 1 |
| `python` | 明确选择的 Python 路径；省略用当前解释器；依赖导入和版本必须来自同一个解释器 |
| `targetDir` | 计划输出目录；有catalog时省略用其父目录（游戏库目录），否则用当前目录；未存在时只查最近存在祖先的卷，不建目录 |
| `requiredFreeBytes` | 显式出包计划预算，非负整数字节数；优先于catalog计算，存在时不读取catalog |
| `catalog` / `gameId` | 可选游戏库catalog.json路径与游戏id；两者齐全且无显式预算时，沿用library.py的selected_packages和共用预算纯函数；缺配对字段/未知游戏不判绿。三者（含 `requiredFreeBytes`）都没给 = 还没选游戏：不列 `disk` 项、不拖 `ready`，选好游戏开始前带上再查 |
| `metal` | 显式Metal路径；省略用S2A_METAL_EXE，再沿用出包目录选择：S2A_METAL_BIN → PATH metal.exe → 官方默认目录；普通非链接文件才执行有界--version |
| `shell` / `target` | 预编壳目录、出包目标（`ios` 缺省 / `mac`）；交给出包件自检（见下） |
| `anisette` | 可选对象：`source` 为 `local`、`self-hosted` 或 `public`；远程配置提供 `endpoint`，公共备选须显式 `allowPublic: true` |
| `offline` | 可选布尔值，声明离线场景；两种模式均只读本机，不发健康检查或登录请求，不省略检查项 |

远程端点只做配置形状检查：HTTPS、主机名、无用户信息/查询串/片段；不输出端点。
这是配置约束，不是anisette协议、服务身份或可用性证明。合法自建配置仍unverified；公共显选为user-selected。
请求由启动器生成；不要在配置中放账户、密码、密钥或设备号。

返回 `{schema: 1, ready: bool, checks: [...]}`。沿用 `toolchain.preflight` 的
`name/ok/message/requiredFor/group/action/help`，增加稳定 `id/status/source/detected/code/url`。
`url`：玩家自己装、许可不许随包的件（Metal 工具、Apple Devices、iCloud、Impactor）没就绪时的官方下载页，客户端渲染成按钮；其余为 null。

这是 Windows 启动器唯一的「开始前检查」（11-07 启动器线统一）：
- 出包件 `pack.<项>`（随包运行时 .NET、Git Bash、zstd，预编件、预编改写器、glslang、spirv-cross，字体、时区，预编壳…）直接取
  `pack/orch/oneclick_win.py` 的 `env_items`（即 `oneclick_win --check` 那一份），不另写规则；其中 python、metal 两行由本模块核得更细，不重复列。
- 签名装机 `impactor`：玩家自装的官方 Impactor 实测版本 + 同版本命令行 plumesign（`pack/jobs/impactor.py`、`pack/orch/install_win.py`）。
- 作废（随包运行时 + 预编件之后）：`dotnet_sdk`（玩家只要随包的 .NET 运行时）、`rewriter` / `sidecar` 的「待发布」项。

Metal 的默认目录选择与 `oneclick_win --check` 共用 `oneclick_win.metal_exe()`：`S2A_METAL_BIN` 优先；未配置时找 PATH 上 `metal.exe` 的所在目录，最后用 `C:\Program Files\Metal Developer Tools\bin`。显式目录无效时照实报缺，不偷偷换成 PATH 上另一份。出直连库时把选中目录作为 `S2A_METAL_BIN` 传给子进程，Python / C# 的 `metal.exe` 与 `metallib.exe` 都从同一目录取；不会从 PATH 混用另一份链接器。相对 PATH 条目会先转成绝对路径。请求 `metal` 与 `S2A_METAL_EXE` 仍只覆盖本次只读版本探测；要配置实际出包目录用 `S2A_METAL_BIN`。

只有 Impactor、anisette、Apple 设备服务和 USB 设备属于签名装机（`requiredFor` 为 `install`），不挡导入和出包。其余各项属于 `prepare/ipa`，`blocking` 为真（确实缺或确实不符）才挡；认不出、探测失败只提示。状态为 `ok/missing/mismatch/unverified/error/user-selected`；
只有所有必需项 `ok` 才能 `ready=true`。一个探测失败仍列出其余项；解释器失败时逐项列依赖未核实。
摘要和 JSON 使用相同状态，异常不回显命令原始输出、私有路径或配置内容。

退出码：`0` 全部就绪；`1` 有效报告但未就绪；`2` 调用/配置错误。
错误 JSON 含 `error.code=invalid_request`、`ready=false`、空 `checks`。
检查项/状态映射或固定恢复文案源违约时同为退出2，但明确`error.code=winenv_recovery_contract`，不伪装成普通请求错误或`job_failed`。

## 错误码与安装指引（#1100）

`ok`项的`code`与`action`均为null；其它状态必须有以下确定错误码，`action`来自固定可信仓内
`pack/jobs/errors.py`的`recovery(code, packaged=True)`。`help/message`保留原诊断文字，
不因显示安装方法改变探测、status或ready。即使源码环境运行，也使用packaged恢复口径，不显示开发机补依赖步骤。
固定文件用独立模块名加载，不从PATH、当前目录或调用者配置寻找同名errors模块。

| 项id | 状态 | code |
|---|---|---|
| host | mismatch | win_host_unsupported |
| python、python.依赖 | missing / mismatch / unverified | tool_missing |
| pack.<项> | missing / mismatch | tool_missing |
| impactor | missing（含缺 plumesign） | impactor_missing |
| impactor | mismatch（非实测版本） | impactor_version_untested |
| metal | missing | win_metal_missing |
| apple_driver | missing | win_apple_devices_missing |
| apple_driver | mismatch | win_apple_service_stopped |
| apple_usb | unverified | device_not_connected |
| anisette | missing | win_icloud_missing |
| anisette | mismatch / user-selected / unverified | win_anisette_source |
| disk | mismatch | disk_full |
| disk | unverified | win_disk_budget_unknown |
| 其它已知检查项（含python_requirements） | error | win_check_failed |

表外项/状态、码缺失或恢复源不可读都报具名合同错误，不调用recovery的job_failed兜底。
例如disk/missing不在本合同码表，明确违约；本片不把原探测状态改成另一个状态来凑码。
公共anisette的user-selected仍不绿；安装提示只告诉用户去哪里装/处理后点「重新检查」，本工具不安装或启服务。

RECOVERY只增加10个Windows码，已有tool_missing/device_not_connected/disk_full不另写第二份文案。
共享`launchers/shared/experience.v1.json`同步中英title/detail和来源指针，revision6→7；
next_action只指pack/jobs/errors.py，不复制恢复动作，不改Swift或出包/翻译键。
安装入口经官方资料核对：[.NET SDK](https://learn.microsoft.com/en-us/dotnet/core/install/windows)、
[Windows Metal开发者工具](https://developer.apple.com/metal/tools/)、
[Apple Devices与iTunes](https://support.apple.com/118290)、[iCloud for Windows](https://support.apple.com/103232)。

本片新增19方法，原85方法及判绿函数/NativeProbe的AST保持不变。首17方法修前35断言FAIL、0errors/skip；
只补RECOVERY未补规格时，自检1380断言中1红（缺10码）；同步后1510/1510，89码。
Windows Python3.13.5原生test_winenv完整104/104、0errors/skip；WSL Ubuntu3.14.4原生/tmp，显式
PYTHONPATH指向既有pack/jobs/tests后跑error_recovery+winenv：135发现134通过1Windows SCMskip、0errors。
以上是错误码片的历史验收：当时Windows联合命令先遇jobcase导入错误，直接入口被core无条件fcntl阻挡，
该文件0个行为用例实跑。当前文件锁片已移除这一导入障碍；新的原生行为分母与尚存POSIX失败见下节，不改写历史记录。

7/7有效撤修断言检出：撤code、撤revision递增、撤一条RECOVERY、误用开发版恢复、表外组合兜底job_failed、
USB映射错码、用安装指引覆盖help。首轮help负控在Metal项被原有help重设纠正而未红，保留为无效，
换host项后检出，未弱化生产/正测。WSL首试135例1error1skip来自临时副本safe_killpg.sh的CRLF，
仅规范临时副本sh行尾后才得上述135结果，首试原样保留。无安装下载、手机连接或Mac访问；Mac整套交维护方。

## 要求来源与探测边界

不维护第二份版本表；空间预算抽出共用纯函数，Mac适配器调用同一规则，其它Mac检查不改：

| 项目 | 唯一要求来源 / 实际读数 |
|---|---|
| Python 最低版本 | 从 `toolchain.py:preflight` 的现有 Python 比较定义读取；定义不再可识别即报错，不退到自造版本 |
| Python 依赖 | 现有 `launchers/macos/requirements-runtime.txt` 精确钉版；该文件名虽带 macos，本入口只复用其清单，不运行 Mac 工作流 |
| .NET SDK | `compat/tree/global.json` 和 `ipa.py:_sdk_ok`；运行 `dotnet --list-sdks`，运行时不能替代 SDK |
| Metal | 请求metal路径 → S2A_METAL_EXE → 共用出包目录选择（S2A_METAL_BIN → PATH → 官方默认目录）；版本命令与唯一metal_iphoneos测试锁提示 |
| Apple 支持 | SCM运行+本机27015连接即断，只证明驱动就绪；单独ListDevices确认USB数量 |
| anisette | 当前用户Store安装元数据身份与来源；显选公共备选只标user-selected，仍挡ready |
| 空间 | `shutil.disk_usage` 实际可用字节，与显式预算或library.py共用出包计划预算比较；不另写公式 |

`toolchain` / `ipa` 的整体导入会经过 `core`；当前core文件锁已跨平台，但工具执行仍有POSIX/Mac宿主依赖。
Windows环境入口仍只从固定仓内可信源
读取 Python 最低版本 AST，并提取/执行原有纯函数 `_sdk_ok`；不复制其规则，不接受配置指定代码文件。
相关共享源码字节保持不变。SDK 策略见 [.NET global.json 官方说明](https://learn.microsoft.com/en-us/dotnet/core/tools/global-json)。
SCM 查询见 [QueryServiceStatusEx](https://learn.microsoft.com/en-us/windows/win32/api/winsvc/nf-winsvc-queryservicestatusex)。

Python 和 dotnet 子探测默认各限 3 秒，标准输出限 64 KiB，关闭原始 stderr，超时/超限回收直接子进程。
所有子进程 `shell=False`，不安装包，使用 `-B` 禁写 pyc。iCloud查询通过系统Windows PowerShell的固定
`-NoProfile -NonInteractive`只读Appx脚本，不插入用户文本、不改执行策略；不是Bash/MSYS2依赖。
Python 依赖导入仍会执行已安装模块的导入代码；
这不是对任意第三方模块副作用的沙箱保证。报告文本和 detected 元数据复用 `tools/redact.py`，
显式 `key_values=()`，不读取本机 `prod.keys`。

**当前未交付组件仍挡就绪**：#1118改写器身份/能力、#1098签名组件与自建anisette协议。
文件存在不等组件可用，显选公共来源也不让ready绿。当前仍不能给出齐套绿灯。
正式出包/签名/装机/游戏验收须由各组件接入后另做，不能用夹具替代。

## 游戏库空间预算接线（#1100，同卡独立片）

例如请求`{"catalog":"D:/游戏库/catalog.json","gameId":"库中游戏id"}`：不读取游戏包正文，
只读普通非链接的catalog元数据（上限512KiB），按原`selected_packages`选最高版本本体、更新和每组DLC。
`pack_need_bytes(packages)`与Mac的`space_budget`共用：按sha去重，NSZ/XCZ登记的contentBytes与文件size取大值；
沿用原倍数、保留空间和直连额外量，不另抄公式。出包只有直连（11-07 拆通用模式），直连额外量恒计。

Windows环境入口只提取固定可信`library.py`中的纯函数和原常量，不整体导入Context/任务执行模块，配置不能指定代码源。
规则缺失/变形报具名error，不用备用公式。显式`requiredFreeBytes`优先，即使catalog损坏也不读取它；
显式`targetDir`优先，否则有catalog时查其父目录所在卷。只有gameId或catalog、未知id、空选包都不绿并说明原因；
坏JSON/重复键或游戏id、非整数/负数包大小、无法核实规则为disk error，不丢其它检查项。
两个字段都未给且无显式预算时仍unverified。报告仅freeBytes/requiredFreeBytes，不输出游戏id、标题、包hash或路径。

原创夹具：最高版base 5,000,000,000B，NSZ更新文件200,000,000B但contentBytes 3,000,000,000B，
DLC 2,700,000,000B；另一DLC与base同sha只计一次，旧版大包不入选。独立手算size=10,700,000,000B，
通用need=32,137,418,240B，直连need=36,137,418,240B，均为用例字面期望；Windows与Mac实际space_budget函数体
的控制Context/磁盘夹具对位。该函数体夹具是可移植逻辑验证，不冒称Mac宿主或Mac整套通过。

10个聚焦方法修前16处断言FAIL、0errors/skip（含旧API拒绝新增字段和Mac适配器未调共用函数），修后通过；
最终新增15方法，与旧70共85。Windows Python3.13.5原生85/85、0errors/skip；WSL Ubuntu Python3.14.4
原生/tmp运行`bash tests/winenv_test.sh`：85发现84通过、1 Windows SCMskip，0errors。
9/9机制撤修有效断言检出：压缩大小、sha去重、版本选择、Mac共用函数、Windows同源规则、显式预算优先、
默认卷、未知游戏和坏元数据。首轮预算优先负控为测试读取空detected引发1error，原记录保留且不计有效；
先补强状态断言，再撤修得到断言FAIL，未弱化生产判据。普通CLI中文空格路径/UTF8mode0与只读catalog已测。
没有真实游戏、下载安装或手机连接；Mac全pack/jobs单测及维护方coldcheck结果仍需另验，不能用这些绿例替代。

## Windows能力证据接续（#1100，同卡独立片）

Metal仅从`translator/dmsh_chain.lock`读取唯一`metal_iphoneos`，不保存第二表、不改翻译键。
`--version`成功且有`Apple metal version`头才ok；版本与测试锁不同只提示“仅需重编”，不挡ready。
缺文件、失败退出、输出error:或没有版本头为missing；锁不可读/不唯一、超时、超限仍error。
Metal的stderr仅在内存中并入同一64KiB受限版本输出，以识别error:，不原样打印或存盘。

Apple驱动项与`apple_usb`设备项分开。驱动探测只连接`127.0.0.1:27015`后断开、不发请求；设备项
另开连接发v1 plist `ListDevices`（16字节little-endian头，消息8、回显tag）。不发Listen、Connect、
配对或信任请求。收包共用3秒截止时间、整帧64KiB上限，校验长度/版本/类型/tag与清单形状；
协议错误、截断、超时不能吞成空清单。仅返回USB/Network计数，网络不替代USB；没有USB继续unverified。
设备清单可能含序列号，仅在内存解析后丢弃，不进入报告/日志；USB被列出不等配对、签名或安装成功。
协议事实参照[libusbmuxd ListDevices实现](https://github.com/libimobiledevice/libusbmuxd/blob/master/src/libusbmuxd.c)
和[帧头定义](https://github.com/libimobiledevice/usbmuxd/blob/master/src/usbmuxd-proto.h)，本片使用标准库独立实现，不安装该库。

官方iCloud当前支持的正证据：当前用户注册的包家族正好是`AppleInc.iCloud_nzyj5cx40ttqa`（末段由发布者算出，实机发布者是商店分配的编号，不是Apple Inc.）、Store签名类别、
Ok状态和有效版本元数据同时齐全。仅名称匹配或Developer/损坏包不判绿，输出只来源/版本，
不输出发布者DN、包家族、路径或用户资料。[Get-AppxPackage](https://learn.microsoft.com/en-us/powershell/module/appx/get-appxpackage)
默认只查询当前用户。未读账号、iCloud数据/密码或安装包内容。
HKLM64/32和HKCU卸载记录：DisplayName为iCloud且Publisher为Apple Inc.算官方传统安装版，判ok（11-08 实机：官方安装器装的7.21不走商店）；
其余记录且无齐全Store证据报unverified。当前未检出只代表这些查询范围，不宣称所有用户或任意路径不存在。
权限拒绝/安装查询超时为error。正安装证据使local来源ok，不证明登录或anisette生成成功；未显式来源时
先查官方local，无正证据则保持unverified等待自建协议，绝不自动公共回退。显选公共且HTTPS配置有效时
状态`user-selected`、ok=false；自建协议交付前unverified，不联网健康探测。

本片独立16方法修前22处断言FAIL、0errors，修后16/16。增补原生传输/配置边界后共70方法：
Windows Python3.13.5正常权限70/70、0skip/errors；WSL Python3.14.4原生/tmp为69通过、1 Windows SCMskip。
开发中受限Windows快照69通过1 SCM权限skip及两次未执行的审批拒绝保留；用户明确允许不连接手机的
只读检查后才完成正常权限验证。实际普通CLI14项：5ok/2missing/2mismatch/5unverified、ready=false、exit1。
Apple服务运行且端口连通，驱动项ok；实际ListDevices为USB0/Network0，设备能力仍unverified。
Metal缺失，所查当前用户Appx及卸载登记未匹配iCloud；不代表全用户/全盘无安装，真实正安装与USB正例仍未验。
9/9机制撤修有效断言检错；首次只撤CN而保留O的负控未红，已保留无效记录，未计为有效检出。
旧47例/4负控及列车12/12仅属于上一片绑定，不当本片完整Windows、真实Metal/iCloud安装或USB设备正例。

## 进程启动失败的统一边界（列车评审修订）

`Popen` 启动时的 `OSError` 家族（缺失、无执行权限/策略阻断、无效可执行文件等）
统一转换为有界 `ProbeError(missing=True)`，对应项为 `missing`，提示检查安装、权限和系统策略。
不回显系统原文，不把 `missing` 精确解释为“文件不存在”：POSIX PATH 搜索遇不可搜索目录，
即使命令名不存在也可能返回 EACCES。启动后的超时、输出超限、失败退出仍为 `error`；
SCM 权限错误也仍是 `error`。缺解释器时，各依赖继续逐项 `unverified`，其它项照查。

本修订 Windows Python3.13.5 正常权限 47/47、0 skip/errors；WSL Python3.14.4
47发现46通过1 Windows SCM skip。5个聚焦方法修前7处断言失败、0 errors，修后通过；
4/4撤修负控检出、0 errors/skip。WSL实际不可搜索PATH目录复现：旧实现 PermissionError(errno13)，
修后 ProbeError(missing=True)；另有 POSIX chmod0644 普通文件实跑。Windows普通文件实跑为无效镜像错误，
访问/策略拒绝由注入 PermissionError 覆盖，不冒称 Windows ACL 或 Mac 真机复现。

## 首版验证读数（33a，保留原绑定）

- Windows Python 3.13.5：43/43，0 skip / errors；包括正常权限 SCM 原生查询。
- WSL Ubuntu Python 3.14.4：43 发现、42 通过、1 Windows SCM skip，0 errors；在原生 `/tmp` 副本运行。
- 10 组机制撤修：10/10 触发断言失败，0 errors / skip。覆盖钉版、ready 汇总、元数据脱敏、
  解释器选择、SDK 策略、预算缺失、禁读密钥、重复配置键、公共来源显选、损坏导入分类。
- 首版 4 个真实行为缺陷修前 4 断言红，修后绿；最初缺入口日志是 0 用例导入错误，不能计为行为检出。
- 本机部分安装和 `offline=true` 两模式各列 13 项、退出 1：4 ok / 1 missing / 2 mismatch / 6 unverified。
  缺 numpy、Pillow 与钉版不符、SDK 与主线要求不符；服务运行仍未核驱动能力；空间无预算。

离线模式读数来自本机实际探测，**未禁用网络适配器、未物理断网**。无网络请求路径与未联网夹具
不能冒充物理断网验收。没有新安装/下载、手机、Mac、签名/装机/游戏验证。
贡献者入口 `bash tests/winenv_test.sh`，原生 Windows 可直接运行 `python -I -B pack/jobs/tests/test_winenv.py`。



## pack/jobs 在 Windows 上的单测（#1100 文件锁片）

`core` 不再无条件导入 `fcntl`。`filelock.try_lock(f)` / `unlock(f)` 只用标准库：
POSIX 保留 `flock(LOCK_EX | LOCK_NB)` / `LOCK_UN`；Windows先seek(0)，
用msvcrt的LK_NBLCK/LK_UNLCK锁住0起1字节，不写占位内容，忙锁EACCES/EDEADLK统一抛BlockingIOError，
其它错误保留。Context仍报「另一个任务正在使用游戏库，请稍后重试。」；未改进程执行/信号/出包键/翻译指纹。
区域可越EOF的标准库定义见[Python msvcrt](https://docs.python.org/3/library/msvcrt.html#msvcrt.locking)。

新增test_filelock.py由现有tests/pack_jobs_test.sh的`test_*.py`收取；改测试目录会全收，不加第7个门禁文件。
跨进程使用subprocess+同一sys.executable，不fork；覆盖持锁拒绝、显式释放、进程退出自动释放、Context原文、
中文空格路径、非零文件指针仍锁字节0、空文件无改写以及Windows忙锁/其它errno边界。
原test_jobs/test_saves仅替换既有持锁片段，WSL中这两个原锁用例均通过。

先红：独立core导入判据1方法1断言FAIL、0errors；最初缺filelock.py时8个方法均在setUp导入失败，
0个新锁行为实际执行，不把8errors当机制检出。修后Windows聚焦9/9、0errors/skip。
撤修2/2有效断言检出、0errors/skip：core改回fcntl导入→独立导入判据FAIL；Windows不转换忙锁异常→Context原Failure判据FAIL。

验收命令是`python -m unittest discover -s pack/jobs/tests`，以下读数来自同一发现范围的unittest结果记录器，
原生Windows Python3.13.5正常权限、WSL Ubuntu Python3.14.4原生/tmp（仅临时副本sh归一LF）。
记录器不改测试、断言、跳过或被测代码；失败/错误含subTest记录，不能直接与方法数相加。
初次全量观察与后续编辑重叠，单独保留为未绑定观察，不拿作干净main A/B或下表最终分母。

| 宿主 | 发现方法 | 完整通过方法 | 非通过方法 | skip | FAIL记录 | error记录 | failed module / 导入异常 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Windows | 473 | 313 | 147 | 13 | 89 | 85 | 0 / 0 |
| WSL | 473 | 441 | 17 | 15 | 3 | 14 | 0 / 0 |

| 三份合同文件 | Windows：方法/通过/FAIL/error/skip | WSL：方法/通过/FAIL/error/skip |
|---|---|---|
| test_filelock.py | 9/9/0/0/0 | 9/8/0/0/1 |
| test_error_recovery.py | 31/18/9/4/0 | 31/31/0/0/0 |
| test_winenv.py | 104/104/0/0/0 | 104/103/0/0/1 |

Windows锁与winenv通过，导入错误0已实证；**Windows error_recovery尚未全绿**，31方法18通过、9FAIL/4error。
真实单跑回调取消用例1例1error：core.run清理经signal_child调用`/bin/ps`，Windows CreateProcess WinError2。
其余原行为还涉及0600/uid/Unix信号、O_NOFOLLOW、符号链接、shebang/Apple工具和APFS等；本片未改这些范围外机制，
不一概断言所有失败同因。合同“三份全过”与“不改进程执行”交界已向维护方带实证对齐，未通过前保持Draft，
不跳过失败或以WSL冒充Windows。WSL三份无FAIL/errors，Windows适配/SCM各1skip按宿主保留；总套件也非全绿。
Mac整套由维护方代验，本机未访问Mac；当前无Mac通过回执，不沿用旧PR的非阻挡裁定。

逐文件列出本次其余红项；“FAIL/error”为unittest记录数（含subTest），首条信息只摘类型/诊断并去临时个人路径：

| 宿主 / 文件 | 方法 | 通过 | FAIL/error | 首条错误或断言 |
|---|---:|---:|---:|---|
| Windows / test_archs.py | 19 | 17 | 3/0 | AssertionError: '【红】' not found in '\\u3010\\u7ea2\\u3011\\u51fa\\u5305\\u76ee\\u6807\\uff1a\\u7528\\u6237\\u5305\\u8981\\u76ee\\u6807\\u… |
| Windows / test_cache_retention.py | 26 | 20 | 0/6 | NotImplementedError: utime: follow_symlinks unavailable on this platform |
| Windows / test_control_plane.py | 32 | 6 | 21/5 | SystemExit: request must be owned by this user and private (0600) |
| Windows / test_devicectl.py | 2 | 0 | 0/2 | FileNotFoundError: [WinError 2] The system cannot find the file specified |
| Windows / test_engine_git.py | 4 | 2 | 0/2 | TypeError: argument of type 'NoneType' is not iterable |
| Windows / test_error_recovery.py | 31 | 18 | 9/4 | FileNotFoundError: [WinError 2] The system cannot find the file specified |
| Windows / test_import.py | 2 | 0 | 2/0 | AssertionError: [] is not true : request must be owned by this user and private (0600) |
| Windows / test_install.py | 18 | 12 | 1/5 | FileNotFoundError: [WinError 2] The system cannot find the file specified |
| Windows / test_ipa_graphics.py | 9 | 8 | 3/0 | AssertionError: 1 != 2 : request must be owned by this user and private (0600) |
| Windows / test_job_failure_codes.py | 18 | 9 | 27/1 | OSError: [WinError 193] %1 is not a valid Win32 application |
| Windows / test_jobs.py | 76 | 53 | 7/17 | FileNotFoundError: [WinError 2] The system cannot find the file specified |
| Windows / test_library_volume.py | 4 | 3 | 1/0 | AssertionError: '' != 'apfs' |
| Windows / test_save_formats.py | 24 | 11 | 14/3 | ValueError: 认不出布局（JKSV / 裸文件路径里没有程序号），须指明程序号 |
| Windows / test_saves.py | 44 | 6 | 1/37 | AttributeError: module 'os' has no attribute 'O_NOFOLLOW' |
| Windows / test_target.py | 7 | 4 | 0/3 | KeyError: 'DEVELOPER_DIR' |
| WSL / test_devicectl.py | 2 | 1 | 0/1 | FileNotFoundError: [Errno 2] No such file or directory: '<synthetic-temp>' |
| WSL / test_install.py | 18 | 14 | 0/4 | FileNotFoundError: [Errno 2] No such file or directory: '/usr/bin/ditto' |
| WSL / test_jobs.py | 76 | 68 | 2/6 | KeyError: 'DEVELOPER_DIR' |
| WSL / test_library_volume.py | 4 | 3 | 1/0 | AssertionError: '' != 'apfs' |
| WSL / test_target.py | 7 | 4 | 0/3 | KeyError: 'DEVELOPER_DIR' |

以上仅Windows/WSL读取合成夹具和既有只读检查；没有安装下载、真实游戏/密钥输入、手机连接或签名装机。

## Windows 子进程、请求隐私与目录句柄（#1100 接续片）

合同6598603323经6601700216明确为7文件，无额外登记；6602698737随后明确批准第8文件jobcase.py的Windows夹具及reader I/O注入边界。只使用标准库ctypes调用Windows API，
POSIX继续原路径；不是整卡齐套出包或设备正例验收。当前尚在独立分支开发，原生31方法的先前阻挡与最终口径分段保留。

- 请求普通文件/非重解析点，按已打开句柄查询owner与DACL；owner必须是当前TokenUser，
  用户/SYSTEM/Administrators外有读ACE、未知ACE或查询失败均不能判私有。`GetSecurityInfo`对固定句柄查询，
  没有按可替换路径重新读取权限，原SystemExit文案保留。
- 工具以CREATE_NEW_PROCESS_GROUP | CREATE_SUSPENDED创建，专属Job启用KILL_ON_JOB_CLOSE。
  Assign成功后才恢复初始线程；分配失败杀掉精确Popen子进程并关作业，工具代码不能先跑出孙进程。
  Windows必须先按任务env的PATH解析工具，不能假设CreateProcess会用传入env的PATH搜可执行文件。
- TERM/KILL只终止所持Job；正常退出、取消及异常时按Job枚举/终止残留并关闭句柄，作业外对照不可伤。
  cancel用GetProcessTimes创建时间比对，缺失/伪造拒job_pid_reused；持同一验证进程句柄并对该句柄
  TerminateProcess，不先关后按数字PID重开。拒权限为job_not_ours。
- G只适配_rm_direct_transient及diagnostics两函数：逐级打开不跟随重解析点的目录句柄，核验属性，
  相对叶项通过NtCreateFile(RootDirectory)打开并再次拒重解析点；枚举、读、删除均基于已持有对象，
  删除用SetFileInformationByHandle的删除标记，不回到拼接路径。句柄不共享DELETE，读取期间替换/重命名拒绝；
  文件先stat后open的身份差异仍沿诊断原断言拒绝。UNC/设备路径本片明确拒绝，不冒称已支持。

原生Windows Python3.13.5：固定领取基点072cf4fdb的独立原机制3方法修前3断言FAIL、0errors/skip；
首轮捕获只期待目标异常导致1FAIL2行为error也原样保留，随后把实际异常作为值断言检出，没有算setup错为有效红。
A/D/E三项私有撤修各1方法1断言FAIL、0errors/skip。工具PATH独立1方法修前FAIL→修后通过。
最新聚焦数与源码SHA256存本地1100-winproc-*证据；不是Mac宿主。测试仅原创请求、临时ACL和持有的父孙/对照进程，
无手机、游戏、安装、配对或签名操作。16方法初轮包含3个继承重复的原机制方法，分母按unittest实际方法ID计，非16种互不相同的机制。

**夹具扩写批准前的原生error_recovery历史**：31方法原18完整过9FAIL4error→当前20完整过9FAIL1error，
另1方法只有“Windows无执行权限位”独立subTest跳过，后续同工具恢复启动已执行。
9FAIL为原jobcase生成请求的继承DACL不符A，必须拒；1error为旧reader I/O mock在CREATE_SUSPENDED
构造器返回前等待退出。只在诊断私有副本给请求私有ACL后，-I子进程中文JSON受cp1252阻挡；
再固定测试子进程-X utf8后25完整过/4FAIL1error，4红仍涉及POSIX shebang合成dotnet读取器。
这些诊断副本不计正式验收，不改本支jobcase.py或放宽生产A/B；已在6602548384请求精确夹具写集，当时未获准前未扩写；6602698737随后明确允许，见下段。

WSL Ubuntu Python3.14.4原生/tmp（仅临时sh转LF）首轮489方法444完整过、3FAIL13error29skip，
导入错误0；error_recovery31/31、filelock8过1Windows适配skip、winenv103过1SCMskip。
该轮新增winproc16方法2过14Windows宿主skip。相对已归档473/441过3FAIL14error15skip，
旧文件逐项FAIL/error不增：devicectl0/1、install0/4、jobs2/5（原2/6）、library_volume1/0、target0/3。
原jobs.test_seed_stage_keeps_dlc_and_does_not_modify_input本轮由error到通过，但尚未干净main A/B归因，不冒称全由本片修好。
新方法数随后增加的Windows专属边界不据此改写这轮全量绑定。Mac整套由维护方代验，本机未访问Mac。

setup失败另保留：首次基线打包含不存在路径，0行为执行；首次跨调用复用WSL/tmp树已消失，
0项lint执行。重新同调用建树再检，不把setup错误当机制负控或产品失败。

### 第8测试文件的明确批准与复核

合入线6602698737允许jobcase.py仅Windows请求ACL/UTF8/合成读取器及reader I/O注入边界，
POSIX原夹具保留。合成请求用带私有安全描述符的CreateFileW(CREATE_NEW)直接创建，
不先公开创建再改ACL；写完原子替换。-I的Windows worker显式-X utf8，捕获也明确UTF8，
不修改用户环境/区域设置。合成读取器只复制既有解释器/DLL至测试临时目录并运行原创Python脚本，
不是真.NET，未下载、安装或发布这些本机派生夹具。reader I/O故障在winproc.popen归属Job并恢复后注入，
EIO传播及children清除原断言保持，生产A/B没有放宽。

扩写后原生error_recovery发现31方法，0FAIL/0errors：30方法完整成功，另1方法含唯一许可的
“Windows无执行权限位”subTest skip，随后同方法的恢复启动仍执行通过。不能写成0skip。
最终新增边界22/22、原文件锁9/9，分别有正常权限读数，0FAIL/errors/skip。
WSL聚焦winproc22+error_recovery31+filelock9共62发现、41过21宿主skip、0FAIL/errors。
安全入口killpg_lint与shell_lint退出0；weak_link_decl首次退出1，后续使用关闭换行转换的真实Git基线/本支对照及完整contrib结果另列，
不为了绿修改范围外原脚本/断言或安装工具。首次weak对照未归一临时sh，两个setup退出2另留，不计生产判据通过。

最终两宿主组合：Windows新winproc22+error_recovery31+filelock9共62发现，61完整过，
仅1个许可subTest skip、0FAIL/errors；其中权限恢复方法的其它步骤已执行。WSL最终8文件discover
495发现、444完整过、3FAIL13errors35skip、导入0，按文件核原失败不增加；不是全绿。
后续仅把4条Windows私有性夹具改为创建即私有，这4条原生定向4/4绿；未改WSL实际执行路径，
不将该495全量说成之后每个新头都重新执行。

最终A撤修原9个worker方法加独立私有请求判据，共10方法10断言FAIL、0errors/skip；D与E各1断言FAIL、0errors/skip。
第一次扩A到10方法时私有副本缺完整engine源码，9FAIL1setup error无效，补齐原源码依赖后才计有效，原记录保留。
killpg_lint、shell_lint退出0。weak_link_decl早期archive展开后只归一sh，两个输入各5/7过、2红；
这不是干净Git checkout证据，其中手写参数注入红受Makefile换行转换影响，不作为产品主线红。
改用已同步真实refs、core.autocrlf=false的干净Git clone后，固定main072cf与实现头6c0bfcf84
同机各7判据6过1红，仅Python/make host放行参数对位未过，手写注入负例已经通过。
原两红、setup退出2及最终一红日志全部保留。此对照只覆盖该入口，不能给其它contrib红免责，
不修改共享入口/阈值；本片仍需维护方裁定/Mac复验。

实现头6c0bfcf84的完整贡献者自检原摘要：tool档8文件，36应跑/34实跑、23绿11红、2缺工具，
80秒exit1。红addr_need/app_only/census/direct_profile/make_tzdata/npdm_isa/oneclick_census/
oneclick/pack_contracts/pack_jobs/space_path；缺save_transfer_interop(.NET10)、userenv固定依赖，
Mac launchers/macos/gate.sh及第二层未跑。没有逐项全量干净main A/B，不称11红全部同因。
同头pre合真实main5f19cf77a无冲突、4/4绿29秒；公开增量新增0/未覆盖0/未审类型0，库存182，
扫描器绿不等整仓可公开。此后若仅改本文验证读数，保留实现头输入绑定，不冒称新头整套又跑。

### E负控物理断言加固

交付后复核发现，最初E撤修先在返回名称列表为空处FAIL，尚未命中后续孙进程存活断言。
原记录保留为API输出断言检出，不再将它算作合同要求的物理收尾负控。
现先检查实际孙进程已退出，再核返回列表：同一实际进程夹具定向1/1通过；撤掉E收尾后，
“owned grandchild survived”明确1方法1断言FAIL、0errors/skip，随后由测试持有作业回收。
生产实现不变，A/D负控不变。Windows62组合保留09e输入绑定，最新仅本E方法重新执行；
不称每个文档/测试顺序头均全套另跑。
