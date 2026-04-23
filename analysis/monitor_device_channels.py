from __future__ import annotations

import argparse
import pathlib
import struct
import time
from collections import Counter
from typing import Dict, Iterable, List, Optional, Set, Tuple

import pywinusb.hid as hid

from read_media_keys import BITMAP_HINTS, TARGET_PID, TARGET_VID


Bit = Tuple[int, int]


def now_ts() -> str:
    return time.strftime("%H:%M:%S")


def bits_from_payload(payload: bytes) -> Set[Bit]:
    out: Set[Bit] = set()
    for i, b in enumerate(payload):
        if b == 0:
            continue
        for n in range(8):
            m = 1 << n
            if b & m:
                out.add((i, m))
    return out


def bit_name(bit: Bit) -> str:
    return BITMAP_HINTS.get(bit, f"BYTE{bit[0]}_0x{bit[1]:02X}")


def format_names(bits: Iterable[Bit]) -> str:
    names = [bit_name(x) for x in sorted(bits)]
    return ", ".join(names) if names else "-"


def run_hid_monitor(vid: int, pid: int) -> None:
    devices = hid.HidDeviceFilter(vendor_id=vid, product_id=pid).get_devices()
    if not devices:
        print(f"[{now_ts()}] [HID] device not found VID:PID={vid:04X}:{pid:04X}")
        return

    print(f"[{now_ts()}] [HID] monitoring {len(devices)} device(s) VID:PID={vid:04X}:{pid:04X}")

    state: Dict[int, Set[Bit]] = {}

    def make_handler(dev_idx: int):
        def on_raw(data):
            if not data:
                return
            payload = bytes(data)
            rid = payload[0] if payload else 0
            active = bits_from_payload(payload)
            prev = state.get(dev_idx, set())
            pressed = active - prev
            released = prev - active
            state[dev_idx] = active

            p4 = payload[4] if len(payload) > 4 else 0
            insert_over = 1 if (p4 & 0x01) else 0

            print(
                f"[{now_ts()}] [IN ] dev#{dev_idx} rid=0x{rid:02X} len={len(payload)} "
                f"insert_over={insert_over} raw={payload.hex()}"
            )

            if pressed or released:
                if pressed:
                    print(f"[{now_ts()}] [IN ]   + {format_names(pressed)}")
                if released:
                    print(f"[{now_ts()}] [IN ]   - {format_names(released)}")

        return on_raw

    opened = []
    try:
        for i, d in enumerate(devices):
            d.open()
            d.set_raw_data_handler(make_handler(i))
            opened.append(d)
            print(f"[{now_ts()}] [HID] open dev#{i}: {d.vendor_name} / {d.product_name}")

        while True:
            time.sleep(0.05)
    except KeyboardInterrupt:
        print(f"[{now_ts()}] [HID] stopped")
    finally:
        for d in opened:
            try:
                d.close()
            except Exception:
                pass


class PcapFollower:
    def __init__(self, pcap_path: pathlib.Path, bus: int, dev: int):
        self.path = pcap_path
        self.bus = bus
        self.dev = dev
        self.offset = 0
        self.ready = False
        self.last_insert_over: Optional[int] = None

    def _ensure_header(self, raw: bytes) -> bool:
        if len(raw) < 24:
            return False
        magic = struct.unpack_from("<I", raw, 0)[0]
        if magic not in (0xA1B2C3D4, 0xD4C3B2A1):
            raise RuntimeError(f"unsupported pcap magic: {magic:#x}")
        self.offset = 24
        self.ready = True
        return True

    def poll(self) -> None:
        raw = self.path.read_bytes()

        if not self.ready:
            if not self._ensure_header(raw):
                return

        while self.offset + 16 <= len(raw):
            ts_sec, ts_usec, incl_len, _ = struct.unpack_from("<IIII", raw, self.offset)
            if self.offset + 16 + incl_len > len(raw):
                break

            self.offset += 16
            pkt = raw[self.offset:self.offset + incl_len]
            self.offset += incl_len

            self._handle_record(ts_sec + ts_usec * 1e-6, pkt)

    def _handle_record(self, ts: float, pkt: bytes) -> None:
        if len(pkt) < 27:
            return

        hdr_len = struct.unpack_from("<H", pkt, 0)[0]
        if hdr_len < 27 or hdr_len > len(pkt):
            return

        bus = struct.unpack_from("<H", pkt, 17)[0]
        dev = struct.unpack_from("<H", pkt, 19)[0]
        if bus != self.bus or dev != self.dev:
            return

        ep = pkt[21]
        xfer = pkt[22]
        data_len = struct.unpack_from("<I", pkt, 23)[0]
        payload = pkt[hdr_len:hdr_len + data_len] if data_len else b""

        direction = "IN" if (ep & 0x80) else "OUT"
        ep_num = ep & 0x7F

        if direction == "IN" and xfer == 1 and len(payload) >= 5:
            rid = payload[0]
            insert_over = 1 if (payload[4] & 0x01) else 0
            if self.last_insert_over is None or insert_over != self.last_insert_over:
                edge = "ON" if insert_over else "OFF"
                print(
                    f"[{now_ts()}] [PCAP-IN] t={ts:.6f} ep={ep_num} rid=0x{rid:02X} "
                    f"INSERT_OVER->{edge} raw={payload[:16].hex()}"
                )
                self.last_insert_over = insert_over

        # HID SET_REPORT(Output): bm=0x21 req=0x09 wValue=0x0200 wIndex=0x0000 wLen=64
        if direction == "OUT" and xfer == 2 and len(payload) >= 8 + 64:
            bm, req, w_val, w_idx, w_len = struct.unpack_from("<BBHHH", payload, 0)
            if (bm, req, w_val, w_idx, w_len) != (0x21, 0x09, 0x0200, 0x0000, 64):
                return
            rep = payload[8:8 + 64]
            b11 = rep[11]
            b14 = rep[14]
            led_guess = "ON?" if (b14 & 0x02) else "OFF?"
            print(
                f"[{now_ts()}] [PCAP-OUT] t={ts:.6f} ep={ep_num} SET_REPORT "
                f"b11=0x{b11:02X} b14=0x{b14:02X} led={led_guess} rep={rep[:20].hex()}"
            )


