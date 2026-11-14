# OLYMPUS DR 系列 HID 工具

[English](README.md) | 简体中文

本仓库包含适用于 Windows 平台的 OLYMPUS DR 系列 USB/HID 设备的实用工具与逆向工程脚本。

## 已稳定功能

仓库根目录下的主要工作流脚本：

- `dictation.py`
  - 主听写运行时（`sherpa-onnx` + SenseVoice，经由 ASR 网关路由）。
  - 按住 `FAST_BACKWARD` 开始录音，松开后运行 ASR 并将识别文本输入到当前焦点窗口。
  - 录音期间在屏幕上显示悬浮指示器（见下文）。
- `overlay.py`
  - 始终置顶的半透明胶囊浮窗，用于可视化麦克风电平。
  - 独立实现（tkinter + ctypes），不引入额外依赖。
- `asr_service/`
  - HTTP 语音识别服务。优先使用 GPU 主机（`192.0.2.175`）上的 Qwen3-ASR，
    该主机不可达时降级到本地 SenseVoice。
  - API 与调参说明见 `asr_service/README.md`。
- `read_media_keys.py`
  - 实时 HID 读取器及位掩码解析器。
  - 当前按键位映射的权威来源。
- `keys_monitor.py`
  - 用于按键验证的人类可读状态转换输出。
- `print_current_state.py`
  - 原始 HID 快照/调试输出。
- `create_dictation_shortcut.py`
  - 一键安装助手（创建 `.venv`、安装依赖、生成桌面快捷方式）。
- `dev/`
  - 仅供开发调试，日常使用不需要。
  - `overlay_demo.py`：用合成包络驱动指示器（不需要麦克风和 HID 设备）。
  - `test_visual.py`：按固定电平渲染各状态并保存截图。
  - `test_live.py`：真实麦克风 → 指示器，同样不需要 HID 设备。
  - `shot.py`：极简 ctypes 截图工具（输出 24 位 BMP，不依赖 Pillow）。

## 当前听写按键映射

`dictation.py` 中配置的运行时按键动作：

| 按键            | 动作                      |
|-----------------|---------------------------|
| `FAST_BACKWARD` | 按住录音，松开转写并输入  |
| `NEW`           | 发送 `Esc`                |
| `F1`            | 发送 `Ctrl+C`             |
| `F2`            | 输入文本 `continue`       |
| `F3`            | 发送 `Ctrl+Enter`         |
| `F4`            | 发送 `Backspace`，按住连发（0.4 秒后开始，约 22 次/秒） |
| `REW`           | 发送 `Enter`              |
| `FF`            | 鼠标滚轮向下              |
| `INSERT_OVER`   | 鼠标滚轮向上              |

## 仓库结构

- 根目录：
  - 运行时脚本、安装脚本、按键映射文件及抓包文件（`*.pcap`）。
- `asr_service/`：
  - `remote_server.py`：Qwen3-ASR HTTP 服务，运行在 RTX 5080 主机上。
  - `gateway.py`：本机网关，远端优先、本地兜底。
  - `smoke_test.py`：端到端自检。
- `analysis/`：
  - 协议/pcap 分析脚本及回放实验。
  - `qwen3_integration_research.md`：Qwen3/Qwen3-ASR 集成可行性快速调研。
  - `_tmp_*.py` 文件为逆向工程过程中使用的临时探针脚本。
- `dev/`：
  - `overlay.py` 的可视化调试工具与截图辅助脚本。任意目录下均可运行：
    `python dev/overlay_demo.py`。

## 设备/协议发现

- 目标设备：`VID:PID = 07B4:0256`。
- 输入状态来自 HID 中断 IN 报文。
- `INSERT_OVER` 状态切换与主机 HID `SET_REPORT(Output)`（`EP0`）相关联。
- 观察到的 LED 相关控制模式：
  - `bmRequestType=0x21`
  - `bRequest=0x09`
  - `wValue=0x0200`
  - `wIndex=0x0000`
  - `wLength=64`
- 回放候选的 64 字节输出报文仍无法可靠复现 LED 行为，可能缺少设备/会话上下文信息。

## 环境要求

- 操作系统：Windows
- Python：3.12
- 推荐使用本地 `.venv` 虚拟环境

依赖项见 `requirements.txt`：

```powershell
python -m pip install -r .\requirements.txt
```

主要依赖包：

- `pywinusb`
- `sherpa-onnx`
- `sounddevice`
- `numpy`
- `pynput`
- `fastapi` / `uvicorn` / `python-multipart` / `soundfile`（ASR 网关）

## 屏幕录音指示器

按住 `FAST_BACKWARD` 时，会在一块显示器的底部中央弹出半透明胶囊浮窗。
选中的是**前台窗口所在的显示器**，而不是鼠标所在的显示器——你实际在哪儿打字，
它就出现在哪儿，从而始终能看见"正在听"的反馈。

| 状态 | 表现 |
| --- | --- |
| 录音中 | 白色柱随实时麦克风电平跳动；即使静音也有轻微呼吸，明确表示"在听" |
| 识别中 | 柱子变蓝，一道波从左向右流动 |
| 识别完成 | 柱子闪烁绿色，然后淡出（约 0.55 秒） |
| 未识别到语音 | 柱子收拢成灰点，然后淡出（约 0.40 秒） |

改动它之前值得了解的实现细节：

- 窗口用 `overrideredirect` + 置顶，但**没有**用窗口区域（region）裁剪——
  region 是二值掩码，Windows 永远不做抗锯齿，圆角处会出现明显台阶。实际像素来自
  32 位预乘 ARGB 的 DIB，通过 `UpdateLayeredWindow` 提交，因此边缘有真正的 8 位
  alpha 渐变（再由 `EDGE_FEATHER` 进一步柔化）。
