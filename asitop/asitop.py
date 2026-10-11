import argparse
import time
import curses
import os
import re
import shutil
import sys

from blessed import Terminal
from collections import deque

from dashing import HChart as _HChart, HGauge as _HGauge, HSplit as _HSplit
from dashing import VGauge as _VGauge, VSplit as _VSplit

from .utils import (
    clear_console,
    safe_label,
    get_ram_metrics_dict,
    get_soc_info,
    parse_powermetrics,
    run_powermetrics_process,
    terminate_powermetrics_process,
)

def parse_boolean(value):
    values = {"0": False, "1": True, "false": False, "true": True}
    try:
        return values[value.lower()]
    except KeyError:
        raise argparse.ArgumentTypeError("expected 0, 1, false or true")


def build_parser():
    parser = argparse.ArgumentParser(
        description="asitop: Performance monitoring CLI tool for Apple Silicon")
    parser.add_argument("--interval", type=int, default=1,
                        help="Display and sampling interval in seconds (positive)")
    parser.add_argument("--color", type=int, default=2, choices=range(9),
                        help="Display color (0~8)")
    parser.add_argument("--avg", type=int, default=30,
                        help="Averaging window in seconds (at least interval)")
    parser.add_argument("--show_cores", type=parse_boolean, nargs="?", const=True, default=False,
                        help="Show individual cores; accepts 0/1/false/true")
    parser.add_argument("--max_count", type=int, default=0,
                        help="Restart collector after this many display cycles; 0 disables")
    return parser


_text_terminal = Terminal(kind="dumb")


def clip_text(text, width):
    text = safe_label(text)
    width = max(0, width)
    if _text_terminal.length(text) <= width:
        return text
    if width <= 3:
        return _text_terminal.truncate(text, width)
    return _text_terminal.truncate(text, width - 3) + "..."


class _ClippedTitle:
    def _draw_title(self, tbox, fill_all_width):
        title = clip_text(self.title or "", max(0, tbox.w - 2))
        if not title:
            return
        length = _text_terminal.length(title)
        margin = max(0, (tbox.w - length) // 20)
        color = "" if self.border_color is None else tbox.t.color(self.border_color)
        if fill_all_width:
            title = " " * margin + title + " " * max(0, tbox.w - margin - length)
            print(tbox.t.move(tbox.x, tbox.y) + color + title)
        else:
            print(tbox.t.move(tbox.x, tbox.y + margin) + color + " " * margin + title + " " * margin)


class HGauge(_ClippedTitle, _HGauge):
    pass


class VGauge(_ClippedTitle, _VGauge):
    pass


class HChart(_ClippedTitle, _HChart):
    pass


class HSplit(_ClippedTitle, _HSplit):
    pass


class VSplit(_ClippedTitle, _VSplit):
    pass


def resolve_terminal_kind(kind=None, setupterm=None):
    setupterm = setupterm or curses.setupterm
    kind = os.environ.get("TERM", "") if kind is None else kind
    kind = {"xterm-ghossty": "xterm-ghostty"}.get(kind, kind)
    candidates = [kind] if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+_.-]{0,79}", kind) else []
    candidates.extend(["xterm-256color", "xterm", "ansi", "vt100"])
    for candidate in dict.fromkeys(candidates):
        try:
            setupterm(candidate)
            return candidate
        except (curses.error, OSError):
            continue
    return None


def build_terminal():
    if not sys.stdout.isatty():
        return Terminal(kind="dumb"), None
    kind = resolve_terminal_kind()
    return (Terminal(kind=kind, force_styling=True), kind) if kind else (Terminal(kind="dumb"), None)


def display_dashboard(ui, terminal):
    width, height = shutil.get_terminal_size(fallback=(80, 24))
    if not terminal.does_styling or width < 40 or height < 12:
        lines = []
        def collect(tile):
            if tile.title:
                lines.append(clip_text(tile.title, width))
            for child in getattr(tile, "items", []):
                collect(child)
        collect(ui)
        print("\n".join(lines[:max(1, height - 1)]), flush=True)
    else:
        ui._terminal = terminal
        ui.display()


