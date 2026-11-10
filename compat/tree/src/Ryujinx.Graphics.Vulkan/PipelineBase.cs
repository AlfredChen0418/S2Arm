using Ryujinx.Common.Memory;
using Ryujinx.Graphics.GAL;
using Ryujinx.Graphics.Shader;
using S2Arm.Backends.Vulkan.ProgramCache;
using Silk.NET.Vulkan;
using System;
using System.Collections.Generic;
using System.Linq;
using System.Numerics;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Threading.Tasks;
using BlendOp = Silk.NET.Vulkan.BlendOp;
using Buffer = Silk.NET.Vulkan.Buffer;
using CompareOp = Ryujinx.Graphics.GAL.CompareOp;
using FrontFace = Ryujinx.Graphics.GAL.FrontFace;
using IndexType = Ryujinx.Graphics.GAL.IndexType;
using PolygonMode = Ryujinx.Graphics.GAL.PolygonMode;
using PrimitiveTopology = Ryujinx.Graphics.GAL.PrimitiveTopology;
using Viewport = Ryujinx.Graphics.GAL.Viewport;

namespace Ryujinx.Graphics.Vulkan
{
    /// <summary>
    /// S2A：驱动管线缓存落盘的先后（拆线程收局请求时提前落盘的后台写，可能与 Dispose 时的再存并发，复审第 2 轮 P2-C）：快照按取的先后编号；
    /// 写盘串行（两次 TryWriteAtomic 不并发 rename）；不新于已写成版本的快照跳过，旧快照不覆盖新快照。
    /// </summary>
    public sealed class S2AVersionedSave
    {
        private readonly object _gate = new();
        private long _taken, _written = -1;

        /// <summary>取快照时调：返回这份快照的版本（单调递增）</summary>
        public long Take() => System.Threading.Interlocked.Increment(ref _taken);

        /// <summary>已写成的最新版本（-1 = 还没写成过）</summary>
        public long Written => System.Threading.Interlocked.Read(ref _written);

        /// <summary>串行写盘：version 不新于已写成的就不写（skipped，返回 true——盘上已是同样新或更新的快照）；否则调 write，成功记下版本</summary>
        public bool Write(long version, Func<bool> write, out bool skipped)
        {
            lock (_gate)
            {
                skipped = version <= _written;
                if (skipped)
                {
                    return true;
                }

                if (!write())
                {
                    return false;
                }

                System.Threading.Interlocked.Exchange(ref _written, version);

                return true;
            }
        }

        /// <summary>
        /// 写盘回执的等待口径：收局路径 bounded——有界等（≤ milliseconds），退出期限到没写完交后台、旧文件保留；
        /// 定期落盘不界等——调用方在编译通道上，等写盘会让通道上排队的新管线白等，后台写结果只记日志。
        /// </summary>
        public static bool AwaitReceipt(Task<bool> save, int milliseconds, string label, bool bounded)
        {
            if (!bounded) return true;
            try
            {
                if (!save.Wait(milliseconds))
                {
                    Console.Error.WriteLine($"[vkpipe] 驱动缓存写盘仍在后台{label}（退出期限已到，旧文件保留）");
                    return false;
                }
                return save.Result;
            }
            catch (AggregateException e)
            {
                Console.Error.WriteLine($"[vkpipe] 驱动管线缓存写盘异常{label}：{e.GetBaseException().Message}");
                return false;
            }
        }
    }

    class PipelineBase : IDisposable
    {
        public const int DescriptorSetLayouts = 4;

        public const int UniformSetIndex = 0;
        public const int StorageSetIndex = 1;
        public const int TextureSetIndex = 2;
        public const int ImageSetIndex = 3;

        protected readonly VulkanRenderer Gd;
        protected readonly Device Device;
        public readonly PipelineCache PipelineCache;

        private readonly bool _s2aOwnsPipelineCache;
        private readonly PipelineCacheIdentity _s2aPipelineCacheIdentity;
        private readonly string _s2aPipelineCachePath;
        private bool _s2aDisposed;

        public readonly AutoFlushCounter AutoFlush;
        public readonly Action EndRenderPassDelegate;

        protected PipelineDynamicState DynamicState;
        protected bool IsMainPipeline;
        private PipelineState _newState;
        private bool _graphicsStateDirty;
        private bool _computeStateDirty;
        private bool _bindingBarriersDirty;
        private PrimitiveTopology _topology;

        private ulong _currentPipelineHandle;

        protected Auto<DisposablePipeline> Pipeline;

        protected PipelineBindPoint Pbp;

        protected CommandBufferScoped Cbs;
        protected CommandBufferScoped? PreloadCbs;
        protected CommandBuffer CommandBuffer;

        public CommandBufferScoped CurrentCommandBuffer => Cbs;

        private ShaderCollection _program;

        protected FramebufferParams FramebufferParams;
        private Auto<DisposableFramebuffer> _framebuffer;
        private RenderPassHolder _rpHolder;
        private Auto<DisposableRenderPass> _renderPass;
        private RenderPassHolder _nullRenderPass;
        private int _writtenAttachmentCount;

        private bool _framebufferUsingColorWriteMask;

        private ITexture[] _preMaskColors;
        private ITexture _preMaskDepthStencil;

        // S2A（#468）：见 ZeroMaskWantedTargets。_zeroMaskScratch 是筛目标用的工作副本，免得每次建帧缓冲都分配；
        // _zeroMaskClearTargets 是为清除临时放回帧缓冲的槽位，下一次绘制前复位。
        private readonly ITexture[] _zeroMaskScratch = new ITexture[Constants.MaxRenderTargets];
        private uint _zeroMaskClearTargets;

        private readonly DescriptorSetUpdater _descriptorSetUpdater;

        private IndexBufferState _indexBuffer;
        private IndexBufferPattern _indexBufferPattern;
        private readonly BufferState[] _transformFeedbackBuffers;
        private readonly VertexBufferState[] _vertexBuffers;
        private ulong _vertexBuffersDirty;
        protected Rectangle<int> ClearScissor;

        private readonly VertexBufferUpdater _vertexBufferUpdater;

        public IndexBufferPattern QuadsToTrisPattern;
        public IndexBufferPattern TriFanToTrisPattern;

        private bool _needsIndexBufferRebind;
        private bool _needsTransformFeedbackBuffersRebind;

        private bool _tfEnabled;
        private bool _tfActive;

        private FeedbackLoopAspects _feedbackLoop;
        private bool _passWritesDepthStencil;

        private readonly PipelineColorBlendAttachmentState[] _storedBlend;
        public ulong DrawCount { get; private set; }
        public bool RenderPassActive { get; private set; }

        public unsafe PipelineBase(VulkanRenderer gd, Device device, PipelineCache? sharedCache = null)
        {
            Gd = gd;
            Device = device;

            AutoFlush = new AutoFlushCounter(gd);
            EndRenderPassDelegate = EndRenderPass;

            _s2aOwnsPipelineCache = !sharedCache.HasValue;
            if (sharedCache.HasValue)
            {
                // Helper pipelines share the main device cache. One VkPipelineCache, one
                // compile lane, one persistence file; helper results are not overwritten.
                PipelineCache = sharedCache.Value;
            }
            else
            {
                _s2aPipelineCachePath = gd.S2APipelineCachePath;
                _s2aPipelineCacheIdentity = S2ACacheIdentity(gd);

                // S2A：上个进程被杀留下的落盘临时文件（*.tmp.<pid>.*）在这里清——只删进程号已证实不在的；
                // 本进程号的一律不动：进程内重建会话时旧会话的后台写可能还在进行，删了会让它 rename 失败。
                if (_s2aPipelineCachePath != null)
                {
                    int orphans = PipelineCacheFile.CleanOrphanedTemporaries(_s2aPipelineCachePath);
                    if (orphans > 0)
                        Console.Error.WriteLine($"[vkpipe] 清掉上个进程留下的落盘临时文件 {orphans} 个");
                }

                byte[] initialData = null;
                string readError = null;
                if (_s2aPipelineCachePath != null &&
                    PipelineCacheFile.TryRead(_s2aPipelineCachePath, _s2aPipelineCacheIdentity, out byte[] cached, out readError))
                {
                    initialData = cached;
                    S2APipelineDiagnostics.ReportCacheImport(cached.Length, null);
                    Console.Error.WriteLine($"[vkpipe] 导入驱动管线缓存 {cached.Length} 字节");
                }
                else if (_s2aPipelineCachePath != null)
                {
                    S2APipelineDiagnostics.ReportCacheImport(0, readError);
                    Console.Error.WriteLine($"[vkpipe] 驱动管线缓存未导入：{readError}");
                }

                PipelineCacheCreateInfo pipelineCacheCreateInfo = new()
                {
                    SType = StructureType.PipelineCacheCreateInfo,
                };

                if (initialData != null)
                {
                    fixed (byte* pInitialData = initialData)
                    {
                        pipelineCacheCreateInfo.InitialDataSize = (nuint)initialData.Length;
                        pipelineCacheCreateInfo.PInitialData = pInitialData;
                        Result result = gd.Api.CreatePipelineCache(device, in pipelineCacheCreateInfo, null, out PipelineCache);
                        if (result.IsError)
                        {
                            S2APipelineDiagnostics.ReportCacheImport(0, "DRIVER_REJECTED: " + result);
                            Console.Error.WriteLine($"[vkpipe] 导入的驱动缓存被拒（{result}），改用空缓存");
                            pipelineCacheCreateInfo.InitialDataSize = 0;
                            pipelineCacheCreateInfo.PInitialData = null;
                            gd.Api.CreatePipelineCache(device, in pipelineCacheCreateInfo, null, out PipelineCache).ThrowOnError();
                        }
                    }
                }
                else
                {
                    gd.Api.CreatePipelineCache(device, in pipelineCacheCreateInfo, null, out PipelineCache).ThrowOnError();
                }
            }

            _descriptorSetUpdater = new DescriptorSetUpdater(gd, device);
            _vertexBufferUpdater = new VertexBufferUpdater(gd);

            _transformFeedbackBuffers = new BufferState[Constants.MaxTransformFeedbackBuffers];
            _vertexBuffers = new VertexBufferState[Constants.MaxVertexBuffers + 1];

            const int EmptyVbSize = 16;

            using BufferHolder emptyVb = gd.BufferManager.Create(gd, EmptyVbSize);
            emptyVb.SetData(0, new byte[EmptyVbSize]);
            _vertexBuffers[0] = new VertexBufferState(emptyVb.GetBuffer(), 0, 0, EmptyVbSize);
            _vertexBuffersDirty = ulong.MaxValue >> (64 - _vertexBuffers.Length);

            ClearScissor = new Rectangle<int>(0, 0, 0xffff, 0xffff);

            _storedBlend = new PipelineColorBlendAttachmentState[Constants.MaxRenderTargets];

            _newState.Initialize();
        }

