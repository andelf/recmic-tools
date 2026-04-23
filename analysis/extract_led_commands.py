import struct
import pathlib
from collections import Counter

PCAP_PATH = pathlib.Path(r"C:\Temp\Workspace\usb233。.pcap")
TARGET_BUS = 1
TARGET_DEV = 3


def parse_pcap_usbpcap(path: pathlib.Path):
    raw = path.read_bytes()
    if len(raw) < 24:
        raise RuntimeError("pcap too small")

    magic = struct.unpack_from("<I", raw, 0)[0]
    if magic not in (0xA1B2C3D4, 0xD4C3B2A1):
        raise RuntimeError(f"unsupported pcap magic: {magic:#x}")

    off = 24
    out = []
    while off + 16 <= len(raw):
        ts_sec, ts_usec, incl_len, _ = struct.unpack_from("<IIII", raw, off)
        off += 16
        pkt = raw[off:off + incl_len]
        off += incl_len

        if len(pkt) < 27:
            continue

        hdr_len = struct.unpack_from("<H", pkt, 0)[0]
        if hdr_len < 27 or hdr_len > len(pkt):
            continue

        bus = struct.unpack_from("<H", pkt, 17)[0]
        dev = struct.unpack_from("<H", pkt, 19)[0]
        ep = pkt[21]
        xfer = pkt[22]
        data_len = struct.unpack_from("<I", pkt, 23)[0]

        direction = "IN" if (ep & 0x80) else "OUT"
        ep_num = ep & 0x7F
        body = pkt[hdr_len:]
        payload = body[:data_len] if data_len > 0 else b""

        out.append(
            {
                "ts": ts_sec + ts_usec * 1e-6,
                "bus": bus,
                "dev": dev,
                "xfer": xfer,
                "dir": direction,
                "ep": ep_num,
                "payload": payload,
            }
        )
    return out


def detect_insert_over_edges(records):
    edges = []
    last_state = None

    for r in records:
        if not (r["bus"] == TARGET_BUS and r["dev"] == TARGET_DEV):
            continue
        if not (r["xfer"] == 1 and r["dir"] == "IN"):
            continue

        p = r["payload"]
        if len(p) < 5:
            continue

        state = 1 if (p[4] & 0x01) else 0
        if last_state is None:
            last_state = state
            continue

        if state != last_state:
            edges.append(
                {
                    "ts": r["ts"],
                    "state": state,
                    "report_prefix": p[:8].hex(),
                }
            )
            last_state = state

    return edges


def parse_setup_from_ctrl_payload(payload: bytes):
    if len(payload) < 8:
        return None
    bm, req, w_val, w_idx, w_len = struct.unpack_from("<BBHHH", payload, 0)
    return bm, req, w_val, w_idx, w_len, payload[8:]


def main():
    records = parse_pcap_usbpcap(PCAP_PATH)
    dev_records = [r for r in records if r["bus"] == TARGET_BUS and r["dev"] == TARGET_DEV]

    edges = detect_insert_over_edges(records)
    print(f"records={len(records)} dev_records={len(dev_records)} insert_over_edges={len(edges)}")

    window = 0.20
    signature_count = Counter()

    for i, e in enumerate(edges, 1):
        ts = e["ts"]
        state = "ON" if e["state"] else "OFF"
        nearby = []

        for r in dev_records:
            if r["dir"] != "OUT" or r["xfer"] != 2:
                continue
            if abs(r["ts"] - ts) > window:
                continue

            setup = parse_setup_from_ctrl_payload(r["payload"])
            if not setup:
                continue

            bm, req, w_val, w_idx, w_len, report = setup
            key = (bm, req, w_val, w_idx, w_len, report[:8])
            signature_count[key] += 1

            nearby.append(
                {
                    "dt": r["ts"] - ts,
                    "bm": bm,
                    "req": req,
                    "w_val": w_val,
                    "w_idx": w_idx,
                    "w_len": w_len,
                    "report_prefix": report[:16].hex(),
                }
            )

        print(f"\nedge {i:02d} {state} t={ts:.6f} in={e['report_prefix']} nearby_ctrl_out={len(nearby)}")
        for n in nearby[:6]:
            print(
                "  dt={:+.4f}s bm=0x{:02X} req=0x{:02X} wVal=0x{:04X} wIdx=0x{:04X} wLen={} report={}".format(
                    n["dt"], n["bm"], n["req"], n["w_val"], n["w_idx"], n["w_len"], n["report_prefix"]
                )
            )

    print("\nTop signatures near INSERT_OVER edges:")
    for (bm, req, w_val, w_idx, w_len, prefix), c in signature_count.most_common(10):
        print(
            f"  count={c:2d} bm=0x{bm:02X} req=0x{req:02X} wVal=0x{w_val:04X} "
            f"wIdx=0x{w_idx:04X} wLen={w_len} report_prefix={prefix.hex()}"
        )


if __name__ == "__main__":
    main()
