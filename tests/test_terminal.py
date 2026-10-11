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
                memory, ram, allocation = app._build_memory_widgets(2)
                ram.value = 96
                app._update_gpu_memory(allocation, 3 * 2**30, 8 * 2**30)
                ui = app.VSplit(processor, memory,
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


class MemoryAlignmentRenderingTests(unittest.TestCase):
    def test_memory_numbers_align_on_rendered_terminal(self):
        import pyte

        for width in [83, 120, 200]:
            for swap in [0, 8.0]:
                with self.subTest(width=width, swap=swap):
                    panel, ram, gpu = app._build_memory_widgets(2)
                    app._update_ram_memory(ram, {
                        'used_GB': 34.7, 'total_GB': 36.0, 'free_percent': 96,
                        'swap_total_GB': swap, 'swap_used_GB': 7.0})
                    app._update_gpu_memory(gpu, 3 * 2**30, 36 * 2**30)
                    with patch.dict(os.environ, {'NO_COLOR': ''}):
                        terminal = Terminal(kind='xterm-256color', force_styling=True)
                    with patch.object(app.shutil, 'get_terminal_size', return_value=(width, 20)), \
                         patch.object(Terminal, 'width', width), \
                         patch.object(Terminal, 'height', 20), \
                         patch.object(app.sys, 'stdout', io.StringIO()) as output:
                        app.display_dashboard(panel, terminal)
                    screen = pyte.Screen(width, 20)
                    pyte.Stream(screen).feed(output.getvalue())
                    ram_line = next(line for line in screen.display if 'RAM Usage:' in line)
                    gpu_line = next(line for line in screen.display if 'GPU allocated:' in line)
                    self.assertEqual(ram_line.index('/'), gpu_line.index('/'))
                    self.assertEqual(ram_line.index('GiB'), gpu_line.index('GiB'))
