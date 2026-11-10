using Gommon;
using S2Arm.Backends.Vulkan;
using S2Arm.Backends.Vulkan.ProgramCache;
using Ryujinx.Common.Configuration;
using Ryujinx.Common.Helper;
using Ryujinx.Common.Logging;
using Ryujinx.Graphics.GAL;
using Ryujinx.Graphics.Shader;
using Ryujinx.Graphics.Shader.Translation;
using Ryujinx.Graphics.Vulkan.MoltenVK;
using Ryujinx.Graphics.Vulkan.Queries;
using Silk.NET.Vulkan;
using Silk.NET.Vulkan.Extensions.EXT;
using Silk.NET.Vulkan.Extensions.KHR;
using System;
using System.Collections.Generic;
using System.Linq;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using Format = Ryujinx.Graphics.GAL.Format;
using PrimitiveTopology = Ryujinx.Graphics.GAL.PrimitiveTopology;
using SamplerCreateInfo = Ryujinx.Graphics.GAL.SamplerCreateInfo;

namespace Ryujinx.Graphics.Vulkan
{
    public static class S2AVulkanCaps
    {
        // Set once by the platform before GPU initialization; zero adds no S2A requirement.
        public static uint MinimumApiVersion { get; set; }
        public static string UnsupportedMessage { get; set; }

        internal static void CheckApiVersion(uint version)
        {
            if (MinimumApiVersion != 0 && version < MinimumApiVersion)
            {
                throw new NotSupportedException(UnsupportedMessage ?? "Initialization failed, none of the available GPUs meets the minimum requirements.");
            }
        }
    }

    public sealed class VulkanRenderer : IRenderer, IHostProgramCachePolicy, IComputeTranslationProfileProvider
    {
        private VulkanInstance _instance;
        private SurfaceKHR _surface;
        private VulkanPhysicalDevice _physicalDevice;
        private Device _device;
        private DeviceFeatureSnapshot _enabledFeatures;
        private WindowBase _window;

        // The Android adapter borrows this renderer's queue/pool and owns its leased surface.
        // Other frontends retain the original Window and renderer-owned surface lifetime.
        internal Func<VulkanRenderer, SurfaceKHR, PhysicalDevice, Device, WindowBase> WindowFactory { get; set; }
        // Instance policy: a host that replaces guest backing must not retain imported pointers.
        internal bool AllowHostMemoryImport { get; set; } = true;
        internal bool UseSingleQueue { get; set; }
        // Only the Android adapter can opt out through its debug-build QA gate.
        internal bool S2AForceNoSwapchainMaintenance { get; set; }
        internal static bool S2ASurfaceMaintenanceEnabled(bool androidWindow, bool instanceExtension, bool qaForceOff) =>
            androidWindow && instanceExtension && !qaForceOff;
        // Android opt-in; keep proprietary-driver attachment clears on the existing path.
        internal bool TransferFullClears { get; set; }
        internal long TransferFullClearCount { get; set; }
        internal DeviceFeatureSnapshot EnabledDeviceFeatures => _enabledFeatures;

        private bool _initialized;

        public uint ProgramCount { get; set; } = 0;

        internal FormatCapabilities FormatCapabilities { get; private set; }
        internal HardwareCapabilities Capabilities;
        internal int MinTexelBufferOffsetAlignment { get; private set; }

        internal Vk Api { get; private set; }
        internal KhrSurface SurfaceApi { get; private set; }
        internal KhrSwapchain SwapchainApi { get; private set; }
        internal ExtConditionalRendering ConditionalRenderingApi { get; private set; }
        internal ExtExtendedDynamicState ExtendedDynamicStateApi { get; private set; }
        internal KhrPushDescriptor PushDescriptorApi { get; private set; }
        internal ExtTransformFeedback TransformFeedbackApi { get; private set; }
        internal KhrDrawIndirectCount DrawIndirectCountApi { get; private set; }
        internal ExtAttachmentFeedbackLoopDynamicState DynamicFeedbackLoopApi { get; private set; }

        internal uint QueueFamilyIndex { get; private set; }
        internal Queue Queue { get; private set; }
        internal Queue BackgroundQueue { get; private set; }
        internal Lock BackgroundQueueLock { get; private set; }
        internal Lock QueueLock { get; private set; }

        internal MemoryAllocator MemoryAllocator { get; private set; }
        internal HostMemoryAllocator HostMemoryAllocator { get; private set; }
        internal CommandBufferPool CommandBufferPool { get; private set; }
        internal PipelineLayoutCache PipelineLayoutCache { get; private set; }
        internal BackgroundResources BackgroundResources { get; private set; }
        internal Action<Action> InterruptAction { get; private set; }
        internal SyncManager SyncManager { get; private set; }

        internal BufferManager BufferManager { get; private set; }

        /// <summary>S2A（F1）：存储缓冲越界记录（按程序分段，命令缓冲完成后读回报红）；缺省关（S2A_SSBO_OOB_DIAG=1 才建），关时为 null</summary>
        internal S2AStorageOobRecorder S2AStorageOob { get; private set; }

        internal HashSet<ShaderCollection> Shaders { get; }
        internal HashSet<ITexture> Textures { get; }
        internal HashSet<SamplerHolder> Samplers { get; }

        private VulkanDebugMessenger _debugMessenger;
        private Counters _counters;

        private PipelineFull _pipeline;

        // Android shader/pipeline cache lane. The owner pumps S2AWaitPump while the single
        // worker executes a driver call; the host sets the pump to the WSI lease poll.
        internal CompileWorker S2ACompileWorker { get; set; }
        internal Action S2AWaitPump { get; set; }
        internal Action S2ADeviceCleanup { get; set; }

        /// <summary>
        /// S2A：本渲染器里「等别的线程推进」的循环（计数队列 FlushTo）每次醒来调一次，可抛异常结束等待；宿主（Android 拆线程）
        /// 用它做有界与失败闩检查。空 = 原样
        /// </summary>
        public Action S2AWaitCheck { get; set; }
        public bool S2ACounterLifetime { get; set; }
        public bool S2AStopCountersForExit(int milliseconds) => _counters.S2AStopForExit(milliseconds);

        // Query results counted on the failure path this session, for the收局 summary line.
        // 口径: each bounded-wait expiry counts once (an event retried every 500 ms counts
        // per expiry, per the逐拍报红 policy); a latched segment overflow counts once per query.
        private long _queryTimeouts;
        public long QueryTimeouts => Interlocked.Read(ref _queryTimeouts);
        internal void IncrementQueryTimeouts() => Interlocked.Increment(ref _queryTimeouts);

        private long _s2aUniformBufferRangeErrors;
        public long S2AUniformBufferRangeErrors => Interlocked.Read(ref _s2aUniformBufferRangeErrors);
        public string S2AUniformBufferRangeSummary => $"UBO 范围不足 {S2AUniformBufferRangeErrors} 次";
        internal long S2ANoteUniformBufferRangeError() => Interlocked.Increment(ref _s2aUniformBufferRangeErrors);

        // S2A：空 storage image view 回退到 1×1 dummy 的次数，并入收局汇总行。
        private long _s2aNullImageFallbacks;
        public long S2ANullImageFallbacks => Interlocked.Read(ref _s2aNullImageFallbacks);
        internal void S2ANoteNullImageFallback() => Interlocked.Increment(ref _s2aNullImageFallbacks);

        // S2A：空 texel buffer view 回退到 R32_UINT dummy view 的次数，并入收局汇总行。
        private long _s2aNullTexelFallbacks;
        public long S2ANullTexelFallbacks => Interlocked.Read(ref _s2aNullTexelFallbacks);
        internal void S2ANoteNullTexelFallback() => Interlocked.Increment(ref _s2aNullTexelFallbacks);

        /// <summary>
        /// S2A（Android 拆线程收局）：把一次驱动管线缓存落盘排到编译通道（高优先级，与管线创建串行），不等它；返回该次落盘是否写成的任务，
        /// 没有编译通道 / 管线 / 通道已在停 = null。释放时照常再存一次
        /// </summary>
        public Task<bool> S2ASavePipelineCacheEarly()
        {
            CompileWorker worker = S2ACompileWorker;
            PipelineFull pipeline = _pipeline;
            if (worker == null || pipeline == null || worker.IsStopping) return null;

            return worker.Run(pipeline.S2ASavePipelineCacheEarly, highPriority: true);
        }

