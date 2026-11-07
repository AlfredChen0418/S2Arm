#!/usr/bin/env python3
"""Real Android package backends, rejection-before-replace, and Java consumers.

No game data or network. Uses the shared C resolver and this interpreter's pinned
Python dependencies; S2A_T05_EVIDENCE can retain command logs outside the checkout.
The shared-page contract is deliberately not skipped on an unfixed producer.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
MAKE = REPO / 'android/tools/make_pack.py'
spec = importlib.util.spec_from_file_location('android_pack', MAKE)
pack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pack)


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob('*')) if p.is_file()}


class MakePackRewriterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.retained = os.environ.get('S2A_T05_EVIDENCE')
        cls.base = Path(cls.retained or tempfile.mkdtemp(prefix='android-pack-backends-'))
        if cls.retained:
            cls.base.mkdir(parents=True, exist_ok=False)
        cls.commands = []
        cls.env = {k: v for k, v in os.environ.items() if not k.startswith('S2A_')}
        for name in ('S2A_REWRITER_DEPS', 'S2A_REWRITER_C'):
            if os.environ.get(name):
                cls.env[name] = os.environ[name]
        cls.env.update(PY=sys.executable, SOURCE_DATE_EPOCH='1700000000',
                       S2A_REWRITER_CACHE=str(cls.base / 'C cache'))
        result = cls.command([sys.executable, str(REPO / 'pack/rewriter.py'), 'c-path'], cls.env)
        if result.returncode:
            raise RuntimeError('shared C resolver failed: ' + result.stderr)
        cls.c = result.stdout.strip()

    @classmethod
    def tearDownClass(cls):
        if not cls.retained:
            shutil.rmtree(cls.base)

    @classmethod
    def command(cls, argv, env):
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=90)
        number = len(cls.commands)
        (cls.base / f'{number:03}.stdout.log').write_text(result.stdout)
        (cls.base / f'{number:03}.stderr.log').write_text(result.stderr)
        cls.commands.append(dict(argv=[str(x) for x in argv], returncode=result.returncode,
                                 environment={k: v for k, v in env.items() if k in
                                              ('PY', 'SOURCE_DATE_EPOCH', 'S2A_REWRITER', 'S2A_REWRITER_C',
                                               'S2A_TARGET', 'S2A_REWRITE_SPIN', 'S2A_SDK_ADDR_CUT')}))
        (cls.base / 'commands.json').write_text(json.dumps(cls.commands, indent=2) + '\n')
        return result

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix=self._testMethodName + '-', dir=self.base))
        self.root = Path(pack.synthetic_root(str(self.directory)))
        # A legitimate non-overlapping fixture: data begins on a 16KiB boundary.
        # The separate shared-page test retains the original layout and must pass
        # only when the shared C/Python Android producer contract is actually fixed.
        self.original_nso = (self.root / 'exefs/main').read_bytes()
        source = bytearray(self.original_nso)
        struct.pack_into('<I', source, 0x34, 0x4000)
        (self.root / 'exefs/main').write_bytes(source)
        shutil.copyfile(self.root / 'exefs/main', self.root / 'exefs/sdk')
        (self.root / 'widen_verified.handheld.tsv').write_text('authored test marker\n')
        # F7：合成安卓产物根带合法的平台能力清单。
        (self.root / 's2a_caps.tsv').write_text('plat\tandroid\nwiden\t1\nfps60\t1\n')

    def make(self, backend='c', out=None, expected=0, **settings):
        out = out or self.directory / ('package-' + backend)
        env = dict(self.env, **settings)
        if backend is not None:
            env['S2A_REWRITER'] = backend
        result = self.command([sys.executable, str(MAKE), str(out), '--root', str(self.root)], env)
        if expected == 0:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return out, result

    def table(self, heap=128, program='0100000000abcdef'):
        build = (self.root / 'exefs/main').read_bytes()[0x40:0x50].hex()
        return f'版本\t2\n程序号\t{program}\n构建号\t{build}\n别名区GB\t63\n普通堆MB\t{heap}\n'

    def old_output(self):
        out = self.directory / 'old-package'; out.mkdir()
        (out / 'manifest.json').write_text('{"version":2}')
        (out / 'index.tsv').write_text('old index\n')
        (out / 'keep.bin').write_bytes(b'previous package must survive')
        return out

    def assert_preserved(self, **settings):
        out = self.old_output(); before = snapshot(out)
        _, result = self.make(out=out, expected=1, **settings)
        self.assertEqual(snapshot(out), before)
        return result

    def wrapper(self, fault):
        path = self.directory / ('C-wrapper-' + fault)
        # Fault injection wraps the actual C CLI; no fake rewritten fixture is
        # used to establish a successful package or backend byte comparison.
        path.write_text('#!' + sys.executable + '\n' +
                        'import os,pathlib,subprocess,sys,json,re\n' +
                        f'fault={fault!r}\n' +
                        "if fault=='nonzero': sys.exit(7)\n" +
                        f'rc=subprocess.call([{self.c!r}]+sys.argv[1:])\n' +
                        'if rc: sys.exit(rc)\n' +
                        "prefix=pathlib.Path(sys.argv[2])/sys.argv[3]\n" +
                        "p=pathlib.Path(str(prefix)+'.fixups.tsv')\n" +
                        "s=p.read_text()\n" +
                        "if fault=='target': p.write_text(s.replace('# TARGET\\tandroid', '# TARGET\\tapple'))\n" +
                        "if fault=='abi': p.write_text(s.replace('android-sp-safe-v1','android-below-sp-v1'))\n" +
                        "if fault=='bias': p.write_text(s+'# LOAD_BIAS\\t0x1000\\n')\n" +
                        "if fault=='build': p.write_text(re.sub(r'(?m)^# BUILD_ID\\t.*$', '# BUILD_ID\\tinvalid', s))\n" +
                        "if fault=='duplicate': p.write_text(s+'# TARGET\\tandroid\\n')\n" +
                        "if fault=='missing': pathlib.Path(str(prefix)+'.gates.tsv').unlink()\n" +
                        "if fault=='gaps': pathlib.Path(str(prefix)+'.gaps.tsv').write_text('0x100\\tfixture\\tgap\\n')\n" +
                        "if fault=='environment':\n" +
                        " pathlib.Path(__file__+'.env.json').write_text(json.dumps({k:os.environ.get(k) for k in ('S2A_TARGET','S2A_REWRITE_SPIN','S2A_SDK_ADDR_CUT')}))\n" +
                        "if fault=='fps':\n" +
                        " pathlib.Path(__file__+'.fps.json').write_text(json.dumps(os.environ.get('S2A_FPS_TSV')))\n")
        path.chmod(0o700)
        return str(path)

    def test_real_backends_full_bytes_and_java_consumers(self):
        # Python 改写器 10-31 删除（ea97dc1e0）、不再对拍：C 是唯一后端；显式要 python 必须报红、不回退
        comparisons = []
        for spin, cut in ((0, 1), (1, 1), (0, 0)):
            options = dict(S2A_REWRITE_SPIN=str(spin), S2A_SDK_ADDR_CUT=str(cut))
            a, _ = self.make('c', out=self.directory / f'c-{spin}-{cut}', **options)
            comparisons.append(dict(spin=spin, sdk_addr_cut=cut, files={name:
                dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
                for name, data in snapshot(a).items()}))
        (self.base / 'comparisons.json').write_text(json.dumps(comparisons, indent=2) + '\n')
        a = self.directory / 'c-0-1'
        _, refused = self.make('python', out=self.directory / 'python-refused', expected=1)
        self.assertIn('Python 改写器已删除', refused.stdout + refused.stderr)
        # Check the default backend independently of explicit c selection.
        default, _ = self.make(None, out=self.directory / 'default-backend',
                              S2A_REWRITE_SPIN='0', S2A_SDK_ADDR_CUT='1')
        self.assertEqual(snapshot(default), snapshot(self.directory / 'c-0-1'))
        self.assertEqual((a / 'modules/addr_need.tsv').read_bytes(), (a / 'exefs/addr_need.tsv').read_bytes())
        # The table is static recognition of main alone: the heap is always the application pool (tools/addr_need.py APP_POOL_MB)
        self.assertIn('普通堆MB\t3284\n', (a / 'exefs/addr_need.tsv').read_text())
        self.assertEqual((a / 'modules/widen_verified.handheld.tsv').read_text(), 'authored test marker\n')
        artifact = json.loads((a / 'game-build-artifact.json').read_text())
        self.assertEqual(artifact['contentRoot'], 'sha256-' + hashlib.sha256((a / 'index.tsv').read_bytes()).hexdigest())
        # A table lying in the root (per-game or from an earlier pack) is never read back: same bytes as c-0-1 (same backend and options)
        before = json.loads((self.directory / 'c-0-1/game-build-artifact.json').read_text())['contentRoot']
        (self.root / 'addr_need.tsv').write_text(self.table(256))
        (self.root / 'exefs/addr_need.tsv').write_text(self.table(256))
        c, _ = self.make('c', out=self.directory / 'stray-requirements')
        self.assertEqual(before, json.loads((c / 'game-build-artifact.json').read_text())['contentRoot'])
        java = REPO / 'android/app/src/main/java/org/s2arm/android'
        tests = REPO / 'android/tests'
        classes = self.directory / 'classes'; classes.mkdir()
        sources = [str(java / (n + '.java')) for n in ('Json','BootContract','RuntimeLibrary','GamePack','GameFingerprint','GameInstall','GameStorage','GameMetadata')]
        sources += [str(tests / (n + '.java')) for n in ('GamePackTest','GameInstallTest')]
        compiled = self.command(['javac', '-encoding', 'UTF-8', '-d', str(classes)] + sources, self.env)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        for name in ('GamePackTest', 'GameInstallTest'):
            result = self.command(['java','-cp',str(classes),'org.s2arm.android.' + name,str(a)], self.env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_android_defaults_and_explicit_options_reach_actual_cli(self):
        wrapper = self.wrapper('environment')
        out, _ = self.make(S2A_REWRITER_C=wrapper, S2A_TARGET='apple')
        observed = json.loads(Path(wrapper + '.env.json').read_text())
        self.assertEqual(observed, dict(S2A_TARGET='android',S2A_REWRITE_SPIN='0',S2A_SDK_ADDR_CUT='1'))
        self.make(out=self.directory / 'explicit', S2A_REWRITER_C=wrapper,
                  S2A_REWRITE_SPIN='1', S2A_SDK_ADDR_CUT='0')
        observed = json.loads(Path(wrapper + '.env.json').read_text())
        self.assertEqual(observed, dict(S2A_TARGET='android',S2A_REWRITE_SPIN='1',S2A_SDK_ADDR_CUT='0'))
        self.assertIn('# TARGET\tandroid', (out / 'modules/main.fixups.tsv').read_text())

    def test_inherited_fps_table_does_not_reach_cli(self):
        # #48 D2：安卓没链 runtime/src/fps.c，读槽指令打进去槽一直是 0；环境里留着的 S2A_FPS_TSV 不许交给改写器
        table = self.directory / 'fps.tsv'
        table.write_text('# 夹具：内容不被读到\n')
        wrapper = self.wrapper('fps')
        self.make(S2A_REWRITER_C=wrapper, S2A_FPS_TSV=str(table))
        self.assertIsNone(json.loads(Path(wrapper + '.fps.json').read_text()))

    def test_selector_and_execution_failures_preserve_previous_output(self):
        for mode, settings in [('backend',dict(backend='invalid')),('missing',dict(S2A_REWRITER_C=str(self.directory/'absent'))),
                               ('exit',dict(S2A_REWRITER_C=self.wrapper('nonzero')))]:
            with self.subTest(mode=mode):
                out = self.directory / ('old-' + mode); out.mkdir()
                (out / 'manifest.json').write_text('{"version":2}'); (out / 'index.tsv').write_text('old\n')
                before = snapshot(out)
                self.make(out=out, expected=1, **settings)
                self.assertEqual(snapshot(out), before)

    def test_actual_cli_output_faults_are_rejected_before_replace(self):
        for fault in ('target','abi','bias','build','duplicate','missing','gaps'):
            with self.subTest(fault=fault):
                out = self.directory / ('old-' + fault); out.mkdir()
                (out/'manifest.json').write_text('{"version":2}'); (out/'index.tsv').write_text('old\n')
                before = snapshot(out)
                self.make(out=out, expected=1, S2A_REWRITER_C=self.wrapper(fault))
                self.assertEqual(snapshot(out), before)

    def test_write_pack_revalidates_every_module_before_replacing_output(self):
        # Start from real backend output, then corrupt the staged copy after the
        # rewriter's own admission. This isolates write_pack's final boundary.
        valid, _ = self.make(out=self.directory / 'valid-for-admission')
        for fault in ('missing-main', 'sdk-abi', 'sdk-missing-gates'):
            with self.subTest(fault=fault):
                modules = self.directory / ('modules-' + fault)
                shutil.copytree(valid / 'modules', modules)
                if fault == 'missing-main':
                    (modules / 'main.patched.bin').unlink()
                elif fault == 'sdk-abi':
                    path = modules / 'sdk.fixups.tsv'
                    path.write_text(path.read_text().replace('android-sp-safe-v1', 'wrong-abi'))
                else:
                    (modules / 'sdk.gates.tsv').unlink()
                out = self.directory / ('previous-' + fault); out.mkdir()
                (out / 'manifest.json').write_text('{"version":2}')
                (out / 'index.tsv').write_text('old index\n')
                (out / 'keep.bin').write_bytes(b'previous package must survive')
                before = snapshot(out)
                entries = [(name, str(valid / name)) for name in ('ctl', 'exefs', 'romfs')]
                entries.append(('modules', str(modules)))
                with self.assertRaises(SystemExit):
                    try:
                        pack.write_pack(str(out), entries, force=True)
                    finally:
                        self.assertTrue(snapshot(out) == before, fault + ' changed the previous package')

    def test_bad_nso_preserves_previous_output_and_foreign_table_is_ignored(self):
        good = (self.root/'exefs/main').read_bytes()
        (self.root/'exefs/main').write_bytes(b'not an NSO')
        self.assert_preserved()
        (self.root/'exefs/main').write_bytes(good)
        clean,_=self.make(out=self.directory/'clean')
        (self.root/'addr_need.tsv').write_text(self.table(program='0100000000000000'))
        stray,_=self.make(out=self.directory/'foreign-table')
        self.assertEqual((stray/'exefs/addr_need.tsv').read_bytes(),(clean/'exefs/addr_need.tsv').read_bytes())

    def test_force_cannot_replace_input_root(self):
        before=snapshot(self.root)
        result=self.command([sys.executable,str(MAKE),str(self.root),'--root',str(self.root),'--force'],self.env)
        self.assertNotEqual(result.returncode,0);self.assertEqual(snapshot(self.root),before)

    def check_shared_page_contract(self, backend):
        (self.root/'exefs/main').write_bytes(self.original_nso)
        (self.root/'exefs/sdk').unlink()
        out,_=self.make(backend)
        header=pack.fixups_header(str(out/'modules/main.fixups.tsv'))
        self.assertEqual(int(header.get('LOAD_BIAS','0'),16),0)

    def test_shared_page_android_contract_c(self):
        self.check_shared_page_contract('c')


if __name__ == '__main__':
    unittest.main()
