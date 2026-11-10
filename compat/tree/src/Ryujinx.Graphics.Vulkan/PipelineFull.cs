using Ryujinx.Graphics.GAL;
using Ryujinx.Graphics.Vulkan.Queries;
using Silk.NET.Vulkan;
using System;
using System.Collections.Generic;

namespace Ryujinx.Graphics.Vulkan
{
    class PipelineFull : PipelineBase, IPipeline
    {
        private const ulong MinByteWeightForFlush = 256 * 1024 * 1024; // MiB

        // Queries keep begin/end inside the same subpass (VUID 07004/07007). A query that stays
        // open across a pass boundary is split into per-pass segments: each segment owns one pool
        // slot and one buffer slot, and the guest-visible result is the CPU-side sum.
        private readonly List<(QueryPool Pool, bool IsOcclusion, BufferedQuery Query)> _activeQueries;
        private readonly List<(QueryPool Pool, bool IsOcclusion, BufferedQuery Query)> _pendingQueryBegins;
        private readonly List<(QueryPool Pool, bool IsOcclusion, BufferedQuery Query)> _resumeQueries;
        private CounterQueueEvent _activeConditionalRender;

        private readonly List<(BufferedQuery Query, int Segment)> _pendingQueryCopies;
        private readonly List<BufferHolder> _activeBufferMirrors;

        private ulong _byteWeight;
        private int _transferClearsSinceFlush;
        private const int MaxTransferClears = 64;
        private HashSet<(Silk.NET.Vulkan.Format, uint)> _transferClearFallbacks;

        private readonly List<BufferHolder> _backingSwaps;

        public PipelineFull(VulkanRenderer gd, Device device) : base(gd, device)
        {
            _activeQueries = [];
            _pendingQueryBegins = [];
            _resumeQueries = [];
            _pendingQueryCopies = [];
            _backingSwaps = [];
            _activeBufferMirrors = [];

            CommandBuffer = (Cbs = gd.CommandBufferPool.Rent()).CommandBuffer;

            IsMainPipeline = true;
        }

        private void CopyPendingQuery()
        {
            foreach ((BufferedQuery query, int segment) in _pendingQueryCopies)
            {
                query.PoolCopy(Cbs, segment);
            }

            _pendingQueryCopies.Clear();
        }

        public void ClearRenderTargetColor(int index, int layer, int layerCount, uint componentMask, ColorF color)
        {
            if (FramebufferParams == null)
            {
                return;
            }

            if (Gd.TransferFullClears && TryClearColorByTransfer(index, layer, layerCount, componentMask, color))
            {
                return;
            }
            if (Gd.TransferFullClears) LogTransferClearFallback(index, componentMask);

            if (componentMask != 0xf || Gd.IsQualcommProprietary)
            {
                // We can't use CmdClearAttachments if not writing all components,
                // because on Vulkan, the pipeline state does not affect clears.
                // On proprietary Adreno drivers, CmdClearAttachments appears to execute out of order, so it's better to not use it at all.
                // S2A（#468）：目标因写掩码为 0 不在帧缓冲里时（PipelineBase.ZeroMaskWantedTargets），清除照样落到它上面。
                // 全掩码清除不走这里：PipelineBase.ClearRenderTargetColor 会先把目标放回帧缓冲。
                TextureView dstTexture = FramebufferParams.GetColorView(index);
                TextureView filteredOut = dstTexture == null ? ZeroMaskFilteredColorView(index) : null;
                dstTexture ??= filteredOut;
                if (dstTexture == null)
                {
                    return;
                }

                Span<float> clearColor = stackalloc float[4];
                clearColor[0] = color.Red;
                clearColor[1] = color.Green;
                clearColor[2] = color.Blue;
                clearColor[3] = color.Alpha;

                // TODO: Clear only the specified layer.
                Gd.HelperShader.Clear(
                    Gd,
                    dstTexture,
                    clearColor,
                    componentMask,
                    filteredOut != null ? dstTexture.Width : (int)FramebufferParams.Width,
                    filteredOut != null ? dstTexture.Height : (int)FramebufferParams.Height,
                    filteredOut != null ? ComponentTypeOf(dstTexture) : FramebufferParams.GetAttachmentComponentType(index),
                    ClearScissor);
            }
            else
            {
                ClearRenderTargetColor(index, layer, layerCount, color);
            }
        }