        /// <summary>
        /// S2A（Android 拆线程收局、进程马上退出）：提前落盘之后又建了管线才再排一次落盘到编译通道（不等）；没有新管线 = 已完成的 true，
        /// 没有编译通道 / 管线 = null。代替释放时的落盘（这时下层不逐个释放）
        /// </summary>
        public Task<bool> S2ASavePipelineCacheForExit()
        {
            CompileWorker worker = S2ACompileWorker;
            PipelineFull pipeline = _pipeline;
            if (worker == null || pipeline == null || worker.IsStopping) return null;
            if (pipeline.S2APipelineCacheCurrent) return Task.FromResult(true);

            return worker.Run(pipeline.S2ASavePipelineCacheForExit, highPriority: true);
        }

        /// <summary>
        /// S2A（11-03 定期落盘）：有编译通道时排到通道（普通优先级，避免插到正在创建的管线前），没有通道时由 GPU owner 直接落盘；
        /// 没有新管线 = 已完成的 true，没有管线 / 通道已在停 = null。失败只记日志，不改变渲染路径。
        /// </summary>
        public Task<bool> S2ASavePipelineCachePeriodic()
        {
            CompileWorker worker = S2ACompileWorker;
            PipelineFull pipeline = _pipeline;
            if (pipeline == null) return null;
            if (worker == null) return Task.FromResult(pipeline.S2ASavePipelineCachePeriodic());
            if (worker.IsStopping) return null;

            return worker.Run(pipeline.S2ASavePipelineCachePeriodic, highPriority: false);
        }

        /// <summary>
        /// S2A（Android 单线程收局）：在停 WSI / 证明退休之前，把驱动管线缓存落盘一次（走同一编译通道，与管线创建串行）。
        /// 原生收局兜底 1.5 s，单线程完整释放 + 窗口 / WSI 退休可能吃掉全部预算（9000 r8 18:02 局：GPU Run 退出 +0.670 s、
        /// Surface detached +1.029 s、1.509 s 被 _exit，落盘排在 WSI 退休之后没赶上）。这里先落盘（几十毫秒），后面超时也不丢。
        /// 返回是否写成；没有编译通道 / 管线 / 通道已在停 = false。
        /// </summary>
        public bool S2ASavePipelineCacheBeforeWsiRetire()
        {
            CompileWorker worker = S2ACompileWorker;
            PipelineFull pipeline = _pipeline;
            if (worker == null || pipeline == null || worker.IsStopping) return false;
            if (pipeline.S2APipelineCacheCurrent) return true;

            Task<bool> save = worker.Run(pipeline.S2ASavePipelineCacheBeforeWsiRetire, highPriority: true);
            return save != null && save.Wait(1200) && save.IsCompletedSuccessfully && save.Result;
        }
        internal string S2APipelineCachePath { get; set; }
        private readonly object _s2aProgramIdentityGate = new();
        private ProgramBinaryIdentity _s2aProgramBinaryIdentity;
        private string _s2aHostCacheDiscriminator;
        private bool _s2aFreezeProgramProfile;
        private bool _s2aRequestedSpirv;
        private string _s2aGpuTranslationProducer;
        private readonly object _s2aProfileOwner = new();
        private ProgramBinaryProfileSnapshot _s2aProfileSnapshot;

        /// <summary>Android opts in before Initialize; the default retains the v1 cache path.</summary>
        public void S2AConfigureProgramProfileFreeze(bool requestedSpirv, string gpuTranslationProducer)
        {
            if (_initialized || _s2aFreezeProgramProfile)
                throw new InvalidOperationException("Configure the translation profile once, before device initialization.");
            _s2aRequestedSpirv = requestedSpirv;
            _s2aGpuTranslationProducer = gpuTranslationProducer;
            _s2aFreezeProgramProfile = true;
        }

        private VulkanProfile S2ASelectedProgramProfile =>
            (_s2aProfileSnapshot ?? throw new InvalidOperationException("The Vulkan device has no initialized translation profile."))
                .RequireCurrent(_s2aProfileOwner);

        public ComputeTranslationProfile ComputeTranslationProfile =>
            _s2aFreezeProgramProfile ? S2ASelectedProgramProfile.ComputeProfile : null;

        public Capabilities FrozenTranslationCapabilities => S2ASelectedProgramProfile.Capabilities;

        // This runs after SetupContext (including TryCreate of the optional OOB recorder),
        // and before the host may publish capabilities or consume shader cache files.
        private void S2ASelectProgramProfile()
        {
            if (!_s2aFreezeProgramProfile) return;
            ComputeTranslationProfile selected = Ryujinx.Graphics.GAL.ComputeTranslationProfile.Select(
                _s2aRequestedSpirv, _physicalDevice.PhysicalDeviceFeatures.RobustBufferAccess,
                _enabledFeatures.Core.RobustBufferAccess, S2AStorageOob != null);
            Capabilities capabilities = BuildCapabilities(selected);
            S2ACompleteProgramProfile(in capabilities, selected);
        }

        // The query half stays in SetupContext/BuildCapabilities. This commit half publishes
        // exactly once and is CPU-testable with already captured device facts.
        private void S2ACompleteProgramProfile(in Capabilities capabilities, ComputeTranslationProfile selected)
        {
            if (!_s2aFreezeProgramProfile || _s2aProfileSnapshot != null)
                throw new InvalidOperationException("The renderer translation profile was not configured or is already frozen.");
            VulkanProfile profile = VulkanProfile.CreateFrozen(in capabilities, Shader.TargetVulkanVersion,
                S2ATranslatorClosure(), selected, _s2aGpuTranslationProducer,
                typeof(ComputeTranslationProfile).Assembly.ManifestModule.ModuleVersionId.ToString("N"));
            _s2aProfileSnapshot = new ProgramBinaryProfileSnapshot(_s2aProfileOwner, profile);
        }

        internal long S2AProgramBinaryLoads;
        internal long S2AProgramBinaryRejects;
        private static readonly bool S2ACacheDiagnostics = Environment.GetEnvironmentVariable("S2A_SHADER_CACHE_DIAG") == "1";
        private static bool S2AShouldLogProgramBinaryLoad(long loads) =>
            S2ACacheDiagnostics && (loads <= 8 || (loads & (loads - 1)) == 0);

        // Host-cache policy: VKPB records are loaded on demand through this renderer's single
        // compile lane. The shared GPU cache does not create/drop host programs in the
        // background for this backend; Apple backends leave the interface unimplemented.
        public bool DisableHostProgramPrefetch => true;

        public string HostProgramCacheDiscriminator
        {
            get
            {
                if (_s2aFreezeProgramProfile) _ = S2ASelectedProgramProfile;
                string discriminator = _s2aHostCacheDiscriminator;
                if (discriminator != null) return discriminator;
                ProgramBinaryIdentity identity = S2AProgramBinaryIdentity;
                discriminator = Convert.ToHexStringLower(SHA256.HashData(Encoding.ASCII.GetBytes(
                    $"{identity.Scheme}|{identity.ProfileKey}|{identity.TranslationKey}|{identity.BindingAbi}")));
                _s2aHostCacheDiscriminator = discriminator;
                return discriminator;
            }
        }

        internal VulkanPhysicalDevice PhysicalDevice => _physicalDevice;

        internal ProgramBinaryIdentity S2AProgramBinaryIdentity
        {
            get
            {
                if (_s2aFreezeProgramProfile) return S2ASelectedProgramProfile.Identity;
                ProgramBinaryIdentity identity = _s2aProgramBinaryIdentity;
                if (identity != null) return identity;

                lock (_s2aProgramIdentityGate)
                {
                    identity = _s2aProgramBinaryIdentity;
                    if (identity != null) return identity;
                    identity = S2ACreateProgramProfile().Identity;
                    _s2aProgramBinaryIdentity = identity;
                    return identity;
                }
            }
        }

        // One producer for the offline pack (S2) and the runtime identity: same capabilities,
        // same SPIR-V target, same translator closure.
        internal VulkanProfile S2ACreateProgramProfile()
        {
            if (_s2aFreezeProgramProfile) return S2ASelectedProgramProfile;
            Capabilities capabilities = GetCapabilities();
            return VulkanProfile.Create(in capabilities, Shader.TargetVulkanVersion, S2ATranslatorClosure());
        }

        internal static string S2ATranslatorClosure() =>
            typeof(ShaderProgram).Assembly.ManifestModule.ModuleVersionId.ToString("N") +
            ":" + typeof(VulkanRenderer).Assembly.ManifestModule.ModuleVersionId.ToString("N");

