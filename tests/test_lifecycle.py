from contextlib import ExitStack
import unittest
from unittest.mock import Mock, patch

from asitop import asitop as app
from test_asitop import make_reading


class LifecycleTests(unittest.TestCase):
    def run_dashboard(self, readings, failure=None, argv=None):
        with ExitStack() as stack:
            session = Mock(log_path='/private/metrics')
            session.poll.return_value = None
            run = stack.enter_context(patch.object(app, 'run_powermetrics_process', return_value=session))
            stop = stack.enter_context(patch.object(app, 'terminate_powermetrics_process'))
            parse = stack.enter_context(patch.object(app, 'parse_powermetrics', side_effect=readings))
            stack.enter_context(patch.object(app, 'get_soc_info', return_value={
                'name': 'Apple M5', 'gpu_core_count': 20, 'cpu_max_power': 20, 'gpu_max_power': 20}))
            stack.enter_context(patch.object(app, 'get_ram_metrics_dict', return_value={
                'used_GB': 1, 'total_GB': 8, 'total_bytes': 8 * 2**30, 'free_percent': 12, 'swap_total_GB': 0}))
            stack.enter_context(patch.object(app, 'get_gpu_memory_bytes', return_value=None))
            stack.enter_context(patch.object(app, 'clear_console'))
            stack.enter_context(patch.object(app.time, 'sleep'))
            display = stack.enter_context(patch.object(app, 'display_dashboard', side_effect=failure))
            if failure:
                with self.assertRaisesRegex(RuntimeError, 'render'):
                    app.main(argv or [])
            else:
                self.assertEqual(app.main(argv or []), 0)
            self.assertGreaterEqual(stop.call_count, 1)
            stop.assert_called_with(session)
            for call in parse.call_args_list:
                self.assertEqual(call.kwargs, {'path': session.log_path, 'timecode': ''})
            return run, display

    def test_duplicate_timestamps_do_not_render_or_add_power_samples(self):
        _, display = self.run_dashboard([make_reading(1), make_reading(2), make_reading(2), KeyboardInterrupt])
        self.assertEqual(display.call_count, 1)

    def test_render_failure_cleans_up(self):
        self.run_dashboard([make_reading(1), make_reading(2)], RuntimeError('render'))

    def test_interrupt_during_first_reading_cleans_up(self):
        self.run_dashboard([KeyboardInterrupt])

    def test_restart_uses_new_session_and_cleans_up(self):
        run, _ = self.run_dashboard([make_reading(1), make_reading(2), make_reading(3), KeyboardInterrupt], argv=['--max_count', '1'])
        self.assertEqual(run.call_count, 3)

    def test_collector_exit_while_waiting_reports_failure(self):
        session = Mock(log_path='/private/metrics')
        session.poll.return_value = 1
        with patch.object(app, 'parse_powermetrics', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'exited'):
                app._wait_for_reading(session)

    def test_empty_topology_builds_dashboard(self):
        result = app._build_processor_widgets([], 2, False)
        self.assertEqual(result[1], {})
        self.assertEqual(len(result[0].items), 1)

    def test_more_than_eight_cores_each_get_their_own_gauge(self):
        groups = [{'family': 'Q', 'label': 'Q-CPU', 'core_count': 17,
                   'active': 30, 'freq_Mhz': 1000,
                   'cores': [{'id': i, 'active': i, 'freq_Mhz': 1000} for i in range(17)]}]
        split, gauges, cores, _, _ = app._build_processor_widgets(groups, 2, True)
        app._update_processor_widgets(groups, gauges, cores, True)
        self.assertEqual(len(cores['Q']), 17)
        self.assertEqual([g.value for g in cores['Q'].values()], list(range(17)))
        self.assertEqual(len(split.items), 6)

    def test_labels_cannot_inject_terminal_controls(self):
        title = app._processor_title('chip\x1b[2J\n', [], 1)
        self.assertNotIn('\x1b', title)
        self.assertNotIn('\n', title)

class LifetimeBoundaryTests(unittest.TestCase):
    def test_normal_return_cleans_up_current_session(self):
        session = Mock()
        def finish(args, sessions):
            sessions.append(session)
            return 0
        with patch.object(app, '_main', side_effect=finish), \
             patch.object(app, 'terminate_powermetrics_process') as stop:
            self.assertEqual(app.main([]), 0)
        stop.assert_called_once_with(session)

    def test_widget_construction_failure_cleans_up(self):
        session = Mock(log_path='/private/log')
        session.poll.return_value = None
        with patch.object(app, 'get_soc_info', return_value={}), \
             patch.object(app, 'run_powermetrics_process', return_value=session), \
             patch.object(app, 'parse_powermetrics', return_value=make_reading(1)), \
             patch.object(app, '_build_processor_widgets', side_effect=RuntimeError('widget')), \
             patch.object(app, 'terminate_powermetrics_process') as stop:
            with self.assertRaisesRegex(RuntimeError, 'widget'):
                app.main([])
        stop.assert_called_once_with(session)

    def test_reading_timeout_is_bounded(self):
        session = Mock(log_path='/private/log')
        session.poll.return_value = None
        with patch.object(app, 'parse_powermetrics', return_value=False), \
             patch.object(app.time, 'monotonic', side_effect=[0, 16]):
            with self.assertRaisesRegex(RuntimeError, '15 seconds'):
                app._wait_for_reading(session)
