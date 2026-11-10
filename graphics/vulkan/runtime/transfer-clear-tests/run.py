#!/usr/bin/env python3
# 门禁手动：要传参数：--deps 与当前源码匹配的预构建运行时 DLL 目录（门禁机没有固定的依赖目录）+ --dotnet / --out
"""Actual dispatch slices with controlled endpoints; independent of driver pixel probe."""
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
    p.add_argument('--mutation', choices=['mask','conditional','format','extent','scissor','usage','capability','pending','bound','reset','barrier','clear','color-bits','filtered'])
    a = p.parse_args()
    here = Path(__file__).resolve().parent
    repo = a.source_root or here.parents[3]
    path = repo/'compat/tree/src/Ryujinx.Graphics.Vulkan/PipelineFull.cs'
    source = path.read_text()
    clear = block(source, 'public void ClearRenderTargetColor(int index, int layer, int layerCount, uint componentMask, ColorF color)')
    transfer = block(source, 'private bool TryClearColorByTransfer(') if 'private bool TryClearColorByTransfer(' in source else ''
    reset = '_transferClearsSinceFlush = 0;' if '_transferClearsSinceFlush = 0;' in block(source,'public void FlushCommandsImpl()') else ''
    format_guard = transfer[transfer.index('dst.VkFormat is not ('):transfer.index('dst.VkFormat != dst.Storage.VkFormat')].rstrip() if transfer else ''
    controls = {
        'mask':('componentMask != 0xf ||',''),
        'conditional':('_activeConditionalRender != null ||',''),
        'format':(format_guard,''),
        'extent':('FramebufferParams.Width != dst.Width ||',''),
        'scissor':('ClearScissor.Width < dst.Width ||',''),
        'usage':('(dst.Storage.UsageFlags & ImageUsageFlags.TransferDstBit) == 0 ||',''),
        'capability':('!Gd.FormatCapabilities.OptimalFormatSupports(FormatFeatureFlags.TransferDstBit, dst.Info.Format)','false'),
        'pending':('AutoFlush.ShouldFlushQuery() ||',''),
        'bound':('_transferClearsSinceFlush >= MaxTransferClears','_transferClearsSinceFlush < 0'),
        'reset':('_transferClearsSinceFlush = 0;',''),
        'barrier':('Gd.Barriers.Flush(Cbs, false, null, null);',''),
        'clear':('Gd.Api.CmdClearColorImage(CommandBuffer, image, ImageLayout.General, in value, 1, in range);',''),
        'color-bits':('ClearColorValue value = new(color.Red, color.Green, color.Blue, color.Alpha);','ClearColorValue value = new((uint)color.Red, (uint)color.Green, (uint)color.Blue, (uint)color.Alpha);'),
    }
    if a.mutation=='filtered':
        # #468 negative control: without the left-out view the clear of a filtered target is dropped again.
        assert clear.count('dstTexture ??= filteredOut;')==1
        clear=clear.replace('dstTexture ??= filteredOut;','')
    elif a.mutation:
        before,after=controls[a.mutation]
        assert (transfer+reset).count(before)==1
        transfer,reset=transfer.replace(before,after),reset.replace(before,after)
    diagnostic=block(source,'private void LogTransferClearFallback(') if 'private void LogTransferClearFallback(' in source else ''
    component=block(source,'private static ComponentType ComponentTypeOf(TextureView view)') if 'private static ComponentType ComponentTypeOf(TextureView view)' in source else ''
    text=(here/'Fixture.cs.in').read_text().replace('// COMPONENT_TYPE_METHOD',component).replace('// CLEAR_METHOD',clear).replace('// TRANSFER_METHOD',transfer).replace('// DIAGNOSTIC_METHOD',diagnostic).replace('/* FLUSH_RESET */',reset)
    a.out.mkdir(parents=True,exist_ok=False)
    (a.out/'Program.cs').write_text(text)
    refs=''.join(f'<Reference Include="{a.deps.resolve()/ (n+".dll")}" />' for n in ['Ryujinx.Graphics.GAL','Silk.NET.Vulkan','Silk.NET.Core'])
    (a.out/'Proof.csproj').write_text('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net10.0</TargetFramework><AllowUnsafeBlocks>true</AllowUnsafeBlocks><NuGetAudit>false</NuGetAudit><ImportDirectoryBuildProps>false</ImportDirectoryBuildProps><ImportDirectoryBuildTargets>false</ImportDirectoryBuildTargets></PropertyGroup><ItemGroup>'+refs+'</ItemGroup></Project>')
    (a.out/'NuGet.Config').write_text('<configuration><packageSources><clear /></packageSources></configuration>')
    results=[]
    for name,cmd in [('build',[a.dotnet,'build',str(a.out/'Proof.csproj'),'-c','Release','--configfile',str(a.out/'NuGet.Config')]),('run',[a.dotnet,str(a.out/'bin/Release/net10.0/Proof.dll')])]:
        r=subprocess.run(cmd,capture_output=True,text=True,timeout=60)
        (a.out/(name+'.log')).write_text(r.stdout+r.stderr);results.append({'command':cmd,'exit':r.returncode})
        if r.returncode:print(r.stdout[-1000:]+r.stderr[-1000:]);break
    (a.out/'result.json').write_text(json.dumps({'sourceSha256':hashlib.sha256(path.read_bytes()).hexdigest(),'mutation':a.mutation,'commands':results},indent=2))
    if not r.returncode:print(r.stdout.strip())
    return int(r.returncode!=0)


if __name__=='__main__':raise SystemExit(main())