        public void Initialize()
        {
            _descriptorSetUpdater.Initialize(IsMainPipeline);

            QuadsToTrisPattern = new IndexBufferPattern(Gd, 4, 6, 0, [0, 1, 2, 0, 2, 3], 4, false);
            TriFanToTrisPattern = new IndexBufferPattern(Gd, 3, 3, 2, [int.MinValue, -1, 0], 1, true);
        }

        public unsafe void Barrier()
        {
            Gd.Barriers.QueueMemoryBarrier();
        }

        public void ComputeBarrier()
        {
            MemoryBarrier memoryBarrier = new()
            {
                SType = StructureType.MemoryBarrier,
                SrcAccessMask = AccessFlags.MemoryReadBit | AccessFlags.MemoryWriteBit,
                DstAccessMask = AccessFlags.MemoryReadBit | AccessFlags.MemoryWriteBit,
            };

            Gd.Api.CmdPipelineBarrier(
                CommandBuffer,
                PipelineStageFlags.ComputeShaderBit,
                PipelineStageFlags.AllCommandsBit,
                0,
                1,
                new ReadOnlySpan<MemoryBarrier>(in memoryBarrier),
                0,
                ReadOnlySpan<BufferMemoryBarrier>.Empty,
                0,
                ReadOnlySpan<ImageMemoryBarrier>.Empty);
        }

        public void BeginTransformFeedback(PrimitiveTopology topology)
        {
            Gd.Barriers.EnableTfbBarriers(true);
            _tfEnabled = true;
        }

        public void ClearBuffer(BufferHandle destination, int offset, int size, uint value)
        {
            EndRenderPass();

            Buffer dst = Gd.BufferManager.GetBuffer(CommandBuffer, destination, offset, size, true).Get(Cbs, offset, size, true).Value;

            BufferHolder.InsertBufferBarrier(
                Gd,
                Cbs.CommandBuffer,
                dst,
                BufferHolder.DefaultAccessFlags,
                AccessFlags.TransferWriteBit,
                PipelineStageFlags.AllCommandsBit,
                PipelineStageFlags.TransferBit,
                offset,
                size);

            Gd.Api.CmdFillBuffer(CommandBuffer, dst, (ulong)offset, (ulong)size, value);

            BufferHolder.InsertBufferBarrier(
                Gd,
                Cbs.CommandBuffer,
                dst,
                AccessFlags.TransferWriteBit,
                BufferHolder.DefaultAccessFlags,
                PipelineStageFlags.TransferBit,
                PipelineStageFlags.AllCommandsBit,
                offset,
                size);
        }

        public unsafe void ClearRenderTargetColor(int index, int layer, int layerCount, ColorF color)
        {
            ZeroMaskAttachForClear(index);

            if (FramebufferParams == null || !FramebufferParams.IsValidColorAttachment(index))
            {
                return;
            }

            if (_renderPass == null)
            {
                CreateRenderPass();
            }

            Gd.Barriers.Flush(Cbs, RenderPassActive, _rpHolder, EndRenderPassDelegate);

            BeginRenderPass();

            ClearValue clearValue = new(new ClearColorValue(color.Red, color.Green, color.Blue, color.Alpha));
            ClearAttachment attachment = new(ImageAspectFlags.ColorBit, (uint)index, clearValue);
            ClearRect clearRect = FramebufferParams.GetClearRect(ClearScissor, layer, layerCount);

            Gd.Api.CmdClearAttachments(CommandBuffer, 1, &attachment, 1, &clearRect);
        }

        public unsafe void ClearRenderTargetDepthStencil(int layer, int layerCount, float depthValue, bool depthMask, int stencilValue, bool stencilMask)
        {
            if (FramebufferParams == null || !FramebufferParams.HasDepthStencil)
            {
                return;
            }

            ClearValue clearValue = new(null, new ClearDepthStencilValue(depthValue, (uint)stencilValue));
            ImageAspectFlags flags = depthMask ? ImageAspectFlags.DepthBit : 0;

            if (stencilMask)
            {
                flags |= ImageAspectFlags.StencilBit;
            }

            flags &= FramebufferParams.GetDepthStencilAspectFlags();

            if (flags == ImageAspectFlags.None)
            {
                return;
            }

            if (_renderPass == null)
            {
                CreateRenderPass();
            }

            Gd.Barriers.Flush(Cbs, RenderPassActive, _rpHolder, EndRenderPassDelegate);

            BeginRenderPass();

            ClearAttachment attachment = new(flags, 0, clearValue);
            ClearRect clearRect = FramebufferParams.GetClearRect(ClearScissor, layer, layerCount);

            Gd.Api.CmdClearAttachments(CommandBuffer, 1, &attachment, 1, &clearRect);
        }

        public unsafe void CommandBufferBarrier()
        {
            Gd.Barriers.QueueCommandBufferBarrier();
        }

        public void CopyBuffer(BufferHandle source, BufferHandle destination, int srcOffset, int dstOffset, int size)
        {
            EndRenderPass();

            Auto<DisposableBuffer> src = Gd.BufferManager.GetBuffer(CommandBuffer, source, srcOffset, size, false);
            Auto<DisposableBuffer> dst = Gd.BufferManager.GetBuffer(CommandBuffer, destination, dstOffset, size, true);

            BufferHolder.Copy(Gd, Cbs, src, dst, srcOffset, dstOffset, size);
        }

        public void DirtyVertexBuffer(Auto<DisposableBuffer> buffer)
        {
            for (int i = 0; i < _vertexBuffers.Length; i++)
            {
                if (_vertexBuffers[i].BoundEquals(buffer))
                {
                    _vertexBuffersDirty |= 1UL << i;
                }
            }
        }

        public void DirtyIndexBuffer(Auto<DisposableBuffer> buffer)
        {
            if (_indexBuffer.BoundEquals(buffer))
            {
                _needsIndexBufferRebind = true;
            }
        }

        public void DispatchCompute(int groupsX, int groupsY, int groupsZ)
        {
            if (!_program.IsLinked)
            {
                S2APipelineDiagnostics.ReportUnlinkedSkip(
                    PipelineBindPoint.Compute, "DispatchCompute", _newState.S2AKeyString(), _program.S2AShaderHashes);
                return;
            }

            EndRenderPass();
            RecreateComputePipelineIfNeeded();

            Gd.Api.CmdDispatch(CommandBuffer, (uint)groupsX, (uint)groupsY, (uint)groupsZ);

            // 修①：dispatch 记录完成——绑定阶段挂的 texel 写回账从此刻起生效可落。
            Gd.BufferManager.PromotePendingAlignedTexelWritebacks();
        }

        public void DispatchComputeIndirect(Auto<DisposableBuffer> indirectBuffer, int indirectBufferOffset)
        {
            if (!_program.IsLinked)
            {
                S2APipelineDiagnostics.ReportUnlinkedSkip(
                    PipelineBindPoint.Compute, "DispatchComputeIndirect", _newState.S2AKeyString(), _program.S2AShaderHashes);
                return;
            }

            EndRenderPass();
            RecreateComputePipelineIfNeeded();

            Gd.Api.CmdDispatchIndirect(CommandBuffer, indirectBuffer.Get(Cbs, indirectBufferOffset, 12).Value, (ulong)indirectBufferOffset);

            Gd.BufferManager.PromotePendingAlignedTexelWritebacks();
        }

        public void Draw(int vertexCount, int instanceCount, int firstVertex, int firstInstance)
        {
            if (vertexCount == 0)
            {
                return;
            }

            if (!RecreateGraphicsPipelineIfNeeded())
            {
                return;
            }

            BeginRenderPass();
            DrawCount++;

            if (Gd.TopologyUnsupported(_topology))
            {
                // Temporarily bind a conversion pattern as an index buffer.
                _needsIndexBufferRebind = true;

                IndexBufferPattern pattern = _topology switch
                {
                    PrimitiveTopology.Quads => QuadsToTrisPattern,
                    PrimitiveTopology.TriangleFan or
                    PrimitiveTopology.Polygon => TriFanToTrisPattern,
                    _ => throw new NotSupportedException($"Unsupported topology: {_topology}"),
                };

                BufferHandle handle = pattern.GetRepeatingBuffer(vertexCount, out int indexCount);
                Auto<DisposableBuffer> buffer = Gd.BufferManager.GetBuffer(CommandBuffer, handle, false);

                Gd.Api.CmdBindIndexBuffer(CommandBuffer, buffer.Get(Cbs, 0, indexCount * sizeof(int)).Value, 0, Silk.NET.Vulkan.IndexType.Uint32);

                BeginRenderPass(); // May have been interrupted to set buffer data.
                ResumeTransformFeedbackInternal();

                Gd.Api.CmdDrawIndexed(CommandBuffer, (uint)indexCount, (uint)instanceCount, 0, firstVertex, (uint)firstInstance);
            }
            else
            {
                ResumeTransformFeedbackInternal();

                Gd.Api.CmdDraw(CommandBuffer, (uint)vertexCount, (uint)instanceCount, (uint)firstVertex, (uint)firstInstance);
            }

            // 修①：draw 记录完成——绑定阶段挂的 texel 写回账从此刻起生效可落。
            Gd.BufferManager.PromotePendingAlignedTexelWritebacks();
        }

        private void UpdateIndexBufferPattern()
        {
            IndexBufferPattern pattern = null;

            if (Gd.TopologyUnsupported(_topology))
            {
                pattern = _topology switch
                {
                    PrimitiveTopology.Quads => QuadsToTrisPattern,
                    PrimitiveTopology.TriangleFan or
                    PrimitiveTopology.Polygon => TriFanToTrisPattern,
                    _ => throw new NotSupportedException($"Unsupported topology: {_topology}"),
                };
            }

            if (_indexBufferPattern != pattern)
            {
                _indexBufferPattern = pattern;
                _needsIndexBufferRebind = true;
            }
        }