        /// <summary>
        /// Serializes the exact translation profile the device uses for VKPB records. The
        /// offline package producer consumes this file; it is not a capability guess.
        /// </summary>
        public byte[] S2ASerializeProgramProfile() => S2ACreateProgramProfile().ToJson();

        internal T S2ARunDriver<T>(Func<T> work)
        {
            CompileWorker worker = S2ACompileWorker;
            return worker == null ? work() : worker.RunInline(work, S2AWaitPump);
        }

        internal void S2AWaitForTask(Task task)
        {
            CompileWorker worker = S2ACompileWorker;
            if (worker == null) task.Wait();
            else worker.Wait(task, S2AWaitPump);
        }

        /// <summary>
        /// S2A：有界等待一个后台任务，等待期间照常泵宿主事件（S2AWaitPump：WSI 租约 / 停止）。
        /// 返回 false = 超时、被取消或任务失败；不把加载屏卡死，也不把失败当成功。
        /// </summary>
        internal bool S2AWaitForTask(Task task, int timeoutMs)
        {
            ArgumentNullException.ThrowIfNull(task);
            ArgumentOutOfRangeException.ThrowIfLessThan(timeoutMs, 1);

            CompileWorker worker = S2ACompileWorker;

            if (worker == null)
            {
                // 没有编译通道：退化为有界阻塞等待（苹果路径不进入加载屏预热，这里只作兜底）。
                try
                {
                    return task.Wait(timeoutMs);
                }
                catch (AggregateException)
                {
                    return false;
                }
            }

            // 编译通道线程不能等排在自己队列里的活（会自锁）；已完成才算数。
            if (worker.IsWorkerThread)
            {
                return task.IsCompletedSuccessfully;
            }

            try
            {
                worker.Wait(task, S2AWaitPump, timeoutMs);
                return true;
            }
            catch (TimeoutException)
            {
                return false;
            }
            catch (OperationCanceledException)
            {
                return false;
            }
            catch (Exception)
            {
                return false;
            }
        }

        internal HelperShader HelperShader { get; private set; }
        internal PipelineFull PipelineInternal => _pipeline;

        internal BarrierBatch Barriers { get; private set; }

        public IPipeline Pipeline => _pipeline;

        public IWindow Window => _window;

        private readonly Func<Instance, Vk, SurfaceKHR> _getSurface;
        private readonly Func<string[]> _getRequiredExtensions;
        private readonly string _preferredGpuId;

        private int[] _pdReservedBindings;
        private readonly static int[] _pdReservedBindingsNvn = [3, 18, 21, 36, 30];
        private readonly static int[] _pdReservedBindingsOgl = [17, 18, 34, 35, 36];

        internal Vendor Vendor { get; private set; }
        internal bool IsAmdWindows { get; private set; }
        internal bool IsIntelWindows { get; private set; }
        internal bool IsIntelLinux { get; private set; }
        internal bool IsAmdGcn { get; private set; }
        internal bool IsAmdRdna3 { get; private set; }
        internal bool IsNvidiaPreTuring { get; private set; }
        internal bool IsIntelArc { get; private set; }
        internal bool IsQualcommProprietary { get; private set; }
        // S2A（#468）：驱动怪癖 A-ZMASK（graphics/vulkan/core/DriverQuirks.cs）。为真时主管线建帧缓冲不绑写掩码为 0 的颜色目标。
        internal bool ZeroWriteMaskColorAttachmentsDisabled { get; private set; }
        internal bool IsMoltenVk { get; private set; }
        internal bool SupportsMTL31 { get; private set; }
        internal bool IsTBDR { get; private set; }
        internal bool IsSharedMemory { get; private set; }

        public string GpuVendor { get; private set; }
        public string GpuDriver { get; private set; }
        public string GpuRenderer { get; private set; }
        public string GpuVersion { get; private set; }
        public string GpuDriverVersion { get; private set; }

        public bool PreferThreading => true;

        public event EventHandler<ScreenCaptureImageInfo> ScreenCaptured;

        public VulkanRenderer(Vk api, Func<Instance, Vk, SurfaceKHR> getSurface, Func<string[]> requiredExtensionsFunc, string preferredGpuId)
        {
            _getSurface = getSurface;
            _getRequiredExtensions = requiredExtensionsFunc;
            _preferredGpuId = preferredGpuId;
            Api = api;
            Shaders = [];
            Textures = [];
            Samplers = [];

            // Any device running on MacOS is using MoltenVK, even Intel and AMD vendors.
            if (IsMoltenVk = OperatingSystem.IsMacOS())
                MVKInitialization.Initialize();

            SupportsMTL31 = OperatingSystem.IsMacOSVersionAtLeast(14);
        }

        public static VulkanRenderer Create(
            string preferredGpuId,
            Func<Instance, Vk, SurfaceKHR> getSurface,
            Func<string[]> getRequiredExtensions
        ) => new(Vk.GetApi(), getSurface, getRequiredExtensions, preferredGpuId);