- `WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW` 让它可点击穿透、
  不抢焦点、不出现在任务栏和 Alt-Tab 中，因此不会干扰打字。
- 置顶状态每秒重申一次，大多数全屏应用也压不住它。
- DPI 感知在 import 时设置；在此之前创建的任何窗口都会锁定进程的缩放模式，
  浮窗会变模糊。
- 胶囊形状由圆角矩形的**有符号距离场**光栅化得到，所以边缘柔和度就是一个数字：
  `overlay.py` 里的 `EDGE_FEATHER`（1.0 = 普通抗锯齿，4.0 = 当前的软边）。
  窗口额外留了 `PAD` 像素内边距，否则羽化带会被窗口边界裁掉。
- `AudioCapture.level` 把绝对 dBFS 曲线与慢衰减峰值结合起来，因此无论麦克风
  灵敏度高低，柱子的观感都正常。

不接硬件也能预览：

```powershell
python .\dev\overlay_demo.py 6   # 6 秒合成"说话"，然后进入识别/完成状态
```

运行时自带调试开关：

```powershell
python .\dictation.py --dry-run              # 只打印识别结果，不输入到窗口
python .\dictation.py --dry-run --simulate 4 # 合成一次 4 秒的按下/松开周期
```

## 快速开始

**创建虚拟环境 + 安装依赖 + 生成桌面快捷方式：**

```powershell
python .\create_dictation_shortcut.py
```

**监控 HID 报文：**

```powershell
python .\read_media_keys.py
```

**运行人类可读按键监控：**

```powershell
python .\keys_monitor.py
```

**启动听写运行时：**

```powershell
python .\dictation.py
```

## ASR 服务（HTTP，远端优先）

识别后端可通过 HTTP 访问，其他工具也能复用。

```powershell
.\start_asr_gateway.bat                                  # 网关监听 127.0.0.1:8799
python .\asr_service\smoke_test.py                       # 自检两个引擎
```

```bash
curl -F "file=@sample.wav" http://127.0.0.1:8799/asr
```

路由逻辑：`POST /asr` 优先请求 `192.0.2.175:8790` 上的 Qwen3-ASR（RTX 5080），
主机不可达时降级到本地 SenseVoice int8。熔断器会阻止网关在远端不可达期间
为每个请求反复支付连接超时。响应体带有 `engine` 与 `fallback` 字段，
调用方可据此判断本次由哪个引擎完成。

`dictation.py --local` 完全绕过网关；不带该参数时默认走网关，
若网关自身不可用再降级到本地模型。

**网关运行在 `dictation.py` 自身的一个工作线程上**——没有子进程、没有计划任务、
没有注册表项、没有服务注册。dictation 活着它的网关就活着；dictation 退出，
端口随之释放。删掉这个文件夹不会残留任何东西。

启动时会探测 `http://127.0.0.1:8799/health`。若无人应答，则导入
`asr_service.gateway:app` 并在 `asr-gateway` 线程上用 uvicorn 提供服务
（首次应答约 0.5 秒，SenseVoice 兜底引擎随后在后台预热）。若该端口已被
**其他进程**占用，则直接复用，退出时不会去动它。

共用同一进程带来两个额外好处：

- 因为能直接拿到网关的 SenseVoice 实例，客户端借用它即可，
  不必再加载第二份权重。
- "网关不可达"这条路径几乎不再发生，因此不会出现某次按键悄悄降级到
  纯 CPU 识别却没有任何日志的情况。

实测：SenseVoice 解码期间，16 ms 的 UI 循环仍保持 16.0 ms 的帧间隔中位数
（最差一帧 29 ms，相当于掉一帧）；1.5 秒的模型加载从未让循环阻塞超过 32 ms，
原因是 sherpa-onnx 在 `decode_stream` 内部释放了 GIL。
代价是：网关里的解释器级崩溃现在会带走整个应用。对于个人工具而言可以接受——
两者本来就随着同一次 Ctrl+C 一起退出。

```powershell
python .\dictation.py --no-gateway-autostart   # 只探测，不启动
```

## ASR 模型设置

下载并解压 SenseVoice 模型（或通过 `--model-dir` 参数指定路径）：

```
https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models
sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2
```

支持中文、英文、日语、韩语、粤语，自动语言检测，并带有 ITN（逆文本规范化）。

## 注意事项

- 分析脚本有意放在 `analysis/` 子目录下，与生产脚本分离。
- `logs/` 与 `*.bmp` 是运行产物和截图，已在 `.gitignore` 中排除。
- 临时探针脚本后续可根据需要清理，以维护更严格的生产目录结构。

## 许可证

[MIT](LICENSE) © 2026 Andelf。

MIT 只覆盖本仓库自身的代码，有两块不在其中：

**模型。** ASR 权重需另行下载，遵循各自的上游许可——SenseVoice 遵循 FunASR
的条款，Qwen3-ASR 遵循阿里侧的条款。再分发或商用前请自行核对。

**依赖。** 安装时拉取的库各有各的许可证：`sherpa-onnx`（Apache-2.0）、
`fastapi`（MIT）、`python-multipart`（Apache-2.0）、`uvicorn` / `numpy` /
`soundfile`（BSD-3-Clause）、`sounddevice`（MIT）、`pywinusb`（BSD）、
`pynput`（LGPLv3，运行时导入，未做修改）。

`analysis/` 下的 USB 抓包与协议分析是基于自有设备观察得到的原创内容，
为互操作性目的提供，同样适用 MIT 条款。
