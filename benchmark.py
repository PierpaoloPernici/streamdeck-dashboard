#!/usr/bin/env python3
"""Per-tick cost breakdown of deck_stats.py: sensors, nvidia-smi, render, USB.

The loop has a fixed UPDATE_SECONDS budget, so what matters is not only the
total but where it goes: nvidia-smi is a subprocess, rendering is PIL CPU work,
and set_key_image pushes ~28 KB of bitmap per key over a slow HID endpoint.

Usage (from the repo, with the venv):
  .venv/bin/python benchmark.py                    # all phases, needs the deck
  .venv/bin/python benchmark.py --skip-deck        # CPU-side phases only
  .venv/bin/python benchmark.py --iters 60 --soak-seconds 0
  .venv/bin/python benchmark.py --soak-pid auto    # RSS/CPU of the live service

--soak-pid measures an *already running* process from /proc, so it costs it
nothing; "auto" reads MainPID of deck-stats.service.

The deck phase opens the real device, which the service also holds, so stop
the service around it and start it again afterwards:

  systemctl --user stop deck-stats
  .venv/bin/python benchmark.py --skip-soak
  systemctl --user start deck-stats
"""

import argparse
import gc
import os
import statistics
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

import psutil
from PIL import __version__ as PIL_VERSION

import deck_stats as ds
from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.ImageHelpers import PILHelper

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_KB = 1024


# ---------------- measurement helpers ----------------

def timeit(fn, iters, warmup=3):
    """Wall time of fn(), in ms, for iters calls after a warmup."""
    for _ in range(warmup):
        fn()
    gc.collect()
    samples = []
    for _ in range(iters):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1e3)
    return samples


def pct(sorted_vals, frac):
    if not sorted_vals:
        return float("nan")
    return sorted_vals[min(len(sorted_vals) - 1, int(frac * len(sorted_vals)))]


class Table:
    """Aligned ms table: median / p95 / min / max over the iterations."""

    def __init__(self, title):
        print(f"\n=== {title} ===")
        print(f"{'item':<34}{'median':>9}{'p95':>9}{'min':>9}{'max':>9}{'n':>6}")
        print(f"{'':<34}{'ms':>9}")

    def add(self, name, samples_ms, note=""):
        s = sorted(samples_ms)
        med = statistics.median(s)
        line = (f"{name:<34}{med:>9.3f}{pct(s, 0.95):>9.3f}"
                f"{s[0]:>9.3f}{s[-1]:>9.3f}{len(s):>6}")
        print(line + (f"  {note}" if note else ""))
        return med


# ---------------- phase 1/2: sensor acquisition ----------------

