from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RepositoryHygieneTests(unittest.TestCase):
    def test_no_tracked_macos_metadata(self):
        tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
        self.assertEqual([p for p in tracked if Path(p).name == '.DS_Store' or Path(p).name.startswith('._')], [])

    def test_macos_metadata_ignored_at_root_and_nested(self):
        for path in ['.DS_Store', 'images/.DS_Store', 'nested/.AppleDouble/file',
                     'nested/._resource', '.Spotlight-V100/file', 'Icon\r\r', 'nested/.apdisk']:
            with self.subTest(path=path):
                result = subprocess.run(['git', 'check-ignore', '--no-index', '--quiet', path], cwd=ROOT)
                self.assertEqual(result.returncode, 0)

    def test_legitimate_files_and_runtime_modules_remain_trackable(self):
        for name in ['README.md', 'setup.py', 'asitop/__init__.py', 'asitop/asitop.py',
                     'asitop/utils.py', 'asitop/parsers.py', 'images/screenshot.png', 'Icon']:
            with self.subTest(path=name):
                result = subprocess.run(['git', 'check-ignore', '--no-index', '--quiet', name], cwd=ROOT)
                self.assertEqual(result.returncode, 1)
        for name in ['__init__.py', 'asitop.py', 'utils.py', 'parsers.py']:
            self.assertTrue((ROOT / 'asitop' / name).is_file())

    def test_ignore_entries_are_deduplicated(self):
        entries = [line for line in (ROOT / '.gitignore').read_bytes().decode().split('\n') if line and not line.startswith('#')]
        self.assertEqual(len(entries), len(set(entries)))