        // S2A（#468）：不在帧缓冲里的目标按格式取分量类型，规则同 FramebufferParams.GetAttachmentComponentType。
        private static ComponentType ComponentTypeOf(TextureView view)
        {
            return view.Info.Format.IsSignedInt ? ComponentType.SignedInteger
                : view.Info.Format.IsUnsignedInt ? ComponentType.UnsignedInteger
                : ComponentType.Float;
        }

        private void LogTransferClearFallback(int index, uint mask)
        {
            _transferClearFallbacks ??= new();
            if (_transferClearFallbacks.Count >= 8) return;
            TextureView dst = FramebufferParams.GetColorView(index);
            if (dst == null || !_transferClearFallbacks.Add((dst.VkFormat, mask))) return;
            Console.Error.WriteLine($"[s2a] transfer clear fallback: {dst.VkFormat}, mask={mask:x}, " +
                $"view={dst.Width}x{dst.Height}, framebuffer={FramebufferParams.Width}x{FramebufferParams.Height}, " +
                $"scissor={ClearScissor.X},{ClearScissor.Y},{ClearScissor.Width},{ClearScissor.Height}, " +
                $"target={dst.Info.Target}, samples={dst.Info.Samples}, level={dst.FirstLevel}, layer={dst.FirstLayer}");
        }

        private bool TryClearColorByTransfer(int index, int layer, int layerCount, uint componentMask, ColorF color)
        {
            if (!Gd.IsQualcommProprietary || componentMask != 0xf || layer != 0 || layerCount != 1 ||
                _activeConditionalRender != null || !FramebufferParams.IsValidColorAttachment(index))
            {
                return false;
            }

            TextureView dst = FramebufferParams.GetColorView(index);
            // ClearColorImage acts on storage, not a view/scissored rectangle. Start with
            // the real-device-tested formats and exact base-level 2D storage coverage.
            if (dst == null || !dst.Valid || dst.Storage == null ||
                dst.VkFormat is not (Silk.NET.Vulkan.Format.R8G8B8A8Unorm or Silk.NET.Vulkan.Format.B8G8R8A8Unorm or
                    Silk.NET.Vulkan.Format.R8G8B8A8Srgb or Silk.NET.Vulkan.Format.R16G16B16A16Sfloat or
                    Silk.NET.Vulkan.Format.B10G11R11UfloatPack32 or Silk.NET.Vulkan.Format.R8Unorm or
                    Silk.NET.Vulkan.Format.R16G16SNorm or Silk.NET.Vulkan.Format.R8Uint) ||
                dst.VkFormat != dst.Storage.VkFormat || dst.FirstLayer != 0 || dst.FirstLevel != 0 ||
                dst.Info.Target != Target.Texture2D || dst.Storage.Info.Target != Target.Texture2D ||
                dst.Info.Samples != 1 || dst.Storage.Info.Samples != 1 ||
                dst.Width != dst.Storage.Info.Width || dst.Height != dst.Storage.Info.Height ||
                FramebufferParams.Width != dst.Width || FramebufferParams.Height != dst.Height ||
                ClearScissor.X != 0 || ClearScissor.Y != 0 ||
                ClearScissor.Width < dst.Width || ClearScissor.Height < dst.Height ||
                (dst.Storage.UsageFlags & ImageUsageFlags.TransferDstBit) == 0 ||
                !Gd.FormatCapabilities.OptimalFormatSupports(FormatFeatureFlags.TransferDstBit, dst.Info.Format))
            {
                return false;
            }

            if (AutoFlush.ShouldFlushQuery() || _transferClearsSinceFlush >= MaxTransferClears)
            {
                FlushCommandsImpl();
            }
            EndRenderPass();
            Gd.Barriers.Flush(Cbs, false, null, null);
            Image image = dst.GetImage().Get(Cbs).Value; // Retain storage until this CB completes.
            TextureView.InsertImageBarrier(Gd.Api, CommandBuffer, image,
                AccessFlags.MemoryReadBit | AccessFlags.MemoryWriteBit, AccessFlags.TransferWriteBit,
                PipelineStageFlags.AllCommandsBit, PipelineStageFlags.TransferBit,
                ImageAspectFlags.ColorBit, 0, 0, 1, 1);
            ClearColorValue value = new(color.Red, color.Green, color.Blue, color.Alpha);
            ImageSubresourceRange range = new(ImageAspectFlags.ColorBit, 0, 1, 0, 1);
            Gd.Api.CmdClearColorImage(CommandBuffer, image, ImageLayout.General, in value, 1, in range);
            dst.Storage.RecordModification(AccessFlags.TransferWriteBit, PipelineStageFlags.TransferBit);
            TextureView.InsertImageBarrier(Gd.Api, CommandBuffer, image,
                AccessFlags.TransferWriteBit, AccessFlags.MemoryReadBit | AccessFlags.MemoryWriteBit,
                PipelineStageFlags.TransferBit, PipelineStageFlags.AllCommandsBit,
                ImageAspectFlags.ColorBit, 0, 0, 1, 1);
            _transferClearsSinceFlush++;
            if (++Gd.TransferFullClearCount == 1)
            {
                Console.Error.WriteLine($"[s2a] Vulkan full-color transfer clear used: {dst.VkFormat} {dst.Width}x{dst.Height}");
            }
            return true;
        }

