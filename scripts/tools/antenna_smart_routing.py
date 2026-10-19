#!/usr/bin/env python3
"""
Antenna Smart Routing Daemon — lazy RF switch controller for Haven nodes.

Controls an HMC349/HMC849 SPDT RF switch to select the best antenna.
Uses a "don't fix what ain't broke" algorithm:

  HAPPY:    Link is good → stay on current antenna, check infrequently
  CURIOUS:  Link is degrading → test the other antenna
  DESPERATE: Both antennas marginal → hunt more aggressively

Supports two control methods:
  --serial /dev/ttyUSB0   CP2102 USB-TTL adapter (TXD → VCT1, no soldering)
  --gpio 17               Direct RPi GPIO pin (sysfs)

Usage:
    python3 /root/antenna_smart_routing.py --serial /dev/ttyUSB0
    python3 /root/antenna_smart_routing.py --serial /dev/ttyUSB0 --invert
    python3 /root/antenna_smart_routing.py --serial /dev/ttyUSB0 --dry-run
    python3 /root/antenna_smart_routing.py --gpio 17

Wiring (CP2102 USB-TTL → HMC349):
    CP2102 3V3   →  HMC349 VCC
    CP2102 GND   →  HMC349 GND
    CP2102 TXD   →  HMC349 VCT1
    HaLow radio  →  HMC349 RFC (common)
    Antenna A     →  HMC349 RF1
    Antenna B     →  HMC349 RF2
"""

import argparse
import collections
import json
import os
import re
import subprocess
import sys
import time

# ── Thresholds ────────────────────────────────────────────────────────

SNR_HAPPY = 15       # Above this: link is solid, check less often
SNR_CURIOUS = 8      # Below this: worth checking the other antenna
SNR_DESPERATE = 3    # Below this: both are bad, hunt aggressively

SAMPLE_WINDOW = 4    # Seconds on new antenna before judging
COOLDOWN = 10        # Min seconds between switches
SWITCH_HYSTERESIS = 3  # dB better required to justify switch
HAPPY_PROBE_EVERY = 1  # Probe other antenna every N happy cycles (~10s)

# ── Switch control backends ───────────────────────────────────────────

class SerialSwitch:
    """Control HMC349 via CP2102 USB-TTL."""

    def __init__(self, port='/dev/ttyUSB0', dry_run=False, pin='txd', invert=False):
        self.port = port
        self.dry_run = dry_run
        self.current = 0
        self.pin = pin
        self.invert = invert
        if not dry_run:
            import serial
            self.ser = serial.Serial(port, baudrate=300, timeout=1)
            self._drive(0)

    def _drive(self, antenna):
        if self.dry_run:
            return
        hw = antenna ^ self.invert
        if self.pin == 'dtr':
            self.ser.dtr = (hw == 0)
        elif self.pin == 'rts':
            self.ser.rts = (hw == 0)
        else:
            if hw == 0:
                self.ser.break_condition = True
            else:
                self.ser.break_condition = False
                self.ser.write(b'\xff' * 4)
                self.ser.flush()

    def select(self, antenna):
        self.current = antenna
        self._drive(antenna)

    def toggle(self):
        new = 1 - self.current
        self.select(new)
        return new

    def cleanup(self):
        if not self.dry_run and hasattr(self, 'ser'):
            self.ser.break_condition = False
            self.ser.close()


class GPIOSwitch:
    """Control HMC349 via sysfs GPIO."""

    def __init__(self, pin, dry_run=False):
        self.pin = pin
        self.dry_run = dry_run
        self.current = 0
        self._export()

    def _export(self):
        if self.dry_run:
            return
        gpio_path = f'/sys/class/gpio/gpio{self.pin}'
        if not os.path.exists(gpio_path):
            with open('/sys/class/gpio/export', 'w') as f:
                f.write(str(self.pin))
            time.sleep(0.1)
        with open(f'{gpio_path}/direction', 'w') as f:
            f.write('out')
        self._write(0)

    def _write(self, value):
        if self.dry_run:
            return
        with open(f'/sys/class/gpio/gpio{self.pin}/value', 'w') as f:
            f.write(str(value))

    def select(self, antenna):
        self.current = antenna
        self._write(antenna)

    def toggle(self):
        new = 1 - self.current
        self.select(new)
        return new

    def cleanup(self):
        if self.dry_run:
            return
        try:
            with open('/sys/class/gpio/unexport', 'w') as f:
                f.write(str(self.pin))
        except OSError:
            pass


