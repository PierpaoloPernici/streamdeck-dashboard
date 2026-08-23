#!/usr/bin/env python3
"""Stream Deck 8x4 dashboard (final layout).

  Row 0  CPU    UTIL | TEMP | CCD1 | CLK | PWR | LOAD | MAX | PROC
  Row 1  GPU    UTIL | TEMP | VRAM | MEM | PWR | FAN  | CLK | MAX
  Row 2  SYSTEM RAM  | MEM  | NV1  | NV2 | PSU | VRM  | 12V | FAN
  Row 3  LINUX  LOAD1|LOAD5| DOWN | UP  |READ |WRITE | UPT | TIME

Graphic rules:
  - 4-6 char label on top, large centered value, unit inside the value (45° 66W 3.7G)
  - bar only on UTIL/VRAM/RAM/FAN %
  - temperatures: white normal -> yellow -> red

Usage:
  deck_stats.py            run the monitor
  deck_stats.py --preview  render the whole deck preview to /tmp/deck_preview
"""

import sys
import time
from datetime import datetime
from pathlib import Path

import psutil
from PIL import Image, ImageDraw, ImageFont

from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.ImageHelpers import PILHelper

# ---------------- Config -------------

UPDATE_SECONDS = 1.5
BRIGHTNESS = 70
KEY_W, KEY_H = 96, 96
COLS = 8

FONT_BOLD = "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/TTF/DejaVuSans.ttf"

COL_BG = (14, 14, 18)
COL_TRACK = (42, 42, 53)
COL_TITLE = (154, 165, 181)
COL_VALUE = (240, 240, 245)
COL_OFF = (96, 96, 108)
COL_OK = (61, 220, 132)
COL_WARN = (255, 179, 64)
COL_CRIT = (255, 82, 82)

GPU_POWER_MAX = 550.0
PSU_POWER_MAX = 650.0

RAPL = Path("/sys/class/powercap/intel-rapl:0/energy_uj")


# ---------------- Sensors ----------------

def find_hwmon(name: str):
    for p in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        try:
            if (p / "name").read_text().strip() == name:
                return p
        except OSError:
            pass
    return None


def find_hwmons(name: str) -> list:
    return [p for p in sorted(Path("/sys/class/hwmon").glob("hwmon*"))
            if (p / "name").read_text().strip() == name]


def hwmon_value(hwmon_dir, attr: str):
    if hwmon_dir is None:
        return None
    try:
        return int((hwmon_dir / attr).read_text())
    except (OSError, ValueError):
        return None


K10TEMP = find_hwmon("k10temp")
NVMES = find_hwmons("nvme")
CORSAIR = find_hwmon("corsairpsu")


def gpu_read():
    """GPU data from nvidia-smi (single call ~50ms)."""
    import subprocess
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
    """Total bytes read/written from /proc/diskstats."""
    read = write = 0
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 10:
                    continue
                if parts[2].startswith(("sd", "nvme", "vd")):
                    read += int(parts[5]) * 512
                    write += int(parts[9]) * 512
    except (OSError, ValueError, IndexError):
        pass
    return read, write


class App:
    """Keeps state between samples (peaks, rates, RAPL)."""

    def __init__(self):
        self.st = {
            "peaks": {},
            "net": None, "disk": None,
            "rapl": None, "rapl_t": None,
        }
        self.psu_w_last = None
        self.gpu_power_last = None

    # --- data sources ---

    def sample(self):
        s = {
            "cpu_pct": psutil.cpu_percent(None),
            "cpu_temp": (hwmon_value(K10TEMP, "temp1_input") or 0) / 1000.0,
            "ccd1_temp": (hwmon_value(K10TEMP, "temp3_input") or 0) / 1000.0,
            "cpu_clk": self._cpu_clk(),
            "gpu": gpu_read(),
            "nvme_temps": [(hwmon_value(n, "temp1_input") or 0) / 1000.0
                           for n in NVMES],
            "psu_w": (hwmon_value(CORSAIR, "power1_input") or 0) / 1e6,
            "psu_vrm": (hwmon_value(CORSAIR, "temp1_input") or 0) / 1000.0,
            "psu_fan_pct": hwmon_value(CORSAIR, "pwm1"),
            "psu_fan_rpm": hwmon_value(CORSAIR, "fan1_input"),
            "rail12": (hwmon_value(CORSAIR, "in1_input") or 0) / 1000.0,
            "ram": psutil.virtual_memory(),
            "load": [float(x) for x in open("/proc/loadavg").read().split()[:2]],
            "proc": len(psutil.pids()),
        }
        s["net_rx"], s["net_tx"] = _net_io()
        s["disk_r"], s["disk_w"] = _disk_io()
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
        """CPU power from RAPL; fallback: PSU total - GPU (rest of system).
        In fallback the label stays PWR but the value is "system without GPU"""
        try:
            e = int(RAPL.read_text())
        except (OSError, ValueError):
            e = None
        t = time.monotonic()
        if e is not None and self.st["rapl"] is not None:
            de = e - self.st["rapl"]
            dt = t - self.st["rapl_t"]
            if de < 0:  # counter wrap-around
                de += int(Path("/sys/class/powercap/intel-rapl:0/"
                               "max_energy_range_uj").read_text())
            w = de / dt / 1e6
            self.st["rapl"] = e
            self.st["rapl_t"] = t
            return w
        self.st["rapl"] = e
        self.st["rapl_t"] = t
        if (self.psu_w_last is not None and self.gpu_power_last is not None
                and self.gpu_power_last > 0):
            return self.psu_w_last - self.gpu_power_last
        return None

    def peak(self, key, value):
        """Rolling peak of a temperature."""
        prev = self.st["peaks"].get(key)
        if value is None:
            return prev
        if prev is None or value > prev:
            self.st["peaks"][key] = value
        return self.st["peaks"][key]

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

