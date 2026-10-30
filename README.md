# Deck Stats — Stream Deck 8×4 dashboard

![Dashboard layout — device mockup](assets/deck-mockup.png)

> Device mockup rendered by `deck_stats.py --preview`; shown values are simulated.

On-screen system monitoring for **Stream Deck XL** (32 addressable keys =
4 rows × 8 columns). No Elgato plugins required — plain Python, `psutil`,
hwmon and `nvidia-smi`.

## Layout

```
        0     1     2     3     4     5     6     7
CPU     UTIL  TEMP  MAX   CLK   PWR   LOAD  CCD1  CORE
GPU     UTIL  TEMP  MAX   CLK   PWR   FAN   VRAM  MEM
SYSTEM  TIME  NV1   NV2   NV3   DSK   SWAP  RAM   MEM
I/O+PSU DOWN  UP    READ  WRITE PSU   FAN   VRM   12V
```

Columns 0–4 of the CPU and GPU rows carry the same metric in the same column,
so the two rows read as one table and a value can be compared vertically. `PSU`
sits under the two `PWR` keys (all watts) and the PSU `FAN` under the GPU `FAN`.
Row 3 is I/O on the left and power supply on the right.

## Graphic rules

- 4–6 char label on top, small; large centered value
- Value typeset in DejaVu Sans **Condensed** Bold when the distro ships it
  (plain Bold otherwise), so 5-character values such as `28.3G` or `412W`
  still render at 30 px or more
- Unit inside the value, not in the label: `45°` `66W` `3.7G` `2.8G`
- A separate unit sits in the top-right corner only where the value carries none
  of its own — the rates: `DOWN` `UP` `READ` `WRITE` show `MB/s`. If the label
  plus the unit would not fit, the unit is dropped for that key only
- `LOAD` shows the 1 min average large, with the 5 min one as a small second line
- `TIME` shows the clock large, with the uptime underneath: `up 3d 04h`,
  `up 4h 07m`, `up 12m`. Uptime comes from `psutil.boot_time()`, so it also
  covers suspend time (a `time.monotonic()` uptime would not)
- `MEM` (GPU), `MEM` (RAM) and `DSK` show the total as a small second line:
  `28.3G` over `/32G`, `842.1G` over `/1863G`. A missing total means no line,
  never a fake one
- `DSK` is the free space on `/` — what a normal user can still write, not
  counting the reserve the root filesystem keeps for itself
- Colored bar **only** on UTIL, CORE, VRAM, RAM, SWAP and FAN %
- **Sparkline** (last 40 samples, ~1 min at 1.5 s) in the bottom zone of the
  keys that would otherwise leave it empty: CPU/GPU `TEMP`, CPU/GPU `PWR`,
  `PSU`, `DOWN`, `UP`, `READ`, `WRITE`. The bottom zone holds exactly one thing,
  in priority order: bar → sub-line → sparkline. The line is scaled min-max over
  the window, with a minimum span of 1 % of the value so a flat series stays flat
  instead of blowing up sensor noise, and it is drawn in the key's current color
- A 3 px accent strip on the top edge of every key marks its group:
  blue = CPU row, violet = GPU row, cyan = SYSTEM row, gray = `TIME` (a clock,
  not a system metric) and the I/O keys, pink = the power-supply half of row 3.
  These hues are outside the green/amber/red family on purpose, which is
  reserved for status
- `UTIL` is the average load across all threads; `CORE` is the busiest single
  logical CPU. On a wide CPU the two tell different stories: 3 % average can
  still mean one thread pinned at 100 %. `CORE` is amber from 90 %, and
  deliberately **never red**: one saturated thread is normal, not an alarm
- Temperatures: green normal → yellow → red
- `LOAD` has no absolute warning level: it is normalised by core count, yellow
  from 70 % of the cores, red from 100 %. The 5 min average stays a gray sub-line
- `12V` is colored by the deviation from 12.00 V: yellow from 5 %, red from 8 %
- `SWAP` is yellow from 50 % and red from 80 % — in use at all is the signal,
  saturating it is the alarm
