"""
USB PCAP Analyzer for USBPcap / DLT=249 (LINKTYPE_USB_LINUX / Windows USBPcap)
Determined by manual byte inspection: header is 28 bytes (USBPcap format).

USBPcap header layout (hdrLen bytes, typically 27-28):
  0-1:   USHORT  headerLen
  2-9:   UINT64  irpId
  10-13: USBD_STATUS status
  14-15: USHORT  function
  16:    UCHAR   info   (bit0: 0=OUT,1=IN at URB level)
  17-18: USHORT  bus
  19-20: USHORT  device
  21:    UCHAR   endpoint (bit7: direction)
  22:    UCHAR   transfer (0=ISO,1=INT,2=CTRL,3=BULK)
  23-26: UINT32  dataLength
  (if CTRL: next 8 bytes after header = setup packet if function==9/11,
            or just data)
"""

import struct
import collections
import re
import pathlib

PCAP_FILE = r"c:\Temp\Workspace\usb-1.3.2.pcap"

XFER = {0: "ISO", 1: "INT", 2: "CTRL", 3: "BULK"}
SETUP_REQ = {
    0x00: "GET_STATUS",
    0x01: "CLEAR_FEATURE",
    0x03: "SET_FEATURE",
    0x05: "SET_ADDRESS",
    0x06: "GET_DESCRIPTOR",
    0x07: "SET_DESCRIPTOR",
    0x08: "GET_CONFIGURATION",
    0x09: "SET_CONFIGURATION",
    0x0A: "GET_INTERFACE",
    0x0B: "SET_INTERFACE",
}
DESC_TYPE = {
    1: "DEVICE", 2: "CONFIGURATION", 3: "STRING",
    4: "INTERFACE", 5: "ENDPOINT", 33: "HID", 34: "HID_REPORT"
}
USBPCAP_FUNC = {
    0: "SELECT_CONFIGURATION",
    1: "SELECT_INTERFACE",
    9: "CONTROL_TRANSFER",
    11: "GET_DESCRIPTOR",
    13: "CLASS_INTERFACE",
    14: "CLASS_ENDPOINT",
    31: "BULK_OR_INTERRUPT_TRANSFER",
    34: "ISOCH_TRANSFER",
}


def read_pcap_packets(path: str):
    raw = pathlib.Path(path).read_bytes()
    magic, vmaj, vmin, tz, acc, snaplen, dlt = struct.unpack_from("<IHHiIII", raw, 0)
    print(f"[PCAP] magic=0x{magic:08X}  version={vmaj}.{vmin}  DLT={dlt}  snaplen={snaplen}")
    off = 24
    packets = []
    while off + 16 <= len(raw):
        ts_sec, ts_usec, incl_len, orig_len = struct.unpack_from("<IIII", raw, off)
        off += 16
        pkt = raw[off:off + incl_len]
        off += incl_len
        packets.append(pkt)
    return packets


def parse_usbpcap(pkt: bytes):
    if len(pkt) < 27:
        return None
    hdrLen = struct.unpack_from("<H", pkt, 0)[0]
    if hdrLen < 27 or hdrLen > len(pkt):
        return None
    irpId = struct.unpack_from("<Q", pkt, 2)[0]
    status = struct.unpack_from("<I", pkt, 10)[0]
    func = struct.unpack_from("<H", pkt, 14)[0]
    info = pkt[16]
    bus = struct.unpack_from("<H", pkt, 17)[0]
    device = struct.unpack_from("<H", pkt, 19)[0]
    endpoint = pkt[21]
    transfer = pkt[22]
    data_len = struct.unpack_from("<I", pkt, 23)[0]
    direction = "IN" if (endpoint & 0x80) else "OUT"
    ep_num = endpoint & 0x7F
    after_hdr = pkt[hdrLen:]

    setup = None
    payload = b""

    if transfer == 2:  # CTRL
        if len(after_hdr) == 8:
            # This looks like a setup packet (8 bytes, bmRequestType first)
            bmRT = after_hdr[0]
            bReq = after_hdr[1]
            # Heuristic: valid setup packet has known bmRequestType patterns
            if bReq in SETUP_REQ or (bmRT & 0x60) in (0x00, 0x20, 0x40):
                setup = after_hdr
                payload = b""
        else:
            payload = after_hdr[:data_len]
    else:
        payload = after_hdr[:data_len]

    return {
        "irpId": irpId,
        "status": status,
        "func": func,
        "func_name": USBPCAP_FUNC.get(func, f"func_{func}"),
        "info": info,
        "bus": bus,
        "device": device,
        "ep_num": ep_num,
        "direction": direction,
        "transfer": transfer,
        "xfer_name": XFER.get(transfer, str(transfer)),
        "data_len": data_len,
        "setup": setup,
        "payload": payload,
    }


