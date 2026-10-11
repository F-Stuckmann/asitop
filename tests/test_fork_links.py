import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ForkLinkTests(unittest.TestCase):
    def test_startup_banner_directs_help_and_updates_to_fork(self):
        for entry in ['console', 'module']:
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as directory:
                Path(directory, 'sitecustomize.py').write_text(
                    'from asitop import utils\n'
                    'def stop():\n    raise SystemExit(0)\n'
                    'utils.get_soc_info = stop\n'
                    'def forbidden(*args, **kwargs):\n    raise AssertionError("collector launched")\n'
                    'utils.run_powermetrics_process = forbidden\n')
                command = ([str(Path(sys.executable).with_name('asitop'))] if entry == 'console'
                           else [sys.executable, '-m', 'asitop.asitop'])
                result = subprocess.run(command, cwd=directory,
                                        env=dict(os.environ, PYTHONPATH=directory),
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('Get help at `https://github.com/F-Stuckmann/asitop`', result.stdout)
                self.assertIn('pip install --upgrade git+https://github.com/F-Stuckmann/asitop.git', result.stdout)
                self.assertNotIn('https://github.com/tlkh/asitop', result.stdout)
                self.assertNotIn('pip install asitop --upgrade', result.stdout)
