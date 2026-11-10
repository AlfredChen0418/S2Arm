# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 S2Arm contributors
"""Long-batch activity is observed outside the watched build process tree."""
import json
import os
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch
from jobcase import JobCase, ROOT
from core import Cancelled, Failure
from progress_activity import BatchActivity as Activity


class BatchActivity(JobCase):
    stage = '直连库·编批库（MB）'

    def meta(self, **changes):
        data = dict(schema=1, pid=123, stage=self.stage, target=5, pool=10, state='active')
        return '#dsh_activity\t' + json.dumps(data | changes, ensure_ascii=False) + '\n'

    def begin_activity(self):
        self.c._batch_activity = Activity(tree=lambda pid: {pid:(0, 'dsh_batch'), 124:(0, 'metal'), 125:(0, 'metallib'), 126:(0, 'metal.exe')})
        p = self.root / 'progress.tsv'
        p.write_text(f'100\t{self.stage}\t0\t100\n105\t{self.stage}\t20\t100\n' + self.meta(), encoding='utf-8')
        with patch('core.time.monotonic', return_value=105), patch('core.time.time', return_value=105), patch.object(self.c, 'emit') as emit:
            pos = self.c.stage_progress(p, 0)
        return p, pos, emit

    def test_real_progress_reports_target_pool_and_sampled_compilers(self):
        _, _, emit = self.begin_activity()
        event = emit.call_args.kwargs
        self.assertEqual(event.get('activity'), dict(stage=self.stage, pid=123, target=5, pool=10, inFlight=3))
        self.assertEqual((event['done'], event['total']), (20, 100))
        self.assertTrue(event['message'].startswith(self.stage))

    def test_long_batch_pulse_preserves_done_weights_and_progress_file(self):
        p, pos, _ = self.begin_activity()
        before = p.read_bytes()
        with patch('core.time.monotonic', return_value=115), patch('core.time.time', return_value=115), patch.object(self.c, 'emit') as emit:
            self.assertEqual(self.c.stage_progress(p, pos), pos)
        self.assertEqual(emit.call_count, 1)
        event = emit.call_args.kwargs
        self.assertEqual((event['done'], event['total']), (20, 100))
        self.assertEqual(event['counters'][0]['rate'], 1.333)  # 20 MB / 15 elapsed seconds.
        self.assertEqual(p.read_bytes(), before, 'observer pulse must not write to the watchdog input')

    def test_pulse_is_bounded_and_ended_activity_stops_it(self):
        p, pos, _ = self.begin_activity()
        with patch('core.time.monotonic', return_value=106), patch.object(self.c, 'emit') as emit:
            self.c.stage_progress(p, pos)
        emit.assert_not_called()
        with p.open('a', encoding='utf-8') as f:
            f.write(self.meta(state='ended'))
        with patch('core.time.monotonic', return_value=116), patch.object(self.c, 'emit') as emit:
            pos = self.c.stage_progress(p, pos)
            self.c.stage_progress(p, pos)
        self.assertEqual(emit.call_count, 1)
        self.assertNotIn('activity', emit.call_args.kwargs)
        self.assertEqual(emit.call_args.kwargs['message'], self.stage + ' 20/100')

    def test_other_process_end_does_not_clear_active_owner(self):
        p, pos, _ = self.begin_activity()
        with p.open('a', encoding='utf-8') as f:
            f.write(self.meta(pid=999, state='ended'))
        with patch('core.time.monotonic', return_value=115), patch('core.time.time', return_value=115), patch.object(self.c, 'emit') as emit:
            self.c.stage_progress(p, pos)
        self.assertEqual(emit.call_count, 1)
        self.assertEqual(emit.call_args.kwargs['activity']['pid'], 123)

    def test_malformed_metadata_preserves_legacy_progress(self):
        p = self.root / 'invalid.tsv'
        rows = ['#dsh_activity\t{bad}\n', self.meta(target=True), self.meta(schema=True), self.meta(pool=1)]
        for row in rows:
            with self.subTest(row=row):
                p.write_text(f'100\t{self.stage}\t20\t100\n' + row, encoding='utf-8')
                with patch.object(self.c, 'emit') as emit:
                    self.c.stage_progress(p, 0)
                self.assertEqual((emit.call_args.kwargs['done'], emit.call_args.kwargs['total']), (20, 100))
                self.assertNotIn('activity', emit.call_args.kwargs)

    def test_unavailable_sampling_is_unknown_not_zero_or_ended(self):
        def denied(pid):
            raise PermissionError('synthetic denied enumeration')
        for tree in [denied, lambda pid: {pid: (0, '')}]:
            with self.subTest(tree=tree):
                activity = Activity(tree=tree, alive=lambda pid: True)
                activity.accept(self.meta())
                self.assertIsNone(activity.snapshot()['inFlight'])
                self.assertIsNotNone(activity.data)
                self.assertIn('未知', activity.describe(activity.snapshot()))
        activity = Activity(tree=lambda pid: {pid: (0, 'dsh_batch')})
        activity.accept(self.meta())
        self.assertEqual(activity.snapshot()['inFlight'], 0, 'known live tree without compilers is a measured zero')

    def test_abrupt_owner_exit_clears_display_without_advancing_work(self):
        p, pos, _ = self.begin_activity()
        self.c._batch_activity.tree = lambda pid: {pid: (0, '')}
        self.c._batch_activity.alive = lambda pid: False
        with patch('core.time.monotonic', return_value=115), patch.object(self.c, 'emit') as emit:
            self.c.stage_progress(p, pos)
            self.c.stage_progress(p, pos)
        self.assertIsNone(self.c._batch_activity.data)
        self.assertEqual(emit.call_count, 1)
        self.assertEqual((emit.call_args.kwargs['done'], emit.call_args.kwargs['total']), (20, 100))
        self.assertNotIn('activity', emit.call_args.kwargs)

    def test_cache_only_activity_has_no_invented_denominator(self):
        self.c._batch_activity = Activity(tree=lambda pid: {pid: (0, 'dsh_batch')})
        self.c._counters = {self.stage: dict(name=self.stage, done=20, total=100, t0=100, d0=0, t=105)}
        p = self.root / 'cached.tsv'
        p.write_text(self.meta(), encoding='utf-8')
        with patch.object(self.c, 'emit') as emit:
            pos = self.c.stage_progress(p, 0)
        self.assertIn('activity', emit.call_args.kwargs)
        self.assertNotIn('done', emit.call_args.kwargs)
        self.assertNotIn('total', emit.call_args.kwargs)
        with p.open('a', encoding='utf-8') as f:
            f.write(self.meta(state='ended'))
        with patch.object(self.c, 'emit') as emit:
            self.c.stage_progress(p, pos)
        self.assertEqual(emit.call_args.kwargs, dict(message=self.stage))

    def test_process_sampling_is_bounded_by_the_same_pulse_interval(self):
        with patch('progress_activity.time.monotonic', return_value=100):
            activity = Activity(tree=lambda pid: {pid: (0, 'dsh_batch')})
            activity.accept(self.meta())
        with patch.object(activity, 'tree', wraps=activity.tree) as tree:
            for now in [100, 101, 109, 110]:
                with patch('progress_activity.time.monotonic', return_value=now):
                    activity.snapshot()
            self.assertEqual(tree.call_count, 2)

    def child(self, progress, delay=2.2, code=0):
        script = '''import json,os,time,sys
from pathlib import Path
p=Path(os.environ['S2A_PROGRESS']); now=time.time()
p.write_text(f"{now-1}\\t直连库·编批库（MB）\\t0\\t100\\n{now}\\t直连库·编批库（MB）\\t20\\t100\\n",encoding='utf-8')
with p.open('a',encoding='utf-8') as f:
 f.write('#dsh_activity\\t'+json.dumps(dict(schema=1,pid=os.getpid(),stage='直连库·编批库（MB）',target=5,pool=10,state='active'))+'\\n')
time.sleep(float(sys.argv[1])); sys.exit(int(sys.argv[2]))
'''
        return [sys.executable, '-c', script, str(delay), str(code)], dict(os.environ, S2A_PROGRESS=str(progress))

    def test_process_success_and_exception_clear_observer_lifetime(self):
        self.c._batch_activity = Activity(interval=.1)
        p = self.root / 'live.tsv'
        for code in [0, 3]:
            with self.subTest(code=code):
                p.unlink(missing_ok=True)
                command, env = self.child(p, code=code)
                with patch.object(self.c, 'emit', wraps=self.c.emit) as emit:
                    if code:
                        with self.assertRaises(Failure):
                            self.c.run(command, env=env, timeout=6)
                    else:
                        self.c.run(command, env=env, timeout=6)
                pulses = [call.kwargs for call in emit.call_args_list if 'activity' in call.kwargs]
                self.assertGreaterEqual(len(pulses), 2)
                self.assertTrue(all((e['done'], e['total']) == (20, 100) for e in pulses))
                self.assertTrue(all(e['activity']['inFlight'] == 0 for e in pulses), 'real live Python child has no Metal descendants')
                self.assertIsNone(self.c._batch_activity.data)
                self.assertFalse(self.c.children)
                with patch.object(self.c, 'emit', wraps=self.c.emit) as emit:
                    self.c.run([sys.executable, '-c', 'import time; time.sleep(1.2)'], env=env, timeout=4)
                self.assertFalse(any('activity' in call.kwargs for call in emit.call_args_list))

    def test_cancellation_clears_observer_and_reaps_child(self):
        self.c._batch_activity = Activity(interval=.1)
        command, env = self.child(self.root / 'cancel.tsv', delay=30)
        timer = threading.Timer(1.4, self.c.stop)
        timer.start()
        try:
            with patch.object(self.c, 'emit', wraps=self.c.emit) as emit:
                with self.assertRaises(Cancelled):
                    self.c.run(command, env=env, timeout=6)
            self.assertTrue(any('activity' in call.kwargs for call in emit.call_args_list))
            self.assertIsNone(self.c._batch_activity.data)
            self.assertFalse(self.c.children)
        finally:
            timer.cancel(); timer.join(timeout=5)
            self.c.cancelled = False

    def test_observer_pulses_do_not_keep_an_idle_child_alive(self):
        self.c._batch_activity = Activity(interval=.1)
        p = self.root / 'idle.tsv'
        command, env = self.child(p, delay=30)
        env['S2A_NOSTALL_POLL'] = '.1'
        watched = [sys.executable, str(ROOT / 'tools/nostall.py'), '--watch', str(p), '2.3', '--', *command]
        with patch.object(self.c, 'emit', wraps=self.c.emit) as emit:
            with self.assertRaises(Failure):
                self.c.run(watched, env=env, timeout=6)
        self.assertEqual(self.c.last_rc, 124, 'watchdog must detect the idle child despite UI pulses')
        self.assertGreaterEqual(sum('activity' in call.kwargs for call in emit.call_args_list), 2)
        self.assertIsNone(self.c._batch_activity.data)

    @unittest.skipUnless(os.name == 'posix', 'legacy POSIX progress.sh consumer')
    def test_old_shell_reader_ignores_optional_metadata(self):
        p = self.root / 'legacy.tsv'; plain = self.root / 'plain.tsv'
        text = f'100\t{self.stage}\t0\t100\n105\t{self.stage}\t20\t100\n'
        plain.write_text(text, encoding='utf-8'); p.write_text(text + self.meta(), encoding='utf-8')
        def show(path):
            return subprocess.check_output(['/bin/bash', str(ROOT / 'tools/progress.sh'), 'show', str(path), '115'])
        self.assertEqual(show(p), show(plain))


if __name__ == '__main__':
    unittest.main()