def decode_setup(s: bytes):
    if len(s) < 8:
        return None
    bmRT, bReq, wVal, wIdx, wLen = struct.unpack_from("<BBHHH", s)
    req_name = SETUP_REQ.get(bReq, f"0x{bReq:02X}")
    desc_type = DESC_TYPE.get(wVal >> 8) if bReq == 0x06 else None
    return {
        "bmRequestType": bmRT,
        "bRequest": req_name,
        "wValue": wVal,
        "wIndex": wIdx,
        "wLength": wLen,
        "desc_type": desc_type,
    }


def printable(b: bytes, n=32) -> str:
    return "".join(chr(c) if 32 <= c < 127 else "." for c in b[:n])


# ─────────────────────────────────────────────────────────────────────────────

pkts = read_pcap_packets(PCAP_FILE)
print(f"[PCAP] Total packets: {len(pkts)}\n")

records = []
for pkt in pkts:
    r = parse_usbpcap(pkt)
    if r:
        records.append(r)
print(f"[USB]  Parsed USBPcap records: {len(records)}\n")

# ── Devices ────────────────────────────────────────────────────────────────
devices = collections.defaultdict(set)
for r in records:
    devices[(r["bus"], r["device"])].add(r["xfer_name"])

print("=" * 65)
print("  DEVICES OBSERVED")
print("=" * 65)
for (bus, dev), types in sorted(devices.items()):
    print(f"  bus={bus}  dev={dev:3d}   transfer types: {', '.join(sorted(types))}")
print()

# ── Transfer statistics ────────────────────────────────────────────────────
print("=" * 65)
print("  TRANSFER STATISTICS BY DEVICE / ENDPOINT")
print("=" * 65)
ctr = collections.Counter()
bctr = collections.Counter()
for r in records:
    k = (r["bus"], r["device"], r["xfer_name"], r["ep_num"], r["direction"])
    ctr[k] += 1
    bctr[k] += len(r["payload"])
for k in sorted(ctr):
    bus, dev, xt, ep, dr = k
    print(f"  bus={bus}  dev={dev:3d}  {xt:4s}  ep={ep}  {dr:3s}  "
          f"pkts={ctr[k]:5d}  bytes={bctr[k]:8d}")
print()

# ── CTRL Setup transactions ─────────────────────────────────────────────────
print("=" * 65)
print("  CONTROL SETUP TRANSACTIONS")
print("=" * 65)
for r in records:
    if r["transfer"] == 2 and r["setup"]:
        dec = decode_setup(r["setup"])
        if not dec:
            continue
        desc = f"  dev={r['device']:3d}  {dec['bRequest']:22s}"
        if dec["desc_type"]:
            desc += f"  [{dec['desc_type']}]"
        desc += f"  wVal=0x{dec['wValue']:04X}  wIdx={dec['wIndex']}  wLen={dec['wLength']}"
        print(desc)
print()

