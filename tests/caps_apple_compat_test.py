#!/usr/bin/env python3
"""Fixed golden derived from the pre-F7 Apple implementation, with functional mutants."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'tools/caps_policy.py'
FILES = ('runtime/src/widen.c', 'graphics/metal/gal_present.m', 'graphics/metal/fit_judge.h',
         'graphics/nvn/gfx/nvn/nvn_mtl.m', 'runtime/src/caps.c', 'graphics/nvn/widen_glue.inc',
         'graphics/widen/WidenFrame.cs', 'graphics/widen/WidenSession.cs', 'graphics/widen/export/s2a_widen.h',
         'runtime/src/fps.c', 'runtime/src/cpatch.c', 'tools/widen_accept.py', 'pack/cpatch.py',
         'tools/fps_generic.py', 'tools/ue_cvar_loc.py', 'tools/fps_warehouse.py',
         'tools/fps_relocate.py', 'graphics/widen/gal/WidenRenderer.cs')
# 11-09 按公开仓路径重定（审计 / 主线线）：哈希把实现文件的路径名也算进去，迁仓把 native/ → runtime/、gfx/ → graphics/ 等，
#   测试与 tools/caps_policy.py 的路径都跟着改了、这两组值没跟着重算，公开快照 3925321 起就红（只是一直没有改动路由到它）。
#   核过算法没变：迁仓冻结点 migrate/freeze-F 上同一支测试用老路径、老值全过；两版 caps_policy.py 只差注释与路径字符串
CAP = {'widen': '5731125a7e34cd2b', 'fps60': 'ce263cc1029214a4'}
SIGNAL = {'widen': 'b1e392bdd380f8ba', 'fps60': 'c4b93355482ca293'}


def check(module, root, repo):
    cap = module.cap_hashes(str(root), str(repo))
    signal = module.signal_hashes(module.repo_reader(str(repo)))
    assert cap == CAP, f'Apple cap: expected {CAP}, got {cap}'
    assert signal == SIGNAL, f'Apple signal: expected {SIGNAL}, got {signal}'


def load(path):
    spec = importlib.util.spec_from_file_location('caps_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    with tempfile.TemporaryDirectory(prefix='caps-apple-golden-') as tmp:
        temp = Path(tmp)
        root, repo = temp / 'root', temp / 'repo'
        root.mkdir(); repo.mkdir()
        for name in FILES:
            path = repo / name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixture_value = 42\n' if name.endswith('.py') else 'int fixture_value = 42;\n')
        for name, value in (('widen_enable.handheld.tsv', 'main\t0x10\tmovz\t1\n'),
                            ('fps.tsv', 'fps fixture\n'), ('cpatch.tsv', 'widescreen:fixture\nfps:fixture\n')):
            (root / name).write_text(value)
        module = load(SOURCE)
        check(module, root, repo)
        print('Apple baseline cap and signal golden: PASS')
        text = SOURCE.read_text()
        salt = "if target == 'android': d.update(b'plat:' + target.encode() + b'\\0')"
        assert text.count(salt) == 2, 'expected exactly two platform salt seams'
        mutated = temp / 'salt.py'
        mutated.write_text(text.replace(salt, "d.update(b'plat:' + target.encode() + b'\\0')"))
        try:
            check(load(mutated), root, repo)
        except AssertionError:
            print('Apple salt negative control: expected assertion failure')
        else:
            raise AssertionError('Apple salt mutation survived')
        module = load(SOURCE)
        extra = ('graphics/widen/gal/WidenRenderer.cs',)
        module.IMPL['apple']['widen'] += extra
        module.SIGNAL_IMPL['apple']['widen'] += extra
        try:
            check(module, root, repo)
        except AssertionError:
            print('Apple WidenRenderer negative control: expected assertion failure')
        else:
            raise AssertionError('Apple WidenRenderer mutation survived')


if __name__ == '__main__':
    main()
