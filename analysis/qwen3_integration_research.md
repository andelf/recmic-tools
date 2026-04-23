# Qwen3 / Qwen3-Omni 接入快速调研（面向当前听写项目）

## 1. 结论先看

- 对当前项目（本地实时听写）来说，`Qwen3` 主线模型不适合作为直接 ASR 引擎。
- `Qwen3-Omni` 支持语音输入/语音输出与 ASR 类任务，但本地部署资源要求明显高于当前 `sherpa-onnx + SenseVoice` 方案。
- 若目标是“离线、低延迟、可在普通 Windows 设备运行”，当前最稳路线仍然是：
  - ASR：`sherpa-onnx`（你现在在用）
  - 可选增强：识别后再用 Qwen3 做文本纠错/润色/格式化

## 2. 与当前仓库需求的匹配度

当前仓库核心需求：
- 本地离线可用
- 低延迟按键触发听写
- 设备按键到文本输入稳定
- Windows 兼容

对比：
- `Qwen3`（LLM）
  - 优势：文本理解与生成强，适合后处理
  - 劣势：不是原生语音识别引擎
- `Qwen3-Omni`
  - 优势：原生多模态（含音频）
  - 劣势：部署重、参数复杂、显存要求高
- `sherpa-onnx + SenseVoice`
  - 优势：已经跑通、离线轻量、延迟可控
  - 劣势：文本后处理能力不如大模型

结论：短期不建议替换 ASR 主引擎；可考虑“ASR + Qwen3 文本后处理”的两段式方案。

## 3. 你关心的 RS/Rust 绑定

- 在本次调研中，`sherpa-onnx` 仓库明确有 `rust-api-examples`，并且近期提交提到 `Qwen3-ASR` 相关改动（例如 per-stream language hint）。
- 这意味着“Rust 生态 + ASR”在 `sherpa-onnx` 路线下是可行的。
- 但 `Qwen3-Omni` 的主流接入路径更偏向 Transformers / vLLM / Docker，而不是轻量 Rust 本地链路。

## 4. Qwen3-Omni 关键参数（调研摘录）

常见推理参数（vLLM 示例方向）：
- `temperature`
- `top_p`
- `top_k`
- `max_tokens`
- `max_model_len`
- `tensor_parallel_size`
- `limit_mm_per_prompt`
- `max_num_seqs`
- `gpu_memory_utilization`
- `use_audio_in_video`（多模态输入时要前后保持一致）

Transformers 侧关键点：
- 推荐版本：`transformers==4.57.3`（Qwen3-Omni 文档示例）
- 常见选项：
  - `dtype="auto"`
  - `device_map="auto"`
  - `attn_implementation="flash_attention_2"`（需硬件支持）

资源要求（Qwen3-Omni 文档里的量级）：
- 30B-A3B 系列 BF16 推理的最低显存需求很高（几十 GB 级别）
- 对当前“桌面离线听写工具”并不友好

## 5. 对当前项目的建议路线

### 路线 A（推荐，低风险）
- 保持当前 `dictation.py` 的 ASR 主链路（SenseVoice）
- 增加一个可选后处理步骤：
  - 对识别文本做标点恢复、口语整理、命令词规范化
  - 后端可接本地 Qwen3（若硬件允许）或远程 API

### 路线 B（探索，高成本）
- 新建实验脚本尝试 Qwen3-Omni 直接语音到文本/语音
- 需要单独准备较高显存环境
- 不建议直接替换现有生产脚本

## 6. 最终判断（当前阶段）

- 若目标是“马上提升可用性”：继续用当前 ASR 主链路，增加后处理最划算。
- 若目标是“研究前沿多模态能力”：可以并行开一个 `Qwen3-Omni` 实验分支，但不应影响现有稳定工作流。

## 7. 后续可执行项（可选）

1. 在当前 `dictation.py` 后增加 `--post-process` 钩子。
2. 新建 `analysis/qwen_postprocess_demo.py`，演示把 ASR 文本送入 Qwen 做纠错。
3. 评估 3 个指标：
   - 识别后文本可读性
   - 端到端延迟
   - CPU/GPU 资源占用