WIDGETS = [
    # --- Row 0: CPU ---
    (0, 0, "UTIL", "pct",  lambda a, s: s["cpu_pct"], 60, 85),
    (0, 1, "TEMP", "temp", lambda a, s: s["cpu_temp"], 75, 90),
    (0, 2, "CCD1", "temp", lambda a, s: s["ccd1_temp"], 75, 90),
    (0, 3, "CLK",  "ghz",  lambda a, s: s["cpu_clk"], 0, 0),
    (0, 4, "PWR",  "w",    lambda a, s: a.cpu_pwr(s), 0, 0),
    (0, 5, "LOAD", "load", lambda a, s: s["load"][0], 0, 0),
    (0, 6, "MAX",  "peak", lambda a, s: a.peak("cpu", s["cpu_temp"]), 75, 90),
    (0, 7, "PROC", "count", lambda a, s: s["proc"], 0, 0),
    # --- Row 1: GPU ---
    (1, 0, "UTIL", "pct",  lambda a, s: (s["gpu"] or {}).get("util"), 60, 85),
    (1, 1, "TEMP", "temp", lambda a, s: (s["gpu"] or {}).get("temp"), 75, 85),
    (1, 2, "VRAM", "pct",  lambda a, s: (
        (s["gpu"]["mem_used"] / s["gpu"]["mem_total"] * 100)
        if s["gpu"] and s["gpu"].get("mem_total") else None), 75, 90),
    (1, 3, "MEM",  "gb",   lambda a, s: (
        (s["gpu"]["mem_used"] / 1024) if s["gpu"] else None), 0, 0),
    (1, 4, "PWR",  "w",    lambda a, s: (s["gpu"] or {}).get("power"), 350, 500),
    (1, 5, "FAN",  "pct",  lambda a, s: (s["gpu"] or {}).get("fan"), 70, 90),
    (1, 6, "CLK",  "ghz",  lambda a, s: (s["gpu"] or {}).get("clk"), 0, 0),
    (1, 7, "MAX",  "peak", lambda a, s: a.peak(
        "gpu", (s["gpu"] or {}).get("temp")), 75, 85),
    # --- Row 2: System ---
    (2, 0, "RAM",  "pct",  lambda a, s: s["ram"].percent, 70, 90),
    (2, 1, "MEM",  "gb",   lambda a, s: s["ram"].used / 1024 ** 3, 0, 0),
    (2, 2, "NV1",  "temp", lambda a, s: (
        s["nvme_temps"][0] if len(s["nvme_temps"]) > 0 else None), 70, 80),
    (2, 3, "NV2",  "temp", lambda a, s: (
        s["nvme_temps"][1] if len(s["nvme_temps"]) > 1 else None), 70, 80),
    (2, 4, "PSU",  "w",    lambda a, s: s["psu_w"], 450, 600),
    (2, 5, "VRM",  "temp", lambda a, s: s["psu_vrm"], 55, 65),
    (2, 6, "12V",  "v",    lambda a, s: s["rail12"], 0, 0),
    (2, 7, "FAN",  "pct",  lambda a, s: s["psu_fan_pct"], 70, 90),
    # --- Row 3: Linux ---
    (3, 0, "LOAD1", "load", lambda a, s: s["load"][0], 0, 0),
    (3, 1, "LOAD5", "load", lambda a, s: s["load"][1], 0, 0),
    (3, 2, "DOWN",  "rate", lambda a, s: a.rate(
        "net_rx", s["net_rx"], time.monotonic()), 0, 0),
    (3, 3, "UP",    "rate", lambda a, s: a.rate(
        "net_tx", s["net_tx"], time.monotonic()), 0, 0),
    (3, 4, "READ",  "rate", lambda a, s: a.rate(
        "disk_r", s["disk_r"], time.monotonic()), 0, 0),
    (3, 5, "WRITE", "rate", lambda a, s: a.rate(
        "disk_w", s["disk_w"], time.monotonic()), 0, 0),
    (3, 6, "UPT",   "upt",  lambda a, s: time.monotonic(), 0, 0),
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
    if value is None:
        text, ratio, color = "--", None, COL_OFF
    else:
        ratio = None
        color = COL_VALUE
        if kind == "pct":
            text = f"{value:.0f}%"
            ratio = max(0.0, min(1.0, value / 100.0))
            color = key_color(value, warn, crit)
        elif kind == "temp":
            text = f"{value:.0f}°"
            color = key_color(value, warn, crit)
        elif kind == "peak":
            text = f"{value:.0f}°"
            color = key_color(value, warn, crit)
        elif kind == "w":
            text = f"{value:.0f}W"
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

    img = Image.new("RGB", (KEY_W, KEY_H), COL_BG)
    d = ImageDraw.Draw(img)
    d.text((6, 4), label, font=ImageFont.truetype(FONT_BOLD, 14),
           fill=COL_TITLE)

    f_val = ImageFont.truetype(FONT_BOLD, 40)
    while f_val.size > 20 and d.textlength(text, font=f_val) > 84:
        f_val = ImageFont.truetype(FONT_BOLD, f_val.size - 2)
    w = d.textlength(text, font=f_val)
    d.text(((KEY_W - w) / 2, 24), text, font=f_val, fill=color)

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

    t0 = time.monotonic()
    while True:
        s = APP.sample()
        APP.psu_w_last = s["psu_w"]
        APP.gpu_power_last = (s["gpu"] or {}).get("power")
        for row, col, label, kind, getter, warn, crit in WIDGETS:
            try:
                value = getter(APP, s)
            except Exception as e:
                print(f"[{label}] {e}", file=sys.stderr)
                value = None
            img = render_key(label, kind, value, warn, crit)
            key = row * COLS + col
            try:
                deck.set_key_image(key, PILHelper.to_native_key_format(deck, img))
            except Exception as e:
                print(f"[deck] set_key_image({key}): {e}", file=sys.stderr)
                raise
        time.sleep(UPDATE_SECONDS)


def main():
    manager = DeviceManager()
    print("Sensors:", {
        "k10temp": bool(K10TEMP), "nvme": len(NVMES),
        "corsairpsu": bool(CORSAIR),
        "rapl": RAPL.exists(),
    })

    while True:
        try:
            decks = manager.enumerate()
            if decks:
                deck = decks[0]
                if not deck.is_open():
                    deck.open()
                run(deck)
            else:
                print("No Stream Deck found, retrying in 3s...")
                time.sleep(3)
        except Exception as e:
            print(f"Error: {type(e).__name__}: {e}", file=sys.stderr)
            try:
                deck.close()
            except Exception:
                pass
            time.sleep(3)


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
    """Render the whole 8x4 deck preview (grid + device mockup) to /tmp/deck_preview."""
    out = Path("/tmp/deck_preview")
    out.mkdir(exist_ok=True)
    s = {
        "cpu_pct": 57, "cpu_temp": 61.2, "ccd1_temp": 52.0, "cpu_clk": 5.24,
        "gpu": {"temp": 68.0, "util": 94, "fan": 55, "power": 412.0,
                "mem_used": 28950.0, "mem_total": 32607.0, "clk": 2.86},
        "nvme_temps": [41.0, 35.0],
        "psu_w": 510.0, "psu_vrm": 47.0,
        "psu_fan_pct": 34, "psu_fan_rpm": 368, "rail12": 12.06,
        "ram": psutil.virtual_memory(),
        "load": [0.76, 0.55], "proc": 301,
        "net_rx": 0, "net_tx": 0, "disk_r": 0, "disk_w": 0,
    }
    APP.st["peaks"] = {"cpu": 63.0, "gpu": 69.0}
    APP.st["net_rx"] = (0, time.monotonic())
    APP.st["net_tx"] = (0, time.monotonic())
    APP.st["disk_r"] = (0, time.monotonic())
    APP.st["disk_w"] = (0, time.monotonic())

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