        public void DrawIndexed(int indexCount, int instanceCount, int firstIndex, int firstVertex, int firstInstance)
        {
            if (indexCount == 0)
            {
                return;
            }

            UpdateIndexBufferPattern();

            if (!RecreateGraphicsPipelineIfNeeded())
            {
                return;
            }

            BeginRenderPass();
            DrawCount++;

            if (_indexBufferPattern != null)
            {
                // Convert the index buffer into a supported topology.
                IndexBufferPattern pattern = _indexBufferPattern;

                int convertedCount = pattern.GetConvertedCount(indexCount);

                if (_needsIndexBufferRebind)
                {
                    _indexBuffer.BindConvertedIndexBuffer(Gd, Cbs, firstIndex, indexCount, convertedCount, pattern);

                    _needsIndexBufferRebind = false;
                }

                BeginRenderPass(); // May have been interrupted to set buffer data.
                ResumeTransformFeedbackInternal();

                Gd.Api.CmdDrawIndexed(CommandBuffer, (uint)convertedCount, (uint)instanceCount, 0, firstVertex, (uint)firstInstance);
            }
            else
            {
                ResumeTransformFeedbackInternal();

                Gd.Api.CmdDrawIndexed(CommandBuffer, (uint)indexCount, (uint)instanceCount, (uint)firstIndex, firstVertex, (uint)firstInstance);
            }

            Gd.BufferManager.PromotePendingAlignedTexelWritebacks();
        }

        public void DrawIndexedIndirect(BufferRange indirectBuffer)
        {
            Buffer buffer = Gd.BufferManager
                .GetBuffer(CommandBuffer, indirectBuffer.Handle, indirectBuffer.Offset, indirectBuffer.Size, false)
                .Get(Cbs, indirectBuffer.Offset, indirectBuffer.Size).Value;

            UpdateIndexBufferPattern();

            if (!RecreateGraphicsPipelineIfNeeded())
            {
                return;
            }

            BeginRenderPass();
            DrawCount++;

            if (_indexBufferPattern != null)
            {
                // Convert the index buffer into a supported topology.
                IndexBufferPattern pattern = _indexBufferPattern;

                Auto<DisposableBuffer> indirectBufferAuto = _indexBuffer.BindConvertedIndexBufferIndirect(
                    Gd,
                    Cbs,
                    indirectBuffer,
                    BufferRange.Empty,
                    pattern,
                    false,
                    1,
                    indirectBuffer.Size);

                _needsIndexBufferRebind = false;

                BeginRenderPass(); // May have been interrupted to set buffer data.
                ResumeTransformFeedbackInternal();

                Gd.Api.CmdDrawIndexedIndirect(CommandBuffer, indirectBufferAuto.Get(Cbs, 0, indirectBuffer.Size).Value, 0, 1, (uint)indirectBuffer.Size);
            }
            else
            {
                ResumeTransformFeedbackInternal();

                Gd.Api.CmdDrawIndexedIndirect(CommandBuffer, buffer, (ulong)indirectBuffer.Offset, 1, (uint)indirectBuffer.Size);
            }

            Gd.BufferManager.PromotePendingAlignedTexelWritebacks();
        }

        public void DrawIndexedIndirectCount(BufferRange indirectBuffer, BufferRange parameterBuffer, int maxDrawCount, int stride)
        {
            Buffer countBuffer = Gd.BufferManager
                .GetBuffer(CommandBuffer, parameterBuffer.Handle, parameterBuffer.Offset, parameterBuffer.Size, false)
                .Get(Cbs, parameterBuffer.Offset, parameterBuffer.Size).Value;

            Buffer buffer = Gd.BufferManager
                .GetBuffer(CommandBuffer, indirectBuffer.Handle, indirectBuffer.Offset, indirectBuffer.Size, false)
                .Get(Cbs, indirectBuffer.Offset, indirectBuffer.Size).Value;

            UpdateIndexBufferPattern();

            if (!RecreateGraphicsPipelineIfNeeded())
            {
                return;
            }

            BeginRenderPass();
            DrawCount++;

            if (_indexBufferPattern != null)
            {
                // Convert the index buffer into a supported topology.
                IndexBufferPattern pattern = _indexBufferPattern;

                Auto<DisposableBuffer> indirectBufferAuto = _indexBuffer.BindConvertedIndexBufferIndirect(
                    Gd,
                    Cbs,
                    indirectBuffer,
                    parameterBuffer,
                    pattern,
                    true,
                    maxDrawCount,
                    stride);

                _needsIndexBufferRebind = false;

                BeginRenderPass(); // May have been interrupted to set buffer data.
                ResumeTransformFeedbackInternal();

                if (Gd.Capabilities.SupportsIndirectParameters)
                {
                    Gd.DrawIndirectCountApi.CmdDrawIndexedIndirectCount(
                        CommandBuffer,
                        indirectBufferAuto.Get(Cbs, 0, indirectBuffer.Size).Value,
                        0,
                        countBuffer,
                        (ulong)parameterBuffer.Offset,
                        (uint)maxDrawCount,
                        (uint)stride);
                }
                else
                {
                    // This is also fine because the indirect data conversion always zeros
                    // the entries that are past the current draw count.

                    Gd.Api.CmdDrawIndexedIndirect(
                        CommandBuffer,
                        indirectBufferAuto.Get(Cbs, 0, indirectBuffer.Size).Value,
                        0,
                        (uint)maxDrawCount,
                        (uint)stride);
                }
            }
            else
            {
                ResumeTransformFeedbackInternal();

                if (Gd.Capabilities.SupportsIndirectParameters)
                {
                    Gd.DrawIndirectCountApi.CmdDrawIndexedIndirectCount(
                        CommandBuffer,
                        buffer,
                        (ulong)indirectBuffer.Offset,
                        countBuffer,
                        (ulong)parameterBuffer.Offset,
                        (uint)maxDrawCount,
                        (uint)stride);
                }
                else
                {
                    // Not fully correct, but we can't do much better if the host does not support indirect count.
                    Gd.Api.CmdDrawIndexedIndirect(
                        CommandBuffer,
                        buffer,
                        (ulong)indirectBuffer.Offset,
                        (uint)maxDrawCount,
                        (uint)stride);
                }
            }

            Gd.BufferManager.PromotePendingAlignedTexelWritebacks();
        }

        public void DrawIndirect(BufferRange indirectBuffer)
        {
            // TODO: Support quads and other unsupported topologies.

            Buffer buffer = Gd.BufferManager
                .GetBuffer(CommandBuffer, indirectBuffer.Handle, indirectBuffer.Offset, indirectBuffer.Size, false)
                .Get(Cbs, indirectBuffer.Offset, indirectBuffer.Size, false).Value;

            if (!RecreateGraphicsPipelineIfNeeded())
            {
                return;
            }

            BeginRenderPass();
            ResumeTransformFeedbackInternal();
            DrawCount++;

            Gd.Api.CmdDrawIndirect(CommandBuffer, buffer, (ulong)indirectBuffer.Offset, 1, (uint)indirectBuffer.Size);

            Gd.BufferManager.PromotePendingAlignedTexelWritebacks();
        }

        public void DrawIndirectCount(BufferRange indirectBuffer, BufferRange parameterBuffer, int maxDrawCount, int stride)
        {
            if (!Gd.Capabilities.SupportsIndirectParameters)
            {
                // TODO: Fallback for when this is not supported.
                throw new NotSupportedException();
            }

            Buffer buffer = Gd.BufferManager
                .GetBuffer(CommandBuffer, indirectBuffer.Handle, indirectBuffer.Offset, indirectBuffer.Size, false)
                .Get(Cbs, indirectBuffer.Offset, indirectBuffer.Size, false).Value;

            Buffer countBuffer = Gd.BufferManager
                .GetBuffer(CommandBuffer, parameterBuffer.Handle, parameterBuffer.Offset, parameterBuffer.Size, false)
                .Get(Cbs, parameterBuffer.Offset, parameterBuffer.Size, false).Value;

            // TODO: Support quads and other unsupported topologies.

            if (!RecreateGraphicsPipelineIfNeeded())
            {
                return;
            }

            BeginRenderPass();
            ResumeTransformFeedbackInternal();
            DrawCount++;

            Gd.DrawIndirectCountApi.CmdDrawIndirectCount(
                CommandBuffer,
                buffer,
                (ulong)indirectBuffer.Offset,
                countBuffer,
                (ulong)parameterBuffer.Offset,
                (uint)maxDrawCount,
                (uint)stride);

            Gd.BufferManager.PromotePendingAlignedTexelWritebacks();
        }

        public void DrawTexture(ITexture texture, ISampler sampler, Extents2DF srcRegion, Extents2DF dstRegion)
        {
            if (texture is TextureView srcTexture)
            {
                CullModeFlags oldCullMode = _newState.CullMode;
                bool oldStencilTestEnable = _newState.StencilTestEnable;
                bool oldDepthTestEnable = _newState.DepthTestEnable;
                bool oldDepthWriteEnable = _newState.DepthWriteEnable;
                Array16<Silk.NET.Vulkan.Viewport> oldViewports = DynamicState.Viewports;
                uint oldViewportsCount = _newState.ViewportsCount;
                PrimitiveTopology oldTopology = _topology;

                _newState.CullMode = CullModeFlags.None;
                _newState.StencilTestEnable = false;
                _newState.DepthTestEnable = false;
                _newState.DepthWriteEnable = false;
                SignalStateChange();

                Gd.HelperShader.DrawTexture(
                    Gd,
                    this,
                    srcTexture,
                    sampler,
                    srcRegion,
                    dstRegion);

                _newState.CullMode = oldCullMode;
                _newState.StencilTestEnable = oldStencilTestEnable;
                _newState.DepthTestEnable = oldDepthTestEnable;
                _newState.DepthWriteEnable = oldDepthWriteEnable;
                SetPrimitiveTopology(oldTopology);

                DynamicState.SetViewports(ref oldViewports, oldViewportsCount);

                _newState.ViewportsCount = oldViewportsCount;
                SignalStateChange();
            }
        }

        public void EndTransformFeedback()
        {
            Gd.Barriers.EnableTfbBarriers(false);
            PauseTransformFeedbackInternal();
            _tfEnabled = false;
        }

        public bool IsCommandBufferActive(CommandBuffer cb)
        {
            return CommandBuffer.Handle == cb.Handle;
        }

        internal void Rebind(Auto<DisposableBuffer> buffer, int offset, int size)
        {
            _descriptorSetUpdater.Rebind(buffer, offset, size);

            if (_indexBuffer.Overlaps(buffer, offset, size))
            {
                _indexBuffer.BindIndexBuffer(Gd, Cbs);
            }

            for (int i = 0; i < _vertexBuffers.Length; i++)
            {
                if (_vertexBuffers[i].Overlaps(buffer, offset, size))
                {
                    _vertexBuffers[i].BindVertexBuffer(Gd, Cbs, (uint)i, ref _newState, _vertexBufferUpdater);
                }
            }

            _vertexBufferUpdater.Commit(Cbs);
        }