# ── Radio stats ───────────────────────────────────────────────────────

def read_snr(interface='wlan0'):
    """Read SNR from iwinfo assoclist (per-peer, like LuCI). Returns (snr_db, signal, noise) or None."""
    try:
        r = subprocess.run(
            ['iwinfo', interface, 'assoclist'],
            capture_output=True, text=True, timeout=5)
        # Format: "MAC  -46 dBm / -85 dBm (SNR 39)  100 ms ago"
        best = None
        for m in re.finditer(r'([\da-fA-F:]{17})\s+([-0-9]+)\s+dBm\s*/\s*([-0-9]+)\s+dBm\s*\(SNR\s+([0-9]+)\)', r.stdout):
            signal, noise, snr = int(m.group(2)), int(m.group(3)), int(m.group(4))
            if best is None or snr > best[0]:
                best = (snr, signal, noise)
        if best:
            return best
    except Exception:
        pass
    # Fallback: interface-level signal/noise
    try:
        r = subprocess.run(
            ['iwinfo', interface, 'info'],
            capture_output=True, text=True, timeout=5)
        sig_m = re.search(r'Signal:\s*([-0-9]+)', r.stdout)
        noise_m = re.search(r'Noise:\s*([-0-9]+)', r.stdout)
        if sig_m and noise_m:
            signal = int(sig_m.group(1))
            noise = int(noise_m.group(1))
            return signal - noise, signal, noise
    except Exception:
        pass
    return None


def read_batman_neighbors(interface='bat0'):
    """Read BATMAN-adv neighbor stats."""
    try:
        r = subprocess.run(
            ['batctl', 'meshif', interface, 'neighbors_json'],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout.strip())
    except Exception:
        pass
    return []


def get_link_quality(interface='wlan0'):
    """Get combined link quality metrics."""
    snr_data = read_snr(interface)
    if snr_data is None:
        return None
    snr, signal, noise = snr_data
    neighbors = read_batman_neighbors()
    throughputs = [n.get('throughput', 0) / 1000 for n in neighbors if n.get('throughput')]
    avg_tp = sum(throughputs) / len(throughputs) if throughputs else 0
    return {
        'snr': snr, 'signal': signal, 'noise': noise,
        'neighbors': len(neighbors), 'avg_throughput_mbps': avg_tp,
    }


# ── Display ───────────────────────────────────────────────────────────

def snr_bar(snr, width=30):
    """Render an SNR bar: [████████████░░░░░░░░░░] 43dB"""
    norm = max(0.0, min(1.0, (snr - SNR_DESPERATE) / (55 - SNR_DESPERATE)))
    filled = int(norm * width)
    return '\u2588' * filled + '\u2591' * (width - filled)


def snr_label(snr):
    if snr >= 20:
        return 'EXCELLENT'
    elif snr >= SNR_HAPPY:
        return 'GOOD'
    elif snr >= SNR_CURIOUS:
        return 'WEAK'
    elif snr >= SNR_DESPERATE:
        return 'MARGINAL'
    else:
        return 'DEAD'