        public void ClearRenderTargetDepthStencil(int layer, int layerCount, float depthValue, bool depthMask, int stencilValue, int stencilMask)
        {
            if (FramebufferParams == null)
            {
                return;
            }

            if ((stencilMask != 0 && stencilMask != 0xff) || Gd.IsQualcommProprietary)
            {
                // We can't use CmdClearAttachments if not clearing all (mask is all ones, 0xFF) or none (mask is 0) of the stencil bits,
                // because on Vulkan, the pipeline state does not affect clears.
                // On proprietary Adreno drivers, CmdClearAttachments appears to execute out of order, so it's better to not use it at all.
                TextureView dstTexture = FramebufferParams.GetDepthStencilView();
                if (dstTexture == null)
                {
                    return;
                }

                // TODO: Clear only the specified layer.
                Gd.HelperShader.Clear(
                    Gd,
                    dstTexture,
                    depthValue,
                    depthMask,
                    stencilValue,
                    stencilMask,
                    (int)FramebufferParams.Width,
                    (int)FramebufferParams.Height,
                    FramebufferParams.AttachmentFormats[FramebufferParams.AttachmentsCount - 1],
                    ClearScissor);
            }
            else
            {
                ClearRenderTargetDepthStencil(layer, layerCount, depthValue, depthMask, stencilValue, stencilMask != 0);
            }
        }

        public void EndHostConditionalRendering()
        {
            if (Gd.Capabilities.SupportsConditionalRendering)
            {
                // Gd.ConditionalRenderingApi.CmdEndConditionalRendering(CommandBuffer);
            }
            else
            {
                // throw new NotSupportedException();
            }

            _activeConditionalRender?.ReleaseHostAccess();
            _activeConditionalRender = null;
        }

        public bool TryHostConditionalRendering(ICounterEvent value, ulong compare, bool isEqual)
        {
            // Compare an event and a constant value.
            if (value is CounterQueueEvent evt)
            {
                // Easy host conditional rendering when the check matches what GL can do:
                //  - Event is of type samples passed.
                //  - Result is not a combination of multiple queries.
                //  - Comparing against 0.
                //  - Event has not already been flushed.

                if (compare == 0 && evt.Type == CounterType.SamplesPassed && evt.ClearCounter)
                {
                    if (!value.ReserveForHostAccess())
                    {
                        // If the event has been flushed, then just use the values on the CPU.
                        // The query object may already be repurposed for another draw (eg. begin + end).
                        return false;
                    }

                    if (Gd.Capabilities.SupportsConditionalRendering)
                    {
                        // var buffer = evt.GetBuffer().Get(Cbs, 0, sizeof(long)).Value;
                        // var flags = isEqual ? ConditionalRenderingFlagsEXT.InvertedBitExt : 0;

                        // var conditionalRenderingBeginInfo = new ConditionalRenderingBeginInfoEXT
                        // {
                        //     SType = StructureType.ConditionalRenderingBeginInfoExt,
                        //     Buffer = buffer,
                        //     Flags = flags,
                        // };

                        // Gd.ConditionalRenderingApi.CmdBeginConditionalRendering(CommandBuffer, conditionalRenderingBeginInfo);
                    }

                    _activeConditionalRender = evt;
                    return true;
                }
            }

            // The GPU will flush the queries to CPU and evaluate the condition there instead.

            FlushPendingQuery(); // The thread will be stalled manually flushing the counter, so flush commands now.
            return false;
        }

