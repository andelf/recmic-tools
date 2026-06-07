"""
实时读取 VID:PID=07B4:0256 的 HID 输入，并尝试解析媒体键 bitmask。

依赖：pywinusb（Windows）
安装：python -m pip install pywinusb
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Dict, Set, Tuple

import pywinusb.hid as hid

TARGET_VID = 0x07B4
TARGET_PID = 0x0256

# Confirmed bit mappings (calibrated 2026-04-22)
BITMAP_HINTS = {
    # byte1 — transport state bits (mutually exclusive)
    (1, 0x01): "STOP",
    (1, 0x02): "PLAYING",
    (1, 0x04): "REW",           # rewind (active during recording)
    (1, 0x08): "FF",            # fast-forward (active during recording)
    (1, 0x10): "RECORDING",
    # byte2 — button / function bits
    (2, 0x04): "NEW",
    (2, 0x08): "FAST_BACKWARD",
    (2, 0x10): "VOL_DOWN",
    (2, 0x20): "VOL_UP",
    (2, 0x40): "F1",
    (2, 0x80): "F2",
    # byte3 — function + mouse buttons
    (3, 0x08): "F3",
    (3, 0x10): "MOUSE_LEFT",
    (3, 0x20): "MOUSE_RIGHT",
    (3, 0x40): "F4",
    (3, 0x80): "BACK",
    # byte4 — toggle bits
    (4, 0x01): "INSERT_OVER",
}


def bytes_to_active_bits(payload: bytes) -> Set[Tuple[int, int]]:
    active = set()
    for idx, b in enumerate(payload):
        if b == 0:
            continue
        for n in range(8):
            mask = 1 << n
            if b & mask:
                active.add((idx, mask))
    return active


def key_name(bit: Tuple[int, int]) -> str:
    return BITMAP_HINTS.get(bit, f"BYTE{bit[0]}_BIT0x{bit[1]:02X}")


def parse_media_report(data: bytes) -> Dict[str, object]:
    # pywinusb raw_data 第0字节通常是 ReportID
    rid = data[0] if data else 0

    # 抓包对应报告多为 64B 且 ReportID=0x02
    is_media_candidate = rid in (0x02, 0x06, 0x0A)

    active = bytes_to_active_bits(data)
    return {
        "report_id": rid,
        "is_media_candidate": is_media_candidate,
        "active_bits": active,
        "raw_hex": data.hex(),
    }


def bit_to_str(bit: Tuple[int, int]) -> str:
    idx, mask = bit
    return f"{idx}:0x{mask:02X}"


def str_to_bit(s: str) -> Tuple[int, int]:
    idx_s, mask_s = s.split(":")
    return int(idx_s), int(mask_s, 16)


def create_handler(state: Dict[str, object]):
    def on_raw(data):
        if not data:
            return
        payload = bytes(data)
        parsed = parse_media_report(payload)

        if not parsed["is_media_candidate"]:
            return

        state["report_id"] = parsed["report_id"]
        state["active_bits"] = parsed["active_bits"]
        state["raw_hex"] = parsed["raw_hex"]

    return on_raw


def open_target_devices(vid: int, pid: int, on_raw):
    devices = hid.HidDeviceFilter(vendor_id=vid, product_id=pid).get_devices()
    if not devices:
        return []

    opened = []
    for dev in devices:
        dev.open()
        dev.set_raw_data_handler(on_raw)
        opened.append(dev)
        print(f"[+] open: {dev.vendor_name} / {dev.product_name}")
    return opened


def run_monitor(vid: int, pid: int, duration: int):
    state: Dict[str, object] = {
        "report_id": 0,
        "active_bits": set(),
        "raw_hex": "",
    }

    devices = hid.HidDeviceFilter(vendor_id=vid, product_id=pid).get_devices()
    if not devices:
        print(f"[!] 未找到设备 VID:PID={vid:04X}:{pid:04X}")
        print("    请确认设备已连接，且当前用户对 HID 设备有访问权限。")
        return

    print(f"[*] 找到设备 {len(devices)} 个，开始监听 {duration}s")

    last_active: Set[Tuple[int, int]] = set()

    def on_raw(data):
        nonlocal last_active
        if not data:
            return
        payload = bytes(data)
        parsed = parse_media_report(payload)
        print(f"[RAW] report_id=0x{int(parsed['report_id']):02X} len={len(payload)} raw={parsed['raw_hex']}")

        rid = parsed["report_id"]
        active_bits: Set[Tuple[int, int]] = parsed["active_bits"]
        pressed = active_bits - last_active
        released = last_active - active_bits

        # 只输出媒体候选报告，避免鼠标/其他接口刷屏
        if parsed["is_media_candidate"]:
            print(f"\n[REPORT] ID=0x{rid:02X} len={len(payload)}")
            print(f"  raw: {parsed['raw_hex']}")

            if pressed:
                names = ", ".join(sorted(key_name(b) for b in pressed))
                print(f"  + press   : {names}")
            if released:
                names = ", ".join(sorted(key_name(b) for b in released))
                print(f"  - release : {names}")
            if not pressed and not released:
                print("  = no state change")

        last_active = active_bits

        state["report_id"] = rid
        state["active_bits"] = active_bits
        state["raw_hex"] = parsed["raw_hex"]

    opened = []
    try:
        for dev in devices:
            dev.open()
            dev.set_raw_data_handler(on_raw)
            opened.append(dev)
            print(f"[+] open: {dev.vendor_name} / {dev.product_name}")

        t0 = time.time()
        while time.time() - t0 < duration:
            time.sleep(0.05)

    finally:
        for dev in opened:
            try:
                dev.close()
            except Exception:
                pass
        print("\n[*] 监听结束")


def run_calibration(vid: int, pid: int, keys: str, capture_window: float, mapping_out: str):
    key_list = [k.strip() for k in keys.split(",") if k.strip()]
    if not key_list:
        print("[!] 没有可标定的键名，请通过 --keys 提供。")
        return

    state: Dict[str, object] = {
        "report_id": 0,
        "active_bits": set(),
        "raw_hex": "",
    }
    on_raw = create_handler(state)

    opened = open_target_devices(vid, pid, on_raw)
    if not opened:
        print(f"[!] 未找到设备 VID:PID={vid:04X}:{pid:04X}")
        print("    请确认设备已连接，且当前用户对 HID 设备有访问权限。")
        return

    result: Dict[str, Dict[str, object]] = {}

    print("\n[*] 进入自动标定模式")
    print("    操作方法：每次按提示只按一个键（按下后松开），其余键不要碰。")

    try:
        for label in key_list:
            input(f"\n[STEP] 准备标定 [{label}]，按回车后在 {capture_window:.1f}s 内按下并松开该键...")

            observed: Set[Tuple[int, int]] = set()
            prev_active: Set[Tuple[int, int]] = set()
            saw_press = False

            start = time.time()
            while time.time() - start < capture_window:
                current_active = set(state["active_bits"])
                pressed = current_active - prev_active
                if pressed:
                    observed |= pressed
                    saw_press = True

                # 一次完整按下释放后结束当前步骤
                if saw_press and not current_active:
                    break

                prev_active = current_active
                time.sleep(0.02)

            if not observed:
                print(f"  [WARN] 未捕获到 [{label}] 的按键变化")
                result[label] = {"bits": [], "report_id": state["report_id"]}
                continue

            bit_names = [bit_to_str(b) for b in sorted(observed)]
            mapped_names = [key_name(b) for b in sorted(observed)]
            print(f"  [OK] {label} -> bits={bit_names} report_id=0x{int(state['report_id']):02X}")

            result[label] = {
                "bits": bit_names,
                "report_id": int(state["report_id"]),
                "raw_hex_last": str(state["raw_hex"]),
                "hint_names": mapped_names,
            }

    finally:
        for dev in opened:
            try:
                dev.close()
            except Exception:
                pass

    with open(mapping_out, "w", encoding="utf-8") as f:
        json.dump(
            {
                "vid": f"0x{vid:04X}",
                "pid": f"0x{pid:04X}",
                "generated_at": int(time.time()),
                "mapping": result,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(f"\n[*] 标定完成，结果已保存: {mapping_out}")
    print("[*] 映射预览:")
    for label, info in result.items():
        print(f"  - {label}: {info['bits']}")


def main():
    parser = argparse.ArgumentParser(description="Monitor media key reports from HID device")
    parser.add_argument("--vid", type=lambda x: int(x, 0), default=TARGET_VID, help="Vendor ID, e.g. 0x07B4")
    parser.add_argument("--pid", type=lambda x: int(x, 0), default=TARGET_PID, help="Product ID, e.g. 0x0256")
    parser.add_argument("--duration", type=int, default=30, help="Monitor duration in seconds")
    parser.add_argument("--calibrate", action="store_true", help="Enable interactive calibration mode")
    parser.add_argument(
        "--keys",
        default="play_pause,vol_up,vol_down,mute,next_track,prev_track",
        help="Comma-separated key labels for calibration",
    )
    parser.add_argument("--capture-window", type=float, default=5.0, help="Capture seconds per key in calibration")
    parser.add_argument("--mapping-out", default="media_key_mapping.json", help="Output JSON path for calibration result")
    args = parser.parse_args()

    if args.calibrate:
        run_calibration(args.vid, args.pid, args.keys, args.capture_window, args.mapping_out)
    else:
        run_monitor(args.vid, args.pid, args.duration)


if __name__ == "__main__":
    main()