def draw(state, antenna_names):
    """Full-screen live display."""
    W = 58

    print("\033[2J\033[H", end="")  # clear screen
    print()
    print("  Haven Antenna Smart Routing")
    print("  " + "\u2550" * W)

    # ── Active antenna ────────────────────────────────────
    current = state['current_antenna']
    mode = state['mode']

    print()
    print(f"  Active:  {antenna_names[current]}")
    print(f"  Mode:    {mode.upper()}")

    # ── Current signal ────────────────────────────────────
    q = state['current_quality']
    if q:
        bar = snr_bar(q['snr'])
        label = snr_label(q['snr'])
        print()
        print(f"  SNR:     {q['snr']:>3} dB   [{bar}]  {label}")
        print(f"  Signal:  {q['signal']}/{q['noise']} dBm")
        print(f"  Neighbors: {q['neighbors']}    Throughput: {q['avg_throughput_mbps']:.1f} Mbps")
    else:
        print()
        print("  Signal:  waiting for data...")

    # ── Last known SNR per antenna ────────────────────────
    print()
    print("  Antenna Comparison")
    print("  " + "\u2500" * W)
    for i, name in enumerate(antenna_names):
        snr = state['best_snr'].get(i)
        marker = " \u25c0 active" if i == current else ""
        if snr is not None:
            sig = state['best_signal'].get(i)
            nse = state['best_noise'].get(i)
            bar = snr_bar(snr, width=20)
            sigstr = f"{sig}/{nse} dBm" if sig is not None else "---/--- dBm"
            print(f"    {name:.<25s} {sigstr}  SNR {snr:>2} dB  [{bar}]{marker}")
        else:
            print(f"    {name:.<25s} ---/--- dBm  SNR  ? dB  [{'?' * 20}]{marker}")

    # ── Event log ─────────────────────────────────────────
    print()
    print("  Event Log")
    print("  " + "\u2500" * W)
    events = state['events']
    if events:
        for ev in events:
            print(f"    {ev}")
    else:
        print("    (no events yet)")

    # ── Probe countdown ───────────────────────────────────
    print()
    happy_remaining = HAPPY_PROBE_EVERY - state['happy_count']
    if state['mode'] == 'happy':
        print(f"  Next probe in {happy_remaining} cycles "
              f"(~{happy_remaining * state['happy_interval']}s)")
    else:
        print(f"  Actively checking (mode: {mode})")

    print()
    print("  Ctrl+C to stop")
    sys.stdout.flush()


# ── Main loop ─────────────────────────────────────────────────────────

def run(switch, antenna_names, interface, happy_interval, curious_interval,
        desperate_interval, monitor):
    """Main diversity loop."""

    # Shared state for display
    state = {
        'current_antenna': switch.current,
        'mode': 'starting',
        'current_quality': None,
        'best_snr': {0: None, 1: None},
        'best_signal': {0: None, 1: None},
        'best_noise': {0: None, 1: None},
        'happy_count': 0,
        'happy_interval': happy_interval,
        'events': collections.deque(maxlen=8),
    }

    last_switch_time = 0

    def event(msg):
        ts = time.strftime('%H:%M:%S')
        line = f"[{ts}] {msg}"
        state['events'].append(line)
        if not monitor:
            print(line, flush=True)

    event(f"Started on {antenna_names[switch.current]}")

    try:
        while True:
            quality = get_link_quality(interface)
            state['current_quality'] = quality
            state['current_antenna'] = switch.current

            if quality is None:
                state['mode'] = 'no signal'
                event("No signal data - radio down?")
                if monitor:
                    draw(state, antenna_names)
                time.sleep(curious_interval)
                continue

            snr = quality['snr']
            current = switch.current
            state['best_snr'][current] = snr
            state['best_signal'][current] = quality['signal']
            state['best_noise'][current] = quality['noise']

            # ── Decide mode ───────────────────────────────
            if snr >= SNR_HAPPY:
                mode = 'happy'
                interval = happy_interval
            elif snr >= SNR_CURIOUS:
                mode = 'curious'
                interval = curious_interval
                state['happy_count'] = 0
            else:
                mode = 'desperate'
                interval = desperate_interval
                state['happy_count'] = 0

            state['mode'] = mode

            if monitor:
                draw(state, antenna_names)

            # ── Should we probe? ──────────────────────────
            now = time.time()
            since_last_switch = now - last_switch_time

            if mode == 'happy':
                state['happy_count'] += 1
                if state['happy_count'] < HAPPY_PROBE_EVERY:
                    time.sleep(interval)
                    continue
                state['happy_count'] = 0
                event(f"Happy probe: testing {antenna_names[1 - current]}")

            if since_last_switch < COOLDOWN:
                remaining = COOLDOWN - since_last_switch
                event(f"Cooldown: {remaining:.0f}s remaining")
                if monitor:
                    draw(state, antenna_names)
                time.sleep(interval)
                continue

            # ── Test the other antenna ────────────────────
            other = 1 - current
            event(f"Testing {antenna_names[other]}...")
            switch.toggle()
            state['current_antenna'] = switch.current

            if monitor:
                draw(state, antenna_names)

            time.sleep(SAMPLE_WINDOW)

            other_quality = get_link_quality(interface)
            if other_quality is None:
                event(f"{antenna_names[other]}: no signal, back to {antenna_names[current]}")
                switch.select(current)
                state['current_antenna'] = switch.current
                last_switch_time = now
                if monitor:
                    draw(state, antenna_names)
                time.sleep(interval)
                continue

            other_snr = other_quality['snr']
            state['best_snr'][other] = other_snr
            improvement = other_snr - snr

            if improvement >= SWITCH_HYSTERESIS:
                event(f"SWITCH >> {antenna_names[other]}  "
                      f"(SNR {other_snr}dB vs {snr}dB, +{improvement}dB)")
                last_switch_time = now
            else:
                event(f"Staying on {antenna_names[current]}  "
                      f"({antenna_names[other]} was {other_snr}dB vs {snr}dB, "
                      f"delta {improvement:+d}dB)")
                switch.select(current)
                state['current_antenna'] = switch.current
                last_switch_time = now

            if monitor:
                draw(state, antenna_names)

            time.sleep(interval)

    except KeyboardInterrupt:
        event("Stopped.")
        if monitor:
            draw(state, antenna_names)
        print()
    finally:
        switch.cleanup()


