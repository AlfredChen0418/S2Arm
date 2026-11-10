using S2Arm.Backends.Vulkan;
using Silk.NET.Vulkan;
using System;
using System.Linq;

internal static class DriverQuirkTests
{
    public static void Run(Action<bool, string> check)
    {
        const DriverId arm = DriverId.ArmProprietary;
        const DriverId qualcomm = DriverId.QualcommProprietary;
        const DisabledDriverFeatures eds = DisabledDriverFeatures.ExtendedDynamicState;
        const DisabledDriverFeatures none = DisabledDriverFeatures.None;
        // #468：A-ZMASK 对 Arm 专有驱动全版本生效（写掩码为 0 的颜色目标不进帧缓冲）。
        const DisabledDriverFeatures zmask = DisabledDriverFeatures.ZeroWriteMaskColorAttachments;
        // Raw constants calculated independently from the policy helper.
        // #1456：A-GSOCC / A-GSOCC-NID 已移除——GS 留用与否只由运行时 capprobe
        // 指纹结果决定，Arm 专有驱动不再整体禁 GS（含已知坏的 G710/r32p1 也走缓存）。
        Case("r44p1-1", 0x13b5, arm, 0x0b000fff, 2, "A-EDS-44,A-EDS-48,A-ZMASK", eds | zmask);
        Case("r44p1", 0x13b5, arm, 0x0b001000, 2, "A-EDS-48,A-ZMASK", eds | zmask);
        Case("r44p1+1", 0x13b5, arm, 0x0b001001, 2, "A-EDS-48,A-ZMASK", eds | zmask);
        Case("r48-1 CSF", 0x13b5, arm, 0x0bffffff, 2, "A-EDS-48,A-ZMASK", eds | zmask);
        Case("r48 CSF", 0x13b5, arm, 0x0c000000, 2, "A-ZMASK", zmask);
        Case("r48+1 CSF", 0x13b5, arm, 0x0c000001, 2, "A-ZMASK", zmask);
        Case("r46-1 JM", 0x13b5, arm, 0x0b7fffff, 1, "A-EDS-48,A-ZMASK", eds | zmask);
        Case("r46 JM", 0x13b5, arm, 0x0b800000, 1, "A-EDS-48,A-EDS-JM,A-ZMASK", eds | zmask);
        Case("r46+1 JM", 0x13b5, arm, 0x0b800001, 1, "A-EDS-48,A-EDS-JM,A-ZMASK", eds | zmask);
        Case("r51-1 JM", 0x13b5, arm, 0x0cbfffff, 1, "A-EDS-JM,A-ZMASK", eds | zmask);
        Case("r51 JM", 0x13b5, arm, 0x0cc00000, 1, "A-ZMASK", zmask);
        Case("r51+1 JM", 0x13b5, arm, 0x0cc00001, 1, "A-ZMASK", zmask);
        Case("zero JM limit", 0x13b5, arm, 0x0c000000, 0, "A-EDS-JM,A-ZMASK", eds | zmask);
        Case("max CSF limit", 0x13b5, arm, 0x0c000000, uint.MaxValue, "A-ZMASK", zmask);
        Case("ARM high unsigned version", 0x13b5, arm, uint.MaxValue, 1, "A-ZMASK", zmask);
        // #1456 原报告机：Mali-G710 r32p1（134221824）静态表只命中无关的 EDS 规则，
        // GS 禁用由 known-bad 名单 / capprobe 缓存承担，不在这里。
        Case("Mali-G710 r32p1 #1456 #468", 0x13b5, arm, 134221824, 2, "A-EDS-44,A-EDS-48,A-ZMASK", eds | zmask);
        Case("ARM driver not vendor predicate", 0x1234, arm, 0, 2, "A-EDS-44,A-EDS-48,A-ZMASK", eds | zmask);
        Case("ARM missing driver", 0x13b5, null, 0, 1, "", none);
        Case("Qualcomm missing driver", 0x5143, null, 0, 1, "", none);
        Case("PanVK", 0x13b5, DriverId.MesaPanvk, 0x08001000, 2, "", none);
        Case("PanVK-like vendor", 0x13b5, (DriverId)0x7fffffff, 0, 1, "", none);
        Case("AMD", 0x1002, DriverId.AmdProprietary, 0, 1, "", none);
        Case("NVIDIA", 0x10de, DriverId.NvidiaProprietary, 0, 1, "", none);
        Case("Turnip", 0x5143, DriverId.MesaTurnip, 0, 1, "", none);
        Case("unknown", 0x1234, (DriverId)0x7fffffff, 0, 1, "", none);
        foreach (uint version in new uint[] { 0, 0x80000000, 0x80267000, 0x80267200, uint.MaxValue })
        {
            Case("Qualcomm all versions", 0x5143, qualcomm, version, 2, "Q-PD", DisabledDriverFeatures.PushDescriptor);
            Case("Maleoon all versions", 0x19e5, null, version, 2, "H-NULL,H-ROBUST", DisabledDriverFeatures.NullDescriptor,
                ForcedDriverFeatures.RobustBufferAccess);
        }
        Case("independent vendor + driver matches", 0x19e5, qualcomm, 0, 2, "Q-PD,H-NULL,H-ROBUST",
            DisabledDriverFeatures.PushDescriptor | DisabledDriverFeatures.NullDescriptor, ForcedDriverFeatures.RobustBufferAccess);
        check(DriverQuirks.Version(512, 615, 512) == 0x80267200, "Adreno raw version encoding lost high bit");
        check(DriverQuirks.FormatVersion(0x80267200) == "512.615.512", "Adreno decoded major lost high bits");
        check(DriverQuirks.FormatVersion(uint.MaxValue) == "1023.1023.4095", "full unsigned version decode mismatch");
        check(DriverQuirks.Version(44, 1, 0) == 0x0b001000, "Mali version encoding mismatch");

        void Case(string label, uint vendor, DriverId? driver, uint version, uint count, string ids,
            DisabledDriverFeatures disabled, ForcedDriverFeatures forced = ForcedDriverFeatures.None)
        {
            DriverQuirkSelection selection = DriverQuirks.Query(vendor, driver, version, count);
            check(string.Join(",", selection.RuleIds) == ids, label + ": rule IDs mismatch");
            check(selection.RuleIds.Distinct().Count() == selection.RuleIds.Count, label + ": duplicate rule IDs");
            check(selection.Disabled == disabled && selection.Forced == forced, label + ": directed outputs mismatch");
        }
    }
}
