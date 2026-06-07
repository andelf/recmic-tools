"""
Real-time key event printer for OLYMPUS DR Series (VID=07B4 PID=0256).
Prints KEY_PRESS / KEY_RELEASE / STATE_CHANGE events as they happen.

Usage:
    python keys_monitor.py
    python keys_monitor.py --duration 60
"""

from __future__ import annotations

import argparse
import time
from typing import Set, Tuple

import pywinusb.hid as hid

from read_media_keys import TARGET_VID, TARGET_PID, BITMAP_HINTS

# State bits — shown as current state, not press/release events
STATE_BITS = {(1, 0x01), (1, 0x02), (1, 0x10)}


def bit_label(bit: Tuple[int, int]) -> str:
    return BITMAP_HINTS.get(bit, f"BYTE{bit[0]}_0x{bit[1]:02X}")


def active_bits(payload: bytes) -> Set[Tuple[int, int]]:
    result = set()
    for i, b in enumerate(payload):
        if b == 0:
            continue
        for n in range(8):
            mask = 1 << n
            if b & mask:
                result.add((i, mask))
    return result


def run(vid: int, pid: int, duration: int):
    devices = hid.HidDeviceFilter(vendor_id=vid, product_id=pid).get_devices()
    if not devices:
        print(f"[!] Device VID:{vid:04X} PID:{pid:04X} not found. Check connection.")
        return

    last: Set[Tuple[int, int]] = set()

    def on_raw(data):
        nonlocal last
        if not data:
            return
        payload = bytes(data)
        current = active_bits(payload)

        pressed  = current - last
        released = last - current
        last = current

        if not pressed and not released:
            return

        ts = time.strftime("%H:%M:%S")

        # State changes (STOP / PLAYING / RECORDING)
        state_pressed  = pressed  & STATE_BITS
        state_released = released & STATE_BITS
        key_pressed    = pressed  - STATE_BITS
        key_released   = released - STATE_BITS

        if state_pressed or state_released:
            new_states = current & STATE_BITS
            labels = " | ".join(bit_label(b) for b in sorted(new_states)) or "SCAN_BACKWARD"
            print(f"[{ts}] STATE  -> {labels}")

        for bit in sorted(key_pressed):
            print(f"[{ts}] PRESS    {bit_label(bit)}")
        for bit in sorted(key_released):
            print(f"[{ts}] RELEASE  {bit_label(bit)}")

    opened = []
    try:
        for dev in devices:
            dev.open()
            dev.set_raw_data_handler(on_raw)
            opened.append(dev)
            print(f"[+] Opened: {dev.vendor_name} / {dev.product_name}")

        print(f"[*] Monitoring for {duration}s  (Ctrl+C to stop early)\n")
        t0 = time.time()
        while time.time() - t0 < duration:
            time.sleep(0.02)

    except KeyboardInterrupt:
        print("\n[*] Stopped by user.")
    finally:
        for dev in opened:
            try:
                dev.close()
            except Exception:
                pass
        print("[*] Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OLYMPUS DR Series key event printer")
    parser.add_argument("--duration", type=int, default=300, help="Monitor duration in seconds (default: 300)")
    args = parser.parse_args()
    run(TARGET_VID, TARGET_PID, args.duration)
