# ASR 服务：175 优先，本地 SenseVoice 兜底

把语音识别做成 HTTP 服务，优先用 gpu-box 上的 Qwen3-ASR（RTX 5080），
175 不可达时自动退到本机 sherpa-onnx SenseVoice，**调用方永远能拿到文本**。

## 架构

```
客户端 (dictation.py / curl / 任何 HTTP 调用方)
        │
        ▼
  本机网关  127.0.0.1:8799          asr_service/gateway.py
        │
        ├── ① 优先  gpu-box Qwen3-ASR   192.0.2.175:8790   （RTX 5080, sm_120）
        │        失败 → 熔断、冷却、自动探测恢复
        │
        └── ② 兜底  本机 SenseVoice   进程内 sherpa-onnx int8（CPU，无网络依赖）
```

## 为什么中间要加一层网关

1. **准确率与可用性分开保证**。Qwen3-ASR 中文更强（第三方测试 CER 2.4%，SenseVoice 4.4%），
   但那台机器走 Wi-Fi，历史上掉过线。网关让好引擎走快路径，链路一抖就降级，不阻塞调用方。
2. **熔断器避免"每次都要等超时"**。连续 3 次失败后直接跳过远端，冷却 20 秒再探测一次；
   否则 175 挂掉期间每个请求都要白等一次连接超时。
3. **连接与读取分开限时**。连接阶段 2.5 秒封顶（主机无响应时快速失败），
   读取阶段 8 秒（长音频还要留推理时间）。实测 175 掉线时降级耗时从 8 秒降到 2.5 秒。

## 快速开始

### 1. gpu-box 上的 Qwen3-ASR 服务

已配成 systemd user unit，开机自启（`loginctl enable-linger <user>` 已开启）：

```bash
systemctl --user status asr-qwen     # 查状态
systemctl --user restart asr-qwen    # 重启
tail -f /opt/voice/logs/asr_server.log
```

端口 8790，ufw 已放行 `192.0.2.0/24`。

### 2. 本机网关

```bat
start_asr_gateway.bat
```

或直接：

```powershell
.venv\Scripts\python.exe asr_service\gateway.py
```

### 3. 自检

```powershell
.venv\Scripts\python.exe asr_service\smoke_test.py
```

会依次检查：网关健康、远端可达、auto 走 GPU、local 走 SenseVoice、两引擎结果一致性。

## API

### `POST /asr`

请求体二选一：

- `multipart/form-data`，字段名 `file`（可带 `language`、`context`）
- 原始音频字节流（`Content-Type: audio/wav` 等），参数走 query

查询参数：

| 参数 | 说明 |
|---|---|
| `engine=auto` | 默认。优先远端，失败降级 |
| `engine=local` | 强制走本地 SenseVoice（调试/对比用） |
| `engine=remote` | 只用远端，失败直接返回 502 |
| `language` | 语言提示，如 `Chinese` |
| `context` | 识别上下文提示（Qwen3 支持，可塞专有名词） |

响应：

```json
{
  "text": "您好，您预订的江景大床房已经确认，明天下午两点后可以办理入住。",
  "language": "Chinese",
  "duration_s": 6.64,
  "elapsed_ms": 314,
  "engine": "qwen3-asr",
  "engine_detail": "Qwen3-ASR-0.6B @ 192.0.2.175",
  "backend": "cuda",
  "fallback": false,
  "upstream_error": null,
  "gateway_ms": 473
}
```

`engine` 和 `fallback` 让调用方知道这次是谁识别出来的；降级时 `upstream_error`
会写明远端为什么失败。

### `GET /health`

网关自身、本地模型加载状态、熔断器状态。

### `GET /engines`

实时探测远端，返回两个引擎的当前可用性。

## 实测数据（6.64 秒中文音频）

| 路径 | 推理 | 端到端 | 结果 |
|---|---|---|---|
| Qwen3-ASR @ 175 | 314 ms | **473 ms** | 您好，您预订的江景大床房已经确认，明天下午两点后可以办理入住。 |
| SenseVoice 本地 | 227 ms | 238 ms | 您好，您预定的江景大床房已经确认，明天下午2点后可以办理入住。 |

- 本地 CPU 版反而更快（省掉网络往返），但「预订」被识别成「预定」、数字写成「2点」——
  这就是用 GPU 盒子的理由。
- 175 进程被杀后：前 3 次请求各约 2.5 秒（尝试连接后降级），第 4 次起 **243 ms**（熔断生效）。
- 175 恢复后：冷却期结束的第一个请求自动探测成功，熔断关闭，回到 GPU 路径。

## 配置项

本机网关（环境变量）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `QWEN_ASR_URL` | `http://192.0.2.175:8790` | 远端服务地址 |
| `QWEN_ASR_CONNECT_TIMEOUT` | `2.5` | 连接超时（秒） |
| `QWEN_ASR_TIMEOUT` | `8` | 推理读取超时（秒） |
| `QWEN_ASR_PROBE_TIMEOUT` | `3` | 半开探测的读取超时 |
| `ASR_BREAKER_THRESHOLD` | `3` | 连续失败多少次后熔断 |
| `ASR_BREAKER_COOLDOWN` | `20` | 熔断冷却秒数 |
| `SENSEVOICE_MODEL_DIR` | `...\sherpa-onnx-sense-voice-...` | 本地模型目录 |
| `ASR_GATEWAY_PORT` | `8799` | 网关端口 |

远端服务（gpu-box）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `ASR_PORT` | `8790` | 监听端口 |
| `ASR_MODEL_DIR` | `/opt/voice/models/Qwen3-ASR-0.6B` | 模型目录 |
| `ASR_MAX_NEW_TOKENS` | `1024` | 单次输出上限 |

## dictation.py 的用法

默认就走网关，所以听写时也享受「175 优先」：

```powershell
.venv\Scripts\python.exe dictation.py              # 走网关
.venv\Scripts\python.exe dictation.py --local      # 绕过网关，只用本机 SenseVoice
.venv\Scripts\python.exe dictation.py --asr-url http://127.0.0.1:8799/asr
```

网关本身连不上时，`dictation.py` 会自己加载 SenseVoice 继续工作（三层兜底）。

## 已知差异

- **繁简**：同一段音频，Qwen3-ASR 可能输出繁体（`開放時間：早上九點至下午五點。`），
  SenseVoice 输出简体（`开放时间早上9点至下午5点。`）。这是模型行为差异，不是 bug；
  如果下游必须统一简体，在网关返回前加一步 opencc 转换即可。
- **数字写法**：Qwen3 倾向中文数字（「两点」），SenseVoice 用阿拉伯数字（「2点」）。
- **首字**：`download.pytorch.org` 在这台机上只有 75 KB/s，重装环境时走清华/阿里镜像。

## 相关文件

```
asr_service/
  remote_server.py      跑在 gpu-box 的 Qwen3-ASR 服务（FastAPI + uvicorn）
  gateway.py            本机网关（优先远端 / 降级本地 / 熔断）
  smoke_test.py         端到端自检
  asr-qwen.service      gpu-box 的 systemd user unit
start_asr_gateway.bat   本机网关启动脚本
```
