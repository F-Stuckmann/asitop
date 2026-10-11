"""Dependency boundary fixture loaded by subprocess entry-point tests."""
import json
import os
from pathlib import Path
import time
import sys

sys.stdout.isatty = lambda: os.environ.get('ASITOP_REDIRECT') != '1'
from unittest.mock import Mock

import dashing
from asitop import utils
from asitop.parsers import parse_cpu_metrics, parse_gpu_metrics

trace = Path(os.environ['ASITOP_TRACE'])

def record(event, **values):
    with trace.open('a') as output:
        output.write(json.dumps({'event': event, **values}) + '\n')


def launch(*args, **kwargs):
    record('launch', **kwargs)
    session = Mock(log_path='/private/metrics')
    session.poll.return_value = 1 if os.environ.get('ASITOP_AUTH_FAIL') == '1' else None
    return session


def terminate(session):
    record('cleanup')

counter = 0

def reading(**kwargs):
    global counter
    if os.environ.get('ASITOP_AUTH_FAIL') == '1':
        return False
    counter += 1
    cpu = parse_cpu_metrics({'processor': {'clusters': [
        {'name': 'P0-Cluster', 'active_ratio': .25, 'freq_hz': 2e9,
         'cpus': [{'cpu': 0, 'active_ratio': .25}]},
        {'name': 'S-Cluster', 'active_ratio': .5, 'freq_hz': 4e9,
         'cpus': [{'cpu': 1, 'active_ratio': .5}]},
    ], 'cpu_energy': counter * 1000, 'gpu_energy': 500, 'ane_energy': float(os.environ.get('ASITOP_ANE_ENERGY', '0'))}})
    return cpu, parse_gpu_metrics({'gpu': {}}), 'Nominal', None, counter


def widgets(tile):
    result = [{'title': tile.title, 'value': getattr(tile, 'value', None), 'color': tile.color}]
    for child in getattr(tile, 'items', []):
        result.extend(widgets(child))
    return result


def display(tile):
    record('display', widgets=widgets(tile))

sleeps = 0

def sleep(seconds):
    global sleeps
    sleeps += 1
    if sleeps >= int(os.environ.get('ASITOP_LOOPS', '1')):
        raise KeyboardInterrupt

utils.run_powermetrics_process = launch
utils.terminate_powermetrics_process = terminate
utils.parse_powermetrics = reading
utils.get_soc_info = lambda: {'name': 'Apple M5', 'gpu_core_count': 20,
                              'cpu_max_power': 20, 'gpu_max_power': 20}
utils.get_ram_metrics_dict = lambda: {'used_GB': 1, 'total_GB': 8, 'free_percent': 12, 'swap_total_GB': 0}
utils.clear_console = lambda: None
dashing.VSplit.display = display
dashing.HSplit.display = display
time.sleep = sleep