        public void SetAlphaTest(bool enable, float reference, CompareOp op)
        {
            // This is currently handled using shader specialization, as Vulkan does not support alpha test.
            // In the future, we may want to use this to write the reference value into the support buffer,
            // to avoid creating one version of the shader per reference value used.
        }

        public void SetBlendState(AdvancedBlendDescriptor blend)
        {
            Span<PipelineColorBlendAttachmentState> colorBlendAttachmentStateSpan = _newState.Internal.ColorBlendAttachmentState.AsSpan();
            
            for (int index = 0; index < Constants.MaxRenderTargets; index++)
            {
                ref PipelineColorBlendAttachmentState vkBlend = ref colorBlendAttachmentStateSpan[index];

                if (index == 0)
                {
                    BlendOp blendOp = blend.Op.Convert();

                    vkBlend = new PipelineColorBlendAttachmentState(
                        blendEnable: true,
                        colorBlendOp: blendOp,
                        alphaBlendOp: blendOp,
                        colorWriteMask: vkBlend.ColorWriteMask);

                    if (Gd.Capabilities.SupportsBlendEquationAdvancedNonPreMultipliedSrcColor)
                    {
                        _newState.AdvancedBlendSrcPreMultiplied = blend.SrcPreMultiplied;
                    }

                    if (Gd.Capabilities.SupportsBlendEquationAdvancedCorrelatedOverlap)
                    {
                        _newState.AdvancedBlendOverlap = blend.Overlap.Convert();
                    }
                }
                else
                {
                    vkBlend = new PipelineColorBlendAttachmentState(
                        colorWriteMask: vkBlend.ColorWriteMask);
                }

                if (vkBlend.ColorWriteMask == 0)
                {
                    _storedBlend[index] = vkBlend;

                    vkBlend = new PipelineColorBlendAttachmentState();
                }
            }

            SignalStateChange();
        }

        public void SetBlendState(int index, BlendDescriptor blend)
        {
            ref PipelineColorBlendAttachmentState vkBlend = ref _newState.Internal.ColorBlendAttachmentState[index];

            if (blend.Enable)
            {
                vkBlend.BlendEnable = blend.Enable;
                vkBlend.SrcColorBlendFactor = blend.ColorSrcFactor.Convert();
                vkBlend.DstColorBlendFactor = blend.ColorDstFactor.Convert();
                vkBlend.ColorBlendOp = blend.ColorOp.Convert();
                vkBlend.SrcAlphaBlendFactor = blend.AlphaSrcFactor.Convert();
                vkBlend.DstAlphaBlendFactor = blend.AlphaDstFactor.Convert();
                vkBlend.AlphaBlendOp = blend.AlphaOp.Convert();
            }
            else
            {
                vkBlend = new PipelineColorBlendAttachmentState(
                    colorWriteMask: vkBlend.ColorWriteMask);
            }

            if (vkBlend.ColorWriteMask == 0)
            {
                _storedBlend[index] = vkBlend;

                vkBlend = new PipelineColorBlendAttachmentState();
            }

            DynamicState.SetBlendConstants(
                blend.BlendConstant.Red,
                blend.BlendConstant.Green,
                blend.BlendConstant.Blue,
                blend.BlendConstant.Alpha);

            // Reset advanced blend state back defaults to the cache to help the pipeline cache.
            _newState.AdvancedBlendSrcPreMultiplied = true;
            _newState.AdvancedBlendDstPreMultiplied = true;
            _newState.AdvancedBlendOverlap = BlendOverlapEXT.UncorrelatedExt;

            SignalStateChange();
        }

        public void SetDepthBias(PolygonModeMask enables, float factor, float units, float clamp)
        {
            DynamicState.SetDepthBias(factor, units, clamp);

            _newState.DepthBiasEnable = enables != 0;
            SignalStateChange();
        }

        public void SetDepthClamp(bool clamp)
        {
            _newState.DepthClampEnable = clamp;
            SignalStateChange();
        }

        public void SetDepthMode(DepthMode mode)
        {
            bool oldMode = _newState.DepthMode;
            _newState.DepthMode = mode == DepthMode.MinusOneToOne;
            if (_newState.DepthMode != oldMode)
            {
                SignalStateChange();
            }
        }

        public void SetDepthTest(DepthTestDescriptor depthTest)
        {
            _newState.DepthTestEnable = depthTest.TestEnable;
            _newState.DepthWriteEnable = depthTest.WriteEnable;
            _newState.DepthCompareOp = depthTest.Func.Convert();

            UpdatePassDepthStencil();
            SignalStateChange();
        }

        public void SetFaceCulling(bool enable, Face face)
        {
            _newState.CullMode = enable ? face.Convert() : CullModeFlags.None;
            SignalStateChange();
        }

        public void SetFrontFace(FrontFace frontFace)
        {
            _newState.FrontFace = frontFace.Convert();
            SignalStateChange();
        }

        public void SetImage(ShaderStage stage, int binding, ITexture image)
        {
            _descriptorSetUpdater.SetImage(Cbs, stage, binding, image);
        }

        public void SetImage(int binding, Auto<DisposableImageView> image)
        {
            _descriptorSetUpdater.SetImage(binding, image);
        }

        public void SetImageArray(ShaderStage stage, int binding, IImageArray array)
        {
            _descriptorSetUpdater.SetImageArray(Cbs, stage, binding, array);
        }

        public void SetImageArraySeparate(ShaderStage stage, int setIndex, IImageArray array)
        {
            _descriptorSetUpdater.SetImageArraySeparate(Cbs, stage, setIndex, array);
        }

        public void SetIndexBuffer(BufferRange buffer, IndexType type)
        {
            if (buffer.Handle != BufferHandle.Null)
            {
                _indexBuffer = new IndexBufferState(buffer.Handle, buffer.Offset, buffer.Size, type.Convert());
            }
            else
            {
                _indexBuffer = IndexBufferState.Null;
            }

            _needsIndexBufferRebind = true;
        }

        public void SetLineParameters(float width, bool smooth)
        {
            _newState.LineWidth = width;
            SignalStateChange();
        }

        public void SetLogicOpState(bool enable, LogicalOp op)
        {
            _newState.LogicOpEnable = enable;
            _newState.LogicOp = op.Convert();
            SignalStateChange();
        }

        public void SetMultisampleState(MultisampleDescriptor multisample)
        {
            _newState.AlphaToCoverageEnable = multisample.AlphaToCoverageEnable;
            _newState.AlphaToOneEnable = multisample.AlphaToOneEnable;
            SignalStateChange();
        }

        public void SetPatchParameters(int vertices, ReadOnlySpan<float> defaultOuterLevel, ReadOnlySpan<float> defaultInnerLevel)
        {
            _newState.PatchControlPoints = (uint)vertices;
            SignalStateChange();

            // TODO: Default levels (likely needs emulation on shaders?)
        }

        public void SetPointParameters(float size, bool isProgramPointSize, bool enablePointSprite, Origin origin)
        {
            // TODO.
        }

        public void SetPolygonMode(PolygonMode frontMode, PolygonMode backMode)
        {
            // TODO.
        }

        public void SetPrimitiveRestart(bool enable, int index)
        {
            _newState.PrimitiveRestartEnable = enable;
            // TODO: What to do about the index?
            SignalStateChange();
        }

        public void SetPrimitiveTopology(PrimitiveTopology topology)
        {
            _topology = topology;

            Silk.NET.Vulkan.PrimitiveTopology vkTopology = Gd.TopologyRemap(topology).Convert();

            _newState.Topology = vkTopology;

            SignalStateChange();
        }

        public void SetProgram(IProgram program)
        {
            ShaderCollection internalProgram = (ShaderCollection)program;
            PipelineShaderStageCreateInfo[] stages = internalProgram.GetInfos();

            _program = internalProgram;

            _descriptorSetUpdater.SetProgram(Cbs, internalProgram, _currentPipelineHandle != 0);
            _bindingBarriersDirty = true;

            _newState.PipelineLayout = internalProgram.PipelineLayout;
            _newState.HasTessellationControlShader = internalProgram.HasTessellationControlShader;
            _newState.StagesCount = (uint)stages.Length;

            stages.CopyTo(_newState.Stages.AsSpan()[..stages.Length]);

            SignalStateChange();

            if (internalProgram.IsCompute)
            {
                EndRenderPass();
            }
        }

        public void Specialize<T>(in T data) where T : unmanaged
        {
            ReadOnlySpan<byte> dataSpan = MemoryMarshal.AsBytes(MemoryMarshal.CreateReadOnlySpan(ref Unsafe.AsRef(in data), 1));

            if (!dataSpan.SequenceEqual(_newState.SpecializationData.Span))
            {
                _newState.SpecializationData = new SpecData(dataSpan);

                SignalStateChange();
            }
        }

        protected virtual void SignalAttachmentChange()
        {
        }

        public void SetRasterizerDiscard(bool discard)
        {
            _newState.RasterizerDiscardEnable = discard;
            SignalStateChange();

            if (!discard && Gd.IsQualcommProprietary)
            {
                // On Adreno, enabling rasterizer discard somehow corrupts the viewport state.
                // Force it to be updated on next use to work around this bug.
                DynamicState.ForceAllDirty();
            }
        }

        public void SetRenderTargetColorMasks(ReadOnlySpan<uint> componentMask)
        {
            int count = Math.Min(Constants.MaxRenderTargets, componentMask.Length);
            int writtenAttachments = 0;
            
            Span<PipelineColorBlendAttachmentState> colorBlendAttachmentStateSpan = _newState.Internal.ColorBlendAttachmentState.AsSpan();

            for (int i = 0; i < count; i++)
            {
                ref PipelineColorBlendAttachmentState vkBlend = ref colorBlendAttachmentStateSpan[i];
                ColorComponentFlags newMask = (ColorComponentFlags)componentMask[i];

                // When color write mask is 0, remove all blend state to help the pipeline cache.
                // Restore it when the mask becomes non-zero.
                if (vkBlend.ColorWriteMask != newMask)
                {
                    if (newMask == 0)
                    {
                        _storedBlend[i] = vkBlend;

                        vkBlend = new PipelineColorBlendAttachmentState();
                    }
                    else if (vkBlend.ColorWriteMask == 0)
                    {
                        vkBlend = _storedBlend[i];
                    }
                }

                vkBlend.ColorWriteMask = newMask;

                if (componentMask[i] != 0)
                {
                    writtenAttachments++;
                }
            }

            if (_framebufferUsingColorWriteMask && IsMainPipeline && Gd.ZeroWriteMaskColorAttachmentsDisabled &&
                !ZeroMaskTargetsChanged())
            {
                // S2A：留在帧缓冲里的目标没变，只是管线状态变了，不重开渲染通道。
                SignalStateChange();
            }
            else if (_framebufferUsingColorWriteMask)
            {
                SetRenderTargetsInternal(_preMaskColors, _preMaskDepthStencil, true);
            }
            else
            {
                SignalStateChange();

                if (writtenAttachments != _writtenAttachmentCount)
                {
                    SignalAttachmentChange();
                    _writtenAttachmentCount = writtenAttachments;
                }
            }
        }

