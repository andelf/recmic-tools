"""
深度分析 dev=3 设备 (VID=0x07B4, PID=0x0256)
聚焦：枚举信息、HID 接口报文、端点 ep=2/ep=3 的 INT 数据解读
"""

import struct
import collections
import pathlib

PCAP_FILE = r"c:\Temp\Workspace\usb-1.3.2.pcap"
TARGET_DEV = 3

XFER = {0: "ISO", 1: "INT", 2: "CTRL", 3: "BULK"}
SETUP_REQ = {
    0x00: "GET_STATUS",    0x01: "CLEAR_FEATURE", 0x03: "SET_FEATURE",
    0x05: "SET_ADDRESS",   0x06: "GET_DESCRIPTOR", 0x07: "SET_DESCRIPTOR",
    0x08: "GET_CONFIGURATION", 0x09: "SET_CONFIGURATION",
    0x0A: "GET_INTERFACE", 0x0B: "SET_INTERFACE",
}
DESC_TYPE = {
    1: "DEVICE", 2: "CONFIGURATION", 3: "STRING",
    4: "INTERFACE", 5: "ENDPOINT", 33: "HID", 34: "HID_REPORT",
}

# ── PCAP / USBPcap parse ──────────────────────────────────────────────────

def read_pcap_packets(path):
    raw = pathlib.Path(path).read_bytes()
    off = 24
    pkts = []
    while off + 16 <= len(raw):
        ts_sec, ts_usec, incl_len, orig_len = struct.unpack_from("<IIII", raw, off)
        off += 16
        pkts.append((ts_sec + ts_usec * 1e-6, raw[off:off + incl_len]))
        off += incl_len
    return pkts


def parse_usbpcap(ts, pkt):
    if len(pkt) < 27:
        return None
    hdrLen = struct.unpack_from("<H", pkt, 0)[0]
    if hdrLen < 27 or hdrLen > len(pkt):
        return None
    irpId  = struct.unpack_from("<Q",  pkt, 2)[0]
    status = struct.unpack_from("<I",  pkt, 10)[0]
    func   = struct.unpack_from("<H",  pkt, 14)[0]
    info   = pkt[16]
    bus    = struct.unpack_from("<H",  pkt, 17)[0]
    device = struct.unpack_from("<H",  pkt, 19)[0]
    ep     = pkt[21]
    xfer   = pkt[22]
    data_len = struct.unpack_from("<I", pkt, 23)[0]
    direction = "IN" if (ep & 0x80) else "OUT"
    ep_num    = ep & 0x7F
    after_hdr = pkt[hdrLen:]

    setup, payload = None, b""
    if xfer == 2:
        if len(after_hdr) == 8:
            bmRT = after_hdr[0]; bReq = after_hdr[1]
            if bReq in SETUP_REQ or (bmRT & 0x60) in (0x00, 0x20, 0x40):
                setup = after_hdr; payload = b""
            else:
                payload = after_hdr[:data_len]
        else:
            payload = after_hdr[:data_len]
    else:
        payload = after_hdr[:data_len]

    return dict(ts=ts, irpId=irpId, status=status, func=func, info=info,
                bus=bus, device=device, ep_num=ep_num, direction=direction,
                xfer=xfer, xfer_name=XFER.get(xfer, str(xfer)),
                data_len=data_len, setup=setup, payload=payload)


def decode_setup(s):
    if len(s) < 8:
        return None
    bmRT, bReq, wVal, wIdx, wLen = struct.unpack_from("<BBHHH", s)
    return dict(bmRT=bmRT, bReq=bReq, req=SETUP_REQ.get(bReq, f"0x{bReq:02X}"),
                wVal=wVal, wIdx=wIdx, wLen=wLen,
                desc_type=DESC_TYPE.get(wVal >> 8) if bReq == 0x06 else None)


def printable(b, n=32):
    return "".join(chr(c) if 32 <= c < 127 else "." for c in b[:n])


def hexdump(b, indent="    ", width=16):
    lines = []
    for i in range(0, len(b), width):
        chunk = b[i:i + width]
        h = " ".join(f"{x:02x}" for x in chunk)
        t = printable(chunk, width)
        lines.append(f"{indent}{i:04x}:  {h:<{width*3}}  |{t}|")
    return "\n".join(lines)

