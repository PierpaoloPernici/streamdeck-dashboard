#!/usr/bin/env python3
"""Stream Deck 8x4 dashboard (final layout).

  Row 0  CPU    UTIL | TEMP | MAX  | CLK | PWR | LOAD | CCD1 | CORE
  Row 1  GPU    UTIL | TEMP | MAX  | CLK | PWR | FAN  | VRAM | MEM
  Row 2  SYSTEM TIME | NV1  | NV2  | NV3 | DSK | SWAP | RAM  | MEM
  Row 3  I/O+PSU DOWN| UP   | READ |WRITE| PSU | FAN  | VRM  | 12V

Columns 0-4 of rows 0-1 carry the same metric for CPU and GPU, so the two
blocks read as one table; PSU sits under the two PWR keys (all watts) and the
PSU FAN under the GPU FAN. Row 3 is I/O on the left, power supply on the right.

Key layout (96x96 px):

  +-----------------+
  |#################|  3 px group accent strip (see ACCENTS)
  | LABEL      UNIT |  label top-left, optional unit top-right (right of x=90)
  |      VALUE      |  large value, optically centred
  |      bottom     |  exactly one of: bar | sub-line | sparkline
  +-----------------+

Graphic rules:
  - 4-6 char label on top, large centered value, unit inside the value (45° 66W 3.7G)
  - a unit in the top-right corner only for values that carry no unit of their
    own (MB/s rates); it is dropped when it would collide with the label
  - bar only on UTIL/VRAM/RAM/SWAP/FAN %  (bottom zone, priority over the rest)
  - a sub-line carries the total (MEM, DSK), the 5 min load (LOAD) or the
    uptime under the clock (TIME)
  - a sparkline carries the last ~1 min of the keys with a free bottom zone
  - temperatures: green normal -> yellow -> red
  - a key at its crit threshold also gets a dark red background tint
  - LOAD shows the 1 min average large with the 5 min one as a small sub-line,
    colored by load / core count; 12V is colored by its deviation from 12.00 V

Usage:
  deck_stats.py            run the monitor
  deck_stats.py --preview  render the whole deck preview to /tmp/deck_preview

Everything machine-specific is either auto-detected (sensors, fonts, RAPL) or
overridable through the environment:

  DECK_UPDATE_SECONDS  refresh interval, default 1.5
  DECK_BRIGHTNESS      deck brightness 0-100, default 70
  DECK_PEAK_WINDOW     horizon of the MAX keys in seconds, default 3600
  DECK_GPU_POWER_MAX   GPU board power limit in W, default 550
  DECK_PSU_POWER_MAX   PSU rated output in W, default 650
"""

import functools
import math
import os
import signal
import subprocess
import sys
import time
from collections import defaultdict, deque, namedtuple
from datetime import datetime
from pathlib import Path

import psutil
from PIL import Image, ImageDraw, ImageFont

from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.ImageHelpers import PILHelper

# ---------------- Config -------------


def _env_num(name, default, cast=float):
    """Numeric override from the environment, silent fallback to default."""
    try:
        return cast(os.environ[name])
    except (KeyError, ValueError):
        return default


UPDATE_SECONDS = _env_num("DECK_UPDATE_SECONDS", 1.5)
BRIGHTNESS = _env_num("DECK_BRIGHTNESS", 70, int)
PEAK_WINDOW = _env_num("DECK_PEAK_WINDOW", 3600.0)   # rolling peak horizon (s)

KEY_W, KEY_H = 96, 96
COLS = 8

# Horizontal budget for the big value: 3 px of margin per side. The value is
# centred, the label sits above it, so nothing else competes for this width.
VALUE_MAX_W = KEY_W - 6

# Layout zones inside a key (see the module docstring).
LABEL_XY = (6, 6)           # top-left, under the row accent strip
LABEL_SIZE = 14
UNIT_XY = (90, 14)          # top-right, vertically centred on the label
UNIT_SIZE = 11
SUB_CY = 78                 # sub-line centre, bottom zone
SUB_SIZE = 13               # sub-line font, shrunk per key if it would not fit
SUB_MAX_W = 84              # sub-line horizontal budget
BAR_BOX = (6, 78, 90, 90)   # bar, bottom zone
SPARK_BOX = (6, 70, 90, 90)  # sparkline, bottom zone
SPARK_MIN_SPAN = 0.01        # min window span, as a fraction of the value

# DejaVu Sans (condensed where available) is looked up in the usual per-distro
# locations (see _FONT_DIRS).
_FONT_DIRS = (
    "/usr/share/fonts/TTF",                     # Arch
    "/usr/share/fonts/truetype/dejavu",         # Debian / Ubuntu
    "/usr/share/fonts/dejavu",                  # Fedora
    "/usr/share/fonts/dejavu-sans-fonts",       # openSUSE
    "/usr/local/share/fonts",
    "/usr/share/fonts",
    "/Library/Fonts",                           # macOS
)


def _find_font(filename):
    for d in _FONT_DIRS:
        p = Path(d) / filename
        if p.is_file():
            return str(p)
    return None


# Condensed first: it fits the same string in a larger point size on a 96 px
# key ("28.3G", "412W"), which is what makes the values readable at arm's length.
# Per-distro locations live in _FONT_DIRS.
FONT_BOLD_FILE = (_find_font("DejaVuSansCondensed-Bold.ttf")
                  or _find_font("DejaVuSans-Bold.ttf"))


