import struct
import pathlib
import collections

PCAP_PATH = pathlib.Path(r"C:\Temp\Workspace\usb233。.pcap")
TARGET_BUS = 1
TARGET_DEV = 3


def parse_records(path: pathlib.Path):
    raw = path.read_bytes()
    off = 24
    recs = []

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

        payload = pkt[hdr_len:hdr_len + data_len] if data_len else b""

        recs.append(
            {
                "ts": ts_sec + ts_usec * 1e-6,
                "bus": bus,
                "dev": dev,
                "xfer": xfer,
                "dir": "IN" if (ep & 0x80) else "OUT",
                "ep": ep & 0x7F,
                "payload": payload,
            }
        )

    return recs


def insert_over_edges(dev_recs):
    edges = []
    last = None

    for r in dev_recs:
        if r["dir"] != "IN" or r["xfer"] != 1:
            continue
        p = r["payload"]
        if len(p) < 5:
            continue

        state = 1 if (p[4] & 0x01) else 0
        if last is None:
            last = state
            continue
        if state != last:
            edges.append((r["ts"], state, p[:8].hex()))
            last = state

    return edges


def state_at(ts: float, edges):
    state = 0
    for t, s, _ in edges:
        if t > ts:
            break
        state = s
    return state


def main():
    recs = parse_records(PCAP_PATH)
    dev = [r for r in recs if r["bus"] == TARGET_BUS and r["dev"] == TARGET_DEV]
    edges = insert_over_edges(dev)

    print(f"records={len(recs)} dev_records={len(dev)} edges={len(edges)}")

    # Collect HID SET_REPORT payloads and classify by INSERT_OVER state
    on = []
    off = []
    all_reports = []

    for r in dev:
        if r["dir"] != "OUT" or r["xfer"] != 2:
            continue
        p = r["payload"]
        if len(p) < 8 + 64:
            continue

        bm, req, w_val, w_idx, w_len = struct.unpack_from("<BBHHH", p, 0)
        if (bm, req, w_val, w_idx, w_len) != (0x21, 0x09, 0x0200, 0x0000, 64):
            continue

        report = p[8:8 + 64]
        all_reports.append((r["ts"], report))
        if state_at(r["ts"], edges) == 1:
            on.append(report)
        else:
            off.append(report)

    print("\nCore command:")
    print("  HID SET_REPORT(Output)")
    print("  bmRequestType=0x21 bRequest=0x09 wValue=0x0200 wIndex=0x0000 wLength=64")

    print(f"\nSET_REPORT count during INSERT_OVER ON : {len(on)}")
    print(f"SET_REPORT count during INSERT_OVER OFF: {len(off)}")

    # Diff bytes in first 24 bytes
    print("\nChanged bytes between ON/OFF:")
    print(f"  {'idx':>4}  {'OFF values':>30}  {'ON values':>30}")
    print("  " + "-" * 72)

    for i in range(24):
        off_vals = sorted(set(x[i] for x in off)) if off else []
        on_vals = sorted(set(x[i] for x in on)) if on else []
        if off_vals == on_vals:
            continue
        off_s = "{" + ",".join(f"0x{v:02X}" for v in off_vals) + "}"
        on_s = "{" + ",".join(f"0x{v:02X}" for v in on_vals) + "}"
        print(f"  [{i:2d}]  {off_s:>30}  {on_s:>30}")

    print("\nTop report prefixes:")
    for prefix, c in collections.Counter(x[:20] for _, x in all_reports).most_common(10):
        print(f"  count={c:2d}  {prefix.hex()}")

    print("\nEdge-aligned packet view (+/-0.02s):")
    for idx, (ts, s, report_in) in enumerate(edges[:10], 1):
        state_name = "ON" if s else "OFF"
        near = [
            (t, rep)
            for t, rep in all_reports
            if abs(t - ts) <= 0.02
        ]
        print(f"  edge {idx:02d} {state_name} t={ts:.6f} in={report_in} out={len(near)}")
        for t, rep in near[:4]:
            print(f"    dt={t-ts:+.4f}s rep={rep[:20].hex()}")


if __name__ == "__main__":
    main()