        private unsafe void LoadHardwareCapabilities()
        {
            PhysicalDeviceProperties2 properties2 = new()
            {
                SType = StructureType.PhysicalDeviceProperties2,
            };

            PhysicalDeviceIDProperties propertiesId = new()
            {
                SType = StructureType.PhysicalDeviceIDProperties,
                PNext = properties2.PNext,
            };

            properties2.PNext = &propertiesId;

            PhysicalDeviceSubgroupProperties propertiesSubgroup = new()
            {
                SType = StructureType.PhysicalDeviceSubgroupProperties,
                PNext = properties2.PNext,
            };

            properties2.PNext = &propertiesSubgroup;

            PhysicalDeviceBlendOperationAdvancedPropertiesEXT propertiesBlendOperationAdvanced = new()
            {
                SType = StructureType.PhysicalDeviceBlendOperationAdvancedPropertiesExt,
            };

            bool supportsBlendOperationAdvanced = _enabledFeatures.HasExtension("VK_EXT_blend_operation_advanced");

            if (supportsBlendOperationAdvanced)
            {
                propertiesBlendOperationAdvanced.PNext = properties2.PNext;
                properties2.PNext = &propertiesBlendOperationAdvanced;
            }

            bool supportsTransformFeedback = _enabledFeatures.HasExtension(ExtTransformFeedback.ExtensionName);

            PhysicalDeviceTransformFeedbackPropertiesEXT propertiesTransformFeedback = new()
            {
                SType = StructureType.PhysicalDeviceTransformFeedbackPropertiesExt,
            };

            if (supportsTransformFeedback)
            {
                propertiesTransformFeedback.PNext = properties2.PNext;
                properties2.PNext = &propertiesTransformFeedback;
            }

            PhysicalDevicePortabilitySubsetPropertiesKHR propertiesPortabilitySubset = new()
            {
                SType = StructureType.PhysicalDevicePortabilitySubsetPropertiesKhr,
            };

            bool supportsPushDescriptors = _enabledFeatures.HasExtension(KhrPushDescriptor.ExtensionName);

            PhysicalDevicePushDescriptorPropertiesKHR propertiesPushDescriptor = new()
            {
                SType = StructureType.PhysicalDevicePushDescriptorPropertiesKhr
            };

            if (supportsPushDescriptors)
            {
                propertiesPushDescriptor.PNext = properties2.PNext;
                properties2.PNext = &propertiesPushDescriptor;
            }

            bool supportsDepthClipControl = _enabledFeatures.DepthClipControl;
            bool supportsAttachmentFeedbackLoop = _enabledFeatures.AttachmentFeedbackLoopLayout;
            bool supportsDynamicAttachmentFeedbackLoop = _enabledFeatures.AttachmentFeedbackLoopDynamicState;

            PhysicalDeviceFeatures2 features2 = new()
            {
                SType = StructureType.PhysicalDeviceFeatures2,
            };
            PhysicalDevicePortabilitySubsetFeaturesKHR featuresPortabilitySubset = new()
            {
                SType = StructureType.PhysicalDevicePortabilitySubsetFeaturesKhr,
            };

            bool usePortability = _enabledFeatures.HasExtension("VK_KHR_portability_subset");

            if (usePortability)
            {
                propertiesPortabilitySubset.PNext = properties2.PNext;
                properties2.PNext = &propertiesPortabilitySubset;

                featuresPortabilitySubset.PNext = features2.PNext;
                features2.PNext = &featuresPortabilitySubset;
            }

            Api.GetPhysicalDeviceProperties2(_physicalDevice.PhysicalDevice, &properties2);
            if (usePortability)
            {
                Api.GetPhysicalDeviceFeatures2(_physicalDevice.PhysicalDevice, &features2);
            }

            PortabilitySubsetFlags portabilityFlags = PortabilitySubsetFlags.None;
            uint vertexBufferAlignment = 1;

            if (usePortability)
            {
                vertexBufferAlignment = propertiesPortabilitySubset.MinVertexInputBindingStrideAlignment;

                portabilityFlags |= featuresPortabilitySubset.TriangleFans ? 0 : PortabilitySubsetFlags.NoTriangleFans;
                portabilityFlags |= featuresPortabilitySubset.PointPolygons ? 0 : PortabilitySubsetFlags.NoPointMode;
                portabilityFlags |= featuresPortabilitySubset.ImageView2DOn3DImage ? 0 : PortabilitySubsetFlags.No3DImageView;
                portabilityFlags |= featuresPortabilitySubset.SamplerMipLodBias ? 0 : PortabilitySubsetFlags.NoLodBias;
            }

            bool supportsCustomBorderColor = _enabledFeatures.HasExtension("VK_EXT_custom_border_color") &&
                                             _enabledFeatures.CustomBorderColors &&
                                             _enabledFeatures.CustomBorderColorWithoutFormat;

            ref PhysicalDeviceProperties properties = ref properties2.Properties;

            bool hasDriverProperties = _physicalDevice.TryGetPhysicalDeviceDriverPropertiesKHR(Api, out PhysicalDeviceDriverPropertiesKHR driverProperties);

            Vendor = VendorUtils.FromId(properties.VendorID);

            IsAmdWindows = Vendor == Vendor.Amd && OperatingSystem.IsWindows();
            IsIntelWindows = Vendor == Vendor.Intel && OperatingSystem.IsWindows();
            IsIntelLinux = Vendor == Vendor.Intel && OperatingSystem.IsLinux();
            IsTBDR =
                Vendor is Vendor.Apple or
                Vendor.Qualcomm or
                Vendor.ARM or
                Vendor.Broadcom or
                Vendor.ImgTec;

            GpuVendor = VendorUtils.GetNameFromId(properties.VendorID);
            GpuDriver = hasDriverProperties && !OperatingSystem.IsMacOS() ?
                VendorUtils.GetFriendlyDriverName(driverProperties.DriverID) : GpuVendor; // Fallback to vendor name if driver is unavailable or on MacOS where vendor is preferred.
            GpuDriverVersion = TryGetIntelWindowsDriverVersionFromUuid(ref propertiesId, out string intelDriverVersion) ? intelDriverVersion :
                hasDriverProperties ?
                GetDriverInfo(ref driverProperties) ?? ParseDriverVersion(ref properties) :
                ParseDriverVersion(ref properties);

            fixed (byte* deviceName = properties.DeviceName)
            {
                GpuRenderer = Marshal.PtrToStringAnsi((nint)deviceName);
            }

            GpuVersion = $"Vulkan v{ParseStandardVulkanVersion(properties.ApiVersion)}";

            IsAmdGcn = !IsMoltenVk && Vendor == Vendor.Amd && Patterns.AmdGcn.IsMatch(GpuRenderer);

            IsAmdRdna3 = Vendor == Vendor.Amd && (Patterns.AmdRdna3.IsMatch(GpuRenderer)
                                                  // ROG Ally (X) Device IDs
                                                  || properties.DeviceID is 0x15BF or 0x15C8);

            if (Vendor == Vendor.Nvidia)
            {
                Match match = Patterns.NvidiaConsumerClass.Match(GpuRenderer);

                if (match != null && int.TryParse(match.Groups[2].Value, out int gpuNumber))
                {
                    IsNvidiaPreTuring = gpuNumber < 2000;
                }
                else if (GpuRenderer.Contains("TITAN") && !GpuRenderer.Contains("RTX"))
                {
                    IsNvidiaPreTuring = true;
                }
            }
            else if (Vendor == Vendor.Intel)
            {
                IsIntelArc = GpuRenderer.StartsWithIgnoreCase("Intel(R) Arc(TM)");
            }

            IsQualcommProprietary = hasDriverProperties && driverProperties.DriverID == DriverId.QualcommProprietary;

            DriverQuirkSelection driverQuirks = DriverQuirks.Query(
                properties.VendorID,
                hasDriverProperties ? driverProperties.DriverID : null,
                properties.DriverVersion,
                properties.Limits.MaxDrawIndirectCount);
            ZeroWriteMaskColorAttachmentsDisabled =
                driverQuirks.Disabled.HasFlag(DisabledDriverFeatures.ZeroWriteMaskColorAttachments);

            ulong minResourceAlignment = Math.Max(
                Math.Max(
                    properties.Limits.MinStorageBufferOffsetAlignment,
                    properties.Limits.MinUniformBufferOffsetAlignment),
                properties.Limits.MinTexelBufferOffsetAlignment
            );

            MinTexelBufferOffsetAlignment = (int)properties.Limits.MinTexelBufferOffsetAlignment;

            SampleCountFlags supportedSampleCounts =
                properties.Limits.FramebufferColorSampleCounts &
                properties.Limits.FramebufferDepthSampleCounts &
                properties.Limits.FramebufferStencilSampleCounts;

            Capabilities = new HardwareCapabilities(
                _enabledFeatures.IndexTypeUint8,
                supportsCustomBorderColor,
                supportsBlendOperationAdvanced,
                propertiesBlendOperationAdvanced.AdvancedBlendCorrelatedOverlap,
                propertiesBlendOperationAdvanced.AdvancedBlendNonPremultipliedSrcColor,
                propertiesBlendOperationAdvanced.AdvancedBlendNonPremultipliedDstColor,
                _enabledFeatures.HasExtension(KhrDrawIndirectCount.ExtensionName) && _enabledFeatures.Vulkan12.DrawIndirectCount,
                _enabledFeatures.FragmentShaderPixelInterlock,
                _enabledFeatures.HasExtension("VK_NV_geometry_shader_passthrough"),
                _enabledFeatures.Core.ShaderFloat64,
                _enabledFeatures.Vulkan12.ShaderInt8,
                _enabledFeatures.HasExtension("VK_EXT_shader_stencil_export"),
                _enabledFeatures.Core.ShaderStorageImageMultisample,
                _enabledFeatures.ConditionalRendering,
                _enabledFeatures.ExtendedDynamicState,
                _enabledFeatures.Core.MultiViewport && !(IsMoltenVk && Vendor == Vendor.Amd), // Workaround for AMD on MoltenVK issue
                _enabledFeatures.NullDescriptor || IsMoltenVk,
                supportsPushDescriptors,
                IsMoltenVk ? 16 : propertiesPushDescriptor.MaxPushDescriptors, // Prevents vertex explosions on MoltenVK.
                _enabledFeatures.PrimitiveTopologyListRestart,
                _enabledFeatures.PrimitiveTopologyPatchListRestart,
                supportsTransformFeedback && _enabledFeatures.TransformFeedback,
                _enabledFeatures.TransformFeedback && propertiesTransformFeedback.TransformFeedbackQueries,
                _enabledFeatures.Core.OcclusionQueryPrecise,
                _enabledFeatures.Core.PipelineStatisticsQuery,
                _enabledFeatures.Core.GeometryShader,
                _enabledFeatures.Core.TessellationShader,
                _enabledFeatures.HasExtension("VK_NV_viewport_array2"),
                _enabledFeatures.HasExtension(ExtExternalMemoryHost.ExtensionName),
                supportsDepthClipControl && _enabledFeatures.DepthClipControl,
                supportsAttachmentFeedbackLoop && _enabledFeatures.AttachmentFeedbackLoopLayout,
                supportsDynamicAttachmentFeedbackLoop && _enabledFeatures.AttachmentFeedbackLoopDynamicState,
                in propertiesSubgroup,
                supportedSampleCounts,
                portabilityFlags,
                vertexBufferAlignment,
                properties.Limits.SubTexelPrecisionBits,
                minResourceAlignment,
                properties.Limits.MaxUniformBufferRange);
        }

