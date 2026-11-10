# 写掩码为 0 的颜色目标不进帧缓冲（#468）

生产代码在 `PipelineBase`：`CreateFramebuffer`、`SetRenderTargetColorMasks`、`SetRenderTargets`、
四参数的 `ClearRenderTargetColor`，以及 `ZeroMaskWantedTargets`、`ZeroMaskTargetsChanged`、
`ZeroMaskFilteredColorView`、`ZeroMaskAttachForClear`、`ZeroMaskDetachClearTargets`。
由驱动怪癖表的 `A-ZMASK`（`graphics/vulkan/core/DriverQuirks.cs`，Arm 专有驱动）打开。

Mali-G710（驱动 r32p1）上，绑在帧缓冲里、写掩码为 0 的颜色目标经过一次带绘制的 LOAD / STORE
渲染通道后，每隔一个 16 行的 tile 行变成垃圾。现象、读数和设备范围见 `docs/坑记录.md` 同名条目。

做法：

- 主管线建帧缓冲时，写掩码为 0 的颜色目标留在外面；掩码全 0 又没有深度附件时留最低的有效槽位，
  保住帧缓冲尺寸（本后端的空帧缓冲是 1x1）。
- 掩码变化后留下的集合没变就不重建帧缓冲。
- 清除不看绘制写掩码：全掩码清除落在留在外面的目标上时，先把它放回帧缓冲（`vkCmdClearAttachments`
  的范围和层参数照旧），下一次绘制前再留到外面；部分掩码清除走清除着色器，直接画到保存的视图上
  （这一段在 `../transfer-clear-tests` 里测）。
- 被原有的重复附件过滤去掉的槽位放不回来，清除和原来一样不发。

## 主机控制流检查

```sh
python3 graphics/vulkan/runtime/zero-mask-attachment-tests/run.py \
  --dotnet /absolute/path/dotnet --deps /absolute/dir \
  --out /absolute/new-output
```

`--deps` 目录里要有 `Silk.NET.Vulkan.dll` 和 `Silk.NET.Core.dll`（2.23.0，NuGet 包里的即可）。
`run.py` 按方法签名把上面这些生产方法整段剪出来，贴进 `Fixture.cs.in` 的桩类里编译运行；被测逻辑
不复制、不改写，只有 `FramebufferParams`、渲染器、管线状态、纹理和 `CmdClearAttachments` 是记录用的桩。
绘制路径 `RecreateGraphicsPipelineIfNeeded` 太大剪不出来，`run.py` 另按文本核它恰好调一次
`ZeroMaskDetachClearTargets();`。

50 项覆盖：

- 掩码为 0 的槽位不进帧缓冲，槽位号不变；掩码变回非 0 时重建，原纹理对象回到原槽位；
- 掩码变了但留下的集合没变（F → 7）时不重建，只发状态变更；
- 掩码全 0 且有深度时不绑任何颜色目标；掩码全 0 且没有深度（或深度视图无效）时留最低的有效槽位；
- 无效视图和空槽位不会被绑，也不会被报成「留在外面」；越界槽位号回空；
- 原有的重复附件过滤照常；GPU 层重新绑目标后，保存的目标表换成新的；
- 清除落在留在外面的槽位：重建一次、该槽位进帧缓冲、`CmdClearAttachments` 带原来的层范围；同一槽位
  再清不再重建；下一次绘制前留回外面；已在帧缓冲里的槽位、空槽位、无效槽位的清除不重建；
  清除后掩码变化不提前把它留出去；重新绑目标会忘掉这个状态；
- 被重复附件过滤去掉的槽位：清除不发、不留状态；
- 负控：怪癖关、辅助管线、非 TBDR 三种情况都保持原行为（全部绑定、掩码变化和清除都不重建）；
  只有重复附件过滤生效时，不报任何槽位「留在外面」。

它不验证 GPU 像素、渲染通道边界和驱动行为。

## 旧源反证与负控

- `--source-root <旧源码根目录>`：旧实现能编过，在「zero-mask slot must be left out」这一项断言失败，
  不是编译失败（旧源码没有的方法由 `run.py` 填中性替身）。
- `--mutation`，每个只替换一处在对应方法里恰好出现一次的源码文本，都应编译成功、运行失败、退出码非 0：
  - `gate`：`dropZeroMask` 恒为假；
  - `copy`：不换到工作副本上筛（保存的目标表会被置空）；
  - `fallback`：去掉保尺寸的兜底；
  - `changed`：集合比较恒回未变；
  - `filtered`：「留在外面」的查询恒回空；
  - `clear-attach`：清除前不标记要放回的槽位；
  - `detach`：绘制前不复位；
  - `duplicate`：放不回来的槽位不撤销标记。