- A key that reaches its critical threshold also gets a dark red background
  tint, so the alarm survives the distance
- `--` = sensor unavailable at that moment (never a placeholder `0`)

## Data sources

| Group | Sources |
|---|---|
| CPU | `psutil.cpu_percent` (UTIL average, CORE busiest thread), `/proc/loadavg` (LOAD, 1 and 5 min), `k10temp` or `coretemp` hwmon (TEMP, CCD1), RAPL (PWR), cpufreq (max core CLK) |
| GPU | `nvidia-smi` (util, temp, VRAM, power, fan, clock) |
| SYSTEM | RAM/swap (`psutil.virtual_memory`, `psutil.swap_memory`), NVMe hwmon temperatures (NV1–NV3), `os.statvfs("/")` (DSK: free space) |
| I/O | `/proc/net/dev` (DOWN, UP), `/proc/diskstats` (READ, WRITE) |
| PSU | `corsairpsu` hwmon *(optional: power, VRM, fan, 12 V rail)* |
| Clock | local time, `psutil.boot_time()` (uptime, suspend-aware) |

Everything is auto-detected: the CPU temperature chip, the RAPL package
domain, the NVMe drives and the PSU are looked up at startup and reported on
the first log line (`Sensors: {...}`). Missing sensors simply render `--`.

## Configuration

All thresholds have defaults that suit a high-end desktop; override them per
machine through environment variables (put them in the unit's
`Environment=` lines or in your shell for manual runs):

| Variable | Default | Meaning |
|---|---|---|
| `DECK_UPDATE_SECONDS` | `1.5` | refresh interval |
| `DECK_BRIGHTNESS` | `70` | deck brightness 0–100 |
| `DECK_PEAK_WINDOW` | `3600` | horizon of the `MAX` keys, in seconds |
| `DECK_GPU_POWER_MAX` | `550` | GPU board power limit (W); warn at 65 %, red at 90 % |
| `DECK_PSU_POWER_MAX` | `650` | PSU rated output (W); warn at 70 %, red at 92 % |

## Installation

```bash
# 1. Dependencies (any location — no paths are hardcoded)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Permissions (deck access + readable RAPL counter) — needs sudo password
./install-udev.sh

# 3. Run manually
.venv/bin/python deck_stats.py

# 4. Or install the systemd user service (paths resolved at install time)
./install-service.sh
journalctl --user -u deck-stats -f
```

`install-service.sh` generates `~/.config/systemd/user/deck-stats.service`
from the `deck-stats.service` template, substituting the absolute path of your
checkout. Remove it with `./install-service.sh --remove`.

It also enables **lingering** for your user (`sudo loginctl enable-linger $USER`).
This is required on a headless box: without it systemd-logind stops
`user@UID.service` as soon as your last login session ends, which takes the
dashboard down with it on every SSH logout. With linger the user manager starts
at boot and keeps running with no session open, so the deck comes back by
itself after a reboot without any SSH login.

Check it with:

```bash
loginctl show-user "$USER" -p Linger      # Linger=yes
systemctl status user@$UID.service         # active (running), no session needed
```

## Utilities

```bash
.venv/bin/python deck_stats.py --preview   # whole deck preview (8×4) in /tmp/deck_preview
.venv/bin/python benchmark.py --help       # per-tick cost breakdown (see Performance)
.venv/bin/python test_differential.py      # offline checks of the key push path
journalctl --user -u deck-stats -f         # live logs
```

## Performance

Measured with `benchmark.py` on the reference machine (MSI X870E, Ryzen 9, RTX
5090, Corsair PSU, Stream Deck XL): Python 3.14.8, Pillow 13.0.0, psutil 7.2.2,
40 timed iterations per phase and a 180 s soak of the systemd service. All
figures are **per tick** (sample + render + encode + push), and the USB row is
a **full 32-key push** — what the loop pays when every key still has to be
repainted:

