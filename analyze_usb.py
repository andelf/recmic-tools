"""
USB PCAP Analyzer — link-type 249 (LINKTYPE_USB_LINUX / usbmon)
Parses the raw pcap manually (no driver dependency on Windows).

usbmon packet header layout (48 bytes):
  0-7:  id (uint64)
  8:    type ('S'=SUBMIT, 'C'=COMPLETE, 'E'=ERROR)
  9:    transfer_type (0=ISO,1=INT,2=CTRL,3=BULK)
  10:   endpoint (0x80 bit = IN direction)
  11:   device address
  12-13: bus number (uint16 LE)
  14:   setup_flag  ('\x00' means setup packet is present)
  15:   data_flag   ('\x00' means data is present)
  16-23: ts_sec (int64)
  24-27: ts_usec (uint32)
  28-31: status (int32)
  32-35: urb_len (uint32)
  36-39: data_len (uint32)
  (if CTRL+setup_flag==0: bytes 40-47 = setup packet, else nothing extra in header)
"""

import struct, collections, sys

PCAP_FILE = r"c:\Temp\Workspace\usb-1.3.2.pcap"

TRANSFER_TYPES = {0: "ISO", 1: "INT", 2: "CTRL", 3: "BULK"}
EVENT_TYPES    = {ord('S'): "SUBMIT", ord('C'): "COMPLETE", ord('E'): "ERROR"}

# USB Chapter-9 standard request codes
SETUP_REQUESTS = {
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
    0x0C: "SYNCH_FRAME",
}

DESCRIPTOR_TYPES = {1: "DEVICE", 2: "CONFIGURATION", 3: "STRING",
                    4: "INTERFACE", 5: "ENDPOINT", 6: "DEVICE_QUALIFIER",
                    33: "HID", 34: "HID_REPORT", 36: "CS_INTERFACE"}

def read_pcap(path):
    with open(path, "rb") as f:
        raw = f.read()
    magic = struct.unpack_from("<I", raw, 0)[0]
    if magic == 0xA1B2C3D4:
        bo = "<"
    elif magic == 0xD4C3B2A1:
        bo = "<"   # reversed magic but same LE data in practice
    else:
        raise ValueError(f"Unknown magic: {magic:#010x}")
    _vh, _vm, _vmin, _tz, _acc, snaplen, dlt = struct.unpack_from(bo + "IHHiIII", raw, 0)
    print(f"[PCAP] magic={magic:#010x}  DLT={dlt}  snaplen={snaplen}")
    offset = 24
    packets = []
    while offset < len(raw):
        if offset + 16 > len(raw):
            break
        ts_sec, ts_usec, incl_len, orig_len = struct.unpack_from(bo + "IIII", raw, offset)
        offset += 16
        data = raw[offset: offset + incl_len]
        offset += incl_len
        packets.append((ts_sec, ts_usec, data))
    return packets

def parse_usbmon(data):
    if len(data) < 48:
        return None
    (urb_id,
     ev_type, transfer_type, endpoint, dev_addr, bus_num,
     setup_flag, data_flag,
     ts_sec, ts_usec, status, urb_len, data_len) = struct.unpack_from(
         "<QBBBBHBBQIIII", data, 0
     )
    direction = "IN" if (endpoint & 0x80) else "OUT"
    ep_num    = endpoint & 0x7F
    setup_pkt = None
    payload_offset = 48  # default

    if transfer_type == 2 and setup_flag == 0:   # CTRL + setup present
        setup_pkt = data[40:48]
        payload_offset = 48

    payload = data[payload_offset: payload_offset + data_len] if data_len else b""

    return {
        "id":      urb_id,
        "event":   EVENT_TYPES.get(ev_type, chr(ev_type)),
        "type":    TRANSFER_TYPES.get(transfer_type, str(transfer_type)),
        "dir":     direction,
        "ep":      ep_num,
        "dev":     dev_addr,
        "bus":     bus_num,
        "status":  status,
        "urb_len": urb_len,
        "data_len":data_len,
        "setup":   setup_pkt,
        "payload": payload,
        "ts":      ts_sec + ts_usec * 1e-6,
    }

def decode_setup(s):
    if len(s) < 8:
        return {}
    bmRT, bReq, wVal, wIdx, wLen = struct.unpack_from("<BBHHH", s)
    req_name = SETUP_REQUESTS.get(bReq, f"0x{bReq:02X}")
    desc_type = None
    if bReq == 0x06:  # GET_DESCRIPTOR
        desc_type = DESCRIPTOR_TYPES.get(wVal >> 8, f"type {wVal>>8}")
    return {"bmRequestType": bmRT, "bRequest": req_name,
            "wValue": wVal, "wIndex": wIdx, "wLength": wLen,
            "desc_type": desc_type}

def printable(b):
    return "".join(chr(c) if 32 <= c < 127 else "." for c in b)

# ── main ──────────────────────────────────────────────────────────────────────

packets = read_pcap(PCAP_FILE)
print(f"[PCAP] Total raw packets: {len(packets)}\n")

parsed = [p for p in (parse_usbmon(d) for _, _, d in packets) if p]
print(f"[USB]  Parsed usbmon records: {len(parsed)}\n")

# ── 1. Devices seen ────────────────────────────────────────────────────────
devices = collections.defaultdict(set)
for p in parsed:
    devices[(p["bus"], p["dev"])].add(p["type"])
print("=" * 62)
print("  DEVICES OBSERVED")
print("=" * 62)
for (bus, dev), types in sorted(devices.items()):
    print(f"  bus={bus:3d}  dev={dev:3d}   transfer types: {', '.join(sorted(types))}")