def _horizontal_split(widgets):
    return widgets[0] if len(widgets) == 1 else HSplit(*widgets)


def _build_processor_widgets(cluster_groups, color, show_cores):
    group_gauges = {}
    core_gauges = {}
    processor_gauges = []

    for group in cluster_groups:
        family = group["family"]
        group_gauge = HGauge(
            title=safe_label(group["label"]) + " Usage",
            val=0,
            color=color,
        )
        group_gauges[family] = group_gauge
        processor_gauges.append(group_gauge)

        family_core_gauges = {}
        for core in group["cores"]:
            family_core_gauges[core["id"]] = VGauge(
                val=0,
                color=color,
                border_color=color,
            )
        core_gauges[family] = family_core_gauges

        if show_cores:
            core_widgets = list(family_core_gauges.values())
            for row_start in range(0, len(core_widgets), 8):
                processor_gauges.append(
                    _horizontal_split(core_widgets[row_start:row_start + 8])
                )

    gpu_gauge = HGauge(title="GPU Usage", val=0, color=color)
    ane_gauge = HGauge(title="ANE Usage", val=0, color=color)
    if show_cores:
        processor_gauges.extend([gpu_gauge, ane_gauge])
    else:
        processor_gauges = []
        if group_gauges:
            processor_gauges.append(_horizontal_split(list(group_gauges.values())))
        processor_gauges.append(HSplit(gpu_gauge, ane_gauge))

    processor_split = VSplit(
        *processor_gauges,
        title="Processor Utilization",
        border_color=color,
    )
    return processor_split, group_gauges, core_gauges, gpu_gauge, ane_gauge


def _processor_title(soc_name, cluster_groups, gpu_core_count):
    core_groups = "+".join(
        str(group["core_count"]) + group["family"]
        for group in cluster_groups
    )
    return safe_label(f"{soc_name} (Cores: {core_groups}+{gpu_core_count}GPU)")


def _update_processor_widgets(
    cluster_groups,
    group_gauges,
    core_gauges,
    show_cores,
):
    for group in cluster_groups:
        family = group["family"]
        group_gauge = group_gauges.get(family)
        if group_gauge is None:
            continue
        group_gauge.title = (
            f'{safe_label(group["label"])} Usage: {group["active"]}% '
            f'@ {group["freq_Mhz"]} MHz'
        )
        group_gauge.value = group["active"]

        if show_cores:
            family_core_gauges = core_gauges.get(family, {})
            for core in group["cores"]:
                core_gauge = family_core_gauges.get(core["id"])
                if core_gauge is None:
                    continue
                core_gauge.title = (
                    f'Core {core["id"] + 1}: {core["active"]}%'
                )
                core_gauge.value = core["active"]


def _wait_for_reading(session, wait=0.1):
    deadline = time.monotonic() + 15
    while True:
        ready = parse_powermetrics(path=session.log_path, timecode="")
        if ready:
            return ready
        if session.poll() is not None:
            raise RuntimeError("powermetrics exited before producing a reading")
        if time.monotonic() >= deadline:
            raise RuntimeError("powermetrics did not produce a reading within 15 seconds")
        time.sleep(wait)


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval must be positive")
    if args.avg <= 0 or args.avg < args.interval:
        parser.error("--avg must be positive and at least --interval")
    if args.max_count < 0:
        parser.error("--max_count must be nonnegative")
    sessions = []
    try:
        return _main(args, sessions)
    except KeyboardInterrupt:
        print("Stopping...")
        return 0
    finally:
        try:
            if sessions:
                terminate_powermetrics_process(sessions[-1])
        finally:
            if sys.stdout.isatty():
                print("\033[?25h", end="", flush=True)


