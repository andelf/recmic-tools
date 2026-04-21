# dev=3 设备分析记录

更新时间：2026-04-21
数据来源：usb-1.3.2.pcap（USBPcap 抓包）

## 1. 设备身份

- Bus/Dev: bus=1, dev=3
- VID:PID: 0x07B4:0x0256
- bcdUSB: 0x0110（USB 1.1）
- bcdDevice: 0x0100
- 最大功耗：500mA
- 设备类型：复合设备（Audio + HID）

## 2. 枚举过程（Control）

观察到标准枚举序列：

1. GET_DESCRIPTOR(Device)
2. GET_DESCRIPTOR(Configuration)
3. SET_CONFIGURATION(1)

额外观察到 2 次 Class OUT 控制写入（可能是音量/增益设置）：

- 21010002030502009cf8
- 210100020305020014f9

按小端 int16 解释末尾数据：

- 0xF89C = -1636
- 0xF914 = -1772

## 3. 配置描述符结构（wTotalLen=284, NumIF=7）

### IF0 (HID)

- Class=0x03, Sub=0x00, Proto=0x00
- Endpoint: 0x82 IN INT, MaxPkt=64, Interval=10
- HID ReportDescLen=75

### IF1/IF2 (Audio 输入链路)

- IF1: Audio Control
- IF2 alt=1: Endpoint 0x81 IN ISO, MaxPkt=128, Interval=1

### IF3/IF4 (Audio 输出链路)

- IF3: Audio Control
- IF4 alt=1: Endpoint 0x04 OUT ISO, MaxPkt=128, Interval=1

### IF5 (HID, Proto=0x02)

- Endpoint: 0x83 IN INT, MaxPkt=64, Interval=10
- HID ReportDescLen=52
- 抓包中对应 INT ep=3（4字节报文）

### IF6 (HID, Proto=0x01)

- Endpoint: 0x84 IN INT, MaxPkt=8, Interval=10
- HID ReportDescLen=45

## 4. 重点报文行为

## 4.1 INT ep=2 IN（26包，64字节）

- Report ID 分布：
  - 0x02: 24 包（主行为）
  - 0x06: 1 包
  - 0x0A: 1 包
- 典型形态：
  - Byte0=Report ID
  - Byte1/Byte2 为 bitmask（疑似媒体键状态）
  - 其余大部分字节固定 0
- 现象：按键按下与释放基本成对出现（几十毫秒间隔）

## 4.2 INT ep=3 IN（102包，4字节）

- Report ID 分布：0x00 为主（99包）
- 主要数据形态：00 XX YY 00
- 行为特征：XX/YY 持续变化，符合相对位移（鼠标类）上报模式

## 5. 当前结论

- dev=3 是复合设备，包含音频输入/输出与 HID 控制能力。
- 抓包中已识别：
  - 鼠标/位移类 HID 报文（ep=3）
  - 媒体键/控制类 bitmask 报文（ep=2）
  - 两次设备配置后控制写入（可能为音量或增益参数）

## 5.1 已确认状态开关位（实机验证）

- 你在默认状态 -> 回退（滑动开关）-> 默认状态的操作中，观测到：
  - report_id=0x00
  - raw 关键字节变化包含 byte1:0x02 置位
- 当前将该位标注为：SWITCH_BACK_STATE（位定义：byte1 bit1，即 byte1:0x02）
- 说明：该位属于状态位（toggle/position），不是瞬时按键位。

## 6. 下一步计划

1. 用 Python HID 库实时读取该设备输入报告。
2. 聚焦媒体键相关报告（优先 Report ID=0x02）。
3. 建立 bitmask 到语义键名映射（Play/Pause, Vol+/Vol-, Mute 等）。
4. 做一次按键标定：手动按单个键，记录触发位，反推完整映射表。