# ── load & filter ─────────────────────────────────────────────────────────

all_pkts = read_pcap_packets(PCAP_FILE)
all_rec  = [r for r in (parse_usbpcap(ts, p) for ts, p in all_pkts) if r]
dev3     = [r for r in all_rec if r["device"] == TARGET_DEV]

print(f"[*] 总包数: {len(all_rec)}  dev=3 包数: {len(dev3)}\n")

# ── 1. 枚举过程 ────────────────────────────────────────────────────────────
print("=" * 68)
print("  §1  dev=3 设备枚举 (CTRL 事务)")
print("=" * 68)

for r in dev3:
    if r["xfer"] != 2:
        continue
    if r["setup"]:
        dec = decode_setup(r["setup"])
        dt = f"  [{dec['desc_type']}]" if dec["desc_type"] else ""
        print(f"  [SETUP→] {dec['req']:22s}{dt}  wVal=0x{dec['wVal']:04X} wIdx={dec['wIdx']} wLen={dec['wLen']}")
    elif r["payload"] and r["direction"] == "IN":
        pl = r["payload"]
        if len(pl) < 2:
            continue
        bDT = pl[1]
        if bDT == 1 and len(pl) >= 18:
            (bLen, bDT2, bcdUSB, bClass, bSub, bProt, bMaxPS,
             idV, idP, bcdD, iM, iP, iSN, nC
             ) = struct.unpack_from("<BBHBBBBHHHBBBB", pl, 0)
            print(f"\n  ┌─ [DEVICE DESCRIPTOR]")
            print(f"  │  USB规范版本  : {bcdUSB>>8}.{(bcdUSB>>4)&0xF}{bcdUSB&0xF}  "
                  f"(bcdUSB=0x{bcdUSB:04X})")
            print(f"  │  设备类       : 0x{bClass:02X} / 0x{bSub:02X} / 0x{bProt:02X}  (Class/Sub/Protocol)")
            print(f"  │  VID:PID      : 0x{idV:04X} : 0x{idP:04X}  bcdDevice=0x{bcdD:04X}")
            print(f"  │  字符串索引   : iManufacturer={iM}  iProduct={iP}  iSerialNumber={iSN}")
            print(f"  └─ 配置数       : {nC}\n")
        elif bDT == 2 and len(pl) >= 9:
            (bLen, bDT2, wTL, nIF, cVal,
             iCfg, bmA, mPwr
             ) = struct.unpack_from("<BBHBBBBB", pl, 0)
            print(f"\n  ┌─ [CONFIGURATION DESCRIPTOR]  wTotalLen={wTL}  NumIF={nIF}  MaxPower={mPwr*2}mA  Attr=0x{bmA:02X}")
            p2 = 9
            while p2 + 2 <= len(pl):
                bL2, bT2 = pl[p2], pl[p2 + 1]
                if bL2 == 0:
                    break
                prefix = "  │  "
                if bT2 == 4 and p2 + 9 <= len(pl):
                    ifN, altS, nEP, iCls, iSub2, iPr, iIf = struct.unpack_from("<BBBBBBB", pl, p2 + 2)
                    cls_desc = {0x01: "Audio", 0x03: "HID", 0x0E: "Video"}.get(iCls, f"0x{iCls:02X}")
                    print(f"  │")
                    print(f"  ├─ Interface {ifN} alt={altS}  Class={cls_desc}(0x{iCls:02X}) Sub=0x{iSub2:02X} Proto=0x{iPr:02X}  nEP={nEP}")
                elif bT2 == 5 and p2 + 7 <= len(pl):
                    epA, bmAt, wMPS, bInt = struct.unpack_from("<BBHB", pl, p2 + 2)
                    dr  = "IN " if epA & 0x80 else "OUT"
                    atN = ["CTRL", "ISO", "BULK", "INT"][bmAt & 3]
                    print(f"  │    └ Endpoint 0x{epA:02X} ({dr}) {atN:4s}  MaxPktSize={wMPS}  Interval={bInt}")
                elif bT2 == 33 and p2 + 9 <= len(pl):
                    try:
                        bcdHID, bCC, bIdxC, nDesc = struct.unpack_from("<HBBB", pl, p2 + 2)
                        if p2 + 9 <= len(pl):
                            bDT3, wDL = struct.unpack_from("<BH", pl, p2 + 6)
                            print(f"  │    └ HID 0x{bcdHID:04X}  nDesc={nDesc}  ReportDescLen={wDL}")
                    except Exception:
                        pass
                p2 += bL2
            print(f"  └─ (配置描述符结束)\n")
        elif bDT == 3 and len(pl) >= 4:
            s = pl[2:].decode("utf-16-le", errors="replace")
            print(f"  [STRING ←]  \"{s}\"")
    elif r["payload"] and r["direction"] == "OUT":
        pl = r["payload"]
        if pl:
            print(f"  [DATA OUT]  len={len(pl)}  {pl.hex()}  |{printable(pl)}|")

