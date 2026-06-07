# OLYMPUS DR 系列 HID 工具

[English](README.md) | 简体中文

本仓库包含适用于 Windows 平台的 OLYMPUS DR 系列 USB/HID 设备的实用工具与逆向工程脚本。

## 已稳定功能

仓库根目录下的主要工作流脚本：

- `dictation.py`
  - 主听写运行时（`sherpa-onnx` + SenseVoice）。
  - 按住 `FAST_BACKWARD` 开始录音，松开后运行 ASR 并将识别文本输入到当前焦点窗口。
- `read_media_keys.py`
  - 实时 HID 读取器及位掩码解析器。
  - 当前按键位映射的权威来源。
- `keys_monitor.py`
  - 用于按键验证的人类可读状态转换输出。
- `print_current_state.py`
  - 原始 HID 快照/调试输出。
- `create_dictation_shortcut.py`
  - 一键安装助手（创建 `.venv`、安装依赖、生成桌面快捷方式）。

## 当前听写按键映射

`dictation.py` 中配置的运行时按键动作：

| 按键            | 动作                      |
|-----------------|---------------------------|
| `FAST_BACKWARD` | 按住录音，松开转写并输入  |
| `NEW`           | 发送 `Esc`                |
| `F1`            | 发送 `Ctrl+C`             |
| `F2`            | 输入文本 `continue`       |
| `F3`            | 发送 `Ctrl+Enter`         |
| `F4`            | 发送 `Backspace`          |
| `REW`           | 发送 `Enter`              |
| `FF`            | 鼠标滚轮向下              |
| `INSERT_OVER`   | 鼠标滚轮向上              |

## 仓库结构

- 根目录：
  - 运行时脚本、安装脚本、按键映射文件及抓包文件（`*.pcap`）。
- `analysis/`：
  - 协议/pcap 分析脚本及回放实验。
  - `qwen3_integration_research.md`：Qwen3/Qwen3-ASR 集成可行性快速调研。
  - `_tmp_*.py` 文件为逆向工程过程中使用的临时探针脚本。

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

## ASR 模型设置

下载并解压 SenseVoice 模型（或通过 `--model-dir` 参数指定路径）：

```
https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models
sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2
```

支持中文、英文、日语、韩语、粤语，自动语言检测，并带有 ITN（逆文本规范化）。

## 注意事项

- 分析脚本有意放在 `analysis/` 子目录下，与生产脚本分离。
- 临时探针脚本后续可根据需要清理，以维护更严格的生产目录结构。
