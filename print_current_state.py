"""
Print current media-key state snapshot from HID reports.

Usage:
  c:/Temp/Workspace/.venv/Scripts/python.exe print_current_state.py --duration 3
"""

from __future__ import annotations

import argparse
import time
from typing import Dict, Set, Tuple

import pywinusb.hid as hid

from read_media_keys import TARGET_PID, TARGET_VID, BITMAP_HINTS, parse_media_report, key_name

Bit = Tuple[int, int]


def bit_to_str(bit: Bit) -> str:
    return f"byte{bit[0]}:0x{bit[1]:02X}"


def run_snapshot(vid: int, pid: int, duration: float, all_reports: bool) -> int:
    devices = hid.HidDeviceFilter(vendor_id=vid, product_id=pid).get_devices()
    if not devices:
        print(f"[!] Device not found: VID:PID={vid:04X}:{pid:04X}")
        return 1

    state: Dict[str, object] = {
        "report_id": 0,
        "active_bits": set(),
        "raw_hex": "",
        "events": 0,
    }

    def on_raw(data):
        if not data:
            return
        parsed = parse_media_report(bytes(data))
        if (not all_reports) and (not parsed["is_media_candidate"]):
            return
        print(
            f"[RAW] report_id=0x{int(parsed['report_id']):02X} "
            f"len={len(bytes(data))} raw={parsed['raw_hex']}"
        )
        state["report_id"] = parsed["report_id"]
        state["active_bits"] = set(parsed["active_bits"])
        state["raw_hex"] = parsed["raw_hex"]
        state["events"] = int(state["events"]) + 1

    opened = []
    try:
        for dev in devices:
            dev.open()
            dev.set_raw_data_handler(on_raw)
            opened.append(dev)
            print(f"[+] open: {dev.vendor_name} / {dev.product_name}")

        print(f"[*] Capturing media state for {duration:.1f}s ...")
        t0 = time.time()
        while time.time() - t0 < duration:
            time.sleep(0.02)

    finally:
        for dev in opened:
            try:
                dev.close()
            except Exception:
                pass

    events = int(state["events"])
    report_id = int(state["report_id"])
    active_bits: Set[Bit] = set(state["active_bits"])
    raw_hex = str(state["raw_hex"])

    print("\n=== Current Media State Snapshot ===")
    print(f"events_seen: {events}")

    if events == 0:
        if all_reports:
            print("[WARN] No HID report received in this window.")
        else:
            print("[WARN] No media candidate report received in this window.")
            print("       Try pressing a media/switch key while capturing.")
        return 0

    print(f"report_id: 0x{report_id:02X}")
    print(f"raw: {raw_hex}")

    print("\nKnown bits (ON/OFF):")
    for bit in sorted(BITMAP_HINTS.keys()):
        status = "ON" if bit in active_bits else "OFF"
        print(f"  {bit_to_str(bit):12s}  {status:3s}  {BITMAP_HINTS[bit]}")

    unknown = sorted(b for b in active_bits if b not in BITMAP_HINTS)
    print("\nActive bits in this snapshot:")
    if active_bits:
        names = ", ".join(key_name(b) for b in sorted(active_bits))
        print(f"  {names}")
    else:
        print("  (none)")

    if unknown:
        unk = ", ".join(bit_to_str(b) for b in unknown)
        print(f"\nUnknown active bits: {unk}")

    return 0


def main():
    parser = argparse.ArgumentParser(description="Print current media-key state snapshot")
    parser.add_argument("--vid", type=lambda x: int(x, 0), default=TARGET_VID)
    parser.add_argument("--pid", type=lambda x: int(x, 0), default=TARGET_PID)
    parser.add_argument("--duration", type=float, default=3.0, help="Capture window in seconds")
    parser.add_argument("--all-reports", action="store_true", help="Capture all HID report IDs, not only media candidates")
    args = parser.parse_args()

    raise SystemExit(run_snapshot(args.vid, args.pid, args.duration, args.all_reports))


if __name__ == "__main__":
    main()