# ── CLI ───────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description='Antenna smart routing daemon for Haven mesh nodes')

    ctrl = ap.add_mutually_exclusive_group(required=True)
    ctrl.add_argument('--serial', metavar='PORT',
                      help='CP2102 USB-TTL serial port (e.g. /dev/ttyUSB0)')
    ctrl.add_argument('--gpio', type=int,
                      help='GPIO pin connected to HMC349 VCT1')

    ap.add_argument('--pin', choices=['dtr', 'rts', 'txd'], default='txd',
                    help='Which serial pin drives VCT1 (default: txd)')
    ap.add_argument('--invert', action='store_true',
                    help='Swap RF1/RF2 mapping if wiring is reversed')
    ap.add_argument('--rf1-name', default='RF1 (yogurt cup)',
                    help='Name for RF1 antenna')
    ap.add_argument('--rf2-name', default='RF2 (alfa 915)',
                    help='Name for RF2 antenna')
    ap.add_argument('--interface', default='wlan0',
                    help='Wireless interface (default: wlan0)')
    ap.add_argument('--happy-interval', type=int, default=10,
                    help='Seconds between checks when link is good (default: 10)')
    ap.add_argument('--curious-interval', type=int, default=6,
                    help='Seconds between checks when link is degrading (default: 6)')
    ap.add_argument('--desperate-interval', type=int, default=2,
                    help='Seconds between checks when link is bad (default: 2)')
    ap.add_argument('--dry-run', action='store_true',
                    help='Log decisions without toggling switch')
    ap.add_argument('--no-monitor', action='store_true',
                    help='Disable live display, log-only mode (for background use)')
    args = ap.parse_args()

    antenna_names = [args.rf1_name, args.rf2_name]

    if args.serial:
        switch = SerialSwitch(args.serial, dry_run=args.dry_run, pin=args.pin,
                              invert=args.invert)
    else:
        switch = GPIOSwitch(args.gpio, dry_run=args.dry_run)

    run(switch, antenna_names, args.interface, args.happy_interval,
        args.curious_interval, args.desperate_interval,
        monitor=not args.no_monitor)


if __name__ == '__main__':
    main()
