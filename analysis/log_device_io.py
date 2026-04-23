from __future__ import annotations

import argparse
import json
import pathlib
import struct
import time
from dataclasses import dataclass
from typing import Optional

XFER_NAME = {0: "ISO", 1: "INT", 2: "CTRL", 3: "BULK"}


@dataclass
class TailState:
    offset: int = 0
    ready: bool = False
    insert_over: Optional[int] = None
    file_size: int = 0


def parse_usbpcap_record(ts: float, pkt: bytes):
    if len(pkt) < 27:
        return None

    hdr_len = struct.unpack_from("<H", pkt, 0)[0]
    if hdr_len < 27 or hdr_len > len(pkt):
        return None

    bus = struct.unpack_from("<H", pkt, 17)[0]
    dev = struct.unpack_from("<H", pkt, 19)[0]
    ep = pkt[21]
    xfer = pkt[22]
    data_len = struct.unpack_from("<I", pkt, 23)[0]

    direction = "IN" if (ep & 0x80) else "OUT"
    ep_num = ep & 0x7F
    payload = pkt[hdr_len:hdr_len + data_len] if data_len else b""

    return {
        "ts": ts,
        "bus": bus,
        "dev": dev,
        "dir": direction,
        "ep": ep_num,
        "xfer": xfer,
        "xfer_name": XFER_NAME.get(xfer, str(xfer)),
        "payload": payload,
    }


def decode_ctrl_setup(payload: bytes):
    if len(payload) < 8:
        return None
    bm, req, w_val, w_idx, w_len = struct.unpack_from("<BBHHH", payload, 0)
    return {
        "bmRequestType": bm,
        "bRequest": req,
        "wValue": w_val,
        "wIndex": w_idx,
        "wLength": w_len,
    }


def infer_insert_over_from_in_report(payload: bytes):
    if len(payload) < 5:
        return None
    return 1 if (payload[4] & 0x01) else 0


def append_jsonl(path: pathlib.Path, obj: dict):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def ensure_pcap_ready(raw: bytes, state: TailState) -> bool:
    if state.ready:
        return True
    if len(raw) < 24:
        return False

    magic = struct.unpack_from("<I", raw, 0)[0]
    if magic not in (0xA1B2C3D4, 0xD4C3B2A1):
        raise RuntimeError(f"Unsupported pcap magic: {magic:#x}")

    state.offset = 24
    state.ready = True
    state.file_size = len(raw)
    return True


def process_growth(
    pcap_path: pathlib.Path,
    out_jsonl: pathlib.Path,
    state: TailState,
    bus: int,
    dev: int,
    verbose: bool,
):
    raw = pcap_path.read_bytes()
    if not ensure_pcap_ready(raw, state):
        return 0

    # USBPcap writers may recreate/truncate the file when a new capture starts.
    # If that happens, restart parsing from the pcap packet section.
    if len(raw) < state.offset:
        state.offset = 24
        state.insert_over = None

    state.file_size = len(raw)

    processed = 0
    while state.offset + 16 <= len(raw):
        ts_sec, ts_usec, incl_len, _ = struct.unpack_from("<IIII", raw, state.offset)
        if state.offset + 16 + incl_len > len(raw):
            break

        state.offset += 16
        pkt = raw[state.offset:state.offset + incl_len]
        state.offset += incl_len

        rec = parse_usbpcap_record(ts_sec + ts_usec * 1e-6, pkt)
        if not rec:
            continue
        if rec["bus"] != bus or rec["dev"] != dev:
            continue

        payload: bytes = rec["payload"]
        event = {
            "ts": rec["ts"],
            "bus": rec["bus"],
            "dev": rec["dev"],
            "direction": rec["dir"],
            "endpoint": rec["ep"],
            "transfer": rec["xfer_name"],
            "payload_len": len(payload),
            "payload_hex": payload.hex(),
            "insert_over_state": state.insert_over,
            "note": "",
        }

        if rec["dir"] == "IN" and rec["xfer"] == 1:
            io = infer_insert_over_from_in_report(payload)
            if io is not None and io != state.insert_over:
                state.insert_over = io
                event["insert_over_state"] = state.insert_over
                event["note"] = f"INSERT_OVER_EDGE_{'ON' if io else 'OFF'}"

        if rec["dir"] == "OUT" and rec["xfer"] == 2:
            setup = decode_ctrl_setup(payload)
            if setup:
                event["setup"] = {
                    "bmRequestType": f"0x{setup['bmRequestType']:02X}",
                    "bRequest": f"0x{setup['bRequest']:02X}",
                    "wValue": f"0x{setup['wValue']:04X}",
                    "wIndex": f"0x{setup['wIndex']:04X}",
                    "wLength": setup["wLength"],
                }
                if (
                    setup["bmRequestType"],
                    setup["bRequest"],
                    setup["wValue"],
                    setup["wIndex"],
                    setup["wLength"],
                ) == (0x21, 0x09, 0x0200, 0x0000, 64):
                    event["note"] = "HID_SET_REPORT_64"
                    report_body = payload[8:8 + 64] if len(payload) >= 8 + 64 else b""
                    event["report_prefix"] = report_body[:20].hex()
                    if len(report_body) > 14:
                        event["report_b11"] = f"0x{report_body[11]:02X}"
                        event["report_b14"] = f"0x{report_body[14]:02X}"

        append_jsonl(out_jsonl, event)
        processed += 1

        if verbose:
            note = f" {event['note']}" if event["note"] else ""
            print(
                f"[{time.strftime('%H:%M:%S')}] t={event['ts']:.6f} {event['direction']:>3} "
                f"{event['transfer']:<4} ep={event['endpoint']} len={event['payload_len']}{note}"
            )

    return processed


def main():
    parser = argparse.ArgumentParser(description="Follow USBPcap file and log device read/write interactions")
    parser.add_argument("--pcap", default=r"C:\Temp\Workspace\usb233。.pcap", help="USBPcap file path")
    parser.add_argument("--out", default=r"C:\Temp\Workspace\device_io_log.jsonl", help="Output JSONL log file")
    parser.add_argument("--bus", type=int, default=1)
    parser.add_argument("--dev", type=int, default=3)
    parser.add_argument("--interval", type=float, default=0.2, help="Polling interval seconds")
    parser.add_argument("--from-start", action="store_true", help="Process existing pcap from start")
    parser.add_argument("--verbose", action="store_true", help="Print every logged record")
    args = parser.parse_args()

    pcap_path = pathlib.Path(args.pcap)
    out_path = pathlib.Path(args.out)

    if not pcap_path.exists():
        print(f"pcap not found: {pcap_path}")
        return

    out_path.parent.mkdir(parents=True, exist_ok=True)

    state = TailState()
    if not args.from_start:
        raw = pcap_path.read_bytes()
        if len(raw) >= 24:
            state.offset = len(raw)
            state.ready = True
            state.file_size = len(raw)

    print(f"Following pcap: {pcap_path}")
    print(f"Target bus/dev: {args.bus}/{args.dev}")
    print(f"Writing log to: {out_path}")
    print("Press Ctrl+C to stop.")

    total = 0
    try:
        while True:
            n = process_growth(
                pcap_path=pcap_path,
                out_jsonl=out_path,
                state=state,
                bus=args.bus,
                dev=args.dev,
                verbose=args.verbose,
            )
            total += n
            if n == 0 and args.verbose:
                print(f"[{time.strftime('%H:%M:%S')}] waiting for new packets... file_size={state.file_size}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass

    print(f"Stopped. Logged records: {total}")


if __name__ == "__main__":
    main()