        private unsafe void LoadFeatures(uint maxQueueCount, uint queueFamilyIndex)
        {
            FormatCapabilities = new FormatCapabilities(Api, _physicalDevice.PhysicalDevice);

            if (Api.TryGetDeviceExtension(_instance.Instance, _device, out ExtConditionalRendering conditionalRenderingApi))
            {
                ConditionalRenderingApi = conditionalRenderingApi;
            }

            if (Api.TryGetDeviceExtension(_instance.Instance, _device, out ExtExtendedDynamicState extendedDynamicStateApi))
            {
                ExtendedDynamicStateApi = extendedDynamicStateApi;
            }

            if (Api.TryGetDeviceExtension(_instance.Instance, _device, out KhrPushDescriptor pushDescriptorApi))
            {
                PushDescriptorApi = pushDescriptorApi;
            }

            if (Api.TryGetDeviceExtension(_instance.Instance, _device, out ExtTransformFeedback transformFeedbackApi))
            {
                TransformFeedbackApi = transformFeedbackApi;
            }

            if (Api.TryGetDeviceExtension(_instance.Instance, _device, out KhrDrawIndirectCount drawIndirectCountApi))
            {
                DrawIndirectCountApi = drawIndirectCountApi;
            }

            if (Api.TryGetDeviceExtension(_instance.Instance, _device, out ExtAttachmentFeedbackLoopDynamicState dynamicFeedbackLoopApi))
            {
                DynamicFeedbackLoopApi = dynamicFeedbackLoopApi;
            }

            if (maxQueueCount >= 2)
            {
                Api.GetDeviceQueue(_device, queueFamilyIndex, 1, out Queue backgroundQueue);
                BackgroundQueue = backgroundQueue;
                BackgroundQueueLock = new();
            }

            LoadHardwareCapabilities();

            IsSharedMemory = MemoryAllocator.IsDeviceMemoryShared(_physicalDevice);

            MemoryAllocator = new MemoryAllocator(Api, _physicalDevice, _device);

            Api.TryGetDeviceExtension(_instance.Instance, _device, out ExtExternalMemoryHost hostMemoryApi);
            HostMemoryAllocator = new HostMemoryAllocator(MemoryAllocator, Api, hostMemoryApi, _device);

            CommandBufferPool = new CommandBufferPool(Api, _device, Queue, QueueLock, queueFamilyIndex, IsQualcommProprietary);

            PipelineLayoutCache = new PipelineLayoutCache();

            BackgroundResources = new BackgroundResources(this, _device);

            BufferManager = new BufferManager(this, _device);

            // S2A（F1）：越界记录是缺省关的诊断（S2A_SSBO_OOB_DIAG=1），且只在钳位生效（未启用 robustBufferAccess）时建
            if (!_enabledFeatures.Core.RobustBufferAccess && S2AStorageOobRecorder.DiagnosticsRequested)
            {
                S2AStorageOob = S2AStorageOobRecorder.TryCreate(this, _physicalDevice.PhysicalDeviceProperties.Limits.MinStorageBufferOffsetAlignment);
            }

            SyncManager = new SyncManager(this, _device);
            // One renderer-owned lane for every shader-module and pipeline driver call.
            S2ACompileWorker = new CompileWorker();
            _pipeline = new PipelineFull(this, _device);
            _pipeline.Initialize();

            HelperShader = new HelperShader(this, _device);

            Barriers = new BarrierBatch(this);

            _counters = new Counters(this, _device, _pipeline);
        }

        private void SetupContext(GraphicsDebugLevel logLevel)
        {
            string[] instanceExtensions = _getRequiredExtensions();
            _instance = VulkanInitialization.CreateInstance(Api, logLevel, instanceExtensions);
            _debugMessenger = new VulkanDebugMessenger(Api, _instance.Instance, logLevel);

            if (Api.TryGetInstanceExtension(_instance.Instance, out KhrSurface surfaceApi))
            {
                SurfaceApi = surfaceApi;
            }

            _surface = _getSurface(_instance.Instance, Api);
            _physicalDevice = VulkanInitialization.FindSuitablePhysicalDevice(Api, _instance, _surface, _preferredGpuId);

            Shader.TargetVulkanVersion = _physicalDevice.PhysicalDeviceProperties.ApiVersion < Vk.Version12.Value
                ? Vk.Version11.Value
                : Vk.Version12.Value;

            S2AVulkanCaps.CheckApiVersion(_physicalDevice.PhysicalDeviceProperties.ApiVersion);

            uint queueFamilyIndex = VulkanInitialization.FindSuitableQueueFamily(Api, _physicalDevice, _surface, out uint maxQueueCount);
            if (UseSingleQueue)
            {
                maxQueueCount = 1;
            }

            _device = VulkanInitialization.CreateDeviceWithSurfaceMaintenance(Api, _physicalDevice, queueFamilyIndex, maxQueueCount, out _enabledFeatures,
                S2ASurfaceMaintenanceEnabled(WindowFactory != null, Array.IndexOf(instanceExtensions, "VK_EXT_surface_maintenance1") >= 0, S2AForceNoSwapchainMaintenance));

            if (Api.TryGetDeviceExtension(_instance.Instance, _device, out KhrSwapchain swapchainApi))
            {
                SwapchainApi = swapchainApi;
            }

            Api.GetDeviceQueue(_device, queueFamilyIndex, 0, out Queue queue);
            Queue = queue;
            QueueLock = new();

            LoadFeatures(maxQueueCount, queueFamilyIndex);

            QueueFamilyIndex = queueFamilyIndex;

            _window = WindowFactory?.Invoke(this, _surface, _physicalDevice.PhysicalDevice, _device)
                ?? new Window(this, _surface, _physicalDevice.PhysicalDevice, _device);

            _initialized = true;
        }

        internal int[] GetPushDescriptorReservedBindings(bool isOgl)
        {
            // The first call of this method determines what push descriptor layout is used for all shaders on this renderer.
            // This is chosen to minimize shaders that can't fit their uniforms on the device's max number of push descriptors.
            if (_pdReservedBindings == null)
            {
                if (Capabilities.MaxPushDescriptors <= Constants.MaxUniformBuffersPerStage * 2)
                {
                    _pdReservedBindings = isOgl ? _pdReservedBindingsOgl : _pdReservedBindingsNvn;
                }
                else
                {
                    _pdReservedBindings = [];
                }
            }

            return _pdReservedBindings;
        }

        public BufferHandle CreateBuffer(int size, BufferAccess access)
        {
            return BufferManager.CreateWithHandle(this, size, access.HasFlag(BufferAccess.SparseCompatible), access.Convert(), access.HasFlag(BufferAccess.Stream));
        }

        public BufferHandle CreateBuffer(nint pointer, int size)
        {
            if (!AllowHostMemoryImport)
            {
                return BufferHandle.Null;
            }

            return BufferManager.CreateHostImported(this, pointer, size);
        }

        public BufferHandle CreateBufferSparse(ReadOnlySpan<BufferRange> storageBuffers)
        {
            return BufferManager.CreateSparse(this, storageBuffers);
        }

        public IImageArray CreateImageArray(int size, bool isBuffer)
        {
            return new ImageArray(this, size, isBuffer);
        }

        public IProgram CreateProgram(ShaderSource[] sources, ShaderInfo info)
        {
            ProgramCount++;

            bool isCompute = sources.Length == 1 && sources[0].Stage == ShaderStage.Compute;

            if (info.State.HasValue || isCompute)
            {
                return new ShaderCollection(this, _device, sources, info.ResourceLayout, info.State ?? default, info.FromCache);
            }

            return new ShaderCollection(this, _device, sources, info.ResourceLayout);
        }

        internal ShaderCollection CreateProgramWithMinimalLayout(ShaderSource[] sources, ResourceLayout resourceLayout, SpecDescription[] specDescription = null)
        {
            return new ShaderCollection(this, _device, sources, resourceLayout, specDescription, isMinimal: true);
        }

        public ISampler CreateSampler(SamplerCreateInfo info)
        {
            return new SamplerHolder(this, _device, info);
        }

        public ITexture CreateTexture(TextureCreateInfo info)
        {
            if (info.Target == Target.TextureBuffer)
            {
                return new TextureBuffer(this, info);
            }

            return CreateTextureView(info);
        }

        public ITextureArray CreateTextureArray(int size, bool isBuffer)
        {
            return new TextureArray(this, size, isBuffer);
        }

        internal TextureView CreateTextureView(TextureCreateInfo info)
        {
            // This should be disposed when all views are destroyed.
            TextureStorage storage = CreateTextureStorage(info);
            return storage.CreateView(info, 0, 0);
        }

        internal TextureStorage CreateTextureStorage(TextureCreateInfo info)
        {
            return new TextureStorage(this, _device, info);
        }

