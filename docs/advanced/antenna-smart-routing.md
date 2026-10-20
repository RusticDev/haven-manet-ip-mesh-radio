# Antenna Smart Routing

Automatic antenna switching for Haven nodes using an RF SPDT switch. The daemon monitors link quality and selects the best antenna, improving range and reliability without manual intervention.

## Overview

| Property | Value |
|----------|-------|
| Script | `scripts/tools/antenna_smart_routing.py` |
| Runs on | Haven node (gate or point) |
| RF switch | HMC349 / HMC849 SPDT |
| Control | CP2102 USB-TTL adapter or RPi GPIO |
| Dependencies | `pyserial` (for USB-TTL mode) |

## How It Works

The daemon uses a lazy diversity algorithm with three modes:

| Mode | Condition | Behavior |
|------|-----------|----------|
| **Happy** | SNR 15 dB or above | Stay on current antenna, probe the other about every 14s (10s interval plus 4s sample) |
| **Curious** | SNR 8 dB up to 15 dB | Probe the other antenna about every 10s (6s interval plus 4s sample) |
| **Desperate** | SNR below 8 dB | Poll every 2s; the 10s cooldown limits probes to about every 10s |

When probing, the daemon:
1. Switches to the other antenna
2. Waits 4 seconds for the signal to stabilize
3. Compares SNR on both antennas
4. Keeps the better one (if the improvement exceeds 3 dB hysteresis)
5. Switches back if the other antenna isn't meaningfully better

A cooldown timer prevents rapid flapping between antennas.

## Hardware

### RF Switch

Any SPDT RF switch that covers 900 MHz and accepts 3.3V logic control. Tested with:

| Switch | Frequency Range | Insertion Loss (900 MHz) | Switching Speed |
|--------|----------------|-------------------------|-----------------|
| **HMC349** | DC - 4 GHz | ~0.5 dB | Nanoseconds |
| **HMC849** | DC - 6 GHz | ~0.5 dB | Nanoseconds |

### Wiring (CP2102 USB-TTL — no soldering)

Use a CP2102 USB-to-TTL adapter to control the switch without touching the RPi GPIO header. This is ideal when HATs (Seeed WM6180, UPS, etc.) occupy the GPIO pins.

```
CP2102 USB-TTL          HMC349 SPDT Switch
──────────────          ──────────────────
    3V3  ──────────────→  VCC
    GND  ──────────────→  GND
    TXD  ──────────────→  VCT1

HaLow Radio             HMC349
───────────             ──────
 Antenna port  ────────→  RFC (common)

Antennas                HMC349
────────                ──────
 Antenna A  ───────────→  RF1
 Antenna B  ───────────→  RF2
```

The CP2102 plugs into a USB port on the RPi. The TXD line is driven low (via UART break condition) or high to select RF1 or RF2.

### Wiring (Direct GPIO)

If GPIO pins are accessible:

```
RPi GPIO                HMC349
────────                ──────
 GPIO 17 (pin 11)  ───→  VCT1
 3.3V (pin 1)  ───────→  VCC
 GND (pin 6)  ────────→  GND
```

### Antenna Pairing Suggestions

| Pairing | Best For |
|---------|----------|
| RHCP + Omni whip | Suburban / mobile — RHCP wins in multipath, omni wins in open |
| Omni + Directional | Fixed gate with one distant point — omni for general mesh, directional for the long link |
| Two different omnis | General diversity — different radiation patterns catch different reflections |

## Usage

### Live Monitor (interactive)

```bash
python3 /root/antenna_smart_routing.py --serial /dev/ttyUSB0
```

Shows a full-screen display with:
- Active antenna and current mode
- SNR bar graph with signal/noise levels
- Side-by-side antenna comparison
- Rolling event log of probes and switches

### Background Daemon (headless)

```bash
python3 -u /root/antenna_smart_routing.py --serial /dev/ttyUSB0 --no-monitor \
    >> /root/antenna_smart_routing.log 2>&1 &
```

Check the log:
```bash
tail -f /root/antenna_smart_routing.log
```

### GPIO Mode

```bash
python3 /root/antenna_smart_routing.py --gpio 17
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--serial PORT` | — | CP2102 serial port (e.g. `/dev/ttyUSB0`) |
| `--gpio PIN` | — | GPIO pin number for direct control |
| `--pin {dtr,rts,txd}` | `txd` | Which serial control line drives VCT1 |
| `--invert` | off | Swap RF1/RF2 mapping if wiring is reversed |
| `--rf1-name` | `RF1 (yogurt cup)` | Display name for RF1 antenna |
| `--rf2-name` | `RF2 (alfa 915)` | Display name for RF2 antenna |
| `--happy-interval` | 10 | Seconds between checks when happy |
| `--curious-interval` | 6 | Seconds between checks when curious |
| `--desperate-interval` | 2 | Seconds between checks when desperate |
| `--dry-run` | off | Log decisions without toggling the switch |
| `--no-monitor` | off | Disable live display for background use |

### Demo / Fast Probe Mode

For demonstrations or testing, reduce the timings:

```bash
python3 /root/antenna_smart_routing.py --serial /dev/ttyUSB0 \
    --happy-interval 1 --curious-interval 2 --desperate-interval 1
```

## Installation

1. Copy the script to the Haven node:

```bash
scp scripts/tools/antenna_smart_routing.py root@<node-ip>:/root/antenna_smart_routing.py
```

2. Ensure `pyserial` is installed (for USB-TTL mode):

```bash
# On the node:
pip3 install pyserial
# Or on OpenWrt:
opkg install python3-pyserial
```

3. Verify the CP2102 is detected:

```bash
ls /dev/ttyUSB*
```

## Tuning

The thresholds at the top of the script can be adjusted:

| Parameter | Default | Effect |
|-----------|---------|--------|
| `SNR_HAPPY` | 15 dB | Higher = more aggressive probing |
| `SNR_CURIOUS` | 8 dB | Higher = earlier curiosity |
| `SNR_DESPERATE` | 3 dB | Only affects the signal bar and label, not the mode |
| `SWITCH_HYSTERESIS` | 3 dB | Lower = switches more easily |
| `HAPPY_PROBE_EVERY` | 1 cycle | Lower = probes more often when happy |
| `COOLDOWN` | 10s | Lower = allows faster switching |

> **Tip:** Start with defaults. If you notice the daemon staying on a worse antenna too long, lower `SWITCH_HYSTERESIS`. If it's flapping, raise it or increase `COOLDOWN`.