@functools.lru_cache(maxsize=64)
def font(size: int) -> ImageFont.ImageFont:
    """Cached font handle; falls back to Pillow's default when DejaVu is absent."""
    if FONT_BOLD_FILE:
        try:
            return ImageFont.truetype(FONT_BOLD_FILE, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)     # Pillow >= 10.1
    except TypeError:                                # very old Pillow
        return ImageFont.load_default()


COL_BG = (14, 14, 18)
COL_BG_CRIT = (60, 14, 14)      # background tint once a key is in CRIT state
COL_TRACK = (42, 42, 53)
COL_TITLE = (154, 165, 181)
COL_VALUE = (240, 240, 245)
COL_OFF = (96, 96, 108)
COL_OK = (61, 220, 132)
COL_WARN = (255, 179, 64)
COL_CRIT = (255, 82, 82)

# One accent color per group, drawn as a 3 px strip on the top edge of every
# key so the groups stay recognisable at a glance. Deliberately outside the
# green/amber/red family, which is reserved for status.
CPU_ACCENT = (72, 132, 214)        # blue
GPU_ACCENT = (150, 108, 220)       # violet
SYSTEM_ACCENT = (56, 170, 190)     # cyan
GRAY_ACCENT = (128, 128, 138)      # neutral: the clock and the I/O keys
POWER_ACCENT = (240, 120, 190)     # pink: the power-supply half of row 3


# Row 3 holds two groups, so the accent is a per-key table, not a per-row one.
def _accent_map():
    accents = {}
    for col in range(COLS):
        accents[(0, col)] = CPU_ACCENT
        accents[(1, col)] = GPU_ACCENT
        accents[(2, col)] = SYSTEM_ACCENT
        accents[(3, col)] = POWER_ACCENT if col >= 4 else GRAY_ACCENT
    accents[(2, 0)] = GRAY_ACCENT      # TIME is a clock, not a system metric
    return accents


ACCENTS = _accent_map()

# Power thresholds are derived from the machine's own limits (see env vars).
GPU_POWER_MAX = _env_num("DECK_GPU_POWER_MAX", 550.0)
PSU_POWER_MAX = _env_num("DECK_PSU_POWER_MAX", 650.0)
GPU_W_WARN, GPU_W_CRIT = GPU_POWER_MAX * 0.65, GPU_POWER_MAX * 0.90
PSU_W_WARN, PSU_W_CRIT = PSU_POWER_MAX * 0.70, PSU_POWER_MAX * 0.92

# LOAD has no absolute thresholds: one process queueing is "full" on a 2-core
# box and nothing on a 32-thread one, so it is normalised by the core count.
CPU_COUNT = psutil.cpu_count() or 1
LOAD_WARN_RATIO, LOAD_CRIT_RATIO = 0.70, 1.00

# 12 V rail: deviation from nominal, in percent (ATX tolerance is +-5 %).
RAIL12_NOMINAL = 12.0
RAIL12_WARN_PCT, RAIL12_CRIT_PCT = 5.0, 8.0

SECTOR = 512        # bytes per sector in /proc/diskstats


# ---------------- Sensors ----------------

def _hwmon_dirs():
    """(chip name, path) for every readable hwmon device."""
    out = []
    for p in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        try:
            out.append(((p / "name").read_text().strip(), p))
        except OSError:          # device vanished or unreadable: skip it
            continue
    return out


def find_hwmon(*names: str):
    """First hwmon matching any of the given chip names, in priority order."""
    devices = _hwmon_dirs()
    for name in names:
        for chip, path in devices:
            if chip == name:
                return path
    return None


def find_hwmons(name: str) -> list:
    """Every hwmon matching the chip name (e.g. all NVMe drives)."""
    return [p for chip, p in _hwmon_dirs() if chip == name]


def hwmon_name(hwmon_dir):
    if hwmon_dir is None:
        return None
    try:
        return (hwmon_dir / "name").read_text().strip()
    except OSError:
        return None


def hwmon_value(hwmon_dir, attr: str):
    """Raw integer value, or None when the sensor/attribute is missing."""
    if hwmon_dir is None:
        return None
    try:
        return int((hwmon_dir / attr).read_text())
    except (OSError, ValueError):
        return None


def hwmon_scaled(hwmon_dir, attr: str, divisor: float):
    """Value in engineering units (millidegrees -> degC, microwatt -> W).

    Returns None when the sensor is unavailable, so the key renders '--'
    instead of a misleading 0.
    """
    value = hwmon_value(hwmon_dir, attr)
    return None if value is None else value / divisor


def find_rapl():
    """(energy_uj path, wrap-around range) of the first CPU package domain."""
    for p in sorted(Path("/sys/class/powercap").glob("intel-rapl:*")):
        try:
            if not (p / "name").read_text().strip().startswith("package"):
                continue
            energy = p / "energy_uj"
            if not energy.exists():
                continue
            rng = p / "max_energy_range_uj"
            return energy, (int(rng.read_text()) if rng.exists() else None)
        except (OSError, ValueError):
            continue
    return None, None


def hwmon_controller(hwmon_dir):
    """Device behind a hwmon entry (e.g. 'nvme1'), when the kernel exposes it."""
    if hwmon_dir is None:
        return None
    try:
        return (hwmon_dir / "device").resolve().name
    except OSError:
        return None


def nvme_hwmons() -> list:
    """NVMe hwmon devices ordered by controller number (nvme0, nvme1, ...).

    hwmonN numbering is assigned at probe time and can change between boots, so
    sorting by it would silently swap which drive sits behind NV1/NV2/NV3.
    """
    found = []
    for hwmon in find_hwmons("nvme"):
        controller = hwmon_controller(hwmon) or ""
        index = controller[4:] if controller.startswith("nvme") else ""
        known = index.isdigit()
        found.append((not known, int(index) if known else 0, str(hwmon), hwmon))
    found.sort(key=lambda item: item[:3])
    return [hwmon for *_, hwmon in found]