        public void DeleteBuffer(BufferHandle buffer)
        {
            BufferManager.Delete(buffer);
        }

        internal void FlushAllCommands()
        {
            _pipeline?.FlushCommandsImpl();
        }

        internal void RegisterFlush()
        {
            SyncManager.RegisterFlush();

            // Periodically free unused regions of the staging buffer to avoid doing it all at once.
            BufferManager.StagingBuffer.FreeCompleted();
        }

        public PinnedSpan<byte> GetBufferData(BufferHandle buffer, int offset, int size)
        {
            return BufferManager.GetData(buffer, offset, size);
        }

        public Capabilities GetCapabilities()
        {
            if (!_s2aFreezeProgramProfile) return BuildCapabilities();
            _ = S2ASelectedProgramProfile;
            return _s2aProfileSnapshot.Capabilities(_s2aProfileOwner, GetTotalGPUMemory());
        }

        private unsafe Capabilities BuildCapabilities(ComputeTranslationProfile selected = null)
        {
            FormatFeatureFlags compressedFormatFeatureFlags =
                FormatFeatureFlags.SampledImageBit |
                FormatFeatureFlags.SampledImageFilterLinearBit |
                FormatFeatureFlags.BlitSrcBit |
                FormatFeatureFlags.TransferSrcBit |
                FormatFeatureFlags.TransferDstBit;

            bool supportsBc123CompressionFormat = FormatCapabilities.OptimalFormatsSupport(compressedFormatFeatureFlags,
                Format.Bc1RgbaSrgb,
                Format.Bc1RgbaUnorm,
                Format.Bc2Srgb,
                Format.Bc2Unorm,
                Format.Bc3Srgb,
                Format.Bc3Unorm);

            bool supportsBc45CompressionFormat = FormatCapabilities.OptimalFormatsSupport(compressedFormatFeatureFlags,
                Format.Bc4Snorm,
                Format.Bc4Unorm,
                Format.Bc5Snorm,
                Format.Bc5Unorm);

            bool supportsBc67CompressionFormat = FormatCapabilities.OptimalFormatsSupport(compressedFormatFeatureFlags,
                Format.Bc6HSfloat,
                Format.Bc6HUfloat,
                Format.Bc7Srgb,
                Format.Bc7Unorm);

            bool supportsEtc2CompressionFormat = FormatCapabilities.OptimalFormatsSupport(compressedFormatFeatureFlags,
                Format.Etc2RgbaSrgb,
                Format.Etc2RgbaUnorm,
                Format.Etc2RgbPtaSrgb,
                Format.Etc2RgbPtaUnorm,
                Format.Etc2RgbSrgb,
                Format.Etc2RgbUnorm);

            bool supports5BitComponentFormat = FormatCapabilities.OptimalFormatsSupport(compressedFormatFeatureFlags,
                Format.R5G6B5Unorm,
                Format.R5G5B5A1Unorm,
                Format.R5G5B5X1Unorm,
                Format.B5G6R5Unorm,
                Format.B5G5R5A1Unorm,
                Format.A1B5G5R5Unorm);

            bool supportsR4G4B4A4Format = FormatCapabilities.OptimalFormatsSupport(compressedFormatFeatureFlags,
                Format.R4G4B4A4Unorm);

            bool supportsAstcFormats = FormatCapabilities.OptimalFormatsSupport(compressedFormatFeatureFlags,
                Format.Astc4x4Unorm,
                Format.Astc5x4Unorm,
                Format.Astc5x5Unorm,
                Format.Astc6x5Unorm,
                Format.Astc6x6Unorm,
                Format.Astc8x5Unorm,
                Format.Astc8x6Unorm,
                Format.Astc8x8Unorm,
                Format.Astc10x5Unorm,
                Format.Astc10x6Unorm,
                Format.Astc10x8Unorm,
                Format.Astc10x10Unorm,
                Format.Astc12x10Unorm,
                Format.Astc12x12Unorm,
                Format.Astc4x4Srgb,
                Format.Astc5x4Srgb,
                Format.Astc5x5Srgb,
                Format.Astc6x5Srgb,
                Format.Astc6x6Srgb,
                Format.Astc8x5Srgb,
                Format.Astc8x6Srgb,
                Format.Astc8x8Srgb,
                Format.Astc10x5Srgb,
                Format.Astc10x6Srgb,
                Format.Astc10x8Srgb,
                Format.Astc10x10Srgb,
                Format.Astc12x10Srgb,
                Format.Astc12x12Srgb);

            PhysicalDeviceFeatures features = _enabledFeatures.Core;
            PhysicalDeviceVulkan12Features featuresVk12 = _enabledFeatures.Vulkan12;
            // SPIR-V 1.5 has separate capabilities; on 1.1, an enabled legacy
            // extension authorizes both outputs with its combined capability.
            bool hostVulkan12 = _physicalDevice.PhysicalDeviceProperties.ApiVersion >= Vk.Version12.Value;
            bool legacyLayerOutput = !hostVulkan12 &&
                (_enabledFeatures.HasExtension("VK_EXT_shader_viewport_index_layer") ||
                 _enabledFeatures.HasExtension("VK_NV_viewport_array2"));
            bool supportsLayerOutput = featuresVk12.ShaderOutputLayer || legacyLayerOutput;
            bool supportsViewportOutput = featuresVk12.ShaderOutputViewportIndex || legacyLayerOutput;

            PhysicalDeviceLimits limits = _physicalDevice.PhysicalDeviceProperties.Limits;
            QueueFamilyProperties mainQueueProperties = _physicalDevice.QueueFamilyProperties[QueueFamilyIndex];

            SystemMemoryType memoryType;

            if (IsSharedMemory)
            {
                memoryType = SystemMemoryType.UnifiedMemory;
            }
            else
            {
                memoryType = Vendor == Vendor.Nvidia ?
                    SystemMemoryType.DedicatedMemorySlowStorage :
                    SystemMemoryType.DedicatedMemory;
            }

            return new Capabilities(
                api: TargetApi.Vulkan,
                GpuVendor,
                memoryType: memoryType,
                hasFrontFacingBug: IsIntelWindows,
                hasVectorIndexingBug: IsQualcommProprietary,
                needsFragmentOutputSpecialization: IsMoltenVk,
                reduceShaderPrecision: IsMoltenVk,
                supportsAstcCompression: features.TextureCompressionAstcLdr && supportsAstcFormats,
                supportsBc123Compression: features.TextureCompressionBC && supportsBc123CompressionFormat,
                supportsBc45Compression: features.TextureCompressionBC && supportsBc45CompressionFormat,
                supportsBc67Compression: features.TextureCompressionBC && supportsBc67CompressionFormat,
                supportsEtc2Compression: features.TextureCompressionEtc2 && supportsEtc2CompressionFormat,
                supports3DTextureCompression: true,
                supportsBgraFormat: true,
                supportsR4G4Format: false,
                supportsR4G4B4A4Format: supportsR4G4B4A4Format,
                supportsScaledVertexFormats: FormatCapabilities.SupportsScaledVertexFormats(),
                supportsSnormBufferTextureFormat: true,
                supports5BitComponentFormat: supports5BitComponentFormat,
                supportsSparseBuffer: features.SparseBinding && mainQueueProperties.QueueFlags.HasFlag(QueueFlags.SparseBindingBit),
                supportsBlendEquationAdvanced: Capabilities.SupportsBlendEquationAdvanced,
                supportsFragmentShaderInterlock: Capabilities.SupportsFragmentShaderInterlock,
                supportsFragmentShaderOrderingIntel: false,
                supportsGeometryShader: Capabilities.SupportsGeometryShader,
                supportsGeometryShaderOcclusionQuery: GsOcclusionQuerySupport.Supported(),
                supportsGeometryShaderPassthrough: Capabilities.SupportsGeometryShaderPassthrough,
                supportsTransformFeedback: Capabilities.SupportsTransformFeedback,
                supportsImageLoadFormatted: features.ShaderStorageImageReadWithoutFormat,
                supportsImageStoreFormatted: features.ShaderStorageImageWriteWithoutFormat,
                s2aSpirv15: hostVulkan12,
                s2aSpirvLayerExt: _enabledFeatures.HasExtension("VK_EXT_shader_viewport_index_layer"),
                s2aSpirvLayerNv: _enabledFeatures.HasExtension("VK_NV_viewport_array2"),
                supportsLayerVertexTessellation: supportsLayerOutput,
                supportsMismatchingViewFormat: true,
                supportsCubemapView: !IsAmdGcn,
                supportsNonConstantTextureOffset: false,
                supportsQuads: false,
                supportsSeparateSampler: true,
                supportsShaderBallot: false,
                supportsShaderBarrierDivergence: Vendor != Vendor.Intel,
                supportsShaderFloat64: Capabilities.SupportsShaderFloat64,

                supportsShaderNonUniformIndexing:
                    featuresVk12.ShaderSampledImageArrayNonUniformIndexing &&
                    featuresVk12.ShaderStorageImageArrayNonUniformIndexing,
                supportsTextureGatherOffsets: features.ShaderImageGatherExtended,
                supportsTextureShadowLod: false,
                supportsVertexStoreAndAtomics: features.VertexPipelineStoresAndAtomics,
                supportsViewportIndexVertexTessellation: supportsViewportOutput,
                supportsViewportMask: Capabilities.SupportsViewportArray2,
                supportsViewportSwizzle: false,
                supportsIndirectParameters: Capabilities.SupportsIndirectParameters,
                supportsDepthClipControl: Capabilities.SupportsDepthClipControl,
                uniformBufferSetIndex: PipelineBase.UniformSetIndex,
                storageBufferSetIndex: PipelineBase.StorageSetIndex,
                textureSetIndex: PipelineBase.TextureSetIndex,
                imageSetIndex: PipelineBase.ImageSetIndex,
                extraSetBaseIndex: PipelineBase.DescriptorSetLayouts,
                maximumExtraSets: Math.Max(0, (int)limits.MaxBoundDescriptorSets - PipelineBase.DescriptorSetLayouts),
                maximumUniformBuffersPerStage: Constants.MaxUniformBuffersPerStage,
                maximumStorageBuffersPerStage: Constants.MaxStorageBuffersPerStage,
                maximumTexturesPerStage: Constants.MaxTexturesPerStage,
                maximumImagesPerStage: Constants.MaxImagesPerStage,
                maximumComputeSharedMemorySize: (int)limits.MaxComputeSharedMemorySize,
                maximumSupportedAnisotropy: (int)limits.MaxSamplerAnisotropy,
                shaderSubgroupSize: (int)Capabilities.SubgroupSize,
                storageBufferOffsetAlignment: (int)limits.MinStorageBufferOffsetAlignment,
                textureBufferOffsetAlignment: (int)limits.MinTexelBufferOffsetAlignment,
                gatherBiasPrecision: IsIntelWindows || IsAmdWindows ? (int)Capabilities.SubTexelPrecisionBits : 0,
                maximumGpuMemory: GetTotalGPUMemory(),
                requiresLinkedVaryingInterface: true,
                maximumVertexOutputComponents: checked((int)limits.MaxVertexOutputComponents),
                maximumFragmentInputComponents: checked((int)limits.MaxFragmentInputComponents),
                // S2A（F1）：没开 robustBufferAccess 的设备（只有 NVIDIA 开）上越界存储缓冲访存没有驱动兜底，翻译期按宿主窗口钳位
                s2aStorageWindowClamp: selected?.StorageWindowClamp ?? !features.RobustBufferAccess,
                s2aStorageOobRecord: selected?.StorageOobRecord ?? (S2AStorageOob != null),
                unsupportedUniformTexelBufferFormats: S2AUnsupportedUniformTexelFormats());
        }

