# Deck Stats — Stream Deck 8×4 dashboard

![Dashboard layout — device mockup](assets/deck-mockup.png)

> Device mockup rendered by `deck_stats.py --preview`; shown values are simulated.

On-screen system monitoring for **Stream Deck XL** (32 addressable keys =
4 rows × 8 columns). No Elgato plugins required — plain Python, `psutil`,
hwmon and `nvidia-smi`.

## Layout

```
ROW 0  CPU     UTIL | TEMP | CCD1 | CLK | PWR | LOAD | MAX | PROC
ROW 1  GPU     UTIL | TEMP | VRAM | MEM | PWR | FAN  | CLK | MAX
ROW 2  SYSTEM  RAM  | MEM  | NV1  | NV2 | PSU | VRM  | 12V | FAN
ROW 3  LINUX   LOAD1|LOAD5| DOWN | UP  |READ |WRITE | UPT | TIME
```

## Graphic rules

- 4–6 char label on top, small; large centered value
- Unit inside the value, not in the label: `45°` `66W` `3.7G` `2.8G`
- Colored bar **only** on UTIL, VRAM, RAM and FAN %
- Temperatures: white normal → yellow → red
- `--` = sensor unavailable at that moment (never a placeholder `0`)

## Data sources

| Group | Sources |
|---|---|
| CPU | `psutil` (load), `k10temp` or `coretemp` hwmon (Tctl/CCD1), RAPL (power), cpufreq (clock) |
| GPU | `nvidia-smi` (util, temp, VRAM, power, fan, clock) |
| System | `corsairpsu` hwmon *(optional: power, VRM, fan, 12V rail)*, NVMe hwmon, `psutil` (RAM) |
| Linux | `/proc/loadavg`, `/proc/net/dev`, `/proc/diskstats`, `psutil.pids()`, clock |

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

## Utilities

```bash
.venv/bin/python deck_stats.py --preview   # whole deck preview (8×4) in /tmp/deck_preview
journalctl --user -u deck-stats -f         # live logs
```

## Notes

- Fixed-rate loop: the interval is measured with `time.monotonic()`, so slow
  ticks do not accumulate drift. `nvidia-smi` costs ~50 ms per tick.
- `MAX` is a rolling peak over `DECK_PEAK_WINDOW`, not an all-time maximum, so
  an old spike does not pin the key forever.
- Rates (DOWN/UP/READ/WRITE) start from the second tick (the first one samples
  the baseline).
- Disk I/O counts whole block devices only: partitions are reported by
  `/proc/diskstats` with overlapping counters and would double the rate.
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

## License

MIT — see [LICENSE](LICENSE).
