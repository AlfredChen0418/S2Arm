using Silk.NET.Vulkan;
using System;
using System.Collections.Generic;

namespace S2Arm.Backends.Vulkan;

[Flags]
public enum DisabledDriverFeatures
{
    None = 0,
    PushDescriptor = 1,
    ExtendedDynamicState = 2,
    NullDescriptor = 4,
    // 写掩码为 0 的颜色目标不进帧缓冲（#468）。和上面三项不同，这一位在渲染路径上读（VulkanRenderer.ZeroWriteMaskColorAttachmentsDisabled）。
    ZeroWriteMaskColorAttachments = 8,
}

[Flags]
public enum ForcedDriverFeatures
{
    None = 0,
    RobustBufferAccess = 1,
}

// GS+遮挡查询（#1456）不再按驱动身份在怪癖表里整体关：现在由 capprobe 运行时
// 探测出能力位（SupportsGeometryShaderOcclusionQuery，见
// GsOcclusionQuerySupport.cs），设备创建时按位决定，探测/名单/运行时回写
// 说不行才走 GS-as-compute。
public sealed class DriverQuirkSelection
{
    public DisabledDriverFeatures Disabled { get; }
    public ForcedDriverFeatures Forced { get; }
    public IReadOnlyList<string> RuleIds { get; }

    internal DriverQuirkSelection(DisabledDriverFeatures disabled, ForcedDriverFeatures forced, List<string> rules)
    {
        Disabled = disabled;
        Forced = forced;
        RuleIds = rules.AsReadOnly();
    }
}

// T102 contract: https://github.com/jhaizhou-ops/S2Arm/issues/782#issuecomment-6472956288
// Driver versions use the vendor convention, NOT Vulkan API-version variant bits.
// OEM backports may make the ARM version cutoffs conservative. No device-name or
// driverInfo parsing, pipeline scheduling, image-format or performance policy here.
public static class DriverQuirks
{
    private readonly record struct Rule(string Id, DriverId? Driver, uint? Vendor,
        uint MinimumVersion, uint? MaximumVersion, bool JobManagerOnly,
        DisabledDriverFeatures Disabled, ForcedDriverFeatures Forced);

    private static readonly Rule[] Rules =
    {
        new("Q-PD", DriverId.QualcommProprietary, null, 0, null, false,
            DisabledDriverFeatures.PushDescriptor, ForcedDriverFeatures.None),
        // ARM SDEN-3735689: 3809538/3809774, r44p1 fix (binding stride).
        new("A-EDS-44", DriverId.ArmProprietary, null, 0, Version(44, 1, 0), false,
            DisabledDriverFeatures.ExtendedDynamicState, ForcedDriverFeatures.None),
        // ANGLE / SDEN-3735689: pre-r48 BindVertexBuffers2 wrong binding stride.
        new("A-EDS-48", DriverId.ArmProprietary, null, 0, Version(48, 0, 0), false,
            DisabledDriverFeatures.ExtendedDynamicState, ForcedDriverFeatures.None),
        // ANGLE / Google issue 336411904: JM r46-r50 regression, fixed in r51.
        new("A-EDS-JM", DriverId.ArmProprietary, null, Version(46, 0, 0), Version(51, 0, 0), true,
            DisabledDriverFeatures.ExtendedDynamicState, ForcedDriverFeatures.None),
        new("H-NULL", null, 0x19e5, 0, null, false,
            DisabledDriverFeatures.NullDescriptor, ForcedDriverFeatures.None),
        new("H-ROBUST", null, 0x19e5, 0, null, false,
            DisabledDriverFeatures.None, ForcedDriverFeatures.RobustBufferAccess),
        // #468: Mali-G710 r32p1 damages a colour attachment that is bound with a zero write mask (every other
        // 16-pixel tile row after a LOAD/STORE pass with draws). Measured on r32p1 only; applied to every
        // version until another one is measured.
        new("A-ZMASK", DriverId.ArmProprietary, null, 0, null, false,
            DisabledDriverFeatures.ZeroWriteMaskColorAttachments, ForcedDriverFeatures.None),
    };

    public static uint Version(uint major, uint minor, uint patch) => (major << 22) | (minor << 12) | patch;

    // Preserve all ten major bits: Adreno 512 must not decode as zero.
    public static string FormatVersion(uint version) => $"{version >> 22}.{(version >> 12) & 0x3ff}.{version & 0xfff}";

    public static DriverQuirkSelection Query(uint vendorId, DriverId? driverId, uint driverVersion, uint maxDrawIndirectCount)
    {
        DisabledDriverFeatures disabled = DisabledDriverFeatures.None;
        ForcedDriverFeatures forced = ForcedDriverFeatures.None;
        List<string> matched = new();
        foreach (Rule rule in Rules)
        {
            if ((rule.Driver.HasValue && rule.Driver != driverId) ||
                (rule.Vendor.HasValue && rule.Vendor.Value != vendorId) ||
                driverVersion < rule.MinimumVersion ||
                (rule.MaximumVersion.HasValue && driverVersion >= rule.MaximumVersion.Value) ||
                (rule.JobManagerOnly && maxDrawIndirectCount > 1))
            {
                continue;
            }

            disabled |= rule.Disabled;
            forced |= rule.Forced;
            matched.Add(rule.Id);
        }

        return new DriverQuirkSelection(disabled, forced, matched);
    }
}