# ── Descriptor responses ────────────────────────────────────────────────────
print("=" * 65)
print("  DESCRIPTOR RESPONSES (CTRL IN payloads)")
print("=" * 65)
for r in records:
    pl = r["payload"]
    if r["transfer"] == 2 and r["direction"] == "IN" and len(pl) >= 2:
        bDT = pl[1]
        if bDT == 1 and len(pl) >= 18:
            (bLen, bDT2, bcdUSB, bClass, bSub, bProt, bMaxPS,
             idV, idP, bcdD, iM, iP, iSN, nC
             ) = struct.unpack_from("<BBHBBBBHHHBBBB", pl, 0)
            print(f"  [DEVICE DESCRIPTOR] dev={r['device']}")
            print(f"    USB={bcdUSB:#06x}  Class=0x{bClass:02X}  SubClass=0x{bSub:02X}  Protocol=0x{bProt:02X}")
            print(f"    VID=0x{idV:04X}  PID=0x{idP:04X}  bcdDevice=0x{bcdD:04X}")
            print(f"    iManufacturer={iM}  iProduct={iP}  iSerialNumber={iSN}  nConfigurations={nC}")
        elif bDT == 2 and len(pl) >= 9:
            (bLen, bDT2, wTL, nIF, cVal,
             iCfg, bmA, mPwr
             ) = struct.unpack_from("<BBHBBBBB", pl, 0)
            print(f"  [CONFIG DESCRIPTOR] dev={r['device']}  wTotalLen={wTL}  "
                  f"NumIF={nIF}  MaxPower={mPwr * 2}mA  Attr=0x{bmA:02X}")
            # walk sub-descriptors
            p2 = 9
            while p2 + 2 <= len(pl):
                bL2, bT2 = pl[p2], pl[p2 + 1]
                if bL2 == 0:
                    break
                if bT2 == 4 and p2 + 9 <= len(pl):
                    ifN, altS, nEP, iCls, iSub2, iPr, iIf = struct.unpack_from("<BBBBBBB", pl, p2 + 2)
                    print(f"    Interface {ifN} alt={altS} nEP={nEP} "
                          f"Class=0x{iCls:02X} Sub=0x{iSub2:02X} Proto=0x{iPr:02X}")
                elif bT2 == 5 and p2 + 7 <= len(pl):
                    epA, bmAt, wMPS, bInt = struct.unpack_from("<BBHB", pl, p2 + 2)
                    dr = "IN" if epA & 0x80 else "OUT"
                    at = bmAt & 3
                    atN = ["CTRL", "ISO", "BULK", "INT"][at]
                    print(f"    Endpoint 0x{epA:02X} ({dr}) {atN} MaxPktSize={wMPS} Interval={bInt}")
                elif bT2 == 33 and p2 + 9 <= len(pl):
                    bcdHID, bCC, bIdxC, nDesc, bDT3, wDL = struct.unpack_from("<HBBBHB", pl, p2 + 2)[:6]
                    print(f"    HID bcdHID=0x{bcdHID:04X} nDesc={nDesc} DescType={bDT3} DescLen={wDL}")
                p2 += bL2
        elif bDT == 3 and len(pl) >= 4:
            try:
                s = pl[2:].decode("utf-16-le", errors="replace")
                print(f"  [STRING] dev={r['device']}  \"{s}\"")
            except Exception:
                pass
        elif bDT == 34:
            print(f"  [HID_REPORT] dev={r['device']}  len={len(pl)}  hex={pl.hex()}")
print()

# ── Bulk/INT samples ────────────────────────────────────────────────────────
print("=" * 65)
print("  BULK / INTERRUPT PAYLOAD SAMPLES (first 30 non-empty)")
print("=" * 65)
shown = 0
for r in records:
    if r["xfer_name"] in ("BULK", "INT") and r["payload"] and shown < 30:
        pl = r["payload"]
        print(f"  dev={r['device']:3d}  ep={r['ep_num']}  {r['direction']:3s}"
              f"  [{r['xfer_name']}]  len={len(pl):4d}"
              f"  {pl[:24].hex()}"
              f"  |{printable(pl, 24)}|")
        shown += 1
if shown == 0:
    print("  (none)")
print()

# ── Printable strings ───────────────────────────────────────────────────────
print("=" * 65)
print("  PRINTABLE STRINGS IN PAYLOADS (len >= 6)")
print("=" * 65)
seen_str: set = set()
for r in records:
    if r["payload"]:
        for m in re.finditer(rb"[ -~]{6,}", r["payload"]):
            s = m.group().decode()
            if s not in seen_str:
                seen_str.add(s)
                print(f"  [{r['xfer_name']:4s}  dev={r['device']:3d}  ep={r['ep_num']}]  \"{s}\"")
if not seen_str:
    print("  (none)")
print()
print("Analysis complete.")
