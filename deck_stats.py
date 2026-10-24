#!/usr/bin/env python3
"""Stream Deck 8x4 dashboard (final layout).

  Row 0  CPU    UTIL | TEMP | CCD1 | CLK | PWR | LOAD | MAX | PROC
  Row 1  GPU    UTIL | TEMP | VRAM | MEM | PWR | FAN  | CLK | MAX
  Row 2  SYSTEM RAM  | MEM  | NV1  | NV2 | NV3 | PSU | VRM  | FAN
  Row 3  LINUX  LOAD | DOWN | UP   | READ|WRITE| 12V | UPT | TIME

Graphic rules:
  - 4-6 char label on top, large centered value, unit inside the value (45° 66W 3.7G)
  - bar only on UTIL/VRAM/RAM/FAN %
  - temperatures: white normal -> yellow -> red
  - LOAD carries the 1 min average, with the 5 min one as a small second line

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
import os
import signal
import subprocess
import sys
import time
from collections import namedtuple
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
COL_TRACK = (42, 42, 53)
COL_TITLE = (154, 165, 181)
COL_VALUE = (240, 240, 245)
COL_OFF = (96, 96, 108)
COL_OK = (61, 220, 132)
COL_WARN = (255, 179, 64)
COL_CRIT = (255, 82, 82)

# Power thresholds are derived from the machine's own limits (see env vars).
GPU_POWER_MAX = _env_num("DECK_GPU_POWER_MAX", 550.0)
PSU_POWER_MAX = _env_num("DECK_PSU_POWER_MAX", 650.0)
GPU_W_WARN, GPU_W_CRIT = GPU_POWER_MAX * 0.65, GPU_POWER_MAX * 0.90
PSU_W_WARN, PSU_W_CRIT = PSU_POWER_MAX * 0.70, PSU_POWER_MAX * 0.92

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


def gpu_read():
    """GPU data from nvidia-smi (single call ~50ms)."""
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=temperature.gpu,utilization.gpu,fan.speed,"
             "power.draw,memory.used,memory.total,clocks.gr",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        ).stdout
        t, u, f, p, mu, mt, clk = (x.strip() for x in out.split(","))
        return {
            "temp": float(t), "util": float(u), "fan": float(f),
            "power": float(p), "mem_used": float(mu), "mem_total": float(mt),
            "clk": float(clk) / 1000.0,          # MHz -> GHz
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


class App:
    """Keeps state between samples (peaks, rates, RAPL)."""

    def __init__(self):
        self.st = {
            "peaks": {},
            "rapl": None, "rapl_t": None,
        }
        self.psu_w_last = None
        self.gpu_power_last = None

    # --- data sources ---

    def sample(self):
        t = time.monotonic()
        s = {
            "t": t,
            "cpu_pct": psutil.cpu_percent(None),
            "cpu_temp": hwmon_scaled(CPUTEMP, "temp1_input", 1000),   # Tctl
            "ccd1_temp": hwmon_scaled(CPUTEMP, "temp3_input", 1000),  # CCD1
            "cpu_clk": self._cpu_clk(),
            "gpu": gpu_read(),
            "nvme_temps": [v for v in
                           (hwmon_scaled(n, "temp1_input", 1000) for n in NVMES)
                           if v is not None],
            "psu_w": hwmon_scaled(CORSAIR, "power1_input", 1e6),
            "psu_vrm": hwmon_scaled(CORSAIR, "temp1_input", 1000),
            "psu_fan_pct": hwmon_value(CORSAIR, "pwm1"),
            "psu_fan_rpm": hwmon_value(CORSAIR, "fan1_input"),
            "rail12": hwmon_scaled(CORSAIR, "in1_input", 1000),
            "ram": psutil.virtual_memory(),
            "load": _loadavg(),
            "proc": len(psutil.pids()),
        }
        s["net_rx"], s["net_tx"] = _net_io()
        s["disk_r"], s["disk_w"] = _disk_io()
        # kept for cpu_pwr()'s PSU - GPU fallback
        self.psu_w_last = s["psu_w"]
        self.gpu_power_last = (s["gpu"] or {}).get("power")
        return s

    @staticmethod
    def _cpu_clk():
        try:
            return int(Path(
                "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"
            ).read_text()) / 1e6  # kHz -> GHz
        except (OSError, ValueError):
            return None

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
# kind: pct=bar+%, temp, peak, w, ghz, gb, v, rpm, load, rate, count, time, upt

APP = App()

# Set by the SIGTERM/SIGINT handler; checked by the render loop.
_STOP = False

def _nvme_temp(s, index):
    """Temperature of the index-th NVMe drive, or None when absent."""
    temps = s["nvme_temps"]
    return temps[index] if len(temps) > index else None


WIDGETS = [
    # --- Row 0: CPU ---
    (0, 0, "UTIL", "pct",  lambda a, s: s["cpu_pct"], 60, 85),
    (0, 1, "TEMP", "temp", lambda a, s: s["cpu_temp"], 75, 90),
    (0, 2, "CCD1", "temp", lambda a, s: s["ccd1_temp"], 75, 90),
    (0, 3, "CLK",  "ghz",  lambda a, s: s["cpu_clk"], 0, 0),
    (0, 4, "PWR",  "w",    lambda a, s: a.cpu_pwr(s), 0, 0),
    (0, 5, "LOAD", "load", lambda a, s: s["load"][0], 0, 0),
    (0, 6, "MAX",  "peak", lambda a, s: a.peak("cpu", s["cpu_temp"], s["t"]),
     75, 90),
    (0, 7, "PROC", "count", lambda a, s: s["proc"], 0, 0),
    # --- Row 1: GPU ---
    (1, 0, "UTIL", "pct",  lambda a, s: (s["gpu"] or {}).get("util"), 60, 85),
    (1, 1, "TEMP", "temp", lambda a, s: (s["gpu"] or {}).get("temp"), 75, 85),
    (1, 2, "VRAM", "pct",  lambda a, s: (
        (s["gpu"]["mem_used"] / s["gpu"]["mem_total"] * 100)
        if s["gpu"] and s["gpu"].get("mem_total") else None), 75, 90),
    (1, 3, "MEM",  "gb",   lambda a, s: (
        (s["gpu"]["mem_used"] / 1024) if s["gpu"] else None), 0, 0),
    (1, 4, "PWR",  "w",    lambda a, s: (s["gpu"] or {}).get("power"),
     GPU_W_WARN, GPU_W_CRIT),
    (1, 5, "FAN",  "pct",  lambda a, s: (s["gpu"] or {}).get("fan"), 70, 90),
    (1, 6, "CLK",  "ghz",  lambda a, s: (s["gpu"] or {}).get("clk"), 0, 0),
    (1, 7, "MAX",  "peak", lambda a, s: a.peak(
        "gpu", (s["gpu"] or {}).get("temp"), s["t"]), 75, 85),
    # --- Row 2: System ---
    (2, 0, "RAM",  "pct",  lambda a, s: s["ram"].percent, 70, 90),
    (2, 1, "MEM",  "gb",   lambda a, s: s["ram"].used / 1024 ** 3, 0, 0),
    (2, 2, "NV1",  "temp", lambda a, s: _nvme_temp(s, 0), 70, 80),
    (2, 3, "NV2",  "temp", lambda a, s: _nvme_temp(s, 1), 70, 80),
    (2, 4, "NV3",  "temp", lambda a, s: _nvme_temp(s, 2), 70, 80),
    (2, 5, "PSU",  "w",    lambda a, s: s["psu_w"], PSU_W_WARN, PSU_W_CRIT),
    (2, 6, "VRM",  "temp", lambda a, s: s["psu_vrm"], 55, 65),
    (2, 7, "FAN",  "pct",  lambda a, s: s["psu_fan_pct"], 70, 90),
    # --- Row 3: Linux ---
    (3, 0, "LOAD",  "loadpair", lambda a, s: tuple(s["load"]), 0, 0),
    (3, 1, "DOWN",  "rate", lambda a, s: a.rate(
        "net_rx", s["net_rx"], s["t"]), 0, 0),
    (3, 2, "UP",    "rate", lambda a, s: a.rate(
        "net_tx", s["net_tx"], s["t"]), 0, 0),
    (3, 3, "READ",  "rate", lambda a, s: a.rate(
        "disk_r", s["disk_r"], s["t"]), 0, 0),
    (3, 4, "WRITE", "rate", lambda a, s: a.rate(
        "disk_w", s["disk_w"], s["t"]), 0, 0),
    (3, 5, "12V",   "v",    lambda a, s: s["rail12"], 0, 0),
    (3, 6, "UPT",   "upt",  lambda a, s: time.time() - psutil.boot_time(), 0, 0),
    (3, 7, "TIME",  "time", lambda a, s: datetime.now().strftime("%H:%M"), 0, 0),
]


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
    d, r = divmod(int(sec), 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"{d}d"
    if h:
        return f"{h}h"
    return f"{m}m"


def key_color(v, warn, crit):
    if v is None:
        return COL_OFF
    if v >= crit:
        return COL_CRIT
    if v >= warn:
        return COL_WARN
    return COL_OK


def render_key(label, kind, value, warn=0, crit=0) -> Image.Image:
    """Render a 96x96 key following the graphic rules."""
    sub = None          # small secondary line under the main value
    size_max = 40
    if value is None:
        text, ratio, color = "--", None, COL_OFF
    elif kind == "loadpair":
        # (1 min, 5 min): the 1 min value leads, the 5 min one goes underneath
        load1, load5 = value
        text = "--" if load1 is None else f"{load1:.2f}"
        sub = None if load5 is None else f"5m {load5:.2f}"
        ratio, color = None, COL_VALUE
        size_max = 34
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
        elif kind == "rpm":
            text = f"{value:.0f}"
        elif kind == "load":
            text = f"{value:.2f}"
        elif kind == "rate":
            text = fmt_rate(value)
        elif kind == "count":
            text = f"{value:.0f}"
        elif kind == "time":
            text = value
        elif kind == "upt":
            text = fmt_uptime(value)
        else:
            text = f"{value}"

    # Vertical centre of the value. Keys that also draw something in the
    # bottom zone (bar or sub-line) sit a little higher to leave room for it.
    cy = 44 if (ratio is not None or sub is not None) else 54

    img = Image.new("RGB", (KEY_W, KEY_H), COL_BG)
    d = ImageDraw.Draw(img)
    d.text((6, 4), label, font=font(14), fill=COL_TITLE)

    size = size_max
    while size > 20 and d.textlength(text, font=font(size)) > VALUE_MAX_W:
        size -= 2
    f_val = font(size)
    # anchor="mm" pins the optical centre, so keys where the font had to shrink
    # ("28.3G") line up with the ones that did not ("45K").
    d.text((KEY_W / 2, cy), text, font=f_val, fill=color, anchor="mm")

    if sub:
        f_sub = font(13)
        w = d.textlength(sub, font=f_sub)
        d.text(((KEY_W - w) / 2, 70), sub, font=f_sub, fill=COL_TITLE)

    if ratio is not None:  # bar only on UTIL/VRAM/RAM/FAN
        d.rounded_rectangle((6, 78, 90, 90), radius=5, fill=COL_TRACK)
        fill_w = int(84 * ratio)
        if fill_w >= 10:
            d.rounded_rectangle((6, 78, 6 + fill_w, 90), radius=5, fill=color)
        elif fill_w > 0:
            d.rectangle((6, 78, 6 + fill_w, 90), fill=color)

    return img


# ---------------- Main app ----------------

def run(deck):
    deck.reset()
    deck.set_brightness(BRIGHTNESS)
    print(f"Connected: {deck.deck_type()} (serial {deck.get_serial_number()})")
    psutil.cpu_percent(None)  # calibrate the first sample

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
                img = render_key(label, kind, value, warn, crit)
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


def preview():
    """Render the whole 8x4 deck preview (grid + device mockup) to /tmp/deck_preview.

    Values are simulated on purpose: the preview is reproducible on any machine.
    """
    out = Path("/tmp/deck_preview")
    out.mkdir(exist_ok=True)
    ram = namedtuple("Ram", "percent used")
    s = {
        "t": time.monotonic(),
        "cpu_pct": 57, "cpu_temp": 61.2, "ccd1_temp": 52.0, "cpu_clk": 5.24,
        "gpu": {"temp": 68.0, "util": 94, "fan": 55, "power": 412.0,
                "mem_used": 28950.0, "mem_total": 32607.0, "clk": 2.86},
        "nvme_temps": [41.0, 35.0, 29.0],
        "psu_w": 510.0, "psu_vrm": 47.0,
        "psu_fan_pct": 34, "psu_fan_rpm": 368, "rail12": 12.06,
        "ram": ram(percent=46, used=12.4 * 1024 ** 3),
        "load": [0.76, 0.55], "proc": 301,
        "net_rx": 0, "net_tx": 0, "disk_r": 0, "disk_w": 0,
    }
    APP.st["peaks"].clear()
    APP.st["net_rx"] = (0, s["t"])
    APP.st["net_tx"] = (0, s["t"])
    APP.st["disk_r"] = (0, s["t"])
    APP.st["disk_w"] = (0, s["t"])
    # prime the CPU power fallback (PSU total - GPU) so the key is not blank
    APP.psu_w_last = s["psu_w"]
    APP.gpu_power_last = s["gpu"]["power"]

    key_imgs = []
    grid = Image.new("RGB", (COLS * KEY_W, 4 * KEY_H), (0, 0, 0))
    for row, col, label, kind, getter, warn, crit in WIDGETS:
        value = getter(APP, s)
        if kind == "rate":
            value = 1.2e6 if "DOWN" in label or "READ" in label else 45e3
        if kind == "upt":
            value = 3 * 86400 + 4 * 3600
        if kind == "time":
            value = "08:45"
        img = render_key(label, kind, value, warn, crit)
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
