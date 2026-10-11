import os
import signal
import tempfile
import time
from dataclasses import dataclass
import subprocess
from subprocess import PIPE
import psutil
from .parsers import *
import plistlib
from xml.parsers.expat import ExpatError


def parse_powermetrics(path='/tmp/asitop_powermetrics', timecode="0"):
    try:
        with open(path+timecode, 'rb') as fp:
            fp.seek(0, os.SEEK_END)
            position = fp.tell()
            chunks = []
            separators = 0
            # Two separators retain a complete sample during a partial write.
            while position > 0 and separators < 2:
                size = min(65536, position)
                position -= size
                fp.seek(position)
                chunk = fp.read(size)
                chunks.append(chunk)
                separators += chunk.count(b'\x00')
            data = b''.join(reversed(chunks))
    except Exception:
        return False

    for segment in reversed(data.split(b'\x00')[-2:]):
        try:
            powermetrics_parse = plistlib.loads(segment)
            thermal_pressure = parse_thermal_pressure(powermetrics_parse)
            cpu_metrics_dict = parse_cpu_metrics(powermetrics_parse)
            gpu_metrics_dict = parse_gpu_metrics(powermetrics_parse)
            timestamp = powermetrics_parse["timestamp"]
            return cpu_metrics_dict, gpu_metrics_dict, thermal_pressure, None, timestamp
        except Exception:
            continue
    return False



def clear_console():
    print("\033[2J\033[H", end="", flush=True)


def safe_label(value):
    return "".join(c for c in str(value) if c.isprintable())


def convert_to_GB(value):
    return round(value/1024/1024/1024, 1)


@dataclass
class Collector:
    process: subprocess.Popen
    directory: str
    log_path: str
    cleaned: bool = False

    @property
    def pid(self):
        return self.process.pid

    def poll(self):
        return self.process.poll()


def run_powermetrics_process(timecode=None, nice=10, interval=1000):
    directory = tempfile.mkdtemp(prefix="asitop-")
    log_path = os.path.join(directory, "powermetrics.plist")
    command = [] if os.geteuid() == 0 else ["/usr/bin/sudo"]
    command.extend([
        "/usr/bin/nice", "-n", str(int(nice)), "/usr/bin/powermetrics",
        "--samplers", "cpu_power,gpu_power,thermal", "-f", "plist",
        "-i", str(int(interval)),
    ])
    try:
        descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            process = subprocess.Popen(command, stdout=output, start_new_session=True)
        return Collector(process, directory, log_path)
    except BaseException:
        if os.path.exists(log_path):
            os.unlink(log_path)
        os.rmdir(directory)
        raise