        public bool TryHostConditionalRendering(ICounterEvent value, ICounterEvent compare, bool isEqual)
        {
            FlushPendingQuery(); // The thread will be stalled manually flushing the counter, so flush commands now.
            return false;
        }

        private void FlushPendingQuery()
        {
            if (AutoFlush.ShouldFlushQuery())
            {
                FlushCommandsImpl();
            }
        }

        public CommandBufferScoped GetPreloadCommandBuffer()
        {
            PreloadCbs ??= Gd.CommandBufferPool.Rent();

            return PreloadCbs.Value;
        }

        public void FlushCommandsIfWeightExceeding(IAuto disposedResource, ulong byteWeight)
        {
            bool usedByCurrentCb = disposedResource.HasCommandBufferDependency(Cbs);

            if (PreloadCbs != null && !usedByCurrentCb)
            {
                usedByCurrentCb = disposedResource.HasCommandBufferDependency(PreloadCbs.Value);
            }

            if (usedByCurrentCb)
            {
                // Since we can only free memory after the command buffer that uses a given resource was executed,
                // keeping the command buffer might cause a high amount of memory to be in use.
                // To prevent that, we force submit command buffers if the memory usage by resources
                // in use by the current command buffer is above a given limit, and those resources were disposed.
                _byteWeight += byteWeight;

                if (_byteWeight >= MinByteWeightForFlush)
                {
                    FlushCommandsImpl();
                }
            }
        }

        public void Restore()
        {
            if (Pipeline != null)
            {
                Gd.Api.CmdBindPipeline(CommandBuffer, Pbp, Pipeline.Get(Cbs).Value);
            }

            SignalCommandBufferChange();

            if (Pipeline != null && Pbp == PipelineBindPoint.Graphics)
            {
                DynamicState.ReplayIfDirty(Gd, CommandBuffer);
            }
        }

        public void FlushCommandsImpl()
        {
            _transferClearsSinceFlush = 0;
            AutoFlush.RegisterFlush(DrawCount);
            EndRenderPass();

            _byteWeight = 0;

            if (PreloadCbs != null)
            {
                PreloadCbs.Value.Dispose();
                PreloadCbs = null;
            }

            Gd.Barriers.Flush(Cbs, false, null, null);
            Gd.S2AStorageOob?.InsertHostReadBarrier(Cbs);   // S2A（F1）：用过带越界记录的程序时，结束前让记录对主机可见
            CommandBuffer = (Cbs = Gd.CommandBufferPool.ReturnAndRent(Cbs)).CommandBuffer;
            Gd.RegisterFlush();

            // Restore per-command buffer state.
            foreach (BufferHolder buffer in _activeBufferMirrors)
            {
                buffer.ClearMirrors();
            }

            _activeBufferMirrors.Clear();

            Gd.ResetCounterPool();

            Restore();
        }

        public void RegisterActiveMirror(BufferHolder buffer)
        {
            _activeBufferMirrors.Add(buffer);
        }

        public void BeginQuery(BufferedQuery query, QueryPool pool, bool needsReset, bool isOcclusion, bool fromSamplePool)
        {
            if (needsReset)
            {
                EndRenderPass();

                Gd.Api.CmdResetQueryPool(CommandBuffer, pool, 0, BufferedQuery.MaxQuerySegments);
                query.OnPoolReset();

                if (fromSamplePool)
                {
                    // Try reset some additional queries in advance.

                    Gd.ResetFutureCounters(CommandBuffer, AutoFlush.GetRemainingQueries());
                }
            }

            if (RenderPassActive)
            {
                int slot = query.BeginSegment();
                if (slot < 0)
                {
                    return;
                }

                bool isPrecise = Gd.Capabilities.SupportsPreciseOcclusionQueries && isOcclusion;
                Gd.Api.CmdBeginQuery(CommandBuffer, pool, (uint)slot, isPrecise ? QueryControlFlags.PreciseBit : 0);

                _activeQueries.Add((pool, isOcclusion, query));
            }
            else
            {
                // Occlusion/transform-feedback queries are only valid inside a subpass;
                // deferring every out-of-pass begin to the next pass's subpass 0 keeps one code path.
                _pendingQueryBegins.Add((pool, isOcclusion, query));
            }
        }