| Phase | median | p95 | share |
|---|--:|--:|--:|
| Sensor acquisition (sysfs + psutil, `nvidia-smi` excluded) | 27.0 ms | 27.3 | 39 % |
| `nvidia-smi` subprocess | 11.2 ms | 11.8 | 16 % |
| Rendering the 32 key images | 3.4 ms | 3.5 | 5 % |
| PIL → native key format ×32 | 1.0 ms | 1.0 | 1.5 % |
| Pushing 32 images to the deck (USB) | 25.7 ms | 121.4 | 38 % |
| **Total** | **68.5 ms** | | **4.6 % of the 1500 ms interval** |

The `p95` of the USB row is the **first** refresh after the device is opened
(libusb/HID warm-up); the remaining ticks stay near 26 ms. The encode row is
worth a note: one cold pass costs 5 ms, 200 warm passes give the 1.0 ms above —
a first-sample number is not a per-tick number.

Inside the 27 ms of acquisition, the cost is hardware, not Python:

| Source | median |
|---|--:|
| 5 `corsairpsu` attributes | 12.0 ms — 2.0 ms each: every read is a USB control transfer to the PSU |
| 3 NVMe temperatures | 4.0 ms — one SMART log command per controller |
| cpufreq ×16, `/proc/diskstats`, per-thread `cpu_percent` | 0.06–0.08 ms each |
| psutil mem/swap, `/proc/net/dev`, `statvfs`, loadavg, k10temp, RAPL | 0.001–0.03 ms each |

### Differential refresh

Before changing anything, the frames were only **observed**: the device was
wrapped in a proxy that compares and times every write while the real loop
keeps sending all 32 of them. Over 199 ticks at 1.5 s on a working desktop:

| what the observation showed | |
|---|--:|
| frames identical to the one before | 67 % of all keys |
| — plain keys (23 of them) | 89 % identical |
| — keys carrying a sparkline (9) | 10 % identical |
| keys that actually move per tick | median 11, min 7, max 14 |
| cost of comparing 32 encoded frames | 0.026 ms per tick |

So most of the USB traffic is re-sending a picture the key already shows. The
dashboard now compares the **encoded bytes** per key and skips the ones that did
not change (`push_key()`): comparing the picture rather than the sensor value is
the point, because a number can stay put while its picture changes anyway (a
sparkline advancing, a value crossing its warn threshold).

| measured with the device free, A/B in the same process | |
|---|--:|
| full 32-key push | 26.5–27.0 ms per tick |
| differential push | **4.1 ms** median, p95 5.9, max 26.1 |
| saving | **22.4 ms/tick — 85 % of the USB phase** |
| frames pushed per tick, end to end (120 s run) | median 11, p95 14, max 16 of 32 |
| USB per tick, same 120 s run | 11.1 ms median, against ~31 ms for a full push |

How much is saved depends on how busy the machine is: 5–6 keys move per tick on
an idle desktop, ~11 while it works, which is why the USB row is a range
(4–11 ms) and not one number. The first tick after a reconnect still pushes
all 32.

`.venv/bin/python test_differential.py` checks the parts that are easy to get
wrong and impossible to notice from the outside, with a fake deck: the first
tick paints 32, later ticks only what changed, a reconnect repaints everything,
a failed HID write leaves the frame owed, and crossing a warn/crit threshold
really does produce different bytes.

Resources, sampled from `/proc` every 2 s for 180 s while the service ran:

| | mean | median | p95 | max |
|---|--:|--:|--:|--:|
| RSS | 35.2 MB | 35.2 | 35.2 | 35.2 |
| CPU (one core) | 0.78 % | 0.50 | 1.50 | 1.50 |

No growth over the window (35.1 → 35.2 MB), threads 4 → 4, file descriptors
8 → 8. systemd's own accounting agrees: 9.98 s of CPU over 9 min 51 s, 41.9 MB
peak. The 32 cached frames (~220 KB) and the JPEG encoder buffers are already
inside these numbers; the CPU is a little lower than before the differential
refresh, which is the write traffic disappearing.