        // S2A：顶点 texel 格式里缺 UNIFORM_TEXEL_BUFFER 支持位的集合（Mali-G710 上
        //   R16Sfloat/R16Sint/A2B10G10R10SintPack32 的 bufferFeatures=0x40）；下游对
        //   命中格式绑同位宽整型替代 view，着色器按 AttributeType.TexelDecode 解码。
        private Format[] S2AUnsupportedUniformTexelFormats()
        {
            List<Format> unsupported = null;

            foreach (Format format in VertexTexelFormats.All)
            {
                if (!FormatCapabilities.BufferFormatSupports(FormatFeatureFlags.UniformTexelBufferBit, format))
                {
                    (unsupported ??= new List<Format>()).Add(format);
                }
            }

            return unsupported?.ToArray();
        }

        private ulong GetTotalGPUMemory()
        {
            ulong totalMemory = 0;

            Api.GetPhysicalDeviceMemoryProperties(_physicalDevice.PhysicalDevice, out PhysicalDeviceMemoryProperties memoryProperties);

            for (int i = 0; i < memoryProperties.MemoryHeapCount; i++)
            {
                MemoryHeap heap = memoryProperties.MemoryHeaps[i];
                if ((heap.Flags & MemoryHeapFlags.DeviceLocalBit) == MemoryHeapFlags.DeviceLocalBit)
                {
                    totalMemory += heap.Size;
                }
            }

            return totalMemory;
        }

        public HardwareInfo GetHardwareInfo()
        {
            return new HardwareInfo(GpuVendor, GpuRenderer, GpuDriver, GpuDriverVersion);
        }

        /// <summary>
        /// Gets the available Vulkan devices using the default Vulkan API
        /// object returned by <see cref="Vk.GetApi()"/>
        /// </summary>
        /// <returns></returns>
        public static DeviceInfo[] GetPhysicalDevices()
        {
            try
            {
                return VulkanInitialization.GetSuitablePhysicalDevices(Vk.GetApi());
            }
            catch (Exception ex)
            {
                Logger.Error?.PrintMsg(LogClass.Gpu, $"Error querying Vulkan devices: {ex.Message}");

                return [];
            }
        }

        public static DeviceInfo[] GetPhysicalDevices(Vk api)
        {
            try
            {
                return VulkanInitialization.GetSuitablePhysicalDevices(api);
            }
            catch (Exception)
            {
                // If we got an exception here, Vulkan is most likely not supported.
                return [];
            }
        }

        private static string ParseStandardVulkanVersion(uint version)
        {
            return $"{version >> 22}.{(version >> 12) & 0x3FF}.{version & 0xFFF}";
        }

        private static string ParseDriverVersion(ref PhysicalDeviceProperties properties)
        {
            uint driverVersionRaw = properties.DriverVersion;

            // NVIDIA differ from the standard here and uses a different format.
            if (properties.VendorID == 0x10DE)
            {
                return $"{(driverVersionRaw >> 22) & 0x3FF}.{(driverVersionRaw >> 14) & 0xFF}.{(driverVersionRaw >> 6) & 0xFF}.{driverVersionRaw & 0x3F}";
            }

            // Intel's Windows Vulkan driver exposes the 101.xxxx build portion in a custom layout.
            if (properties.VendorID == 0x8086 && OperatingSystem.IsWindows())
            {
                return $"{driverVersionRaw >> 14}.{driverVersionRaw & 0x3FFF}";
            }

            return ParseStandardVulkanVersion(driverVersionRaw);
        }

        private static unsafe string GetDriverInfo(ref PhysicalDeviceDriverPropertiesKHR driverProperties)
        {
            fixed (byte* driverInfo = driverProperties.DriverInfo)
            {
                string driverInfoString = Marshal.PtrToStringAnsi((nint)driverInfo);

                return string.IsNullOrWhiteSpace(driverInfoString) ? null : driverInfoString;
            }
        }

        private static unsafe bool TryGetIntelWindowsDriverVersionFromUuid(ref PhysicalDeviceIDProperties propertiesId, out string driverVersion)
        {
            driverVersion = null;

            if (!OperatingSystem.IsWindows())
            {
                return false;
            }

            fixed (byte* driverUuid = propertiesId.DriverUuid)
            {
                string driverUuidString = Marshal.PtrToStringAnsi((nint)driverUuid, (int)Vk.UuidSize);
                int terminatorIndex = driverUuidString.IndexOf('\0');

                if (terminatorIndex >= 0)
                {
                    driverUuidString = driverUuidString[..terminatorIndex];
                }

                if (Patterns.IntelWindowsDriverVersion.IsMatch(driverUuidString))
                {
                    driverVersion = driverUuidString;

                    return true;
                }
            }

            return false;
        }

        internal PrimitiveTopology TopologyRemap(PrimitiveTopology topology)
        {
            return topology switch
            {
                PrimitiveTopology.Quads => PrimitiveTopology.Triangles,
                PrimitiveTopology.QuadStrip => PrimitiveTopology.TriangleStrip,
                PrimitiveTopology.TriangleFan or PrimitiveTopology.Polygon => Capabilities.PortabilitySubset.HasFlag(PortabilitySubsetFlags.NoTriangleFans)
                    ? PrimitiveTopology.Triangles
                    : topology,
                _ => topology,
            };
        }

