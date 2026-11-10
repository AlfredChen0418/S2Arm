# Vulkan core: created-device feature snapshot

> 合流说明（2026-10-28）：本页说明与验证记录取 main；本线只有同一段落的旧版描述，已被 main 取代。本目录源码冲突已解；G1 的树侧适配见 合并分析 §13.23。原验证记录不代表本次候选重新通过。

First A-W5 slice for [#58](https://github.com/jhaizhou-ops/S2Arm/issues/58), based on
`android/integration@ce9db716c393d6a78ea0f6925415d7637dadc9d5`.

`DeviceFeatureSnapshot` is compiled into the existing Ryujinx Vulkan assembly.
`VulkanInitialization.CreateDevice` copies the exact request while its native
pointers are alive, then publishes the snapshot only after `vkCreateDevice`
succeeds. The renderer uses that snapshot for the migrated capability decisions.
The helper owns copied values and extension names, never a Vulkan handle or a
borrowed native pointer. It does not create a second renderer, device or allocator.

This addresses a concrete mismatch: the old renderer queried physical-device
features again and could report sparse binding and vertex/tessellation
Layer/ViewportIndex support even though its device request did not enable them.
Extension presence alone also incorrectly enabled several optional feature bits.

The creation request now queries dynamic state, uint8 indices, pixel interlock and
conditional rendering before selecting their bits. Conditional rendering, native
texture compression and vertex/tessellation Layer/ViewportIndex output are explicitly
enabled when supported. The current SPIR-V 1.3 emitter uses the combined
`ShaderViewportIndexLayerEXT` capability, so GAL exposes both output capabilities
only when both feature bits were enabled, including devices with no geometry shader
or legacy EXT name. A single enabled bit remains recorded in the snapshot but cannot
authorize that combined emission. This matches the
[Vulkan 1.2 promotion contract](https://docs.vulkan.org/refpages/latest/refpages/source/VK_EXT_shader_viewport_index_layer.html)
and the explicit combined-capability check in
[Vulkan-ValidationLayers](https://github.com/KhronosGroup/Vulkan-ValidationLayers/blob/main/layers/vulkan/generated/spirv_validation_helper.cpp).
NVIDIA robustness is
bounded by the supported bit. Feature-dependent GAL decisions use the successful
request; physical format properties, limits and queue properties remain hardware
queries. Unselected features stay disabled and use the renderer's existing fallback
paths; this change does not invent or certify those emulation paths. In particular,
devices lacking either native output feature as well as geometry shaders still need
separate qualification; current shader consumers can generate geometry passthrough
without checking that combination here. Independent single-output support would
require a separate shader-emission change, outside this slice.

The capture contract currently accepts the runtime's `pEnabledFeatures` plus
Vulkan 1.2 and listed extension feature structures. A `VkPhysicalDeviceFeatures2`
creation chain is rejected rather than silently interpreted as empty. Unknown
structures confer no capability through this helper. Callers must provide valid,
live native memory for the complete request; this is not an untrusted byte parser.

## Verification

The renderer also preserves subgroup size, supported shader stages and the full
operation mask from its existing physical-device properties query in
`HardwareCapabilities`. `HasSubgroupProperties` distinguishes an unqueried default
value from a completed query, including a returned zero mask. It does not mean
that a particular shader is eligible to execute. No extension name, subgroup size
or enabled feature is used to infer either mask, and native chain pointers are
not retained. These facts remain local to that renderer's selected device.

The properties query/publication now has a separate renderer method so the host
suite can exercise that production boundary without creating allocators, command
pools or a window. The controlled dispatch covers independent size/stage/operation
changes, extra operation bits, known zero versus unqueried values, different
devices and extension-only inputs. The existing GAL size consumer and shader
selection policy remain unchanged; this slice adds no shader admission rule,
public GAL field or cache key. Device-loss/session-generation qualification and
real GPU execution remain separate work.

Prepare NuGet dependencies explicitly outside tests:

```sh
dotnet restore graphics/vulkan/core/tests/DeviceFeatures.Tests.csproj \
  --configfile compat/tree/NuGet.Config --artifacts-path build/w5-dotnet \
  -p:NuGetAudit=false
```

Run the CPU regression suite without restoring or downloading dependencies:

```sh
dotnet run --project graphics/vulkan/core/tests/DeviceFeatures.Tests.csproj \
  -c Release --no-restore --artifacts-path build/w5-dotnet
bash compat/verify.sh
```

The suite executes the production device-request builder and public GAL capability
consumer against controlled Vulkan dispatch, in addition to snapshot lifetime,
independent-bit and invalid-chain checks. The controlled dispatch is a unit-test
driver substitute: it does not validate driver legality, pipeline execution, Android
WSI or pixels. A regression control restoring only the baseline `GetCapabilities`
method must fail on advertising unenabled sparse support; restoring this change
must pass. Build/test output belongs in ignored `build/`, not the source tree.

Draw-parameters coverage also drives the real query/create path on controlled
Vulkan 1.1, 1.2 and 1.3 devices. A missing required bit must fail before device
creation; a supported bit must use the standalone 1.1 structure and survive the
pointer-free request snapshot. This remains a CPU contract test, not GPU proof.

The helper and tests run on macOS without a Linux container or Android device.
Android runtime acceptance still requires a real built candidate and device runs.
The repository's current Android NativeAOT build scripts use a Linux toolchain;
this slice does not change that build dependency.

## Host program cache admission

Effective capabilities also affect translated programs. The shared disk cache now
names host files with a versioned, explicitly serialized capability digest in
addition to its existing API/vendor namespace. It does not hash native struct
padding, pointers, or runtime object identities. The fixture classifies all GAL
capability fields, including indirect compute-shared-memory and blend-state inputs;
resource allocation, texture conversion and other dynamic policies are explicitly
excluded. Adding or changing an unclassified field fails the coverage check.

This is a private host-program admission identity, not a W9 profile replacement or
a driver/device/UUID key for Vulkan pipeline caches. Existing translator code
versions and ShBake producer closure remain separate requirements. Old host files
without the capability suffix are ignored; original guest/shared records remain
available. A host miss follows on-demand translation from current guest state.

Host TOC slots now use the actual shared program index, including holes left by
missing binaries or a cleared host cache. Reads revalidate the format, codegen and
shared-generation token even when handles are retained. Warm preloads are checked
again before materialization, and batch output handles cannot cross cache roots or
capability identities. Truncated, invalid or out-of-record compressed input fails
instead of spinning at EOF or consuming a following record.

Prepare the host regression dependencies outside tests, then run without restore:

```sh
dotnet restore graphics/vulkan/core/cache-tests/CacheAdmission.Tests.csproj \
  --configfile compat/tree/NuGet.Config --artifacts-path build/w5-cache-dotnet \
  -p:NuGetAudit=false
dotnet run --project graphics/vulkan/core/cache-tests/CacheAdmission.Tests.csproj \
  -c Release --no-restore --artifacts-path build/w5-cache-dotnet
```

The fixture exercises production cache write, boot indexing, lazy materialization
and warm-start paths. Only the renderer boundary is a recorder; its byte tokens are
not compiled GPU binaries. NxCaps is shared by ShBake and the Metal runtime, so new
seed creation, copying and append/restart use the same admission rule. Existing
packaged seeds must be rebuilt to retain offline host-cache hits; rejecting an old
seed safely does not preserve its previous warm-start performance. The existing
shader-pack source closure already includes the changed GPU project.

The Vulkan renderer's real binary loader remains unimplemented. These checks do
not certify Vulkan pipeline-cache compatibility, Apple pixels, or Android gameplay.

## Boundaries and remaining A-W5 work

This is a shared Ryujinx seam change requiring its owners' review before merge.
It affects the existing Vulkan renderer on all platforms and shared disk-cache
consumers, including Metal/ShBake. A host build is not a macOS/MoltenVK or Android
gameplay regression run.
No actual driver, Android APK or game acceptance is claimed here.

Still required for full A-W5: legal Vulkan 1.1/core-extension version selection;
complete raw/plan/enabled/effective rule and profile identity; descriptor and image
usage/limit validation; initialization rollback; stop/device-lost closure; and the
single-runtime interface consumed by A-W6. Existing portability-subset feature
queries and the MoltenVK null-descriptor exception remain legacy behavior, outside
this first slice. They must not be described as a fully audited enabled-feature
contract. A-W6 window ownership and presentation retirement are not implemented here.

The D2R fit/gap assessment selected its supported/requested/enabled distinction as
a reusable mechanism. This implementation uses the existing Ryujinx/Silk structures;
it copies no D2R NVN engine, resource model, shader assets or device initializer.

## Driver quirks (T102)

`DriverQuirks` implements the [maintainer-approved six-rule contract](https://github.com/jhaizhou-ops/S2Arm/issues/782#issuecomment-6472956288).
It accepts vendor ID, optional Vulkan driver ID, raw unsigned driver version and
`maxDrawIndirectCount`; its result separates disabled features from forced features
and retains every matching rule ID (including overlapping ARM rules).

| Rule | Predicate | Requested adjustment |
| --- | --- | --- |
| Q-PD | Qualcomm proprietary driver, any version | Disable push descriptor |
| A-EDS-44 | ARM proprietary, version < 44.1.0 | Disable EDS1 |
| A-EDS-48 | ARM proprietary, version < 48.0.0 | Disable EDS1 |
| A-EDS-JM | ARM proprietary, indirect count <= 1, 46.0.0 <= version < 51.0.0 | Disable EDS1 |
| A-GSOCC | Vendor 0x13B5 AND ARM proprietary driver, any version | Disable geometryShader |
| A-GSOCC-NID | Vendor 0x13B5 AND driver ID unavailable | Disable geometryShader |
| H-NULL | Vendor 0x19E5, any version | Disable null descriptor |
| H-ROBUST | Vendor 0x19E5, any version | Request robust buffer access **only if supported** |
| A-ZMASK | ARM proprietary driver, any version | Do not attach colour targets whose write mask is 0 (#468; read on the render path through `VulkanRenderer.ZeroWriteMaskColorAttachmentsDisabled`, not at device creation) |

Versions use `(major << 22) | (minor << 12) | patch`, compared as `uint`.
Decoding preserves all ten major bits, including Adreno's 512. ARM rXpY is
represented as X.Y.0 by the adopted vendor convention; OEM backports can make
these cutoffs conservative. `driverInfo` is diagnostic only. ARM/Qualcomm rules
require the driver ID, not vendor ID (so PanVK/Turnip are not blacklisted).
If the existing driver-properties query reports unavailable, driver-ID rules do
not match; vendor-only Maleoon rules still apply, and vendor 0x13B5 falls to
A-GSOCC-NID. Every open-source driver on Arm hardware (PanVK included) reports
its own driver ID, so an unidentifiable driver on an Arm GPU is treated as
proprietary: disabling geometryShader costs extra GPU work through the existing
GS-as-compute path, while a missed hit risks a device-lost crash. A Vulkan 1.2
device advertising no `VK_KHR_driver_properties` follows that existing query
helper's unavailable path; this change does not expand its query semantics.

A-GSOCC works around a Mali proprietary-driver defect tracked in #1456: a
geometry-shader draw issued while an occlusion query is open faults the GPU and
the device is lost. The host keeps occlusion queries open across render passes,
so any geometry draw would hit the defect. Clearing the requested
`geometryShader` bit reports `SupportsGeometryShader=false` downstream and the
existing VtgAsCompute path converts the geometry stage to compute; no new
emulation path is added. The rule has no version bound because no fixed driver
build has been identified; it can be scoped once a passing driver sample exists.

`CreateDeviceWithSurfaceMaintenance` applies the selection before snapshot
capture: it filters push-descriptor/EDS extension names, omits the EDS feature
structure, clears robustness2.nullDescriptor, clears requested geometryShader
for A-GSOCC/A-GSOCC-NID, and requests supported core robustBufferAccess for
Maleoon as well as the existing NVIDIA path. The existing
renderer/descriptor/vertex consumers see only the resulting enabled snapshot.
Matched-rule logs include raw decimal/hex and decoded version plus driverInfo;
a matched geometryShader rule also logs the workaround activation line.
There is no native C/C++ consumer or second renderer added by this C# policy.

The existing CPU suite above now includes independent raw-value table cases and
controlled Vulkan query/create requests. It covers rule overlaps, exact version
boundaries, JM/CSF, missing/unknown driver IDs, Turnip, misleading driverInfo,
supported/unsupported Maleoon robustness and unchanged NVIDIA behavior. Captured
extension names, pNext structure presence and requested feature bits are checked,
not merely the policy result. These tests do not certify real driver behavior,
Android/HarmonyOS gameplay, Apple pixels, or any performance improvement.

Pipeline serialization is already provided by `CompileWorker` on main; this card
does not change it or pipeline caches. Subpass-count, dynamic cull state, texture
compression and per-image AFBC/UBWC/mutable-format decisions are not rules here.
No existing Ryujinx patch or downstream renderer/compilation consumer is changed.