def bench_sensors(app, iters):
    tbl = Table(f"sensor acquisition ({iters} samples)")

    # full sample(), nvidia-smi included (what run() actually pays per tick)
    tbl.add("sample() total", timeit(app.sample, iters), "incl. nvidia-smi")

    gpu_times = timeit(ds.gpu_read, iters, warmup=2)
    med_gpu = tbl.add("gpu_read() nvidia-smi", gpu_times, "subprocess")

    # same sample() with the subprocess replaced by a dict return: the pure
    # sysfs/psutil cost, i.e. what is left when nvidia-smi is taken out
    real_gpu = ds.gpu_read
    cached = real_gpu() or {}
    ds.gpu_read = lambda: cached
    try:
        sensors = timeit(app.sample, iters)
        med_sensors = tbl.add("sample() without nvidia-smi", sensors,
                              "sysfs + psutil only")
    finally:
        ds.gpu_read = real_gpu

    tbl.add("gpu share of acquisition", [100.0 * med_gpu / (med_gpu + med_sensors)],
            note="percent (of sample(), not of the tick)")

    # per-source detail: sysfs/proc reads are individually tiny but add up
    nvme, cpu_t, psu = ds.NVMES, ds.CPUTEMP, ds.CORSAIR
    sources = [
        ("psutil.cpu_percent", lambda: psutil.cpu_percent(None)),
        ("psutil.cpu_percent percpu", lambda: ds._core_max()),
        ("cpu_clk (%d files)" % len(ds.CPU_FREQ_FILES), app._cpu_clk),
        ("cpu temp hwmon x2", lambda: (ds.hwmon_scaled(cpu_t, "temp1_input", 1000),
                                       ds.hwmon_scaled(cpu_t, "temp3_input", 1000))),
        ("nvme temps x%d" % len(nvme), lambda: [ds.hwmon_scaled(n, "temp1_input", 1000)
                                                for n in nvme]),
        ("corsair psu hwmon x5", lambda: [ds.hwmon_value(psu, a) for a in
                                          ("power1_input", "temp1_input", "pwm1",
                                           "fan1_input", "in1_input")]),
        ("RAPL energy_uj", lambda: ds.RAPL.read_text() if ds.RAPL else None),
        ("psutil.virtual_memory", psutil.virtual_memory),
        ("psutil.swap_memory", psutil.swap_memory),
        ("os.statvfs(/)", lambda: ds._disk_usage()),
        ("_net_io /proc/net/dev", lambda: ds._net_io()),
        ("_disk_io /proc/diskstats", lambda: ds._disk_io()),
        ("_loadavg /proc/loadavg", ds._loadavg),
    ]
    for name, fn in sources:
        tbl.add("  " + name, timeit(fn, iters))
    return med_gpu, med_sensors


# ---------------- phase 3: rendering the 32 keys ----------------