**These are latencies, not CPU time.** Most of the 68 ms is waiting on devices
— a subprocess, a PSU register, the HID endpoint — which is why the process
uses under 1 % of a core. A rewrite in a compiled language would shrink the
3.4 ms of PIL work, not the 27 ms of sensor reads and the ~26 ms of USB push,
so it buys nothing measurable here. The same question was asked of `nvidia-smi`
itself: reading the GPU through NVML instead of the subprocess costs 0.5 ms
instead of 11 ms per tick, but mapping `libnvidia-ml` pulls the driver's state
into the process and RSS jumps by ~25 MB (35 → 60 MB). Not worth it at 1.5 s,
so `nvidia-smi` stays.

Headroom at faster refresh rates, *theoretical* (the measured per-tick time
divided by the interval — the behaviour of the PSU and the USB endpoint at high
rates is not benchmarked). With the differential refresh a tick lands between
47 ms (quiet machine, ~5 keys moving) and 54 ms (busy machine, ~11 keys moving):

| `DECK_UPDATE_SECONDS` | tick occupancy |
|---|--:|
| 1.5 (default) | 3–4 % |
| 1.0 | 5–6 % |
| 0.5 | 9–11 % |
| 0.25 | 19–23 % |

The default stays 1.5 s: for temperatures and system statistics it is already
faster than readable, and it leaves the bus and the PSU idle. Refreshing faster
also does **not** make the differential refresh cheaper — at 0.5 s the share of
unchanged frames measured the same 67 % as at 1.5 s.

```bash
.venv/bin/python benchmark.py --skip-deck        # sensors + render + soak, device untouched
.venv/bin/python test_differential.py            # push path, no device needed
systemctl --user stop deck-stats                 # the USB phase needs the device
.venv/bin/python benchmark.py --skip-soak
systemctl --user start deck-stats
```

`--soak-pid auto` (default) reads the `MainPID` of the running service, so the
RSS/CPU numbers come from the real process and cost it nothing; `--iters` sets
the iteration count.

## Notes

- Fixed-rate loop: the interval is measured with `time.monotonic()`, so slow
  ticks do not accumulate drift. `nvidia-smi` costs ~11 ms per tick (measured,
  see Performance).
- `CLK` is the highest current core clock across all cores (`scaling_cur_freq`),
  not `cpu0`: the sampler's own core is a poor proxy for boost.
- `MAX` is a rolling peak over `DECK_PEAK_WINDOW`, not an all-time maximum, so
  an old spike does not pin the key forever.
- Rates (DOWN/UP/READ/WRITE) start from the second tick (the first one samples
  the baseline).
- Disk I/O counts whole block devices only: partitions are reported by
  `/proc/diskstats` with overlapping counters and would double the rate.
- `NV1`–`NV3` follow controller order (`nvme0`, `nvme1`, `nvme2`), not `hwmonN`
  numbering: the latter is assigned at probe time and can change between boots,
  which would silently swap the drive behind a label. Extra drives are reported
  on the startup log line (`Sensors: {...}`); SATA disks expose no temperature
  unless the `drivetemp` module is loaded (they are not read yet).
- `nvme_temps` keeps one slot per controller, `None` when a drive is not
  reporting. Dropping unavailable readings from the list would shift the later
  drives onto the previous labels and show a temperature for the wrong disk;
  with a fixed slot the silent drive shows `--` and the others stay put.
- In the GPU row, an `nvidia-smi` field the driver cannot read (`[N/A]`,
  `[Not Supported]`) is a missing value, not a read failure: that key shows
  `--`, the rest of the row stays live and nothing is logged every tick. Only
  a read that genuinely failed (timeout, missing binary, short CSV row) blanks
  the whole GPU block.
- Each key frame is compared with the one already on the key, **as encoded
  bytes**, and an identical frame is not re-sent. Comparing the picture rather
  than the sensor value is the point: the number can stay while the picture
  changes anyway (sparkline advancing, colour crossing a threshold). The cache
  belongs to one connection cycle and starts empty, so after an unplug or an
  error all 32 keys are re-sent — a cache carried over from the previous cycle
  would keep skipping frames that are no longer on the device. A write that
  fails leaves the frame owed, so the next tick tries it again.