def run_pcap_follow(path: pathlib.Path, bus: int, dev: int, interval: float) -> None:
    if not path.exists():
        print(f"[{now_ts()}] [PCAP] file not found: {path}")
        return

    follower = PcapFollower(path, bus, dev)
    print(f"[{now_ts()}] [PCAP] following {path} bus={bus} dev={dev}")
    try:
        while True:
            follower.poll()
            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"[{now_ts()}] [PCAP] stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor HID input channel and pcap output commands")
    parser.add_argument("--mode", choices=["hid", "pcap", "both"], default="both")
    parser.add_argument("--vid", type=lambda x: int(x, 0), default=TARGET_VID)
    parser.add_argument("--pid", type=lambda x: int(x, 0), default=TARGET_PID)
    parser.add_argument("--pcap", default=r"C:\Temp\Workspace\usb233。.pcap")
    parser.add_argument("--bus", type=int, default=1)
    parser.add_argument("--dev", type=int, default=3)
    parser.add_argument("--interval", type=float, default=0.2)
    args = parser.parse_args()

    if args.mode == "hid":
        run_hid_monitor(args.vid, args.pid)
        return

    if args.mode == "pcap":
        run_pcap_follow(pathlib.Path(args.pcap), args.bus, args.dev, args.interval)
        return

    # both: run HID monitor, then pcap follower in same process loop by lightweight multiplexing
    devices = hid.HidDeviceFilter(vendor_id=args.vid, product_id=args.pid).get_devices()
    if not devices:
        print(f"[{now_ts()}] [HID] device not found VID:PID={args.vid:04X}:{args.pid:04X}")
    else:
        print(f"[{now_ts()}] [HID] monitoring {len(devices)} device(s) VID:PID={args.vid:04X}:{args.pid:04X}")

    state: Dict[int, Set[Bit]] = {}

    def make_handler(dev_idx: int):
        def on_raw(data):
            if not data:
                return
            payload = bytes(data)
            rid = payload[0] if payload else 0
            active = bits_from_payload(payload)
            prev = state.get(dev_idx, set())
            pressed = active - prev
            released = prev - active
            state[dev_idx] = active
            insert_over = 1 if (len(payload) > 4 and (payload[4] & 0x01)) else 0

            print(
                f"[{now_ts()}] [IN ] dev#{dev_idx} rid=0x{rid:02X} len={len(payload)} "
                f"insert_over={insert_over} raw={payload.hex()}"
            )
            if pressed or released:
                if pressed:
                    print(f"[{now_ts()}] [IN ]   + {format_names(pressed)}")
                if released:
                    print(f"[{now_ts()}] [IN ]   - {format_names(released)}")

        return on_raw

    opened = []
    for i, d in enumerate(devices):
        d.open()
        d.set_raw_data_handler(make_handler(i))
        opened.append(d)
        print(f"[{now_ts()}] [HID] open dev#{i}: {d.vendor_name} / {d.product_name}")

    follower = PcapFollower(pathlib.Path(args.pcap), args.bus, args.dev)
    print(f"[{now_ts()}] [PCAP] following {args.pcap} bus={args.bus} dev={args.dev}")

    try:
        while True:
            follower.poll()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"[{now_ts()}] stopped")
    finally:
        for d in opened:
            try:
                d.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