print()

# ── 2. CTRL / Setup transactions ──────────────────────────────────────────
print("=" * 62)
print("  CONTROL SETUP TRANSACTIONS")
print("=" * 62)
ctrl_seen = collections.Counter()
setup_list = []
for p in parsed:
    if p["type"] == "CTRL" and p["setup"] and p["event"] == "SUBMIT":
        dec = decode_setup(p["setup"])
        key = (p["dev"], dec.get("bRequest"), dec.get("desc_type"))
        ctrl_seen[key] += 1
        setup_list.append((p, dec))

for p, dec in setup_list:
    desc = f"  dev={p['dev']:3d}  {dec['bRequest']:20s}"
    if dec["desc_type"]:
        desc += f"  [{dec['desc_type']}]"
    desc += f"  wVal=0x{dec['wValue']:04X}  wIdx={dec['wIndex']}  wLen={dec['wLength']}"
    print(desc)
print()

# ── 3. Descriptor payloads from COMPLETE CTRL IN ──────────────────────────
print("=" * 62)
print("  DESCRIPTOR DATA (CTRL COMPLETE, IN direction)")
print("=" * 62)
for p in parsed:
    if p["type"] == "CTRL" and p["event"] == "COMPLETE" and p["dir"] == "IN" and p["payload"]:
        pl = p["payload"]
        # heuristic: device descriptor starts with 12 01, config with 09 02, string with len 03
        if len(pl) >= 2:
            bLength, bDescType = pl[0], pl[1]
            if bDescType == 1 and len(pl) >= 18:   # DEVICE descriptor
                (bLen, bDT, bcdUSB, bClass, bSubClass, bProt, bMaxPS,
                 idVendor, idProduct, bcdDev, iMfr, iProd, iSN, bNumConf
                 ) = struct.unpack_from("<BBHBBBBHHHBBBB", pl, 0)
                print(f"  [DEVICE DESCRIPTOR] dev={p['dev']}")
                print(f"    bcdUSB=0x{bcdUSB:04X}  Class=0x{bClass:02X}  SubClass=0x{bSubClass:02X}  Protocol=0x{bProt:02X}")
                print(f"    idVendor=0x{idVendor:04X}  idProduct=0x{idProduct:04X}  bcdDevice=0x{bcdDev:04X}")
                print(f"    iManufacturer={iMfr}  iProduct={iProd}  iSerialNumber={iSN}  bNumConfigurations={bNumConf}")
            elif bDescType == 2 and len(pl) >= 9:  # CONFIGURATION descriptor
                (bLen, bDT, wTotalLen, bNumIF, bCfgVal,
                 iCfg, bmAttr, bMaxPwr) = struct.unpack_from("<BBHBBBB B", pl, 0)
                print(f"  [CONFIG DESCRIPTOR] dev={p['dev']}  wTotalLen={wTotalLen}  NumInterfaces={bNumIF}  MaxPower={bMaxPwr*2}mA  Attr=0x{bmAttr:02X}")
            elif bDescType == 3:                   # STRING descriptor
                try:
                    s = pl[2:].decode("utf-16-le", errors="replace")
                    print(f"  [STRING] dev={p['dev']}  \"{s}\"")
                except Exception:
                    pass
print()

# ── 4. Transfer statistics ────────────────────────────────────────────────
print("=" * 62)
print("  TRANSFER STATISTICS BY DEVICE / ENDPOINT")
print("=" * 62)
stats = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0]))
for p in parsed:
    key = (p["bus"], p["dev"], p["type"], p["ep"], p["dir"])
    stats[key]["count"][0] += 1
    stats[key]["bytes"][0] += p["data_len"]

counter = collections.Counter()
byte_counter = collections.Counter()
for p in parsed:
    key = (p["bus"], p["dev"], p["type"], p["ep"], p["dir"])
    counter[key] += 1
    byte_counter[key] += p["data_len"]

for key in sorted(counter.keys()):
    bus, dev, ttype, ep, dirn = key
    print(f"  bus={bus}  dev={dev:3d}  {ttype:4s}  ep={ep}  {dirn:3s}  "
          f"pkts={counter[key]:5d}  bytes={byte_counter[key]:8d}")
print()

# ── 5. Bulk/Interrupt data sample ─────────────────────────────────────────
print("=" * 62)
print("  BULK / INTERRUPT PAYLOAD SAMPLES (first 20 non-empty)")
print("=" * 62)
shown = 0
for p in parsed:
    if p["type"] in ("BULK", "INT") and p["payload"] and shown < 20:
        pl = p["payload"]
        hex_str  = pl[:32].hex()
        text_str = printable(pl[:32])
        print(f"  dev={p['dev']:3d} ep={p['ep']:2d} {p['dir']:3s} [{p['type']}]  "
              f"len={len(pl):4d}  hex: {hex_str}  |{text_str}|")
        shown += 1
print()

# ── 6. All printable strings from payloads ────────────────────────────────
print("=" * 62)
print("  PRINTABLE STRINGS IN PAYLOADS (len >= 6)")
print("=" * 62)
seen_strings = set()
import re
for p in parsed:
    if p["payload"]:
        for m in re.finditer(rb'[ -~]{6,}', p["payload"]):
            s = m.group().decode()
            if s not in seen_strings:
                seen_strings.add(s)
                print(f"  [{p['type']:4s} dev={p['dev']:3d} ep={p['ep']}]  \"{s}\"")
print()
print("Done.")
