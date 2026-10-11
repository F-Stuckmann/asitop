import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from asitop import utils


class SessionTests(unittest.TestCase):
    def test_concurrent_sessions_use_private_exclusive_logs_and_fixed_commands(self):
        with tempfile.TemporaryDirectory() as root:
            unrelated = Path(root, 'asitop_powermetrics-other')
            unrelated.write_text('keep')
            with patch.object(utils.tempfile, 'gettempdir', return_value=root), \
                 patch.object(utils.subprocess, 'Popen') as launch, \
                 patch.object(utils.os, 'geteuid', return_value=501), \
                 patch.dict(os.environ, {'PATH': '/hostile'}):
                a = utils.run_powermetrics_process(';$(touch hostile)')
                b = utils.run_powermetrics_process(';$(touch hostile)')
            self.assertNotEqual(a.log_path, b.log_path)
            self.assertEqual(Path(a.directory).stat().st_mode & 0o777, 0o700)
            self.assertEqual(Path(a.log_path).stat().st_mode & 0o777, 0o600)
            self.assertEqual(unrelated.read_text(), 'keep')
            command = launch.call_args.args[0]
            self.assertEqual(command[:4], ['/usr/bin/sudo', '/usr/bin/nice', '-n', '10'])
            self.assertEqual(command[4], '/usr/bin/powermetrics')
            self.assertNotIn('-o', command)
            self.assertTrue(launch.call_args.kwargs['start_new_session'])
            self.assertFalse(launch.call_args.kwargs.get('shell', False))
            self.assertTrue(launch.call_args.kwargs['stdout'].closed)

    def test_root_launch_omits_sudo_and_startup_failure_removes_session(self):
        with tempfile.TemporaryDirectory() as root, \
             patch.object(utils.tempfile, 'gettempdir', return_value=root), \
             patch.object(utils.os, 'geteuid', return_value=0), \
             patch.object(utils.subprocess, 'Popen', side_effect=OSError('launch')) as launch:
            with self.assertRaises(OSError):
                utils.run_powermetrics_process('0')
            self.assertEqual(launch.call_args.args[0][0], '/usr/bin/nice')
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_cleanup_denied_preserves_files_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as root:
            log = Path(root, 'metrics'); log.touch()
            process = Mock(pid=54321)
            process.poll.return_value = None
            session = utils.Collector(process, root, str(log))
            with patch.object(utils.os, 'killpg', side_effect=PermissionError), \
                 patch.object(utils.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'sudo')) as elevate:
                with self.assertRaisesRegex(RuntimeError, 'cleanup'):
                    utils.terminate_powermetrics_process(session)
            self.assertTrue(log.exists())
            self.assertEqual(elevate.call_args.args[0],
                             ['/usr/bin/sudo', '-n', '/bin/kill', '-15', '--', '-54321'])

    def test_real_group_cleanup_is_idempotent_and_leaves_other_session_running(self):
        with tempfile.TemporaryDirectory() as root:
            sessions = []
            for _ in range(2):
                directory = tempfile.mkdtemp(dir=root)
                log = Path(directory, 'metrics'); log.touch()
                process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                           start_new_session=True)
                sessions.append(utils.Collector(process, directory, str(log)))
            try:
                utils.terminate_powermetrics_process(sessions[0])
                utils.terminate_powermetrics_process(sessions[0])
                self.assertIsNotNone(sessions[0].process.poll())
                self.assertFalse(Path(sessions[0].directory).exists())
                self.assertIsNone(sessions[1].process.poll())
                self.assertTrue(Path(sessions[1].log_path).exists())
            finally:
                for session in sessions:
                    if session.process.poll() is None:
                        os.killpg(session.pid, signal.SIGKILL)
                        session.process.wait()

    def test_exited_leader_still_cleans_owned_group(self):
        with tempfile.TemporaryDirectory() as root:
            log = Path(root, 'metrics'); log.touch()
            process = Mock(pid=12345)
            process.poll.return_value = 1
            session = utils.Collector(process, root, str(log))
            with patch.object(utils.os, 'killpg', side_effect=[None, ProcessLookupError]) as kill:
                utils.terminate_powermetrics_process(session)
            self.assertEqual(kill.call_args_list[0].args, (12345, signal.SIGTERM))
            self.assertFalse(log.exists())

class ProcessTreeTests(unittest.TestCase):
    def test_real_parent_and_child_group_are_both_terminated(self):
        code = '''
import signal, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
def stop(signum, frame):
    child.wait(timeout=3)
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
print(child.pid, flush=True)
while True: time.sleep(1)
'''
        with tempfile.TemporaryDirectory() as root:
            directory = tempfile.mkdtemp(dir=root)
            log = Path(directory, 'metrics'); log.touch()
            process = subprocess.Popen([sys.executable, '-c', code], start_new_session=True,
                                       stdout=subprocess.PIPE, text=True)
            try:
                child_pid = int(process.stdout.readline())
                self.assertEqual(os.getpgid(child_pid), process.pid)
                session = utils.Collector(process, directory, str(log))
                utils.terminate_powermetrics_process(session)
                self.assertIsNotNone(process.poll())
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
                self.assertFalse(Path(directory).exists())
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                process.stdout.close()