        // S2A（#468）：Arm 专有驱动上，写掩码为 0 的颜色目标留在帧缓冲里会被写花（Mali-G710 r32p1：
        // 隔一个 16 行 tile 行出垃圾），所以这类目标不进帧缓冲（驱动怪癖表 A-ZMASK）。返回应当留下的槽位位图：
        // 有效且写掩码非 0 的、为清除临时放回的留下；一个都不剩又没有深度附件时留最低的有效槽位，保住帧缓冲尺寸。
        private uint ZeroMaskWantedTargets(ReadOnlySpan<ITexture> original, ITexture depthStencil)
        {
            Span<PipelineColorBlendAttachmentState> blend = _newState.Internal.ColorBlendAttachmentState.AsSpan();
            uint wanted = 0;
            int firstValid = -1;

            for (int i = 0; i < original.Length; i++)
            {
                if (original[i] is not TextureView { Valid: true })
                {
                    continue;
                }

                if (firstValid < 0)
                {
                    firstValid = i;
                }

                if (blend[i].ColorWriteMask != 0 || (_zeroMaskClearTargets & (1u << i)) != 0)
                {
                    wanted |= 1u << i;
                }
            }

            if (wanted == 0 && firstValid >= 0 && depthStencil is not TextureView { Valid: true })
            {
                wanted = 1u << firstValid;
            }

            return wanted;
        }

        // S2A（#468）：按当前写掩码重新筛一遍，结果和现在的帧缓冲有没有差别。
        private bool ZeroMaskTargetsChanged()
        {
            if (_preMaskColors == null || FramebufferParams == null)
            {
                return true;
            }

            uint wanted = ZeroMaskWantedTargets(_preMaskColors, _preMaskDepthStencil);

            for (int i = 0; i < _preMaskColors.Length; i++)
            {
                if ((((wanted >> i) & 1) != 0) != FramebufferParams.IsValidColorAttachment(i))
                {
                    return true;
                }
            }

            return false;
        }

        // S2A（#468）：槽位 index 上因写掩码为 0 没进帧缓冲的目标；没有、或没开这条怪癖时为 null。
        protected TextureView ZeroMaskFilteredColorView(int index)
        {
            if (!IsMainPipeline || !Gd.ZeroWriteMaskColorAttachmentsDisabled ||
                !_framebufferUsingColorWriteMask || _preMaskColors == null || (uint)index >= (uint)_preMaskColors.Length)
            {
                return null;
            }

            if (FramebufferParams != null && FramebufferParams.IsValidColorAttachment(index))
            {
                return null;
            }

            return _preMaskColors[index] is TextureView { Valid: true } view ? view : null;
        }

        // S2A（#468）：清除不看绘制写掩码。目标因写掩码为 0 不在帧缓冲里时，为这次清除把它放回去，
        // vkCmdClearAttachments 的范围和层参数照旧；下一次绘制前由 ZeroMaskDetachClearTargets 复位。
        private void ZeroMaskAttachForClear(int index)
        {
            if (ZeroMaskFilteredColorView(index) == null)
            {
                return;
            }

            _zeroMaskClearTargets |= 1u << index;
            SetRenderTargetsInternal(_preMaskColors, _preMaskDepthStencil, true);

            if (!FramebufferParams.IsValidColorAttachment(index))
            {
                // 同一张纹理还绑在别的槽位上，重复附件过滤又把这个槽位去掉了：放不回来，和原来一样不清。
                _zeroMaskClearTargets &= ~(1u << index);
            }
        }

        // S2A（#468）：每次绘制前调用。为清除临时放回的目标，按当前写掩码重新留在外面。
        private void ZeroMaskDetachClearTargets()
        {
            if (_zeroMaskClearTargets == 0)
            {
                return;
            }

            _zeroMaskClearTargets = 0;

            if (_framebufferUsingColorWriteMask && ZeroMaskTargetsChanged())
            {
                SetRenderTargetsInternal(_preMaskColors, _preMaskDepthStencil, true);
            }
        }

        private void SetRenderTargetsInternal(Span<ITexture> colors, ITexture depthStencil, bool filterWriteMasked)
        {
            CreateFramebuffer(colors, depthStencil, filterWriteMasked);
            CreateRenderPass();
            SignalStateChange();
            SignalAttachmentChange();
        }

        public void SetRenderTargets(Span<ITexture> colors, ITexture depthStencil)
        {
            _framebufferUsingColorWriteMask = false;
            _zeroMaskClearTargets = 0;
            SetRenderTargetsInternal(colors, depthStencil, Gd.IsTBDR);
        }

        public void SetScissors(ReadOnlySpan<Rectangle<int>> regions)
        {
            int maxScissors = Gd.Capabilities.SupportsMultiView ? Constants.MaxViewports : 1;
            int count = Math.Min(maxScissors, regions.Length);
            if (count > 0)
            {
                ClearScissor = regions[0];
            }

            for (int i = 0; i < count; i++)
            {
                Rectangle<int> region = regions[i];
                Offset2D offset = new(region.X, region.Y);
                Extent2D extent = new((uint)region.Width, (uint)region.Height);

                DynamicState.SetScissor(i, new Rect2D(offset, extent));
            }

            DynamicState.ScissorsCount = count;

            _newState.ScissorsCount = (uint)count;
            SignalStateChange();
        }

        public void SetStencilTest(StencilTestDescriptor stencilTest)
        {
            DynamicState.SetStencilMasks(
                (uint)stencilTest.BackFuncMask,
                (uint)stencilTest.BackMask,
                (uint)stencilTest.BackFuncRef,
                (uint)stencilTest.FrontFuncMask,
                (uint)stencilTest.FrontMask,
                (uint)stencilTest.FrontFuncRef);

            _newState.StencilTestEnable = stencilTest.TestEnable;
            _newState.StencilBackFailOp = stencilTest.BackSFail.Convert();
            _newState.StencilBackPassOp = stencilTest.BackDpPass.Convert();
            _newState.StencilBackDepthFailOp = stencilTest.BackDpFail.Convert();
            _newState.StencilBackCompareOp = stencilTest.BackFunc.Convert();
            _newState.StencilFrontFailOp = stencilTest.FrontSFail.Convert();
            _newState.StencilFrontPassOp = stencilTest.FrontDpPass.Convert();
            _newState.StencilFrontDepthFailOp = stencilTest.FrontDpFail.Convert();
            _newState.StencilFrontCompareOp = stencilTest.FrontFunc.Convert();

            UpdatePassDepthStencil();
            SignalStateChange();
        }

        public void SetStorageBuffers(ReadOnlySpan<BufferAssignment> buffers)
        {
            _descriptorSetUpdater.SetStorageBuffers(CommandBuffer, buffers);
        }

        public void SetStorageBuffers(int first, ReadOnlySpan<Auto<DisposableBuffer>> buffers)
        {
            _descriptorSetUpdater.SetStorageBuffers(CommandBuffer, first, buffers);
        }

        public void SetTextureAndSampler(ShaderStage stage, int binding, ITexture texture, ISampler sampler)
        {
            _descriptorSetUpdater.SetTextureAndSampler(Cbs, stage, binding, texture, sampler);
        }

        public void SetTextureAndSamplerIdentitySwizzle(ShaderStage stage, int binding, ITexture texture, ISampler sampler)
        {
            _descriptorSetUpdater.SetTextureAndSamplerIdentitySwizzle(Cbs, stage, binding, texture, sampler);
        }

        public void SetTextureArray(ShaderStage stage, int binding, ITextureArray array)
        {
            _descriptorSetUpdater.SetTextureArray(Cbs, stage, binding, array);
        }

        public void SetTextureArraySeparate(ShaderStage stage, int setIndex, ITextureArray array)
        {
            _descriptorSetUpdater.SetTextureArraySeparate(Cbs, stage, setIndex, array);
        }

        public void SetTransformFeedbackBuffers(ReadOnlySpan<BufferRange> buffers)
        {
            PauseTransformFeedbackInternal();

            int count = Math.Min(Constants.MaxTransformFeedbackBuffers, buffers.Length);

            for (int i = 0; i < count; i++)
            {
                BufferRange range = buffers[i];

                _transformFeedbackBuffers[i].Dispose();

                if (range.Handle != BufferHandle.Null)
                {
                    _transformFeedbackBuffers[i] =
                        new BufferState(Gd.BufferManager.GetBuffer(CommandBuffer, range.Handle, range.Offset, range.Size, true), range.Offset, range.Size);
                    _transformFeedbackBuffers[i].BindTransformFeedbackBuffer(Gd, Cbs, (uint)i);
                }
                else
                {
                    _transformFeedbackBuffers[i] = BufferState.Null;
                }
            }
        }

        public void SetUniformBuffers(ReadOnlySpan<BufferAssignment> buffers)
        {
            _descriptorSetUpdater.SetUniformBuffers(CommandBuffer, buffers);
        }

        public void SetUserClipDistance(int index, bool enableClip)
        {
            // TODO.
        }