        internal bool TopologyUnsupported(PrimitiveTopology topology)
        {
            return topology switch
            {
                PrimitiveTopology.Quads => true,
                PrimitiveTopology.TriangleFan or PrimitiveTopology.Polygon => Capabilities.PortabilitySubset.HasFlag(PortabilitySubsetFlags.NoTriangleFans),
                _ => false,
            };
        }

        private void PrintGpuInformation()
        {
            string gpuInfoMessage = $"{GpuRenderer} ({GpuVersion}, Driver: {GpuDriver} {GpuDriverVersion})";
            if (!GpuRenderer.StartsWithIgnoreCase(GpuVendor))
                gpuInfoMessage = gpuInfoMessage.Prepend(GpuVendor);

            Logger.Notice.Print(LogClass.Gpu, gpuInfoMessage);

            Logger.Notice.Print(LogClass.Gpu, $"GPU Memory: {GetTotalGPUMemory() / (1024 * 1024)} MiB");
        }

        public void Initialize(GraphicsDebugLevel logLevel)
        {
            if (_s2aFreezeProgramProfile && _initialized)
                throw new InvalidOperationException("A rebuilt Vulkan device requires a new renderer/profile owner.");
            SetupContext(logLevel);
            S2ASelectProgramProfile();

            PrintGpuInformation();
        }

        internal bool NeedsVertexBufferAlignment(int attrScalarAlignment, out int alignment)
        {
            if (Capabilities.VertexBufferAlignment > 1)
            {
                alignment = (int)Capabilities.VertexBufferAlignment;

                return true;
            }
            else if (Vendor != Vendor.Nvidia)
            {
                // Vulkan requires that vertex attributes are globally aligned by their component size,
                // so buffer strides that don't divide by the largest scalar element are invalid.
                // Guest applications do this, NVIDIA GPUs are OK with it, others are not.

                alignment = attrScalarAlignment;

                return true;
            }

            alignment = 1;

            return false;
        }

        public void PreFrame()
        {
            SyncManager.Cleanup();
        }

        public ICounterEvent ReportCounter(CounterType type, EventHandler<ulong> resultHandler, float divisor, bool hostReserved)
        {
            return _counters.QueueReport(type, resultHandler, divisor, hostReserved);
        }

        public void ResetCounter(CounterType type)
        {
            _counters.QueueReset(type);
        }

        public void SetBufferData(BufferHandle buffer, int offset, ReadOnlySpan<byte> data)
        {
            BufferManager.SetData(buffer, offset, data, _pipeline.CurrentCommandBuffer, _pipeline.EndRenderPassDelegate);
        }

        public void UpdateCounters()
        {
            _counters.Update();
        }

        public void ResetCounterPool()
        {
            _counters.ResetCounterPool();
        }

        public void ResetFutureCounters(CommandBuffer cmd, int count)
        {
            _counters?.ResetFutureCounters(cmd, count);
        }

        public void BackgroundContextAction(Action action, bool alwaysBackground = false)
        {
            action();
        }

        public void CreateSync(ulong id, bool strict)
        {
            SyncManager.Create(id, strict);
        }

        public IProgram LoadProgramBinary(byte[] programBinary, bool isFragment, ShaderInfo info)
        {
            _ = info; // VKPB restores ShaderInfo from the record; the disk-cache default is empty.

            if (programBinary == null || programBinary.Length == 0)
            {
                S2AReportProgramBinaryReject("EMPTY: no program bytes");
                return new FailedProgram("EMPTY");
            }

            if (!ProgramBinaryCodec.TryDecode(programBinary, S2AProgramBinaryIdentity,
                out ProgramBinary binary, out string error))
            {
                S2AReportProgramBinaryReject(error);
                return new FailedProgram(error);
            }

            bool recordHasFragment = Array.Exists(binary.Sources, source => source.Stage == ShaderStage.Fragment);
            if (isFragment != recordHasFragment)
            {
                S2AReportProgramBinaryReject(
                    $"STAGE: caller fragment flag {isFragment} does not match the program record");
                return new FailedProgram("STAGE");
            }

            long loads = Interlocked.Increment(ref S2AProgramBinaryLoads);
            if (S2AShouldLogProgramBinaryLoad(loads))
                Console.Error.WriteLine($"[vkpb] 程序记录载入 #{loads}：{binary.Sources.Length} 段，{binary.Sources.Sum(s => s.BinaryCode?.Length ?? 0)} 字节");

            // Compute programs carry a default pipeline state in the current CreateProgram
            // path; graphics programs without state were created without background compile.
            if (binary.Info.State.HasValue)
            {
                return new ShaderCollection(this, _device, binary.Sources, binary.Info.ResourceLayout,
                    binary.Info.State.Value, fromCache: true);
            }

            return new ShaderCollection(this, _device, binary.Sources, binary.Info.ResourceLayout);
        }

        private void S2AReportProgramBinaryReject(string error)
        {
            long count = Interlocked.Increment(ref S2AProgramBinaryRejects);
            if (count <= 8 || (count & (count - 1)) == 0)
                Console.Error.WriteLine($"[vkpb] 程序记录拒绝 #{count}：{error}");
        }

        public void WaitSync(ulong id)
        {
            SyncManager.Wait(id);
        }

        public ulong GetCurrentSync()
        {
            return SyncManager.GetCurrent();
        }

        public void SetInterruptAction(Action<Action> interruptAction)
        {
            InterruptAction = interruptAction;
        }

        public void Screenshot()
        {
            _window.ScreenCaptureRequested = true;
        }

        public void OnScreenCaptured(ScreenCaptureImageInfo bitmap)
        {
            ScreenCaptured?.Invoke(this, bitmap);
        }

        public bool SupportsRenderPassBarrier(PipelineStageFlags flags)
        {
            return !(IsMoltenVk || IsQualcommProprietary);
        }

        public unsafe void Dispose()
        {
            if (S2AUniformBufferRangeErrors != 0)
                Console.Error.WriteLine($"[vkpb] 【红】收局：{S2AUniformBufferRangeSummary}");
            if (!_initialized)
            {
                S2ACompileWorker?.Stop(1_000);
                return;
            }

            // Stop the single compile lane before any pipeline cache is snapshotted or any
            // Vulkan object (window, pipeline cache, shaders, device) is released. A timed-out
            // driver call retains the device and is reported by the host instead of being
            // destroyed under a running worker.
            CompileWorker worker = S2ACompileWorker;
            if (worker != null && !worker.Stop(30_000))
            {
                throw new TimeoutException("Vulkan compile worker did not stop; retain the device and terminate the host.");
            }
            _s2aProfileSnapshot?.Retire(_s2aProfileOwner);

            // Shader/helper disposal may still observe a canceled background task; it must
            // not touch the WSI lease after the window starts retiring.
            S2AWaitPump = null;

            // A leased window must prove retirement before any of its borrowed resources go away.
            // Dispose may fail closed; in that case the host retains the renderer until process exit.
            if (WindowFactory != null)
            {
                _window.Dispose();
            }

            CommandBufferPool.Dispose();
            BackgroundResources.Dispose();
            _counters.Dispose();
            if (WindowFactory == null)
            {
                _window.Dispose();
            }
            HelperShader.Dispose();
            _pipeline.Dispose();
            S2AStorageOob?.Dispose();
            BufferManager.Dispose();
            PipelineLayoutCache.Dispose();
            Barriers.Dispose();

            MemoryAllocator.Dispose();

            foreach (ShaderCollection shader in Shaders)
            {
                shader.Dispose();
            }

            foreach (ITexture texture in Textures)
            {
                texture.Release();
            }

            foreach (SamplerHolder sampler in Samplers)
            {
                sampler.Dispose();
            }

            if (WindowFactory == null)
            {
                SurfaceApi.DestroySurface(_instance.Instance, _surface, null);
            }

            S2ADeviceCleanup?.Invoke();
            S2ADeviceCleanup = null;
            Api.DestroyDevice(_device, null);

            _debugMessenger.Dispose();

            // Last step destroy the instance
            _instance.Dispose();
        }

        public bool PrepareHostMapping(nint address, ulong size)
        {
            return AllowHostMemoryImport && Capabilities.SupportsHostImportedMemory &&
                HostMemoryAllocator.TryImport(BufferManager.HostImportedBufferMemoryRequirements, BufferManager.DefaultBufferMemoryFlags, address, size);
        }
    }
}