def _signal_collector(session, sig):
    try:
        os.killpg(session.pid, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        try:
            subprocess.run([
                "/usr/bin/sudo", "-n", "/bin/kill", "-" + str(int(sig)),
                "--", "-" + str(session.pid),
            ], check=True, timeout=3)
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError("powermetrics cleanup failed; session files retained") from error


def _group_exists(pid):
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def terminate_powermetrics_process(session):
    if session is None or session.cleaned:
        return
    # A exited leader can still have live children in its owned group.
    _signal_collector(session, signal.SIGTERM)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if sig == signal.SIGKILL:
            _signal_collector(session, sig)
        deadline = time.monotonic() + 3
        while True:
            session.poll()
            if not _group_exists(session.pid):
                session.process.wait(timeout=3)
                os.unlink(session.log_path)
                os.rmdir(session.directory)
                session.cleaned = True
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    raise RuntimeError("powermetrics cleanup failed; session files retained")


def get_gpu_memory_bytes():
    try:
        raw = subprocess.check_output(
            ['/usr/sbin/ioreg', '-a', '-r', '-c', 'IOAccelerator', '-d', '1'],
            timeout=1, stderr=subprocess.DEVNULL,
        )
        nodes = plistlib.loads(raw)
    except (OSError, subprocess.SubprocessError, ValueError,
            plistlib.InvalidFileException, ExpatError):
        return None
    if not isinstance(nodes, list):
        return None
    for node in nodes:
        if not isinstance(node, dict):
            continue
        stats = node.get('PerformanceStatistics', {})
        if not isinstance(stats, dict):
            continue
        allocated = stats.get('Alloc system memory')
        if type(allocated) is int and allocated >= 0:
            return allocated
    return None


def get_ram_metrics_dict():
    ram_metrics = psutil.virtual_memory()
    swap_metrics = psutil.swap_memory()
    total_GB = convert_to_GB(ram_metrics.total)
    free_GB = convert_to_GB(ram_metrics.available)
    used_GB = convert_to_GB(ram_metrics.total-ram_metrics.available)
    swap_total_GB = convert_to_GB(swap_metrics.total)
    swap_used_GB = convert_to_GB(swap_metrics.used)
    swap_free_GB = convert_to_GB(swap_metrics.total-swap_metrics.used)
    if swap_total_GB > 0:
        swap_free_percent = int(100-(swap_free_GB/swap_total_GB*100))
    else:
        swap_free_percent = None
    ram_metrics_dict = {
        "total_GB": round(total_GB, 1),
        "total_bytes": ram_metrics.total,
        "free_GB": round(free_GB, 1),
        "used_GB": round(used_GB, 1),
        "free_percent": int(100-(ram_metrics.available/ram_metrics.total*100)),
        "swap_total_GB": swap_total_GB,
        "swap_used_GB": swap_used_GB,
        "swap_free_GB": swap_free_GB,
        "swap_free_percent": swap_free_percent,
    }
    return ram_metrics_dict


def get_cpu_info():
    cpu_info = subprocess.check_output(['/usr/sbin/sysctl', '-a'], text=True)
    cpu_info_lines = cpu_info.split("\n")
    data_fields = ["machdep.cpu.brand_string", "machdep.cpu.core_count"]
    cpu_info_dict = {}
    for l in cpu_info_lines:
        for h in data_fields:
            if h in l:
                value = l.split(":")[1].strip()
                cpu_info_dict[h] = value
    return cpu_info_dict


def get_core_counts():
    cores_info = subprocess.check_output(['/usr/sbin/sysctl', '-a'], text=True)
    cores_info_lines = cores_info.split("\n")
    data_fields = ["hw.perflevel0.logicalcpu", "hw.perflevel1.logicalcpu"]
    cores_info_dict = {}
    for l in cores_info_lines:
        for h in data_fields:
            if h in l:
                value = int(l.split(":")[1].strip())
                cores_info_dict[h] = value
    return cores_info_dict


def get_gpu_cores():
    try:
        cores = subprocess.check_output(['/usr/sbin/system_profiler', '-detailLevel', 'basic', 'SPDisplaysDataType'], text=True)
        cores = int(next(line for line in cores.splitlines() if "Total Number of Cores" in line).split(":")[-1])
    except:
        cores = "?"
    return cores


def get_soc_info():
    cpu_info_dict = get_cpu_info()
    core_counts_dict = get_core_counts()
    try:
        e_core_count = core_counts_dict["hw.perflevel1.logicalcpu"]
        p_core_count = core_counts_dict["hw.perflevel0.logicalcpu"]
    except:
        e_core_count = "?"
        p_core_count = "?"
    soc_info = {
        "name": cpu_info_dict["machdep.cpu.brand_string"],
        "core_count": int(cpu_info_dict["machdep.cpu.core_count"]),
        "cpu_max_power": None,
        "gpu_max_power": None,
        "cpu_max_bw": None,
        "gpu_max_bw": None,
        "e_core_count": e_core_count,
        "p_core_count": p_core_count,
        "gpu_core_count": get_gpu_cores()
    }
    # TDP (power)
    if soc_info["name"] == "Apple M1 Max":
        soc_info["cpu_max_power"] = 30
        soc_info["gpu_max_power"] = 60
    elif soc_info["name"] == "Apple M1 Pro":
        soc_info["cpu_max_power"] = 30
        soc_info["gpu_max_power"] = 30
    elif soc_info["name"] == "Apple M1":
        soc_info["cpu_max_power"] = 20
        soc_info["gpu_max_power"] = 20
    elif soc_info["name"] == "Apple M1 Ultra":
        soc_info["cpu_max_power"] = 60
        soc_info["gpu_max_power"] = 120
    elif soc_info["name"] == "Apple M2":
        soc_info["cpu_max_power"] = 25
        soc_info["gpu_max_power"] = 15
    else:
        soc_info["cpu_max_power"] = 20
        soc_info["gpu_max_power"] = 20
    # bandwidth
    if soc_info["name"] == "Apple M1 Max":
        soc_info["cpu_max_bw"] = 250
        soc_info["gpu_max_bw"] = 400
    elif soc_info["name"] == "Apple M1 Pro":
        soc_info["cpu_max_bw"] = 200
        soc_info["gpu_max_bw"] = 200
    elif soc_info["name"] == "Apple M1":
        soc_info["cpu_max_bw"] = 70
        soc_info["gpu_max_bw"] = 70
    elif soc_info["name"] == "Apple M1 Ultra":
        soc_info["cpu_max_bw"] = 500
        soc_info["gpu_max_bw"] = 800
    elif soc_info["name"] == "Apple M2":
        soc_info["cpu_max_bw"] = 100
        soc_info["gpu_max_bw"] = 100
    else:
        soc_info["cpu_max_bw"] = 70
        soc_info["gpu_max_bw"] = 70
    return soc_info