print()

# ── 2. INT ep=2 详细解读 ───────────────────────────────────────────────────
ep2_pkts = [r for r in dev3 if r["xfer"] == 1 and r["ep_num"] == 2 and r["payload"]]

print("=" * 68)
print(f"  §2  INT ep=2 IN 报文  (共 {len(ep2_pkts)} 包)")
print("=" * 68)

if ep2_pkts:
    # 统计报文长度
    lens = collections.Counter(len(r["payload"]) for r in ep2_pkts)
    print(f"  报文长度分布: {dict(sorted(lens.items()))}")

    # 统计首字节（Report ID）
    report_ids = collections.Counter(r["payload"][0] for r in ep2_pkts if r["payload"])
    print(f"  Report ID 分布: { {f'0x{k:02X}': v for k, v in sorted(report_ids.items())} }")

    # 每种 Report ID 显示前 5 包
    by_rid = collections.defaultdict(list)
    for r in ep2_pkts:
        by_rid[r["payload"][0]].append(r)

    for rid, rlist in sorted(by_rid.items()):
        sample_len = len(rlist[0]["payload"])
        print(f"\n  ── Report ID=0x{rid:02X}  (共 {len(rlist)} 包, 每包 {sample_len} 字节) ──")

        # 每个字节的取值变化统计
        byte_vals = collections.defaultdict(set)
        for r in rlist:
            for i, b in enumerate(r["payload"]):
                byte_vals[i].add(b)
        print(f"  {'字节':>4}  {'取值范围':>30}  {'含义推测'}")
        print(f"  {'-'*4}  {'-'*30}  {'-'*20}")
        for i in range(sample_len):
            vals = sorted(byte_vals[i])
            if len(vals) == 1:
                rng = f"固定=0x{vals[0]:02X}"
            elif len(vals) <= 8:
                rng = "{" + ", ".join(f"0x{v:02X}" for v in vals) + "}"
            else:
                rng = f"0x{min(vals):02X}~0x{max(vals):02X}({len(vals)}种)"
            hint = ""
            if i == 0:
                hint = "Report ID"
            elif sample_len == 64:
                # 通用 HID 报告，尝试推测
                hints_64 = {
                    1: "按键/状态bitmask低字节",
                    2: "按键/状态bitmask高字节",
                    3: "X轴低字节",
                    4: "X轴高字节",
                    5: "Y轴低字节",
                    6: "Y轴高字节",
                }
                hint = hints_64.get(i, "")
            print(f"  {i:>4}  {rng:>30}  {hint}")

        # 打印前 10 包原始十六进制
        print(f"\n  前 10 包原始数据:")
        print(f"  {'#':>4}  {'时间戳':>14}  hex")
        t0 = rlist[0]["ts"]
        for idx, r in enumerate(rlist[:10]):
            dt = r["ts"] - t0
            print(f"  {idx:>4}  +{dt:>12.6f}s  {r['payload'].hex()}")

print()

# ── 3. INT ep=3 详细解读 ───────────────────────────────────────────────────
ep3_pkts = [r for r in dev3 if r["xfer"] == 1 and r["ep_num"] == 3 and r["payload"]]

print("=" * 68)
print(f"  §3  INT ep=3 IN 报文  (共 {len(ep3_pkts)} 包)")
print("=" * 68)

