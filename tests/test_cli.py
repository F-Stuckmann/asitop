import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

FIXTURE = Path(__file__).with_name('cli_fixture.py')


class EntryPointTests(unittest.TestCase):
    def execute(self, arguments, entry, loops=1, size=(120, 50), extra_env=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'sitecustomize.py').write_text(FIXTURE.read_text())
            trace = root / 'events.jsonl'
            env = dict(os.environ, PYTHONPATH=directory, ASITOP_TRACE=str(trace),
                       ASITOP_LOOPS=str(loops), TERM='xterm-256color',
                       COLUMNS=str(size[0]), LINES=str(size[1]))
            env.update(extra_env or {})
            env.pop('NO_COLOR', None)
            env.pop('FORCE_COLOR', None)
            env.pop('CLICOLOR_FORCE', None)
            command = ([str(Path(sys.executable).with_name('asitop'))] if entry == 'console'
                       else [sys.executable, '-m', 'asitop.asitop'])
            result = subprocess.run(command + arguments, cwd=root, env=env,
                                    capture_output=True, text=True, timeout=10)
            events = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
            return result, events

    def test_defaults_through_both_entry_points(self):
        for entry in ['console', 'module']:
            with self.subTest(entry=entry):
                result, events = self.execute([], entry)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(events[0], {'event': 'launch', 'interval': 1000})
                titles = [w['title'] or '' for e in events if e['event'] == 'display' for w in e['widgets']]
                self.assertTrue(any(t.startswith('P-CPU Usage: 25%') for t in titles))
                self.assertTrue(any(t.startswith('S-CPU Usage: 50%') for t in titles))
                self.assertFalse(any(t.startswith('Core ') for t in titles))
                self.assertEqual(events[-1]['event'], 'cleanup')

    def test_every_option_and_boolean_form_through_both_entry_points(self):
        forms = [([], False), (['--show_cores'], True)]
        forms += [(['--show_cores', value], expected) for value, expected in
                  [('0', False), ('1', True), ('false', False), ('true', True), ('FALSE', False), ('TrUe', True)]]
        for entry in ['console', 'module']:
            for flags, cores in forms:
                with self.subTest(entry=entry, flags=flags):
                    result, events = self.execute(['--interval', '2', '--avg', '4', '--color', '8', '--max_count', '1'] + flags, entry, loops=2)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    launches = [e for e in events if e['event'] == 'launch']
                    self.assertEqual(launches, [{'event': 'launch', 'interval': 2000}]*2)
                    self.assertEqual(len([e for e in events if e['event'] == 'cleanup']), 2)
                    titles = [w['title'] or '' for e in events if e['event'] == 'display' for w in e['widgets']]
                    self.assertEqual(any(t.startswith('Core ') for t in titles), cores)

    def test_help_and_invalid_options_never_launch_collector(self):
        invalid = [['--show_cores', 'yes'], ['--show_cores', '2'], ['--interval', '0'],
                   ['--interval', '-1'], ['--interval', 'nan'], ['--avg', '0'], ['--avg', '-1'],
                   ['--avg', '1', '--interval', '2'], ['--color', '-1'], ['--color', '9'],
                   ['--avg', 'x'], ['--color', 'x'], ['--interval', 'x'], ['--max_count', '-1'], ['--max_count', 'x'], ['--unknown']]
        for entry in ['console', 'module']:
            for flags in [['-h'], ['--help']] + invalid:
                with self.subTest(entry=entry, flags=flags):
                    result, events = self.execute(flags, entry)
                    self.assertEqual(result.returncode, 0 if flags in [['-h'], ['--help']] else 2, result.stderr)
                    self.assertEqual(events, [])
                    self.assertIn('usage:', result.stdout + result.stderr)

    def test_compact_cores_mode_keeps_family_utilization(self):
        for size in [(99, 28), (100, 27), (20, 5)]:
            with self.subTest(size=size):
                result, events = self.execute(['--show_cores'], 'module', size=size)
                self.assertEqual(result.returncode, 0, result.stderr)
                titles = [w['title'] or '' for e in events if e['event'] == 'display' for w in e['widgets']]
                self.assertFalse(any(t.startswith('Core ') for t in titles))
                values = [w['value'] for e in events if e['event'] == 'display' for w in e['widgets']]
                if size[0] < 40:
                    self.assertIn('25%', result.stdout)
                    self.assertIn('50%', result.stdout)
                else:
                    self.assertIn(25, values)
                    self.assertIn(50, values)

    def test_layout_boundary_allows_core_mode(self):
        result, events = self.execute(['--show_cores'], 'module', size=(100, 28))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any((w['title'] or '').startswith('Core ') for e in events if e['event'] == 'display' for w in e['widgets']))

    def test_color_zero_and_single_sample_averaging_window(self):
        for entry in ['console', 'module']:
            result, events = self.execute(['--interval', '2', '--avg', '2', '--color', '0', '--max_count', '0', '--show_cores', 'false'], entry, loops=2)
            self.assertEqual(result.returncode, 0, result.stderr)
            displays = [e for e in events if e['event'] == 'display']
            self.assertEqual(len(displays), 2)
            self.assertTrue(any(w['title'] == 'CPU: 1.50W (avg: 1.50W peak: 1.50W)' for w in displays[-1]['widgets']))
            self.assertTrue(all(w['color'] == 0 for w in displays[-1]['widgets'] if w['value'] is not None))

    def test_authentication_failure_reports_exit_and_cleans_up(self):
        for entry in ['console', 'module']:
            result, events = self.execute([], entry, extra_env={'ASITOP_AUTH_FAIL': '1'})
            self.assertEqual(result.returncode, 1)
            self.assertIn('powermetrics exited', result.stderr)
            self.assertEqual([e['event'] for e in events], ['launch', 'cleanup'])

    def test_redirected_output_and_hostile_term_never_emit_controls(self):
        for entry in ['console', 'module']:
            result, events = self.execute([], entry, extra_env={'ASITOP_REDIRECT': '1', 'TERM': 'evil\x1b[2J\n'})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('\x1b', result.stdout)
            self.assertIn('P-CPU Usage: 25%', result.stdout)
            self.assertIn('S-CPU Usage: 50%', result.stdout)
            self.assertEqual(events[-1]['event'], 'cleanup')