# CPU temperature chip: AMD exposes k10temp, Intel coretemp.
CPUTEMP = find_hwmon("k10temp", "coretemp")
NVMES = nvme_hwmons()
CORSAIR = find_hwmon("corsairpsu", "corsair-psu")
RAPL, RAPL_RANGE = find_rapl()


def _core_max():
    """Load of the busiest logical CPU, in percent, or None.

    psutil keeps a separate baseline for percpu=True, so this is one call per
    tick (see run(), which calibrates both). One saturated thread is normal, so
    the key is amber from 90 % and deliberately never red.
    """
    try:
        per_cpu = psutil.cpu_percent(percpu=True)
    except Exception:
        return None
    return max(per_cpu) if per_cpu else None


def _f(x):
    """float() for one nvidia-smi field, None when the field is unavailable.

    Even with csv,noheader,nounits nvidia-smi prints fields it cannot read in
    brackets: [N/A], [Not Supported], [Unknown Platform]... Anything that does
    not parse is treated as missing, so only its own key goes blank instead of
    taking the whole GPU block down with it.
    """
    try:
        return float(x)
    except ValueError:
        return None


def gpu_read():
    """GPU data from nvidia-smi (single call ~50ms).

    Only the individual fields are forgiving: a read that genuinely failed
    (timeout, binary missing, short row) still raises into the except below
    and returns None, which is the right signal to blank every GPU key.
    """
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=temperature.gpu,utilization.gpu,fan.speed,"
             "power.draw,memory.used,memory.total,clocks.gr",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        ).stdout
        t, u, f, p, mu, mt, clk = (x.strip() for x in out.split(","))
        clk_mhz = _f(clk)
        return {
            "temp": _f(t), "util": _f(u), "fan": _f(f),
            "power": _f(p), "mem_used": _f(mu), "mem_total": _f(mt),
            "clk": None if clk_mhz is None else clk_mhz / 1000.0,  # MHz -> GHz
        }
    except Exception as e:
        print(f"[gpu] read error: {e}", file=sys.stderr)
        return None


def _loadavg():
    """1 and 5 minute load averages, best effort."""
    try:
        with open("/proc/loadavg") as f:
            return [float(x) for x in f.read().split()[:2]]
    except (OSError, ValueError):
        return [None, None]


def _net_io():
    """Total rx/tx bytes (excluding lo) from /proc/net/dev."""
    rx = tx = 0
    try:
        with open("/proc/net/dev") as f:
            for line in f:
                if ":" not in line:
                    continue
                iface, data = line.split(":")
                if iface.strip() in ("lo",):
                    continue
                fields = data.split()
                rx += int(fields[0])
                tx += int(fields[8])
    except (OSError, ValueError, IndexError):
        pass
    return rx, tx


def _disk_io():
    """Total bytes read/written, whole block devices only.

    Partitions are skipped on purpose: /proc/diskstats reports them next to
    their parent disk with overlapping counters, so summing both would double
    the rate. A real disk has a /sys/block/<dev> entry, a partition does not.
    """
    read = write = 0
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 10:
                    continue
                dev = parts[2]
                if not dev.startswith(("sd", "nvme", "vd", "hd", "mmcblk")):
                    continue
                if not Path("/sys/block", dev).exists():
                    continue                     # partition, already counted
                read += int(parts[5]) * SECTOR
                write += int(parts[9]) * SECTOR
    except (OSError, ValueError, IndexError):
        pass
    return read, write


