from __future__ import annotations

import argparse
import time
from typing import Dict, List

import pywinusb.hid as hid

TARGET_VID = 0x07B4
TARGET_PID = 0x0256

# Captured from HID SET_REPORT(Output, 64B) around INSERT_OVER transitions.
# Layout: 64-byte report body (without setup header).
FRAME_BASE = bytes.fromhex(
    "e107000000000000000000030200000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
)
FRAME_ON_A = bytes.fromhex(
    "e107000000000000000000030203020000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
)
FRAME_ON_B = bytes.fromhex(
    "e107000000000000000000030203000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
)
FRAME_ALT = bytes.fromhex(
    "e107000000000000000000020000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
)
FRAME_ZERO = bytes.fromhex(
    "e000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
)

SEQUENCES: Dict[str, List[bytes]] = {
    # Most common ON-edge sequences seen in usb13/usb233 captures.
    "insert-on-a": [FRAME_BASE, FRAME_ON_A],
    "insert-on-b": [FRAME_BASE, FRAME_ON_B],
    # Common OFF-edge patterns seen shortly after the OFF report arrives.
    "insert-off-b": [FRAME_BASE, FRAME_ON_B],
    "insert-off-a": [FRAME_BASE, FRAME_ON_A],
    # Additional less-common frames seen around later transitions.
    "alt-only": [FRAME_ALT],
    "zero-only": [FRAME_ZERO],
    "alt-zero": [FRAME_ALT, FRAME_ZERO],
    # Probe sequences for explicit transitions.
    "pulse-a": [FRAME_BASE, FRAME_ON_A, FRAME_BASE],
    "pulse-b": [FRAME_BASE, FRAME_ON_B, FRAME_BASE],
    "all-four": [FRAME_BASE, FRAME_ON_A, FRAME_ALT, FRAME_ZERO],
}


def pick_report(dev: hid.HidDevice):
    outs = dev.find_output_reports()
    feats = dev.find_feature_reports()
    if outs:
        return outs[0], "output"
    if feats:
        return feats[0], "feature"
    raise RuntimeError("No output/feature reports found on target device")


def show_reports(dev: hid.HidDevice) -> None:
    outs = dev.find_output_reports()
    feats = dev.find_feature_reports()

    print(f"Output reports: {len(outs)}")
    for i, r in enumerate(outs):
        raw = r.get_raw_data()
        print(f"  OUT[{i}] report_id={raw[0]} raw_len={len(raw)}")

    print(f"Feature reports: {len(feats)}")
    for i, r in enumerate(feats):
        raw = r.get_raw_data()
        print(f"  FEAT[{i}] report_id={raw[0]} raw_len={len(raw)}")


def write_frame(report, frame64: bytes, dry_run: bool) -> None:
    raw = list(report.get_raw_data())
    report_id = raw[0] if raw else 0

    if len(frame64) != 64:
        raise ValueError(f"frame length must be 64 bytes, got {len(frame64)}")

    if len(raw) >= 65:
        raw[1:65] = frame64
    elif len(raw) == 64:
        raw[0:64] = frame64
    else:
        raise RuntimeError(f"unsupported report raw length: {len(raw)}")

    if dry_run:
        print(f"[DRY] report_id={report_id} send={bytes(raw).hex()}")
        return

    report.set_raw_data(raw)
    report.send()
    print(f"[SEND] report_id={report_id} frame={frame64[:20].hex()}...")


def build_sequence(name: str) -> List[bytes]:
    try:
        return SEQUENCES[name]
    except KeyError as exc:
        raise ValueError(f"unsupported sequence: {name}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay candidate LED SET_REPORT frames to OLYMPUS device")
    parser.add_argument("--vid", type=lambda x: int(x, 0), default=TARGET_VID)
    parser.add_argument("--pid", type=lambda x: int(x, 0), default=TARGET_PID)
    parser.add_argument(
        "--sequence",
        choices=sorted(SEQUENCES.keys()),
        default="insert-on-a",
        help="Exact captured frame sequence to replay",
    )
    parser.add_argument("--repeat", type=int, default=1, help="How many times to send the selected sequence")
    parser.add_argument("--gap-ms", type=int, default=12, help="Inter-frame gap in milliseconds")
    parser.add_argument("--discover", action="store_true", help="Only print report capabilities")
    parser.add_argument("--dry-run", action="store_true", help="Print frames but do not send")
    parser.add_argument("--execute", action="store_true", help="Actually send to device")
    args = parser.parse_args()

    if not args.dry_run and not args.execute:
        print("Safety check: add --execute to actually send, or use --dry-run.")
        return

    devs = hid.HidDeviceFilter(vendor_id=args.vid, product_id=args.pid).get_devices()
    if not devs:
        print(f"Device not found VID:PID={args.vid:04X}:{args.pid:04X}")
        return

    dev = devs[0]
    dev.open()
    try:
        print(f"Opened: {dev.vendor_name} / {dev.product_name}")
        show_reports(dev)
        if args.discover:
            return

        report, kind = pick_report(dev)
        print(f"Using {kind} report object")

        seq = build_sequence(args.sequence)
        print(f"Sequence={args.sequence} frames={len(seq)} repeat={args.repeat}")

        for n in range(args.repeat):
            print(f"Cycle {n + 1}/{args.repeat}")
            for frame in seq:
                write_frame(report, frame, dry_run=bool(args.dry_run and not args.execute))
                time.sleep(max(args.gap_ms, 0) / 1000.0)

        print("Done.")
    finally:
        dev.close()


if __name__ == "__main__":
    main()
