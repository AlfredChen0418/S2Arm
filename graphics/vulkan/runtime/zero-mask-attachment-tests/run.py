#!/usr/bin/env python3
# 门禁手动：要传参数：--deps 与当前源码匹配的预构建运行时 DLL 目录（门禁机没有固定的依赖目录）+ --dotnet / --out
"""Production framebuffer-filter blocks from PipelineBase.cs with controlled endpoints; no GPU."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def block(text, marker):
    start = text.index(marker)
    end = text.index('{', start) + 1
    depth = 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dotnet', required=True)
    p.add_argument('--deps', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--source-root', type=Path)
    p.add_argument('--mutation', choices=['gate', 'copy', 'fallback', 'changed', 'filtered', 'clear-attach', 'detach', 'duplicate'])
    a = p.parse_args()
    here = Path(__file__).resolve().parent
    repo = a.source_root or here.parents[3]
    path = repo / 'compat/tree/src/Ryujinx.Graphics.Vulkan/PipelineBase.cs'
    source = path.read_text()
    parts = {
        '// SET_TARGETS_METHOD': ('public void SetRenderTargets(Span<ITexture> colors, ITexture depthStencil)', None),
        '// SET_MASKS_METHOD': ('public void SetRenderTargetColorMasks(ReadOnlySpan<uint> componentMask)', None),
        '// CREATE_FRAMEBUFFER_METHOD': ('private void CreateFramebuffer(Span<ITexture> colors, ITexture depthStencil, bool filterWriteMasked)', None),
        # Old sources do not have these three; a neutral stand-in keeps the fixture compiling so the old
        # behaviour fails at the assertions, not in the compiler.
        '// WANTED_METHOD': ('private uint ZeroMaskWantedTargets(', ''),
        '// CHANGED_METHOD': ('private bool ZeroMaskTargetsChanged(', ''),
        '// FILTERED_METHOD': ('protected TextureView ZeroMaskFilteredColorView(', 'private TextureView ZeroMaskFilteredColorView(int index) { return null; }'),
        '// ATTACH_FOR_CLEAR_METHOD': ('private void ZeroMaskAttachForClear(', ''),
        '// DETACH_METHOD': ('private void ZeroMaskDetachClearTargets(', 'private void ZeroMaskDetachClearTargets() { }'),
        '// CLEAR_METHOD': ('public unsafe void ClearRenderTargetColor(int index, int layer, int layerCount, ColorF color)', None),
    }
    blocks = {}
    for slot, (marker, fallback) in parts.items():
        blocks[slot] = block(source, marker) if marker in source else fallback
        assert blocks[slot] is not None, 'missing production method: ' + marker
    controls = {
        'gate': ('// CREATE_FRAMEBUFFER_METHOD', 'bool dropZeroMask = filterWriteMasked && IsMainPipeline && Gd.ZeroWriteMaskColorAttachmentsDisabled;', 'bool dropZeroMask = false;'),
        'copy': ('// CREATE_FRAMEBUFFER_METHOD', 'colors = filtered;', ''),
        'fallback': ('// WANTED_METHOD', 'wanted = 1u << firstValid;', ''),
        'changed': ('// CHANGED_METHOD', 'return true;\n                }\n            }\n\n            return false;', 'return false;\n                }\n            }\n\n            return false;'),
        'filtered': ('// FILTERED_METHOD', 'return _preMaskColors[index] is TextureView { Valid: true } view ? view : null;', 'return null;'),
        'clear-attach': ('// ATTACH_FOR_CLEAR_METHOD', '_zeroMaskClearTargets |= 1u << index;', ''),
        'detach': ('// DETACH_METHOD', '_zeroMaskClearTargets = 0;', ''),
        'duplicate': ('// ATTACH_FOR_CLEAR_METHOD', '_zeroMaskClearTargets &= ~(1u << index);', ''),
    }
    # The detach hook runs from the draw path, which is too large to cut out; check the call site by text.
    if 'private void ZeroMaskDetachClearTargets(' in source:
        calls = block(source, 'private bool RecreateGraphicsPipelineIfNeeded()').count('ZeroMaskDetachClearTargets();')
        if calls != 1:
            print(f'call site check failed: RecreateGraphicsPipelineIfNeeded calls ZeroMaskDetachClearTargets {calls} times, expected 1')
            return 1
    if a.mutation:
        slot, before, after = controls[a.mutation]
        assert blocks[slot].count(before) == 1, 'mutation anchor not found exactly once: ' + a.mutation
        blocks[slot] = blocks[slot].replace(before, after)
    text = (here / 'Fixture.cs.in').read_text()
    for slot, body in blocks.items():
        text = text.replace(slot, body.replace('protected TextureView ZeroMaskFilteredColorView', 'private TextureView ZeroMaskFilteredColorView'))
    a.out.mkdir(parents=True, exist_ok=False)
    (a.out / 'Program.cs').write_text(text)
    refs = ''.join(f'<Reference Include="{a.deps.resolve() / (n + ".dll")}" />' for n in ['Silk.NET.Vulkan', 'Silk.NET.Core'])
    (a.out / 'Proof.csproj').write_text('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net10.0</TargetFramework><AllowUnsafeBlocks>true</AllowUnsafeBlocks><NuGetAudit>false</NuGetAudit><ImportDirectoryBuildProps>false</ImportDirectoryBuildProps><ImportDirectoryBuildTargets>false</ImportDirectoryBuildTargets></PropertyGroup><ItemGroup>' + refs + '</ItemGroup></Project>')
    (a.out / 'NuGet.Config').write_text('<configuration><packageSources><clear /></packageSources></configuration>')
    results = []
    for name, cmd in [('build', [a.dotnet, 'build', str(a.out / 'Proof.csproj'), '-c', 'Release', '--configfile', str(a.out / 'NuGet.Config')]), ('run', [a.dotnet, str(a.out / 'bin/Release/net10.0/Proof.dll')])]:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        (a.out / (name + '.log')).write_text(r.stdout + r.stderr)
        results.append({'command': cmd, 'exit': r.returncode})
        if r.returncode:
            print(name + ' failed: ' + (r.stdout[-1200:] + r.stderr[-1200:]).strip())
            break
    (a.out / 'result.json').write_text(json.dumps({'sourceSha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'mutation': a.mutation, 'failedStep': name if r.returncode else None, 'commands': results}, indent=2))
    if not r.returncode:
        print(r.stdout.strip())
    return int(r.returncode != 0)


if __name__ == '__main__':
    raise SystemExit(main())
