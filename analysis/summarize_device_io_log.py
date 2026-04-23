from __future__ import annotations

import argparse
import json
import pathlib
from collections import Counter


def load_jsonl(path: pathlib.Path):
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        yield json.loads(line)


def main():
    parser = argparse.ArgumentParser(description="Summarize device_io_log.jsonl")
    parser.add_argument("--in", dest="in_path", default=r"C:\Temp\Workspace\device_io_log.jsonl")
    args = parser.parse_args()

    p = pathlib.Path(args.in_path)
    if not p.exists():
        print(f"log not found: {p}")
        return

    rows = list(load_jsonl(p))
    if not rows:
        print("log is empty")
        return

    print(f"rows={len(rows)}")

    by_dir = Counter(r["direction"] for r in rows)
    by_xfer = Counter((r["direction"], r["transfer"]) for r in rows)
    notes = Counter(r.get("note", "") for r in rows if r.get("note"))

    print("\nDirection counts:")
    for k, v in by_dir.items():
        print(f"  {k}: {v}")

    print("\nDirection/transfer counts:")
    for (d, t), v in by_xfer.most_common():
        print(f"  {d} {t}: {v}")

    print("\nNotable notes:")
    for k, v in notes.most_common():
        print(f"  {k}: {v}")

    print("\nHID_SET_REPORT_64 samples (first 10):")
    c = 0
    for r in rows:
        if r.get("note") != "HID_SET_REPORT_64":
            continue
        print(
            f"  t={r['ts']:.6f} state={r.get('insert_over_state')} "
            f"b11={r.get('report_b11')} b14={r.get('report_b14')} prefix={r.get('report_prefix')}"
        )
        c += 1
        if c >= 10:
            break

    print("\nINSERT_OVER edges:")
    for r in rows:
        note = r.get("note", "")
        if note.startswith("INSERT_OVER_EDGE"):
            print(f"  t={r['ts']:.6f} {note} raw={r.get('payload_hex', '')[:32]}...")


if __name__ == "__main__":
    main()