- If the deck is unplugged the script reconnects automatically; on exit
  (Ctrl-C or SIGTERM from systemd) it blanks the keys instead of leaving stale
  numbers lit.
- If RAPL still gives "Permission denied" (`-r--------`), re-run
  `./install-udev.sh`.

## Reference setup and known adaptations

Measurements were tuned on one machine (MSI X870E + Ryzen 9 + RTX 5090,
Corsair PSU); other hardware works, some keys fall back:

| Key | Intended | Fallback when unavailable |
|---|---|---|
| CPU PWR | RAPL package power | **PSU total − GPU** (rest of the system) if the counter is unreadable |
| CPU FAN | motherboard fan | no fan sensor on the MSI board → uses the PSU fan (pwm %) |
| CPU 12V | vcore | vcore not exposed → **PSU 12V rail** |
| GPU MAX | hotspot | hotspot not exposed by `nvidia-smi` → rolling peak of GPU temp |

## Related projects

Similar open-source work is rare and mostly split in two camps: **standalone**
apps that talk to the device themselves (like this one), and **plugins** that
need a host — the Elgato Stream Deck app, or [OpenDeck](https://github.com/nekename/OpenDeck)
on Linux. Standalone, closest in approach:

| Project | Lang | Platform | Notes |
|---|---|---|---|
| [ZoutMax/FifineControlDeck](https://github.com/ZoutMax/FifineControlDeck) | Python / PyQt6 | Linux | for the fifine "Stream Dock" clone (USB `3142:0060`), not an Elgato; keys for CPU, RAM, VRAM, GPU load/temps, network, disk space |
| [LabRaTox/Deck-Switch](https://github.com/LabRaTox/Deck-Switch) | Python / PyQt6 | Linux | real Stream Deck hardware, own plugin system with a built-in *system* plugin |
| [SmokeyMcBong/StreamDeckMonitor](https://github.com/SmokeyMcBong/StreamDeckMonitor) | C# / .NET | Windows | background console app, CPU/GPU/FPS via MSI Afterburner + StreamDeckSharp |

Plugins (they need the vendor app or OpenDeck running):

| Project | Lang | Platform | Notes |
|---|---|---|---|
| [kahikara/opendeck-redline-monitor](https://github.com/kahikara/opendeck-redline-monitor) | JavaScript | Linux (OpenDeck) | richest metric set, keys **and** encoders, sysfs-first |
| [moeilijk/lhm-streamdeck](https://github.com/moeilijk/lhm-streamdeck) + [lhm-companion](https://github.com/moeilijk/lhm-companion) | JavaScript / Go | Linux, Windows | sensor data served in LibreHardwareMonitor's `data.json` format |
| [Yoshiofthewire/agentglass-streamdeck](https://github.com/Yoshiofthewire/agentglass-streamdeck) | TypeScript | Linux (OpenDeck) | sysfs every tick, `nvidia-smi` every 15 s |
| [ShoMetrics/sho_metrics](https://github.com/ShoMetrics/sho_metrics) | TypeScript | Windows, macOS | Marketplace plugin, includes custom HTTP JSON endpoints |

Related but not a Stream Deck: [LabRaTox/EdgeDisplayWidgets](https://github.com/LabRaTox/EdgeDisplayWidgets)
("Edge Dashboard") drives a Corsair Xeneon Edge 14.5" second display from
`/sys/class/hwmon` and NVML through a FastAPI + QML kiosk.

Commercial equivalents on the Elgato Marketplace: *Metrics*, *System Vitals*,
*HWiNFO*.

How this project differs from the options above:

- **No host app at all.** Direct hidapi via `python-elgato-streamdeck`, one
  process, no Elgato/OpenDeck install and no cloud account.
- **Deep hardware access on Linux.** CPU package power from RAPL, PSU
  telemetry from the `corsairpsu` hwmon (VRM temperature, 12 V rail, fan),
  NVMe hwmon temperatures — none of the projects above expose these.
- **Whole 32-key frame** refreshed at a fixed rate from `time.monotonic()`,
  with only the keys whose encoded frame actually changed going back over USB.

## License

MIT — see [LICENSE](LICENSE).