if ep3_pkts:
    lens = collections.Counter(len(r["payload"]) for r in ep3_pkts)
    report_ids = collections.Counter(r["payload"][0] for r in ep3_pkts if r["payload"])
    print(f"  报文长度分布: {dict(sorted(lens.items()))}")
    print(f"  Report ID 分布: { {f'0x{k:02X}': v for k, v in sorted(report_ids.items())} }")

    by_rid = collections.defaultdict(list)
    for r in ep3_pkts:
        by_rid[r["payload"][0]].append(r)

    for rid, rlist in sorted(by_rid.items()):
        sample_len = len(rlist[0]["payload"])
        print(f"\n  ── Report ID=0x{rid:02X}  (共 {len(rlist)} 包, 每包 {sample_len} 字节) ──")

        byte_vals = collections.defaultdict(set)
        for r in rlist:
            for i, b in enumerate(r["payload"]):
                byte_vals[i].add(b)

        print(f"  {'字节':>4}  {'取值范围':>30}  {'含义推测'}")
        print(f"  {'-'*4}  {'-'*30}  {'-'*20}")
        for i in range(sample_len):
            vals = sorted(byte_vals[i])
            if len(vals) == 1:
                rng = f"固定=0x{vals[0]:02X}"
            elif len(vals) <= 8:
                rng = "{" + ", ".join(f"0x{v:02X}" for v in vals) + "}"
            else:
                rng = f"0x{min(vals):02X}~0x{max(vals):02X}({len(vals)}种)"
            hint = ""
            if i == 0:
                hint = "Report ID"
            elif sample_len == 2:
                hint = ["", "按键 bitmask"][i] if i < 2 else ""
            print(f"  {i:>4}  {rng:>30}  {hint}")

        print(f"\n  所有 {len(rlist)} 包原始数据:")
        print(f"  {'#':>4}  {'时间戳':>14}  hex  解读")
        t0 = rlist[0]["ts"]
        for idx, r in enumerate(rlist):
            dt = r["ts"] - t0
            pl = r["payload"]
            # ep=3 通常是 2字节: [report_id, button_byte]
            interp = ""
            if len(pl) == 2:
                btn = pl[1]
                pressed = [f"btn{b}" for b in range(8) if btn & (1 << b)]
                interp = "按键: " + (", ".join(pressed) if pressed else "无") 
            print(f"  {idx:>4}  +{dt:>12.6f}s  {pl.hex()}  {interp}")

print()

# ── 4. HID OUT 报文 (dev=3 发送给设备的数据) ──────────────────────────────
out_pkts = [r for r in dev3 if r["direction"] == "OUT" and r["payload"]]
print("=" * 68)
print(f"  §4  dev=3 OUT 方向有效载荷 (共 {len(out_pkts)} 包)")
print("=" * 68)
if out_pkts:
    for r in out_pkts:
        pl = r["payload"]
        print(f"  [{r['xfer_name']:4s} ep={r['ep_num']}]  len={len(pl):3d}  {pl.hex()}")
        print(hexdump(pl))
else:
    print("  (无)")
print()

# ── 5. ep=2 数据时序图 ─────────────────────────────────────────────────────
print("=" * 68)
print("  §5  INT ep=2 字节变化时序（字节1~字节3，前60包）")
print("=" * 68)
if ep2_pkts:
    print(f"  {'#':>4}  {'Δt(ms)':>8}  {'B[0]':>4}  {'B[1]':>4}  {'B[2]':>4}  {'B[3]':>4}  "
          f"{'B[4]':>4}  {'B[5]':>4}  raw (前8字节)")
    print(f"  {'-'*4}  {'-'*8}  {'-'*4}  {'-'*4}  {'-'*4}  {'-'*4}  {'-'*4}  {'-'*4}  {'-'*16}")
    t0 = ep2_pkts[0]["ts"]
    for idx, r in enumerate(ep2_pkts[:60]):
        dt_ms = (r["ts"] - t0) * 1000
        pl = r["payload"]
        cols = "  ".join(f"0x{pl[i]:02X}" if i < len(pl) else "    " for i in range(6))
        print(f"  {idx:>4}  {dt_ms:>8.1f}  {cols}  {pl[:8].hex()}")

print()
print("分析完成。")
