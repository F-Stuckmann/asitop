import curses
import io
import os
import unittest
from unittest.mock import Mock, patch

from blessed import Terminal
from asitop import asitop as app


class TerminalTests(unittest.TestCase):
    def test_ghostty_uses_available_terminfo(self):
        setup = Mock()
        self.assertEqual(app.resolve_terminal_kind('xterm-ghostty', setup), 'xterm-ghostty')
        self.assertEqual(setup.call_args.args[0], 'xterm-ghostty')

    def test_ghostty_falls_back_and_typo_is_normalized(self):
        setup = Mock(side_effect=[curses.error('missing'), None])
        self.assertEqual(app.resolve_terminal_kind('xterm-ghossty', setup), 'xterm-256color')
        self.assertEqual([c.args[0] for c in setup.call_args_list], ['xterm-ghostty', 'xterm-256color'])

    def test_all_fallback_failures_return_plain_terminal(self):
        setup = Mock(side_effect=curses.error('missing'))
        self.assertIsNone(app.resolve_terminal_kind('invalid', setup))
        self.assertEqual(setup.call_count, 5)
        with patch.object(app, 'resolve_terminal_kind', return_value=None):
            terminal, kind = app.build_terminal()
        self.assertFalse(terminal.does_styling)
        self.assertIsNone(kind)

    def test_missing_empty_or_control_term_is_never_sent_to_curses(self):
        for kind in ['', '\x1b[2J', 'xterm\n256', 'xterm\x00', 'a'*1000]:
            with self.subTest(kind=kind):
                setup = Mock()
                with patch.dict(app.os.__dict__, environ={'TERM': kind}):
                    self.assertEqual(app.resolve_terminal_kind(setupterm=setup), 'xterm-256color')
                self.assertEqual(setup.call_args.args[0], 'xterm-256color')

    def test_redirected_output_uses_plain_mode_without_terminfo_lookup(self):
        with patch.object(app.sys, 'stdout', io.StringIO()), \
             patch.object(app, 'resolve_terminal_kind') as resolve:
            terminal, kind = app.build_terminal()
        resolve.assert_not_called()
        self.assertFalse(terminal.does_styling)
        self.assertIsNone(kind)

    def test_clip_uses_unicode_display_width_and_removes_controls(self):
        terminal = Terminal(kind='dumb')
        for text in ['界界界界', 'e\u0301'*8, 'long label'*20, '\x1b[2Jevil\n\tname']:
            for width in range(0, 15):
                with self.subTest(text=text, width=width):
                    clipped = app.clip_text(text, width)
                    self.assertLessEqual(terminal.length(clipped), width)
                    self.assertNotIn('\x1b', clipped)
                    self.assertNotIn('\n', clipped)
        self.assertEqual(app.clip_text('界界界', 5), '界...')

    def test_dashing_rendering_at_small_sizes_does_not_crash_or_overflow_titles(self):
        groups = [{'family': 'Q', 'label': '界'*20, 'core_count': 0,
                   'active': 25, 'freq_Mhz': 2000, 'cores': []}]
        for width, height in [(20, 5), (80, 24), (99, 28), (100, 28), (120, 50)]:
            with self.subTest(width=width, height=height):
                ui, gauges, cores, gpu, ane = app._build_processor_widgets(groups, 2, False)
                app._update_processor_widgets(groups, gauges, cores, False)
                terminal = Mock(width=width, height=height, clear='', home='')
                terminal.move.return_value = ''
                terminal.color.return_value = ''
                ui._terminal = terminal
                with patch.object(app.shutil, 'get_terminal_size', return_value=(width, height)), patch.object(app.sys, 'stdout', io.StringIO()) as output:
                    app.display_dashboard(ui, terminal)
                self.assertNotIn('\x1b', output.getvalue())
                self.assertTrue(all(Terminal(kind='dumb').length(line) <= width for line in output.getvalue().splitlines()))


class ResizeRenderingTests(unittest.TestCase):
    def test_resized_screen_matches_fresh_render_and_preserves_history(self):
        import pyte

        groups = [{'family': 'E', 'label': 'E-CPU', 'core_count': 1,
                   'active': 44, 'freq_Mhz': 2169,
                   'cores': [{'id': 0, 'active': 44, 'freq_Mhz': 2169}]}]
        for show_cores in [False, True]:
            with self.subTest(show_cores=show_cores):
                processor, gauges, cores, gpu, ane = app._build_processor_widgets(
                    groups, 2, show_cores)
                app._update_processor_widgets(groups, gauges, cores, show_cores)
                gpu.value = 100
                chart = app.HChart(title='GPU Power', color=2)
                for value in [10, 25, 90, 100] * 30:
                    chart.append(value)
                history = list(chart.datapoints)
                ui = app.VSplit(processor, app.VSplit(
                    app.HGauge(title='RAM Usage', val=96),
                    title='Memory', border_color=2),
                    app.HSplit(chart, app.HChart(title='CPU Power'),
                               title='Power', border_color=2))
                with patch.dict(os.environ, {'NO_COLOR': ''}):
                    terminal = Terminal(kind='xterm-256color', force_styling=True)
                self.assertTrue(terminal.does_styling)
                screen = pyte.Screen(120, 40)
                stream = pyte.Stream(screen)
                # Resize the same screen and reuse the same widgets, including history.
                for width, height in [(120, 40), (83, 25), (140, 47),
                                      (20, 5), (100, 28), (99, 28), (120, 40)]:
                    screen.resize(lines=height, columns=width)
                    with patch.object(app.shutil, 'get_terminal_size',
                                      return_value=(width, height)), \
                         patch.object(Terminal, 'width', width), \
                         patch.object(Terminal, 'height', height), \
                         patch.object(app.sys, 'stdout', io.StringIO()) as output:
                        app.display_dashboard(ui, terminal)
                        frame = output.getvalue()
                    stream.feed(frame)
                    fresh = pyte.Screen(width, height)
                    pyte.Stream(fresh).feed(frame)
                    differences = sum(a != b for old, new in zip(screen.display, fresh.display)
                                      for a, b in zip(old, new))
                    self.assertEqual(differences, 0,
                                     f'{width}x{height}: {differences} stale cells after resize')
                    self.assertEqual(list(chart.datapoints), history)