def bench_render(app, iters):
    tbl = Table(f"rendering the {len(ds.WIDGETS)} key images ({iters} passes)")
    s = app.sample()
    # realistic history: every sparkline key holds a full 40-sample deque
    for key in ds.HISTORY_KEYS:
        app.history[key] = deque(ds.demo_history(50.0, key[0], ds.HISTORY_LEN),
                                 maxlen=ds.HISTORY_LEN)

    def pass_all():
        for row, col, label, kind, getter, warn, crit in ds.WIDGETS:
            spark = (row, col) in ds.HISTORY_KEYS
            img = ds.render_key(label, kind, getter(app, s), warn, crit,
                                cell=(row, col), unit=ds.UNITS.get((row, col)),
                                sub=ds.resolve_sub(row, col, s),
                                history=app.history[(row, col)] if spark else None)
            img.close()

    total = timeit(pass_all, iters)
    med_total = tbl.add("all 32 keys (getters+render)", total, "one tick")

    per_key = []
    for row, col, label, kind, getter, warn, crit in ds.WIDGETS:
        hist = app.history.get((row, col)) if (row, col) in ds.HISTORY_KEYS else None

        def one(row=row, col=col, label=label, kind=kind, getter=getter,
                warn=warn, crit=crit, hist=hist):
            img = ds.render_key(label, kind, getter(app, s), warn, crit,
                                cell=(row, col), unit=ds.UNITS.get((row, col)),
                                sub=ds.resolve_sub(row, col, s), history=hist)
            img.close()
        samples = timeit(one, max(5, iters // 3))
        per_key.append((statistics.median(samples), label, kind,
                        (row, col) in ds.HISTORY_KEYS, samples))
    per_key.sort(reverse=True)
    for med, label, kind, spark, samples in per_key[:5]:
        tbl.add(f"  slowest: {label} [{kind}]", samples,
                "sparkline" if spark else "")

    ref = timeit(lambda: ds.render_key("TEMP", "temp", 61.2, 75, 90, cell=(0, 1)),
                 20)
    img = ds.render_key("TEMP", "temp", 61.2, 75, 90, cell=(0, 1))
    tbl.add("single key image (reference)", ref,
            f"{img.width}x{img.height}, ~{img.width * img.height * 3 // 1024} KB raw")
    img.close()
    return med_total


# ---------------- phase 4: pushing images to the deck ----------------

def bench_deck(iters):
    tbl = Table(f"sending images to the Stream Deck ({iters} ticks)")
    manager = DeviceManager()
    decks = manager.enumerate()
    if not decks:
        print("  no Stream Deck found (service holding it? --skip-deck to ignore)")
        return None
    deck = decks[0]
    deck.open()
    app = ds.App()
    imgs = []
    try:
        print(f"  device: {deck.deck_type()} {deck.key_layout()} "
              f"(serial {deck.get_serial_number()})")

        # build one tick's worth of images once: the USB phase must not include
        # PIL time, otherwise the numbers answer a different question
        t0 = time.perf_counter()
        s = app.sample()
        for row, col, label, kind, getter, warn, crit in ds.WIDGETS:
            imgs.append(ds.render_key(label, kind, getter(app, s), warn, crit,
                                      cell=(row, col),
                                      unit=ds.UNITS.get((row, col)),
                                      sub=ds.resolve_sub(row, col, s)))
        tbl.add("render 32 (prepared once)", [(time.perf_counter() - t0) * 1e3])

        natives = []

        def convert_all():
            return [PILHelper.to_native_key_format(deck, img) for img in imgs]
        natives = convert_all()
        med_convert = tbl.add("PIL -> native key format x32",
                              timeit(convert_all, max(5, iters // 2), warmup=1),
                              f"{len(natives[0])} B/key")

        per_key = timeit(lambda: deck.set_key_image(0, natives[0]), 10, warmup=2)
        tbl.add("set_key_image (1 key)", per_key)

        def push_all():
            for key, data in enumerate(natives):
                deck.set_key_image(key, data)
        tick = timeit(push_all, iters, warmup=1)
        med = tbl.add("set_key_image x32 (one tick)", tick, "full refresh")

        for name, fn in (("deck.reset()", deck.reset),
                         ("set_brightness", lambda: deck.set_brightness(70))):
            tbl.add(name, timeit(fn, 5, warmup=1))
        return med, med_convert
    finally:
        for img in imgs if imgs else []:
            img.close()
        try:
            deck.reset()
        except Exception:
            pass
        deck.close()


# ---------------- phase 5: RSS / CPU of a running process ----------------

def proc_snapshot(pid):
    """(rss_kb, cpu_ticks, threads, fds) of a process, from /proc."""
    st = Path(f"/proc/{pid}/stat").read_text()
    parts = st[st.rindex(")") + 2:].split()          # fields after comm
    rss_kb = int(Path(f"/proc/{pid}/statm").read_text().split()[1]) * \
        os.sysconf("SC_PAGE_SIZE") // 1024
    return (rss_kb, int(parts[11]) + int(parts[12]), int(parts[17]),
            len(os.listdir(f"/proc/{pid}/fd")))


def bench_soak(pid, seconds, interval=2.0):
    print(f"\n=== soak: pid {pid} for {seconds}s (sampling every {interval}s) ===")
    if pid == "auto":
        out = subprocess.run(["systemctl", "--user", "show", "-p", "MainPID",
                              "--value", "deck-stats"],
                             capture_output=True, text=True).stdout.strip()
        pid = int(out) if out.isdigit() and int(out) > 0 else None
        if not pid:
            print("  deck-stats.service is not running")
            return None
    if not Path(f"/proc/{pid}").exists():
        print(f"  pid {pid} is gone")
        return None
    print(f"  cmdline: {' '.join(Path(f'/proc/{pid}/cmdline').read_bytes().decode().split())}")

    rss, cpu_pct, threads, fds = [], [], [], []
    _, prev_ticks, _, _ = proc_snapshot(pid)
    prev_t = time.monotonic()
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        time.sleep(interval)
        try:
            kb, ticks, nth, nfd = proc_snapshot(pid)
        except (OSError, ValueError):
            print("  process exited during the soak")
            break
        now = time.monotonic()
        dt = now - prev_t
        if dt > 0:
            rss.append(kb / 1024.0)
            cpu_pct.append(100.0 * (ticks - prev_ticks) / CLK_TCK / dt)
            threads.append(nth)
            fds.append(nfd)
            prev_ticks, prev_t = ticks, now
            if len(rss) % 15 == 0:                   # ~30s of trend per line
                print(f"  t={now - started:6.0f}s RSS={kb / 1024:7.1f} MB "
                      f"CPU={cpu_pct[-1]:5.2f} % threads={nth} fds={nfd}")
    if not rss:
        print("  not enough samples (increase --soak-seconds)")
        return None
    srt, srs = sorted(cpu_pct), sorted(rss)
    print(f"{'':<12}{'mean':>9}{'median':>9}{'p95':>9}{'min':>9}{'max':>9}")
    print(f"{'RSS MB':<12}{statistics.mean(rss):>9.1f}{statistics.median(rss):>9.1f}"
          f"{pct(srs, 0.95):>9.1f}{min(rss):>9.1f}{max(rss):>9.1f}")
    print(f"{'CPU %':<12}{statistics.mean(cpu_pct):>9.2f}{statistics.median(cpu_pct):>9.2f}"
          f"{pct(srt, 0.95):>9.2f}{min(cpu_pct):>9.2f}{max(cpu_pct):>9.2f}")
    print(f"  RSS growth over the window: {rss[-1] - rss[0]:+.1f} MB "
          f"({rss[0]:.1f} -> {rss[-1]:.1f} MB)")
    print(f"  threads {threads[0]} -> {threads[-1]}, fds {fds[0]} -> {fds[-1]}"
          f"  (a growing fd/thread count leaks)")
    return rss, cpu_pct


# ---------------- main ----------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--iters", type=int, default=40, help="timed iterations")
    ap.add_argument("--soak-seconds", type=float, default=180)
    ap.add_argument("--soak-pid", default="auto",
                    help="'auto' = deck-stats.service MainPID, or a pid, or ''")
    ap.add_argument("--skip-deck", action="store_true",
                    help="do not open the device (service keeps it)")
    ap.add_argument("--skip-soak", action="store_true")
    args = ap.parse_args()

    print(f"host python {sys.version.split()[0]}, Pillow {PIL_VERSION}, "
          f"psutil {psutil.__version__}, {psutil.cpu_count()} CPUs, "
          f"deck {ds.COLS}x4 = {len(ds.WIDGETS)} keys, "
          f"UPDATE_SECONDS={ds.UPDATE_SECONDS}")

    app = ds.App()
    psutil.cpu_percent(None)                      # calibrate, like run() does
    psutil.cpu_percent(None, percpu=True)
    med_gpu, med_sensors = bench_sensors(app, args.iters)
    med_render = bench_render(app, min(args.iters, 25))
    med_deck = None if args.skip_deck else bench_deck(max(5, args.iters // 4))

    print("\n=== per-tick budget ===")
    parts = [("sensors (sysfs+psutil)", med_sensors), ("nvidia-smi", med_gpu),
             ("render 32 keys", med_render)]
    if med_deck:
        parts.append(("PIL -> native format x32", med_deck[1]))
        parts.append(("USB set_key_image x32", med_deck[0]))
    total = sum(v for _, v in parts)
    for name, v in parts:
        print(f"  {name:<24}{v:>9.1f} ms{100 * v / total:>7.1f} %")
    print(f"  {'TOTAL':<24}{total:>9.1f} ms{100:>10.1f} %")
    print(f"  budget {ds.UPDATE_SECONDS * 1000:.0f} ms/tick -> "
          f"{total / (ds.UPDATE_SECONDS * 10):.1f} % used, "
          f"idle {(ds.UPDATE_SECONDS * 1000 - total) / 1000:.2f} s")
    if med_deck:
        print("  note: this is a FULL 32-key push, which is what the phase "
              "measures.\n        run() sends only the keys whose frame "
              "changed: 4.1-11.1 ms/tick measured, depending on how many "
              "keys moved (32 on the first tick after a reconnect). See the "
              "README Performance section.")

    if not args.skip_soak and args.soak_pid:
        bench_soak(args.soak_pid, args.soak_seconds)


if __name__ == "__main__":
    main()