        public void SetVertexAttribs(ReadOnlySpan<VertexAttribDescriptor> vertexAttribs)
        {
            FormatCapabilities formatCapabilities = Gd.FormatCapabilities;

            Span<int> newVbScalarSizes = stackalloc int[Constants.MaxVertexBuffers];

            int count = Math.Min(Constants.MaxVertexAttributes, vertexAttribs.Length);
            uint dirtyVbSizes = 0;
            
            Span<VertexInputAttributeDescription> vertexAttributeDescriptionsSpan = _newState.Internal.VertexAttributeDescriptions.AsSpan();

            for (int i = 0; i < count; i++)
            {
                VertexAttribDescriptor attribute = vertexAttribs[i];
                int rawIndex = attribute.BufferIndex;
                int bufferIndex = attribute.IsZero ? 0 : rawIndex + 1;

                if (!attribute.IsZero)
                {
                    newVbScalarSizes[rawIndex] = Math.Max(newVbScalarSizes[rawIndex], attribute.Format.ScalarSize);
                    dirtyVbSizes |= 1u << rawIndex;
                }

                vertexAttributeDescriptionsSpan[i] = new VertexInputAttributeDescription(
                    (uint)i,
                    (uint)bufferIndex,
                    formatCapabilities.ConvertToVertexVkFormat(attribute.Format),
                    (uint)attribute.Offset);
            }

            while (dirtyVbSizes != 0)
            {
                int dirtyBit = BitOperations.TrailingZeroCount(dirtyVbSizes);

                ref VertexBufferState buffer = ref _vertexBuffers[dirtyBit + 1];

                if (buffer.AttributeScalarAlignment != newVbScalarSizes[dirtyBit])
                {
                    _vertexBuffersDirty |= 1UL << (dirtyBit + 1);
                    buffer.AttributeScalarAlignment = newVbScalarSizes[dirtyBit];
                }

                dirtyVbSizes &= ~(1u << dirtyBit);
            }

            _newState.VertexAttributeDescriptionsCount = (uint)count;
            SignalStateChange();
        }

        public void SetVertexBuffers(ReadOnlySpan<VertexBufferDescriptor> vertexBuffers)
        {
            int count = Math.Min(Constants.MaxVertexBuffers, vertexBuffers.Length);

            _newState.Internal.VertexBindingDescriptions[0] = new VertexInputBindingDescription(0, 0, VertexInputRate.Vertex);

            int validCount = 1;

            BufferHandle lastHandle = default;
            Auto<DisposableBuffer> lastBuffer = null;
            
            Span<VertexInputBindingDescription> vertexBindingDescriptionsSpan = _newState.Internal.VertexBindingDescriptions.AsSpan();

            for (int i = 0; i < count; i++)
            {
                VertexBufferDescriptor vertexBuffer = vertexBuffers[i];

                // TODO: Support divisor > 1
                VertexInputRate inputRate = vertexBuffer.Divisor != 0 ? VertexInputRate.Instance : VertexInputRate.Vertex;

                if (vertexBuffer.Buffer.Handle != BufferHandle.Null)
                {
                    Auto<DisposableBuffer> vb = (vertexBuffer.Buffer.Handle == lastHandle) ? lastBuffer :
                        Gd.BufferManager.GetBuffer(CommandBuffer, vertexBuffer.Buffer.Handle, false);

                    lastHandle = vertexBuffer.Buffer.Handle;
                    lastBuffer = vb;

                    if (vb != null)
                    {
                        int binding = i + 1;
                        int descriptorIndex = validCount++;

                        vertexBindingDescriptionsSpan[descriptorIndex] = new VertexInputBindingDescription(
                            (uint)binding,
                            (uint)vertexBuffer.Stride,
                            inputRate);

                        int vbSize = vertexBuffer.Buffer.Size;

                        if (Gd.Vendor == Vendor.Amd && !Gd.IsMoltenVk && vertexBuffer.Stride > 0)
                        {
                            // AMD has a bug where if offset + stride * count is greater than
                            // the size, then the last attribute will have the wrong value.
                            // As a workaround, simply use the full buffer size.
                            int remainder = vbSize % vertexBuffer.Stride;
                            if (remainder != 0)
                            {
                                vbSize += vertexBuffer.Stride - remainder;
                            }
                        }

                        ref VertexBufferState buffer = ref _vertexBuffers[binding];
                        int oldScalarAlign = buffer.AttributeScalarAlignment;

                        if (Gd.Capabilities.VertexBufferAlignment < 2 &&
                            (vertexBuffer.Stride % FormatExtensions.MaxBufferFormatScalarSize) == 0)
                        {
                            if (!buffer.Matches(vb, descriptorIndex, vertexBuffer.Buffer.Offset, vbSize, vertexBuffer.Stride))
                            {
                                buffer.Dispose();

                                buffer = new VertexBufferState(
                                    vb,
                                    descriptorIndex,
                                    vertexBuffer.Buffer.Offset,
                                    vbSize,
                                    vertexBuffer.Stride);

                                buffer.BindVertexBuffer(Gd, Cbs, (uint)binding, ref _newState, _vertexBufferUpdater);
                            }
                        }
                        else
                        {
                            // May need to be rewritten. Bind this buffer before draw.

                            buffer.Dispose();

                            buffer = new VertexBufferState(
                                vertexBuffer.Buffer.Handle,
                                descriptorIndex,
                                vertexBuffer.Buffer.Offset,
                                vbSize,
                                vertexBuffer.Stride);

                            _vertexBuffersDirty |= 1UL << binding;
                        }

                        buffer.AttributeScalarAlignment = oldScalarAlign;
                    }
                }
            }

            _vertexBufferUpdater.Commit(Cbs);

            _newState.VertexBindingDescriptionsCount = (uint)validCount;
            SignalStateChange();
        }

        public void SetViewports(ReadOnlySpan<Viewport> viewports)
        {
            int maxViewports = Gd.Capabilities.SupportsMultiView ? Constants.MaxViewports : 1;
            int count = Math.Min(maxViewports, viewports.Length);

            static float Clamp(float value)
            {
                return Math.Clamp(value, 0f, 1f);
            }

            DynamicState.ViewportsCount = (uint)count;

            for (int i = 0; i < count; i++)
            {
                Viewport viewport = viewports[i];

                DynamicState.SetViewport(i, new Silk.NET.Vulkan.Viewport(
                    viewport.Region.X,
                    viewport.Region.Y,
                    viewport.Region.Width == 0f ? 1f : viewport.Region.Width,
                    viewport.Region.Height == 0f ? 1f : viewport.Region.Height,
                    Clamp(viewport.DepthNear),
                    Clamp(viewport.DepthFar)));
            }

            _newState.ViewportsCount = (uint)count;
            SignalStateChange();
        }

        public void SwapBuffer(Auto<DisposableBuffer> from, Auto<DisposableBuffer> to)
        {
            _indexBuffer.Swap(from, to);

            for (int i = 0; i < _vertexBuffers.Length; i++)
            {
                _vertexBuffers[i].Swap(from, to);
            }

            for (int i = 0; i < _transformFeedbackBuffers.Length; i++)
            {
                _transformFeedbackBuffers[i].Swap(from, to);
            }

            _descriptorSetUpdater.SwapBuffer(from, to);

            SignalCommandBufferChange();
        }

        public void ForceTextureDirty()
        {
            _descriptorSetUpdater.ForceTextureDirty();
        }

        public void ForceImageDirty()
        {
            _descriptorSetUpdater.ForceImageDirty();
        }

        public unsafe void TextureBarrier()
        {
            Gd.Barriers.QueueTextureBarrier();
        }

        public void TextureBarrierTiled()
        {
            TextureBarrier();
        }

        protected void SignalCommandBufferChange()
        {
            _needsIndexBufferRebind = true;
            _needsTransformFeedbackBuffersRebind = true;
            _vertexBuffersDirty = ulong.MaxValue >> (64 - _vertexBuffers.Length);

            _descriptorSetUpdater.SignalCommandBufferChange();
            DynamicState.ForceAllDirty();
            _currentPipelineHandle = 0;
        }

        private void CreateFramebuffer(Span<ITexture> colors, ITexture depthStencil, bool filterWriteMasked)
        {
            // S2A（#468）：见 ZeroMaskWantedTargets。只管主管线；辅助管线自己绑单个目标。
            bool dropZeroMask = filterWriteMasked && IsMainPipeline && Gd.ZeroWriteMaskColorAttachmentsDisabled;

            if (dropZeroMask)
            {
                if (!_framebufferUsingColorWriteMask)
                {
                    if (_preMaskColors == null || _preMaskColors.Length != colors.Length)
                    {
                        _preMaskColors = colors.ToArray();
                    }
                    else
                    {
                        colors.CopyTo(_preMaskColors);
                    }

                    _preMaskDepthStencil = depthStencil;
                    _framebufferUsingColorWriteMask = true;
                }

                // 在副本上筛：下面会把槽位置空，保存的原始目标表要留全。槽位数不超过 MaxRenderTargets（写掩码状态也只有这么多）。
                Span<ITexture> filtered = _zeroMaskScratch.AsSpan(0, colors.Length);
                colors.CopyTo(filtered);
                colors = filtered;
            }

            if (filterWriteMasked)
            {
                // TBDR GPUs don't work properly if the same attachment is bound to multiple targets,
                // due to each attachment being a copy of the real attachment, rather than a direct write.

                // Just try to remove duplicate attachments.
                // Save a copy of the array to rebind when mask changes.

                void MaskOut(ReadOnlySpan<ITexture> colors)
                {
                    if (!_framebufferUsingColorWriteMask)
                    {
                        _preMaskColors = colors.ToArray();
                        _preMaskDepthStencil = depthStencil;
                    }

                    // If true, then the framebuffer must be recreated when the mask changes.
                    _framebufferUsingColorWriteMask = true;
                }

                // Look for textures that are masked out.

                Span<PipelineColorBlendAttachmentState> colorBlendAttachmentStateSpan =
                    _newState.Internal.ColorBlendAttachmentState.AsSpan();

                for (int i = 0; i < colors.Length; i++)
                {
                    if (colors[i] == null)
                    {
                        continue;
                    }

                    ref PipelineColorBlendAttachmentState vkBlend = ref colorBlendAttachmentStateSpan[i];

                    for (int j = 0; j < i; j++)
                    {
                        // Check each binding for a duplicate binding before it.

                        if (colors[i] == colors[j])
                        {
                            // Prefer the binding with no write mask.
                            ref PipelineColorBlendAttachmentState vkBlend2 = ref colorBlendAttachmentStateSpan[j];
                            if (vkBlend.ColorWriteMask == 0)
                            {
                                colors[i] = null;
                                MaskOut(colors);
                            }
                            else if (vkBlend2.ColorWriteMask == 0)
                            {
                                colors[j] = null;
                                MaskOut(colors);
                            }
                        }
                    }
                }
            }

            if (dropZeroMask)
            {
                uint wanted = ZeroMaskWantedTargets(_preMaskColors, depthStencil);

                for (int i = 0; i < colors.Length; i++)
                {
                    if (((wanted >> i) & 1) == 0)
                    {
                        colors[i] = null;
                    }
                }
            }

            if (IsMainPipeline)
            {
                FramebufferParams?.ClearBindings();
            }

            FramebufferParams = FramebufferParams?.Update(colors, depthStencil) ?? new FramebufferParams(Device, colors, depthStencil);

            if (dropZeroMask)
            {
                // 工作副本不留纹理引用。
                Array.Clear(_zeroMaskScratch);
            }

            if (IsMainPipeline)
            {
                FramebufferParams.AddBindings();

                _newState.FeedbackLoopAspects = FeedbackLoopAspects.None;
                _bindingBarriersDirty = true;
            }

            _passWritesDepthStencil = false;
            UpdatePassDepthStencil();
            UpdatePipelineAttachmentFormats();
        }