        public bool EndQuery(QueryPool pool)
        {
            for (int i = 0; i < _activeQueries.Count; i++)
            {
                if (_activeQueries[i].Pool.Handle == pool.Handle)
                {
                    int slot = _activeQueries[i].Query.SegmentEnded();
                    _activeQueries.RemoveAt(i);
                    Gd.Api.CmdEndQuery(CommandBuffer, pool, (uint)slot);
                    return true;
                }
            }

            for (int i = 0; i < _resumeQueries.Count; i++)
            {
                if (_resumeQueries[i].Pool.Handle == pool.Handle)
                {
                    // The closing pass already ended the recording segment.
                    _resumeQueries.RemoveAt(i);
                    return false;
                }
            }

            for (int i = 0; i < _pendingQueryBegins.Count; i++)
            {
                if (_pendingQueryBegins[i].Pool.Handle == pool.Handle)
                {
                    // Begin not recorded yet; the drain records an adjacent begin/end pair.
                    _pendingQueryBegins[i].Query.EndDeferred = true;
                    return false;
                }
            }

            return false;
        }

        public void CopyQueryResults(BufferedQuery query, int segment)
        {
            CopyQueryResults(query, segment, 1);
        }

        public void CopyQueryResults(BufferedQuery query, int firstSegment, int count)
        {
            for (int i = 0; i < count; i++)
            {
                _pendingQueryCopies.Add((query, firstSegment + i));
            }

            if (count > 0 && AutoFlush.RegisterPendingQuery())
            {
                FlushCommandsImpl();
            }
        }

        // Ends every segment still recording before vkCmdEndRenderPass (VUID 07004).
        // Copies are deliberately not queued here: a mid-cycle Reset clears the slots
        // CPU-side and a stale copy landing afterwards reads back as ready-but-wrong.
        protected override void CloseQueriesForRenderPassEnd()
        {
            foreach ((QueryPool pool, _, BufferedQuery query) in _activeQueries)
            {
                int slot = query.SegmentEnded();
                Gd.Api.CmdEndQuery(CommandBuffer, pool, (uint)slot);
            }

            _resumeQueries.AddRange(_activeQueries);
            _activeQueries.Clear();
        }

        // Records deferred begins and resumes pass-split queries at subpass 0.
        protected override void DrainPendingQueryBegins()
        {
            foreach ((QueryPool pool, bool isOcclusion, BufferedQuery query) in _pendingQueryBegins)
            {
                int slot = query.BeginSegment();
                if (slot < 0)
                {
                    continue;
                }

                bool isPrecise = Gd.Capabilities.SupportsPreciseOcclusionQueries && isOcclusion;
                Gd.Api.CmdBeginQuery(CommandBuffer, pool, (uint)slot, isPrecise ? QueryControlFlags.PreciseBit : 0);
                if (query.EndDeferred)
                {
                    // End arrived first: emit the adjacent pair so the query contributes nothing.
                    query.EndDeferred = false;
                    Gd.Api.CmdEndQuery(CommandBuffer, pool, (uint)slot);
                    query.SegmentEnded();
                }
                else
                {
                    _activeQueries.Add((pool, isOcclusion, query));
                }
            }

            _pendingQueryBegins.Clear();

            foreach ((QueryPool pool, bool isOcclusion, BufferedQuery query) in _resumeQueries)
            {
                int slot = query.BeginSegment();
                if (slot < 0)
                {
                    continue;
                }

                bool isPrecise = Gd.Capabilities.SupportsPreciseOcclusionQueries && isOcclusion;
                Gd.Api.CmdBeginQuery(CommandBuffer, pool, (uint)slot, isPrecise ? QueryControlFlags.PreciseBit : 0);
                _activeQueries.Add((pool, isOcclusion, query));
            }

            _resumeQueries.Clear();
        }

        protected override void SignalAttachmentChange()
        {
            if (AutoFlush.ShouldFlushAttachmentChange(DrawCount))
            {
                FlushCommandsImpl();
            }
        }

        protected override void SignalRenderPassEnd()
        {
            CopyPendingQuery();
        }
    }
}