# All per-core cpufreq counters, globbed once: the set does not change while
# the machine runs, and globbing every tick would cost more than the reads.
CPU_FREQ_FILES = sorted(
    Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_cur_freq"))


def _disk_usage(path: str = "/"):
    """(bytes free for the user, filesystem size) of a mount point.

    f_bavail, not f_bfree: the reserve root keeps for itself is not space a
    dashboard should advertise as available.
    """
    try:
        st = os.statvfs(path)
    except OSError:
        return None, None
    return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize


class App:
    """Keeps state between samples (peaks, rates, RAPL)."""

    def __init__(self):
        self.st = {
            "peaks": {},
            "rapl": None, "rapl_t": None,
        }
        # (row, col) -> recent numeric values, for the sparklines
        self.history = defaultdict(lambda: deque(maxlen=HISTORY_LEN))
        self.psu_w_last = None
        self.gpu_power_last = None

    # --- data sources ---

    def sample(self):
        t = time.monotonic()
        s = {
            "t": t,
            "cpu_pct": psutil.cpu_percent(None),
            "cpu_core_max": _core_max(),
            "cpu_temp": hwmon_scaled(CPUTEMP, "temp1_input", 1000),   # Tctl
            "ccd1_temp": hwmon_scaled(CPUTEMP, "temp3_input", 1000),  # Tccd1
            "cpu_clk": self._cpu_clk(),
            "gpu": gpu_read(),
            # one slot per drive, None when that drive is not reporting: keeps
            # every temperature aligned with its controller instead of shifting
            # the whole list when a sensor goes away
            "nvme_temps": [hwmon_scaled(n, "temp1_input", 1000)
                           for n in NVMES],
            "psu_w": hwmon_scaled(CORSAIR, "power1_input", 1e6),
            "psu_vrm": hwmon_scaled(CORSAIR, "temp1_input", 1000),
            "psu_fan_pct": hwmon_value(CORSAIR, "pwm1"),
            "psu_fan_rpm": hwmon_value(CORSAIR, "fan1_input"),
            "rail12": hwmon_scaled(CORSAIR, "in1_input", 1000),
            "ram": psutil.virtual_memory(),
            "swap": psutil.swap_memory(),
            "load": _loadavg(),
        }
        s["net_rx"], s["net_tx"] = _net_io()
        s["disk_r"], s["disk_w"] = _disk_io()
        s["disk_free"], s["disk_total"] = _disk_usage()
        # kept for cpu_pwr()'s PSU - GPU fallback
        self.psu_w_last = s["psu_w"]
        self.gpu_power_last = (s["gpu"] or {}).get("power")
        return s

    @staticmethod
    def _cpu_clk():
        """Highest current core clock, in GHz.

        Reading only cpu0 reported the core that happened to run the sampler and
        missed boost on the others; the max over all cores is what "CLK" means
        on a dashboard. Unreadable counters are skipped, None if none is left.
        """
        best = None
        for path in CPU_FREQ_FILES:
            try:
                khz = int(path.read_text())
            except (OSError, ValueError):
                continue
            if best is None or khz > best:
                best = khz
        return None if best is None else best / 1e6      # kHz -> GHz

    def cpu_pwr(self, s):
        """CPU package power from RAPL.

        Fallback when RAPL is unreadable or not warmed up yet: PSU total minus
        GPU draw, i.e. the rest of the system (the key stays labelled PWR).
        """
        e = None
        if RAPL is not None:
            try:
                e = int(RAPL.read_text())
            except (OSError, ValueError):
                e = None

        t = s["t"]
        prev, prev_t = self.st["rapl"], self.st["rapl_t"]
        self.st["rapl"], self.st["rapl_t"] = e, t

        if e is not None and prev is not None and prev_t is not None:
            de, dt = e - prev, t - prev_t
            if de < 0 and RAPL_RANGE:        # counter wrap-around
                de += RAPL_RANGE
            if dt > 0 and de >= 0:
                return de / dt / 1e6         # uJ -> W
            return None

        psu, gpu = self.psu_w_last, self.gpu_power_last
        if psu is not None and gpu:
            return psu - gpu
        return None

    def peak(self, key, value, t_now):
        """Rolling peak over the last PEAK_WINDOW seconds.

        Samples older than the horizon are dropped, so a single hot spike does
        not pin the MAX key forever.
        """
        window = self.st["peaks"].setdefault(key, [])
        cutoff = t_now - PEAK_WINDOW
        while window and window[0][0] < cutoff:
            window.pop(0)
        if value is not None:
            window.append((t_now, value))
        return max((v for _, v in window), default=None)

    def rate(self, key, value_now, t_now):
        """Rate from a cumulative counter (bytes/s)."""
        prev = self.st.get(key)
        self.st[key] = (value_now, t_now)
        if prev is None:
            return None
        dt = t_now - prev[1]
        return (value_now - prev[0]) / dt if dt > 0 else 0.0


# ---------------- Widgets ----------------
# (row, column, label, kind, getter(App, sample)->value, warn, crit)
# kind: pct=bar+%, temp, peak, w, ghz, gb, v, rpm, loadpair, clock, rate

APP = App()

# Set by the SIGTERM/SIGINT handler; checked by the render loop.
_STOP = False

def _nvme_temp(s, index):
    """Temperature of the index-th NVMe drive, None when it has no reading."""
    temps = s["nvme_temps"]
    return temps[index] if len(temps) > index else None


WIDGETS = [
    # --- Row 0: CPU ---
    (0, 0, "UTIL", "pct",  lambda a, s: s["cpu_pct"], 60, 85),
    (0, 1, "TEMP", "temp", lambda a, s: s["cpu_temp"], 75, 90),
    (0, 2, "MAX",  "peak", lambda a, s: a.peak("cpu", s["cpu_temp"], s["t"]),
     75, 90),
    (0, 3, "CLK",  "ghz",  lambda a, s: s["cpu_clk"], 0, 0),
    (0, 4, "PWR",  "w",    lambda a, s: a.cpu_pwr(s), 0, 0),
    (0, 5, "LOAD", "loadpair", lambda a, s: tuple(s["load"]), 0, 0),
    (0, 6, "CCD1", "temp", lambda a, s: s["ccd1_temp"], 75, 90),
    (0, 7, "CORE", "pct", lambda a, s: s["cpu_core_max"], 90, 101),
    # --- Row 1: GPU ---
    (1, 0, "UTIL", "pct",  lambda a, s: (s["gpu"] or {}).get("util"), 60, 85),
    (1, 1, "TEMP", "temp", lambda a, s: (s["gpu"] or {}).get("temp"), 75, 85),
    (1, 2, "MAX",  "peak", lambda a, s: a.peak(
        "gpu", (s["gpu"] or {}).get("temp"), s["t"]), 75, 85),
    (1, 3, "CLK",  "ghz",  lambda a, s: (s["gpu"] or {}).get("clk"), 0, 0),
    (1, 4, "PWR",  "w",    lambda a, s: (s["gpu"] or {}).get("power"),
     GPU_W_WARN, GPU_W_CRIT),
    (1, 5, "FAN",  "pct",  lambda a, s: (s["gpu"] or {}).get("fan"), 70, 90),
    (1, 6, "VRAM", "pct",  lambda a, s: (
        (s["gpu"]["mem_used"] / s["gpu"]["mem_total"] * 100)
        if (s["gpu"] and s["gpu"].get("mem_used") is not None
            and s["gpu"].get("mem_total")) else None), 75, 90),
    (1, 7, "MEM",  "gb",   lambda a, s: (
        (s["gpu"]["mem_used"] / 1024)
        if s["gpu"] and s["gpu"].get("mem_used") is not None else None), 0, 0),
    # --- Row 2: System ---
    (2, 0, "TIME", "clock", lambda a, s: (
        datetime.now().strftime("%H:%M"),
        time.time() - psutil.boot_time()), 0, 0),
    (2, 1, "NV1",  "temp", lambda a, s: _nvme_temp(s, 0), 70, 80),
    (2, 2, "NV2",  "temp", lambda a, s: _nvme_temp(s, 1), 70, 80),
    (2, 3, "NV3",  "temp", lambda a, s: _nvme_temp(s, 2), 70, 80),
    (2, 4, "DSK",  "gb",   lambda a, s: (
        None if s["disk_free"] is None else s["disk_free"] / 1024 ** 3), 0, 0),
    (2, 5, "SWAP", "pct",  lambda a, s: s["swap"].percent, 50, 80),
    (2, 6, "RAM",  "pct",  lambda a, s: s["ram"].percent, 70, 90),
    (2, 7, "MEM",  "gb",   lambda a, s: s["ram"].used / 1024 ** 3, 0, 0),
    # --- Row 3: I/O (0-3), power supply (4-7) ---
    (3, 0, "DOWN",  "rate", lambda a, s: a.rate(
        "net_rx", s["net_rx"], s["t"]), 0, 0),
    (3, 1, "UP",    "rate", lambda a, s: a.rate(
        "net_tx", s["net_tx"], s["t"]), 0, 0),
    (3, 2, "READ",  "rate", lambda a, s: a.rate(
        "disk_r", s["disk_r"], s["t"]), 0, 0),
    (3, 3, "WRITE", "rate", lambda a, s: a.rate(
        "disk_w", s["disk_w"], s["t"]), 0, 0),
    (3, 4, "PSU",   "w",    lambda a, s: s["psu_w"], PSU_W_WARN, PSU_W_CRIT),
    (3, 5, "FAN",   "pct",  lambda a, s: s["psu_fan_pct"], 70, 90),
    (3, 6, "VRM",   "temp", lambda a, s: s["psu_vrm"], 55, 65),
    (3, 7, "12V",   "v",    lambda a, s: s["rail12"], 0, 0),
]

# Per-key data that is not part of the WIDGETS tuple, keyed by (row, col).

# Keys that get a sparkline in the bottom zone. Keys with a bar or a sub-line
# are excluded by construction: the bottom zone holds exactly one thing.
HISTORY_KEYS = {
    (0, 1),     # CPU TEMP
    (0, 4),     # CPU PWR
    (1, 1),     # GPU TEMP
    (1, 4),     # GPU PWR
    (3, 4),     # PSU
    (3, 0),     # DOWN
    (3, 1),     # UP
    (3, 2),     # READ
    (3, 3),     # WRITE
}
HISTORY_LEN = 40            # samples kept per key (~1 min at 1.5 s)

# Top-right unit, drawn in COL_TITLE at size 11 when it fits next to the label.
UNITS = {
    (3, 0): "MB/s",     # DOWN
    (3, 1): "MB/s",     # UP
    (3, 2): "MB/s",     # READ
    (3, 3): "MB/s",     # WRITE
}

# Bottom-zone sub-line: callable(App, sample) -> str | None. None means "draw
# nothing", which is also the answer for a missing sensor (never a fake total).
SUBS = {
    # GPU MEM: used / total VRAM, both from the same nvidia-smi sample
    (1, 7): lambda a, s: (
        f"/{s['gpu']['mem_total'] / 1024:.0f}G"
        if s["gpu"] and s["gpu"].get("mem_total") else None),
    # RAM MEM: used / total system RAM, from the psutil virtual_memory sample
    (2, 7): lambda a, s: (
        f"/{s['ram'].total / 1024 ** 3:.0f}G"
        if getattr(s["ram"], "total", None) else None),
    # DSK: free / total on the root filesystem, one statvfs per tick
    (2, 4): lambda a, s: (
        f"/{(s.get('disk_total') or 0) / 1024 ** 3:.0f}G"
        if s.get("disk_total") else None),
}


def _check_layout(widgets, per_key_tables):
    """Fail loudly if the deck layout is not exactly the 8x4 grid.

    A duplicated or missing (row, col) silently leaves a key stale or dark, and
    a per-key dict pointing at a cell that no longer exists fails just as
    quietly - both are cheap to catch here and expensive to notice on the deck.
    """
    grid = {(row, col) for row in range(4) for col in range(COLS)}
    cells = [(row, col) for row, col, *_ in widgets]
    dupes = sorted({c for c in cells if cells.count(c) > 1})
    if dupes or set(cells) != grid:
        raise RuntimeError(
            f"WIDGETS must cover the 4x{COLS} grid exactly once: "
            f"duplicates={dupes}, missing={sorted(grid - set(cells))}")
    for name, table in per_key_tables.items():
        stray = sorted(set(table) - grid)
        if stray:
            raise RuntimeError(
                f"{name} points at cells outside the grid: {stray}")


_check_layout(WIDGETS, {"HISTORY_KEYS": HISTORY_KEYS, "UNITS": UNITS,
                        "SUBS": SUBS})


def print_layout():
    """Four lines of eight labels in grid order (used by --preview)."""
    by_cell = {(w[0], w[1]): w[2] for w in WIDGETS}
    for row in range(4):
        labels = (f"{by_cell[(row, c)]:<6s}" for c in range(COLS))
        print("  " + "".join(labels).rstrip())


# ---------------- Formatting ----------------

def fmt_rate(bps):
    if bps is None:
        return "--"
    if bps >= 1e9:
        return f"{bps / 1e9:.1f}G"
    if bps >= 1e6:
        return f"{bps / 1e6:.1f}M"
    if bps >= 1e3:
        return f"{bps / 1e3:.0f}K"
    return f"{bps:.0f}B"


def fmt_uptime(sec):
    """Uptime for the TIME sub-line: 'up 3d 04h', 'up 4h 07m', 'up 12m'."""
    if sec is None:
        return None
    d, r = divmod(int(sec), 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"up {d}d {h:02d}h"
    if h:
        return f"up {h}h {m:02d}m"
    return f"up {m}m"


def key_color(v, warn, crit):
    if v is None:
        return COL_OFF
    if v >= crit:
        return COL_CRIT
    if v >= warn:
        return COL_WARN
    return COL_OK


def ratio_color(ratio, warn, crit):
    """key_color() for a normalised ratio, with fixed non-zero thresholds."""
    if ratio is None:
        return COL_OFF
    return key_color(ratio, warn, crit)


def load_color(value):
    """LOAD color from load / core count (warn >= 70 %, crit >= 100 %)."""
    if value is None:
        return COL_OFF
    return ratio_color(value / CPU_COUNT, LOAD_WARN_RATIO, LOAD_CRIT_RATIO)


def rail_color(value):
    """12 V color from the relative deviation from 12.00 V."""
    if value is None:
        return COL_OFF
    dev_pct = abs(value - RAIL12_NOMINAL) / RAIL12_NOMINAL * 100.0
    return ratio_color(dev_pct, RAIL12_WARN_PCT, RAIL12_CRIT_PCT)


# Kinds whose value is compared against crit/warn, and so can tint the key
# background when the threshold is crossed.
TINTED_KINDS = ("pct", "temp", "peak", "w")


def resolve_sub(row, col, s):
    """Sub-line for one key, or None when it has none / the data is missing."""
    fn = SUBS.get((row, col))
    if fn is None:
        return None
    try:
        return fn(APP, s)
    except Exception:
        return None


def spark_points(values, box=SPARK_BOX):
    """Polyline points for a sparkline, scaled min-max inside box."""
    x0, y0, x1, y1 = box
    lo, hi = min(values), max(values)
    # Minimum span guard: a flat window must render as a flat line in the
    # middle of the zone instead of magnifying sensor noise to full height.
    guard = max(abs(hi), abs(lo), 1.0) * SPARK_MIN_SPAN
    if hi - lo < guard:
        mid = (hi + lo) / 2
        lo, hi = mid - guard / 2, mid + guard / 2
    span = hi - lo
    step = (x1 - x0) / (len(values) - 1)
    return [(x0 + i * step, y1 - (v - lo) / span * (y1 - y0))
            for i, v in enumerate(values)]


def render_key(label, kind, value, warn=0, crit=0, cell=None, unit=None,
               sub=None, history=None) -> Image.Image:
    """Render a 96x96 key following the graphic rules.

    cell/unit/sub/history come from the per-key dicts (ACCENTS, UNITS, SUBS,
    App.history) and are all optional: without them the key falls back to the
    plain label + value layout.
    """
    size_max = 40
    if value is None:
        text, ratio, color = "--", None, COL_OFF
    elif kind == "clock":
        # (HH:MM, uptime seconds): the clock leads, the uptime goes underneath
        clock, up_sec = value
        text = clock
        if sub is None:
            sub = fmt_uptime(up_sec)      # same bottom-zone path as SUBS
        ratio, color = None, COL_VALUE
    elif kind == "loadpair":
        # (1 min, 5 min): the 1 min value leads, the 5 min one goes underneath
        load1, load5 = value
        text = "--" if load1 is None else f"{load1:.2f}"
        if sub is None and load5 is not None:
            sub = f"5m {load5:.2f}"       # same bottom-zone path as SUBS
        ratio, color = None, load_color(load1)
        size_max = 36          # two decimals: a little smaller than "412W"
    else:
        ratio = None
        color = COL_VALUE
        if kind == "pct":
            text = f"{value:.0f}%"
            ratio = max(0.0, min(1.0, value / 100.0))
            color = key_color(value, warn, crit)
        elif kind in ("temp", "peak"):
            text = f"{value:.0f}°"
            color = key_color(value, warn, crit)
        elif kind == "w":
            text = f"{value:.0f}W"
            # crit == 0 means "no thresholds" (key_color would return CRIT)
            color = key_color(value, warn, crit) if crit else COL_VALUE
        elif kind == "ghz":
            text = f"{value:.1f}G"
        elif kind == "gb":
            text = f"{value:.1f}G"
        elif kind == "v":
            text = f"{value:.1f}V"
            color = rail_color(value)
        elif kind == "rpm":
            text = f"{value:.0f}"
        elif kind == "rate":
            text = fmt_rate(value)
        else:
            text = f"{value}"

    # Vertical centre of the value. Keys that also draw something in the
    # bottom zone (bar, sub-line or sparkline) sit a little higher.
    has_bottom = ratio is not None or sub is not None or history is not None
    cy = 44 if has_bottom else 54

    # A key that reached its critical threshold gets a dark red background so
    # the state is visible from across the room, not just from the glyph color.
    bg = COL_BG
    if crit and kind in TINTED_KINDS and value is not None and value >= crit:
        bg = COL_BG_CRIT

    img = Image.new("RGB", (KEY_W, KEY_H), bg)
    d = ImageDraw.Draw(img)
    accent = ACCENTS.get(cell)
    if accent is not None:
        d.rectangle((0, 0, KEY_W, 2), fill=accent)   # 3 px group accent
    f_label = font(LABEL_SIZE)
    d.text(LABEL_XY, label, font=f_label, fill=COL_TITLE)

    # Top-right unit, dropped for this key only when the label would collide.
    if unit:
        f_unit = font(UNIT_SIZE)
        gap = 4
        room = UNIT_XY[0] - LABEL_XY[0] - d.textlength(label, font=f_label)
        if d.textlength(unit, font=f_unit) + gap <= room:
            d.text(UNIT_XY, unit, font=f_unit, fill=COL_TITLE, anchor="rm")

    size = size_max
    while size > 20 and d.textlength(text, font=font(size)) > VALUE_MAX_W:
        size -= 2
    f_val = font(size)
    # anchor="mm" pins the optical centre, so keys where the font had to shrink
    # ("28.3G") line up with the ones that did not ("45K").
    d.text((KEY_W / 2, cy), text, font=f_val, fill=color, anchor="mm")

    if ratio is not None:  # bar only on UTIL/VRAM/RAM/FAN
        d.rounded_rectangle(BAR_BOX, radius=5, fill=COL_TRACK)
        fill_w = int((BAR_BOX[2] - BAR_BOX[0]) * ratio)
        if fill_w >= 10:
            d.rounded_rectangle((BAR_BOX[0], BAR_BOX[1], BAR_BOX[0] + fill_w,
                                 BAR_BOX[3]), radius=5, fill=color)
        elif fill_w > 0:
            d.rectangle((BAR_BOX[0], BAR_BOX[1], BAR_BOX[0] + fill_w,
                         BAR_BOX[3]), fill=color)
    elif sub:              # bottom zone: bar wins, then the sub-line
        size_sub = SUB_SIZE
        while (size_sub > 9
               and d.textlength(sub, font=font(size_sub)) > SUB_MAX_W):
            size_sub -= 1          # e.g. 'up 999d 23h': shrink this key only
        d.text((KEY_W / 2, SUB_CY), sub, font=font(size_sub), fill=COL_TITLE,
               anchor="mm")
    elif history is not None and len(history) > 1:
        d.line(spark_points(history), fill=color, width=2, joint="curve")

    return img


# ---------------- Main app ----------------

def run(deck):
    deck.reset()
    deck.set_brightness(BRIGHTNESS)
    print(f"Connected: {deck.deck_type()} (serial {deck.get_serial_number()})")
    psutil.cpu_percent(None)  # calibrate the first sample
    psutil.cpu_percent(None, percpu=True)  # separate baseline for CORE

    next_tick = time.monotonic()
    try:
        while not _STOP:
            s = APP.sample()
            for row, col, label, kind, getter, warn, crit in WIDGETS:
                try:
                    value = getter(APP, s)
                except Exception as e:
                    print(f"[{label}] {e}", file=sys.stderr)
                    value = None
                history = None
                if (row, col) in HISTORY_KEYS:
                    if isinstance(value, (int, float)):
                        APP.history[(row, col)].append(float(value))
                    history = APP.history[(row, col)]
                img = render_key(label, kind, value, warn, crit,
                                 cell=(row, col),
                                 unit=UNITS.get((row, col)),
                                 sub=resolve_sub(row, col, s),
                                 history=history)
                key = row * COLS + col
                try:
                    deck.set_key_image(
                        key, PILHelper.to_native_key_format(deck, img))
                except Exception as e:
                    print(f"[deck] set_key_image({key}): {e}", file=sys.stderr)
                    raise

            # fixed-rate loop: sleep the remainder instead of drifting
            next_tick += UPDATE_SECONDS
            delay = next_tick - time.monotonic()
            if delay < 0:                    # fell behind: resync, no bursts
                next_tick = time.monotonic()
                delay = 0
            time.sleep(delay)
    finally:
        # leaving the keys lit on stale numbers looks like a live dashboard
        try:
            deck.reset()
            deck.set_brightness(0)
        except Exception:
            pass


def _graceful_sigterm(signum, frame):
    """Request a clean shutdown: the main loop exits and blanks the deck."""
    global _STOP
    _STOP = True


def _shutdown(deck, code: int = 0):
    """Blank the keys and leave without running hidapi's atexit hook.

    The streamdeck library registers hid_exit() with atexit; calling it from
    Python's normal shutdown races the libusb event thread and aborts the
    process (usbi_mutex_destroy assertion). We therefore reset the deck, flush
    the streams and skip interpreter teardown with os._exit().
    """
    if deck is not None:
        try:
            deck.reset()
            deck.set_brightness(0)
        except Exception:
            pass
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(code)


def main():
    manager = DeviceManager()
    print("Sensors:", {
        "cpu_temp": hwmon_name(CPUTEMP),
        "nvme": [hwmon_controller(h) or h.name for h in NVMES],
        "psu": hwmon_name(CORSAIR),
        "rapl": RAPL is not None,
    })
    signal.signal(signal.SIGTERM, _graceful_sigterm)
    signal.signal(signal.SIGINT, _graceful_sigterm)

    deck = None
    while not _STOP:
        try:
            decks = manager.enumerate()
            if not decks:
                print("No Stream Deck found, retrying in 3s...")
                time.sleep(3)
                continue
            deck = decks[0]
            if not deck.is_open():
                deck.open()
            run(deck)                     # returns once _STOP is set
        except Exception as e:
            print(f"Error: {type(e).__name__}: {e}", file=sys.stderr)
            if deck is not None:
                try:
                    deck.close()
                except Exception:
                    pass
            deck = None
            time.sleep(3)

    _shutdown(deck)


def render_mockup(key_images: list) -> Image.Image:
    """Compose the key icons into a device mockup (product-shot style)."""
    scale = 2
    k = KEY_W * scale          # 192px key
    gap, margin, radius = 18, 38, 16
    cols, rows = COLS, len(key_images) // COLS
    body_w = cols * k + (cols - 1) * gap + 2 * margin
    body_h = rows * k + (rows - 1) * gap + 2 * margin
    pad = 50                    # outer padding for the shot
    canvas = Image.new("RGB", (body_w + 2 * pad, body_h + 2 * pad), (8, 8, 12))
    d = ImageDraw.Draw(canvas)

    # device body with vertical gradient
    top, bottom = (32, 32, 40), (16, 16, 22)
    for y in range(body_h):
        t = y / max(1, body_h - 1)
        col = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        d.line([(pad, pad + y), (pad + body_w, pad + y)], fill=col)
    d.rounded_rectangle([pad, pad, pad + body_w, pad + body_h], radius=radius,
                        outline=(70, 70, 82), width=2)

    # keys with rounded corners
    mask = Image.new("L", (k, k), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, k - 1, k - 1], radius=12, fill=255)
    for i, img in enumerate(key_images):
        row, col = divmod(i, COLS)
        x = pad + margin + col * (k + gap)
        y = pad + margin + row * (k + gap)
        canvas.paste(img.resize((k, k), Image.LANCZOS), (x, y), mask)
        d.rounded_rectangle([x, y, x + k, y + k], radius=12, outline=(0, 0, 0), width=2)
    return canvas


def demo_history(value, phase, n=HISTORY_LEN):
    """Fixed synthetic window that ends on `value`, for the preview only.

    Deterministic on purpose: the preview image must be reproducible, and no
    sensor may be read from preview().
    """
    base = float(value)
    amp = abs(base) * 0.15 or 1.0
    pts = [base + amp * math.sin((i + phase) / 4.0) for i in range(n - 1)]
    pts.append(base)                 # last sample = the value on screen
    return pts


def preview():
    """Render the whole 8x4 deck preview (grid + device mockup) to /tmp/deck_preview.

    Values are simulated on purpose: the preview is reproducible on any machine.
    """
    out = Path("/tmp/deck_preview")
    out.mkdir(exist_ok=True)
    print("Layout:")
    print_layout()
    ram = namedtuple("Ram", "percent used total")
    swap = namedtuple("Swap", "percent")
    s = {
        "t": time.monotonic(),
        "cpu_pct": 57, "cpu_core_max": 93, "cpu_temp": 61.2, "ccd1_temp": 49.0,
        "cpu_clk": 5.24,
        "gpu": {"temp": 68.0, "util": 94, "fan": 55, "power": 412.0,
                "mem_used": 28950.0, "mem_total": 32607.0, "clk": 2.86},
        "nvme_temps": [41.0, 35.0, 29.0],
        "psu_w": 510.0, "psu_vrm": 47.0,
        "psu_fan_pct": 34, "psu_fan_rpm": 368, "rail12": 12.06,
        "ram": ram(percent=46, used=12.4 * 1024 ** 3, total=32 * 1024 ** 3),
        "load": [0.76, 0.55], "swap": swap(percent=12),
        "net_rx": 0, "net_tx": 0, "disk_r": 0, "disk_w": 0,
        "disk_free": 842.1 * 1024 ** 3, "disk_total": 1863 * 1024 ** 3,
    }
    APP.st["peaks"].clear()
    APP.st["net_rx"] = (0, s["t"])
    APP.st["net_tx"] = (0, s["t"])
    APP.st["disk_r"] = (0, s["t"])
    APP.st["disk_w"] = (0, s["t"])
    APP.history.clear()
    # prime the CPU power fallback (PSU total - GPU) so the key is not blank
    APP.psu_w_last = s["psu_w"]
    APP.gpu_power_last = s["gpu"]["power"]

    key_imgs = []
    grid = Image.new("RGB", (COLS * KEY_W, 4 * KEY_H), (0, 0, 0))
    for row, col, label, kind, getter, warn, crit in WIDGETS:
        value = getter(APP, s)
        if kind == "rate":
            value = 1.2e6 if "DOWN" in label or "READ" in label else 45e3
        if kind == "clock":
            value = ("08:45", 3 * 86400 + 4 * 3600)
        history = None
        if (row, col) in HISTORY_KEYS and isinstance(value, (int, float)):
            APP.history[(row, col)].extend(
                demo_history(value, row * COLS + col))
            history = APP.history[(row, col)]
        img = render_key(label, kind, value, warn, crit, cell=(row, col),
                         unit=UNITS.get((row, col)),
                         sub=resolve_sub(row, col, s),
                         history=history)
        key_imgs.append(img)
        grid.paste(img, (col * KEY_W, row * KEY_H))
        print(f"  ({row},{col}) {label:6s} {kind:5s} -> {value}")
    grid.save(out / "deck_preview.png")
    render_mockup(key_imgs).save(out / "deck_mockup.png")
    print(f"\nPreview grid:     {out}/deck_preview.png")
    print(f"Device mockup:    {out}/deck_mockup.png")


if __name__ == "__main__":
    if "--preview" in sys.argv:
        preview()
    else:
        main()