        protected void UpdatePipelineAttachmentFormats()
        {
            Span<Silk.NET.Vulkan.Format> dstAttachmentFormats = _newState.Internal.AttachmentFormats.AsSpan();
            FramebufferParams.AttachmentFormats.AsSpan(..FramebufferParams.AttachmentsCount).CopyTo(dstAttachmentFormats);
            _newState.Internal.AttachmentIntegerFormatMask = FramebufferParams.AttachmentIntegerFormatMask;
            _newState.Internal.LogicOpsAllowed = FramebufferParams.LogicOpsAllowed;

            for (int i = FramebufferParams.AttachmentsCount; i < dstAttachmentFormats.Length; i++)
            {
                dstAttachmentFormats[i] = 0;
            }

            _newState.ColorBlendAttachmentStateCount = (uint)(FramebufferParams.MaxColorAttachmentIndex + 1);
            _newState.HasDepthStencil = FramebufferParams.HasDepthStencil;
            _newState.SamplesCount = FramebufferParams.AttachmentsCount != 0 ? FramebufferParams.AttachmentSamples[0] : 1;
        }

        protected unsafe void CreateRenderPass()
        {
            bool hasFramebuffer = FramebufferParams != null;

            EndRenderPass();

            if (!hasFramebuffer || FramebufferParams.AttachmentsCount == 0)
            {
                // Use the null framebuffer.
                _nullRenderPass ??= new RenderPassHolder(Gd, Device, new RenderPassCacheKey(), FramebufferParams);

                _rpHolder = _nullRenderPass;
                _renderPass = _nullRenderPass.GetRenderPass();
                _framebuffer = _nullRenderPass.GetFramebuffer(Gd, Cbs, FramebufferParams);
            }
            else
            {
                (_rpHolder, _framebuffer) = FramebufferParams.GetPassAndFramebuffer(Gd, Device, Cbs);

                _renderPass = _rpHolder.GetRenderPass();
            }
        }

        protected void SignalStateChange()
        {
            _graphicsStateDirty = true;
            _computeStateDirty = true;
        }

        private void RecreateComputePipelineIfNeeded()
        {
            // 修①：上一次 dispatch 生效的写回账在描述符更新前落掉——本 draw 可能
            // 经没变脏的描述符读同一段源缓冲，必须先拿到影子写回的数据。
            Gd.BufferManager.FlushArmedAlignedTexelWritebacks();

            if (_computeStateDirty || Pbp != PipelineBindPoint.Compute)
            {
                CreatePipeline(PipelineBindPoint.Compute);
                _computeStateDirty = false;
                Pbp = PipelineBindPoint.Compute;

                if (_bindingBarriersDirty)
                {
                    // Stale barriers may have been activated by switching program. Emit any that are relevant.
                    _descriptorSetUpdater.InsertBindingBarriers(Cbs);

                    _bindingBarriersDirty = false;
                }
            }

            Gd.Barriers.Flush(Cbs, _program, _feedbackLoop != 0, RenderPassActive, _rpHolder, EndRenderPassDelegate);

            _descriptorSetUpdater.UpdateAndBindDescriptorSets(Cbs, PipelineBindPoint.Compute);
        }

        private bool ChangeFeedbackLoop(FeedbackLoopAspects aspects)
        {
            // AMD RDNA 3 GPUs only
            if (Gd.IsAmdRdna3)
            {
                if (_feedbackLoop != aspects)
                {
                    if (Gd.Capabilities.SupportsDynamicAttachmentFeedbackLoop)
                    {
                        DynamicState.SetFeedbackLoop(aspects);
                    }
                    else
                    {
                        _newState.FeedbackLoopAspects = aspects;
                    }

                    _feedbackLoop = aspects;

                    return true;
                }
            }

            return false;
        }

        [MethodImpl(MethodImplOptions.AggressiveInlining)]
        private bool UpdateFeedbackLoop()
        {
            List<TextureView> hazards = _descriptorSetUpdater.FeedbackLoopHazards;

            if ((hazards?.Count ?? 0) > 0)
            {
                FeedbackLoopAspects aspects = 0;

                foreach (TextureView view in hazards)
                {
                    // May need to enforce feedback loop layout here in the future.
                    // Though technically, it should always work with the general layout.

                    if (view.Info.Format.IsDepthOrStencil)
                    {
                        if (_passWritesDepthStencil)
                        {
                            // If depth/stencil isn't written in the pass, it doesn't count as a feedback loop.

                            aspects |= FeedbackLoopAspects.Depth;
                        }
                    }
                    else
                    {
                        aspects |= FeedbackLoopAspects.Color;
                    }
                }

                S2APipelineDiagnostics.ReportFeedbackLoopHazards(aspects, hazards, _program.S2AShaderHashes);

                // #1360：若现有 ChangeFeedbackLoop 路径实际生效（布局已激活 / 本次切换成功），
                // 采样看到的是当前附件；否则命中深度 hazard 时结束通道，让 store op 把 tile 存回。
                bool layoutActive = _feedbackLoop != 0;
                bool layoutChanged = ChangeFeedbackLoop(aspects);

                if (S2AFeedbackLoopPolicy.ShouldSplitDepthPass(
                        layoutActive, layoutChanged,
                        (aspects & FeedbackLoopAspects.Depth) != 0, RenderPassActive))
                {
                    EndRenderPass();
                    S2APipelineDiagnostics.ReportDepthPassSplit(hazards.Count, hazards[0], _program.S2AShaderHashes);
                }

                return layoutChanged;
            }
            else if (_feedbackLoop != 0)
            {
                return ChangeFeedbackLoop(FeedbackLoopAspects.None);
            }

            return false;
        }

        private void UpdatePassDepthStencil()
        {
            if (!RenderPassActive)
            {
                _passWritesDepthStencil = false;
            }

            // Stencil test being enabled doesn't necessarily mean a write, but it's not critical to check.
            _passWritesDepthStencil |= (_newState.DepthTestEnable && _newState.DepthWriteEnable) || _newState.StencilTestEnable;
        }

        private bool RecreateGraphicsPipelineIfNeeded()
        {
            // 修①：同 RecreateComputePipelineIfNeeded——上一次 draw 生效的写回账先落。
            Gd.BufferManager.FlushArmedAlignedTexelWritebacks();

            if (AutoFlush.ShouldFlushDraw(DrawCount))
            {
                Gd.FlushAllCommands();
            }

            ZeroMaskDetachClearTargets();

            DynamicState.ReplayIfDirty(Gd, CommandBuffer);

            if (_needsIndexBufferRebind && _indexBufferPattern == null)
            {
                _indexBuffer.BindIndexBuffer(Gd, Cbs);
                _needsIndexBufferRebind = false;
            }

            if (_needsTransformFeedbackBuffersRebind)
            {
                PauseTransformFeedbackInternal();

                for (int i = 0; i < Constants.MaxTransformFeedbackBuffers; i++)
                {
                    _transformFeedbackBuffers[i].BindTransformFeedbackBuffer(Gd, Cbs, (uint)i);
                }

                _needsTransformFeedbackBuffersRebind = false;
            }

            if (_vertexBuffersDirty != 0)
            {
                while (_vertexBuffersDirty != 0)
                {
                    int i = BitOperations.TrailingZeroCount(_vertexBuffersDirty);

                    _vertexBuffers[i].BindVertexBuffer(Gd, Cbs, (uint)i, ref _newState, _vertexBufferUpdater);

                    _vertexBuffersDirty &= ~(1UL << i);
                }

                _vertexBufferUpdater.Commit(Cbs);
            }

            if (_bindingBarriersDirty)
            {
                // Stale barriers may have been activated by switching program. Emit any that are relevant.
                _descriptorSetUpdater.InsertBindingBarriers(Cbs);

                _bindingBarriersDirty = false;
            }

            if (UpdateFeedbackLoop() || _graphicsStateDirty || Pbp != PipelineBindPoint.Graphics)
            {
                if (!CreatePipeline(PipelineBindPoint.Graphics))
                {
                    return false;
                }

                _graphicsStateDirty = false;
                Pbp = PipelineBindPoint.Graphics;
            }

            Gd.Barriers.Flush(Cbs, _program, _feedbackLoop != 0, RenderPassActive, _rpHolder, EndRenderPassDelegate);

            _descriptorSetUpdater.UpdateAndBindDescriptorSets(Cbs, PipelineBindPoint.Graphics);

            return true;
        }

        private bool CreatePipeline(PipelineBindPoint pbp)
        {
            // We can only create a pipeline if the have the shader stages set.
            if (_newState.Stages != null)
            {
                if (pbp == PipelineBindPoint.Graphics && _renderPass == null)
                {
                    CreateRenderPass();
                }

                if (!_program.IsLinked)
                {
                    // Background compile failed, we likely can't create the pipeline because the shader is broken
                    // or the driver failed to compile it.

                    S2APipelineDiagnostics.ReportUnlinkedSkip(
                        pbp, "CreatePipeline", _newState.S2AKeyString(), _program.S2AShaderHashes);

                    return false;
                }

                Auto<DisposablePipeline> pipeline = pbp == PipelineBindPoint.Compute
                    ? _newState.CreateComputePipeline(Gd, Device, _program, PipelineCache)
                    : _newState.CreateGraphicsPipeline(Gd, Device, _program, PipelineCache, _renderPass.Get(Cbs).Value);

                if (pipeline == null)
                {
                    // Host failed to create the pipeline, likely due to driver bugs.

                    return false;
                }

                ulong pipelineHandle = pipeline.GetUnsafe().Value.Handle;

                if (_currentPipelineHandle != pipelineHandle)
                {
                    _currentPipelineHandle = pipelineHandle;
                    Pipeline = pipeline;

                    PauseTransformFeedbackInternal();
                    Gd.Api.CmdBindPipeline(CommandBuffer, pbp, Pipeline.Get(Cbs).Value);
                }
            }

            return true;
        }

