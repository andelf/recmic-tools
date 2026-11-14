@echo off
REM Start the local ASR gateway in the foreground so logs stay visible.
REM The gateway prefers Qwen3-ASR on gpu-box and falls back to SenseVoice.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [!] .venv not found. Run: python -m pip install -r requirements.txt
    exit /b 1
)

set ASR_GATEWAY_PORT=8799
if "%QWEN_ASR_URL%"=="" set QWEN_ASR_URL=http://192.0.2.175:8790

echo [*] ASR gateway on http://127.0.0.1:%ASR_GATEWAY_PORT%
echo [*] Remote engine: %QWEN_ASR_URL%
echo [*] Ctrl+C to stop.
echo.

".venv\Scripts\python.exe" asr_service\gateway.py