def _main(args, sessions):
    print("\nASITOP - Performance monitoring CLI tool for Apple Silicon")
    print("You can update ASITOP by running `pip install asitop --upgrade`")
    print("Get help at `https://github.com/tlkh/asitop`")
    print("P.S. Prefer `asitop`; only powermetrics requires sudo.\n")
    print("\n[1/3] Loading ASITOP\n")
    terminal, terminal_kind = build_terminal()
    if terminal_kind and terminal_kind != os.environ.get("TERM"):
        print(f"Terminal fallback: {safe_label(os.environ.get('TERM', ''))} -> {terminal_kind}")
    if sys.stdout.isatty():
        print("\033[?25l")
    width, height = shutil.get_terminal_size(fallback=(80, 24))
    args.show_cores = args.show_cores and width >= 100 and height >= 28

    soc_info_dict = get_soc_info()

    print("\n[2/3] Starting Powermetrics Process\n")

    powermetrics_process = run_powermetrics_process(
        interval=args.interval * 1000,
    )

    def stop_powermetrics():
        terminate_powermetrics_process(powermetrics_process)

    sessions.append(powermetrics_process)

    print("\n[3/3] Waiting for First Reading...\n")

    ready = _wait_for_reading(powermetrics_process)

    cpu_metrics_dict = ready[0]
    cluster_groups = cpu_metrics_dict["cluster_groups"]
    processor_split, group_gauges, core_gauges, gpu_gauge, ane_gauge = (
        _build_processor_widgets(
            cluster_groups,
            color=args.color,
            show_cores=args.show_cores,
        )
    )

    ram_gauge = HGauge(title="RAM Usage", val=0, color=args.color)
    memory_gauges = VSplit(
        ram_gauge,
        border_color=args.color,
        title="Memory"
    )

    cpu_power_chart = HChart(title="CPU Power", color=args.color)
    gpu_power_chart = HChart(title="GPU Power", color=args.color)
    power_charts = VSplit(
        cpu_power_chart,
        gpu_power_chart,
        title="Power Chart",
        border_color=args.color,
    ) if args.show_cores else HSplit(
        cpu_power_chart,
        gpu_power_chart,
        title="Power Chart",
        border_color=args.color,
    )

    ui = HSplit(
        processor_split,
        VSplit(
            memory_gauges,
            power_charts,
        )
    ) if args.show_cores else VSplit(
        processor_split,
        memory_gauges,
        power_charts,
    )

    usage_gauges = ui.items[0]

    usage_gauges.title = _processor_title(
        soc_info_dict["name"],
        cluster_groups,
        soc_info_dict["gpu_core_count"],
    )
    cpu_max_power = soc_info_dict["cpu_max_power"]
    gpu_max_power = soc_info_dict["gpu_max_power"]
    ane_max_power = 8.0

    cpu_peak_power = 0
    gpu_peak_power = 0
    package_peak_power = 0

    last_timestamp = ready[-1]

    def get_avg(inlist):
        avg = sum(inlist) / len(inlist)
        return avg

    avg_package_power_list = deque([], maxlen=int(args.avg / args.interval))
    avg_cpu_power_list = deque([], maxlen=int(args.avg / args.interval))
    avg_gpu_power_list = deque([], maxlen=int(args.avg / args.interval))

    if sys.stdout.isatty():
        clear_console()

    count=0
    try:
        while True:
            if args.max_count > 0:
                if count >= args.max_count:
                    count = 0
                    stop_powermetrics()
                    powermetrics_process = run_powermetrics_process(
                        interval=args.interval * 1000)
                    sessions.append(powermetrics_process)
                count += 1
            if powermetrics_process.poll() is not None:
                raise RuntimeError("powermetrics exited while monitoring")
            ready = parse_powermetrics(path=powermetrics_process.log_path, timecode="")
            if ready:
                cpu_metrics_dict, gpu_metrics_dict, thermal_pressure, _, timestamp = ready

                if timestamp > last_timestamp:
                    last_timestamp = timestamp

                    if thermal_pressure == "Nominal":
                        thermal_throttle = "no"
                    else:
                        thermal_throttle = "yes"

                    _update_processor_widgets(
                        cpu_metrics_dict["cluster_groups"],
                        group_gauges,
                        core_gauges,
                        show_cores=args.show_cores,
                    )

                    gpu_gauge.title = "".join([
                        "GPU Usage: ",
                        str(gpu_metrics_dict["active"]),
                        "% @ ",
                        str(gpu_metrics_dict["freq_MHz"]),
                        " MHz"
                    ])
                    gpu_gauge.value = gpu_metrics_dict["active"]

                    ane_util_percent = int(
                        min(100, cpu_metrics_dict["ane_W"] / args.interval / ane_max_power * 100))
                    ane_gauge.title = "".join([
                        "ANE Usage: ",
                        str(ane_util_percent),
                        "% @ ",
                        '{0:.1f}'.format(
                            cpu_metrics_dict["ane_W"] / args.interval),
                        " W"
                    ])
                    ane_gauge.value = ane_util_percent

                    ram_metrics_dict = get_ram_metrics_dict()

                    if ram_metrics_dict["swap_total_GB"] < 0.1:
                        ram_gauge.title = "".join([
                            "RAM Usage: ",
                            str(ram_metrics_dict["used_GB"]),
                            "/",
                            str(ram_metrics_dict["total_GB"]),
                            "GB - swap inactive"
                        ])
                    else:
                        ram_gauge.title = "".join([
                            "RAM Usage: ",
                            str(ram_metrics_dict["used_GB"]),
                            "/",
                            str(ram_metrics_dict["total_GB"]),
                            "GB",
                            " - swap:",
                            str(ram_metrics_dict["swap_used_GB"]),
                            "/",
                            str(ram_metrics_dict["swap_total_GB"]),
                            "GB"
                        ])
                    ram_gauge.value = ram_metrics_dict["free_percent"]


                    package_power_W = cpu_metrics_dict["package_W"] / \
                                      args.interval
                    package_peak_power = max(
                        package_peak_power,
                        package_power_W,
                    )
                    avg_package_power_list.append(package_power_W)
                    avg_package_power = get_avg(avg_package_power_list)
                    power_charts.title = "".join([
                        "CPU+GPU+ANE Power: ",
                        '{0:.2f}'.format(package_power_W),
                        "W (avg: ",
                        '{0:.2f}'.format(avg_package_power),
                        "W peak: ",
                        '{0:.2f}'.format(package_peak_power),
                        "W) throttle: ",
                        thermal_throttle,
                    ])

                    cpu_power_percent = int(
                        cpu_metrics_dict["cpu_W"] / args.interval / cpu_max_power * 100)
                    cpu_power_W = cpu_metrics_dict["cpu_W"] / args.interval
                    cpu_peak_power = max(cpu_peak_power, cpu_power_W)
                    avg_cpu_power_list.append(cpu_power_W)
                    avg_cpu_power = get_avg(avg_cpu_power_list)
                    cpu_power_chart.title = "".join([
                        "CPU: ",
                        '{0:.2f}'.format(cpu_power_W),
                        "W (avg: ",
                        '{0:.2f}'.format(avg_cpu_power),
                        "W peak: ",
                        '{0:.2f}'.format(cpu_peak_power),
                        "W)"
                    ])
                    cpu_power_chart.append(cpu_power_percent)

                    gpu_power_percent = int(
                        cpu_metrics_dict["gpu_W"] / args.interval / gpu_max_power * 100)
                    gpu_power_W = cpu_metrics_dict["gpu_W"] / args.interval
                    gpu_peak_power = max(gpu_peak_power, gpu_power_W)
                    avg_gpu_power_list.append(gpu_power_W)
                    avg_gpu_power = get_avg(avg_gpu_power_list)
                    gpu_power_chart.title = "".join([
                        "GPU: ",
                        '{0:.2f}'.format(gpu_power_W),
                        "W (avg: ",
                        '{0:.2f}'.format(avg_gpu_power),
                        "W peak: ",
                        '{0:.2f}'.format(gpu_peak_power),
                        "W)"
                    ])
                    gpu_power_chart.append(gpu_power_percent)

                    display_dashboard(ui, terminal)

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("Stopping...")

    return 0


if __name__ == "__main__":
    main()