        private unsafe void BeginRenderPass()
        {
            if (!RenderPassActive)
            {
                FramebufferParams.InsertLoadOpBarriers(Gd, Cbs);

                Rect2D renderArea = new(null, new Extent2D(FramebufferParams.Width, FramebufferParams.Height));
                ClearValue clearValue = new();

                RenderPassBeginInfo renderPassBeginInfo = new()
                {
                    SType = StructureType.RenderPassBeginInfo,
                    RenderPass = _renderPass.Get(Cbs).Value,
                    Framebuffer = _framebuffer.Get(Cbs).Value,
                    RenderArea = renderArea,
                    PClearValues = &clearValue,
                    ClearValueCount = 1,
                };

                Gd.Api.CmdBeginRenderPass(CommandBuffer, in renderPassBeginInfo, SubpassContents.Inline);
                RenderPassActive = true;
                DrainPendingQueryBegins();
            }
        }

        public void EndRenderPass()
        {
            // 修①：渲染通道结束前主动落掉已生效的 texel 写回账（落账内部会再进这里，
            // 此时账已取空、不会递归）。pending 账不动——它的绘制还没记录。
            Gd.BufferManager.FlushArmedAlignedTexelWritebacks();

            if (RenderPassActive)
            {
                FramebufferParams.AddStoreOpUsage();

                PauseTransformFeedbackInternal();
                CloseQueriesForRenderPassEnd();
                Gd.Api.CmdEndRenderPass(CommandBuffer);
                SignalRenderPassEnd();
                RenderPassActive = false;
            }
        }

        protected virtual void SignalRenderPassEnd()
        {
        }

        // Called before vkCmdEndRenderPass so backends can end in-subpass commands (e.g. queries).
        protected virtual void CloseQueriesForRenderPassEnd()
        {
        }

        // Called right after vkCmdBeginRenderPass at subpass 0 so deferred begins record inside.
        protected virtual void DrainPendingQueryBegins()
        {
        }

        private void PauseTransformFeedbackInternal()
        {
            if (_tfEnabled && _tfActive)
            {
                EndTransformFeedbackInternal();
                _tfActive = false;
            }
        }

        private void ResumeTransformFeedbackInternal()
        {
            if (_tfEnabled && !_tfActive)
            {
                BeginTransformFeedbackInternal();
                _tfActive = true;
            }
        }

        private unsafe void BeginTransformFeedbackInternal()
        {
            Gd.TransformFeedbackApi.CmdBeginTransformFeedback(CommandBuffer, 0, 0, null, null);
        }

        private unsafe void EndTransformFeedbackInternal()
        {
            Gd.TransformFeedbackApi.CmdEndTransformFeedback(CommandBuffer, 0, 0, null, null);
        }

        private static unsafe PipelineCacheIdentity S2ACacheIdentity(VulkanRenderer gd)
        {
            PhysicalDeviceProperties properties = gd.PhysicalDevice.PhysicalDeviceProperties;
            byte[] uuid = new byte[16];
            for (int i = 0; i < uuid.Length; i++)
                uuid[i] = properties.PipelineCacheUuid[i];
            return new PipelineCacheIdentity(properties.VendorID, properties.DeviceID, properties.DriverVersion,
                properties.ApiVersion, uuid, PipelineCacheFile.Schema, PipelineCacheFile.Backend);
        }

        /// <summary>
        /// S2A（Android 拆线程收局）：收到宿主收局请求时在编译通道上提前落盘一次（与管线创建同一条通道，不与驱动调用并发）。
        /// 宿主收尾兜底期限很短（原生 1.5 s），收局后面的步骤来不及时缓存也不丢；释放时照常再存一次（含这之后新编的管线）。返回是否写成
        /// </summary>
        internal bool S2ASavePipelineCacheEarly() => S2ASavePipelineCache("收局请求时提前");
        internal bool S2ASavePipelineCacheForExit() => S2ASavePipelineCache("收局补存");
        // S2A（11-03 定期落盘）：崩溃 / 强杀不会走到收局路径，最多丢一个周期的新管线；没有新管线不写盘。
        //   定期场景不等写盘（waitForWrite: false）：收局才有退出期限要有界等，定期在编译通道上干等只会让新管线排队。
        internal bool S2ASavePipelineCachePeriodic() => S2APipelineCacheCurrent || S2ASavePipelineCache("定期", waitForWrite: false);
        // S2A（10-27，单线程收局）：在停 WSI 之前落盘。单线程收局在原生 1.5 s 内做完整释放，窗口 / WSI 退休可能吃掉大半预算
        //   （9000 r8：通过局总共 1.405 s；18:02 局 GPU Run 退出 +0.670 s、Surface detached +1.029 s、1.509 s 被 _exit）；
        //   落盘只有几十毫秒，先做，后面超时也不丢缓存。释放时若之后没有新管线由 Dispose 跳过重存
        internal bool S2ASavePipelineCacheBeforeWsiRetire() => S2ASavePipelineCache("WSI 退休前");

        /// <summary>S2A：最近一次写成的落盘之后没有新管线（盘上的驱动管线缓存是最新的）</summary>
        internal bool S2APipelineCacheCurrent =>
            System.Threading.Volatile.Read(ref _s2aSavedAtPipelines) == System.Threading.Interlocked.Read(ref PipelineState.S2APipelinesCreated);

        // 最近一次写成的落盘对应的管线数（PipelineState.S2APipelinesCreated）；-1 = 本进程还没写成过
        private long _s2aSavedAtPipelines = -1;
        // 落盘的版本序与最近一次后台写（Dispose 再存前先有界等它）
        private readonly S2AVersionedSave _s2aSaves = new();
        private Task<bool> _s2aPendingSave;

        private unsafe bool S2ASavePipelineCache(string when = null, bool waitForWrite = true)
        {
            if (!_s2aOwnsPipelineCache || _s2aPipelineCachePath == null)
                return false;

            long pipelines = System.Threading.Interlocked.Read(ref PipelineState.S2APipelinesCreated);

            nuint size = 0;
            Result result = Gd.Api.GetPipelineCacheData(Device, PipelineCache, ref size, null);
            if (result.IsError || size == 0 || size > PipelineCacheFile.MaximumBytes)
            {
                Console.Error.WriteLine($"[vkpipe] 驱动缓存大小查询失败：{result} size={size}");
                return false;
            }

            byte[] data = new byte[(int)size];
            fixed (byte* pData = data)
            {
                result = Gd.Api.GetPipelineCacheData(Device, PipelineCache, ref size, pData);
            }

            if (result == Result.Incomplete)
            {
                // Bounded retry: do not persist bytes whose receipt was not verified.
                Console.Error.WriteLine("[vkpipe] 驱动缓存读取不完整（VK_INCOMPLETE），本次不保存");
                return false;
            }
            if (result.IsError || size > (nuint)data.Length)
            {
                Console.Error.WriteLine($"[vkpipe] 驱动缓存读取失败：{result} size={size}");
                return false;
            }

            // Snapshotting uses the driver cache (owner only, after the compile lane stopped);
            // the file write uses no device object. Bound the stop-path wait so the host's
            // shutdown deadline still applies; a late write leaves the previous file intact
            // because the writer uses temp + atomic rename.
            byte[] snapshot = data.AsSpan(0, (int)size).ToArray();
            long version = _s2aSaves.Take();
            PipelineCacheIdentity identity = _s2aPipelineCacheIdentity;
            string path = _s2aPipelineCachePath;
            string label = when == null ? string.Empty : "（" + when + "）";
            Task<bool> save = Task.Run(() =>
            {
                string error = null;
                bool written = _s2aSaves.Write(version, () =>
                {
                    if (!PipelineCacheFile.TryWriteAtomic(path, identity, snapshot, out error)) return false;
                    // 写成才记「盘上已是这份管线数」：在 _s2aSaves 串行区内按版本序推进，晚到的旧快照不回退标记
                    System.Threading.Volatile.Write(ref _s2aSavedAtPipelines, pipelines);
                    return true;
                }, out bool skipped);
                if (skipped)
                {
                    Console.Error.WriteLine($"[vkpipe] 驱动管线缓存这份快照{label}不写：盘上已是更新的快照");
                    return true;
                }
                if (written)
                {
                    Console.Error.WriteLine($"[vkpipe] 驱动管线缓存已保存{label} {snapshot.Length} 字节");
                    return true;
                }
                Console.Error.WriteLine($"[vkpipe] 驱动管线缓存保存失败{label}：{error}");
                return false;
            });
            _s2aPendingSave = save;

            // 定期落盘不界等：写盘继续在后台，编译通道立刻让给新管线；收局路径才有界等。
            return S2AVersionedSave.AwaitReceipt(save, 1000, label, waitForWrite);
        }

        protected virtual void Dispose(bool disposing)
        {
            if (disposing && !_s2aDisposed)
            {
                _s2aDisposed = true;

                // S2A：提前落盘的后台写还没完就先有界等它（≤ 1 s），再判断要不要重存；两次写盘由 _s2aSaves 串行、旧不覆盖新
                if (_s2aPendingSave is { IsCompleted: false } pending)
                    pending.Wait(1000);
                // S2A：收局请求时 / 单线程 WSI 退休前已提前写成、之后没有新管线 → 不再重存（缓存内容相同，省一次几 MB 的同步落盘）
                if (_s2aOwnsPipelineCache && System.Threading.Volatile.Read(ref _s2aSavedAtPipelines) == System.Threading.Interlocked.Read(ref PipelineState.S2APipelinesCreated))
                    Console.Error.WriteLine("[vkpipe] 驱动管线缓存与提前落盘的一致（之后没有新管线），释放时不再重存");
                else if (_s2aOwnsPipelineCache)
                    S2ASavePipelineCache();

                _nullRenderPass?.Dispose();
                _newState.Dispose();
                _descriptorSetUpdater.Dispose();
                _vertexBufferUpdater.Dispose();

                for (int i = 0; i < _vertexBuffers.Length; i++)
                {
                    _vertexBuffers[i].Dispose();
                }

                for (int i = 0; i < _transformFeedbackBuffers.Length; i++)
                {
                    _transformFeedbackBuffers[i].Dispose();
                }

                Pipeline?.Dispose();

                if (_s2aOwnsPipelineCache)
                {
                    unsafe
                    {
                        Gd.Api.DestroyPipelineCache(Device, PipelineCache, null);
                    }
                }
            }
        }

        public void Dispose()
        {
            Dispose(true);
        }
    }
}
