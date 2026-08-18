@echo off
REM ===========================================================================
REM  Retail SKU OCR - start the backend from a fresh checkout.
REM
REM    run.bat                 set up if needed, then start the server
REM    run.bat --setup         force dependency reinstall
REM    run.bat --no-warmup     skip the OCR warmup (first scan pays for it)
REM    run.bat --reload        auto-reload on source changes (development)
REM
REM  Safe to run repeatedly. Every step is skipped when it is already done, so
REM  a second run starts the server in a few seconds.
REM ===========================================================================

setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "DO_SETUP="
set "DO_WARMUP=1"
set "RELOAD="

:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--setup"     set "DO_SETUP=1"
if /i "%~1"=="--no-warmup" set "DO_WARMUP="
if /i "%~1"=="--reload"    set "RELOAD=--reload"
shift
goto parse
:parsed

echo.
echo  Retail SKU OCR - backend
echo  ========================
echo.

REM --- Python ----------------------------------------------------------------
where python >nul 2>&1
if errorlevel 1 (
    echo  [X] Python is not on PATH. Install Python 3.12 and try again.
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set "PYVER=%%v"
echo  [1/5] Python !PYVER!

REM --- Dependencies ----------------------------------------------------------
REM  paddleocr is the slowest and least likely to be present, so it stands in
REM  for the whole requirements file.
python -c "import fastapi, paddleocr, pyodbc, cv2" >nul 2>&1
if errorlevel 1 set "DO_SETUP=1"

if defined DO_SETUP (
    echo  [2/5] Installing dependencies. First run downloads PaddlePaddle, which
    echo        is large - expect several minutes.
    python -m pip install --disable-pip-version-check -q -r requirements.txt
    if errorlevel 1 (
        echo  [X] Dependency install failed. See the output above.
        exit /b 1
    )
) else (
    echo  [2/5] Dependencies present
)

REM --- Configuration ---------------------------------------------------------
if not exist ".env" (
    copy /y ".env.example" ".env" >nul
    echo  [3/5] Created .env from .env.example - review SQL_CONNECTION_STRING
) else (
    echo  [3/5] Using existing .env
)

REM --- Database --------------------------------------------------------------
REM  Non-fatal. The scan pipeline still runs without SQL Server; results come
REM  back with persisted:false rather than being lost.
where sqlcmd >nul 2>&1
if errorlevel 1 (
    echo  [4/5] sqlcmd not found - skipping schema, server will run unpersisted
) else (
    sqlcmd -S "localhost\SQLEXPRESS" -E -i "sql\schema.sql" -b >nul 2>&1
    if errorlevel 1 (
        echo  [4/5] SQL Server unreachable - server will run unpersisted
    ) else (
        echo  [4/5] Database schema applied
    )
)

REM --- OCR models ------------------------------------------------------------
REM  PaddleOCR downloads models on first use. Doing it here means no operator
REM  ever waits on a download mid-scan.
if defined DO_WARMUP (
    echo  [5/6] Warming OCR models...
    python scripts\warmup_models.py
    if errorlevel 1 (
        echo  [!] Warmup failed. Starting anyway; the first scan will be slow.
    )
) else (
    echo  [5/6] Warmup skipped
)

REM --- Vision-language model -------------------------------------------------
REM  Only checked when .env has it switched on.
REM
REM  A VLM failure is swallowed by the scan path so one bad call cannot cost an
REM  operator a scan. That is right for one scan and wrong for a shift: with
REM  VLM_TRIGGER=always, a stopped Ollama means every scan quietly runs on one
REM  engine, and the operator cannot tell, because a missing contested-field
REM  chip looks identical to the two engines agreeing. So it is checked loudly
REM  here rather than discovered from an accuracy drop later.
REM
REM  Non-fatal. The backend is useful without it.
findstr /b /i /c:"VLM_ENABLED=true" .env >nul 2>&1
if errorlevel 1 (
    echo  [6/6] VLM disabled in .env - PP-OCRv5 only
) else (
    set "VLM_MODEL=qwen3-vl:4b-instruct"
    for /f "tokens=2 delims==" %%a in ('findstr /b /c:"VLM_MODEL=" .env') do set "VLM_MODEL=%%a"

    where ollama >nul 2>&1
    if errorlevel 1 (
        echo  [!] VLM is enabled but ollama is not on PATH. Every scan will run
        echo      single-engine and nothing on the device will say so.
    ) else (
        curl -s -m 3 http://127.0.0.1:11434/api/tags >nul 2>&1
        if errorlevel 1 (
            echo  [6/6] Starting Ollama...
            start "" /b ollama serve >nul 2>&1
            REM  Give the daemon a moment to bind its port before probing.
            powershell -NoProfile -Command "Start-Sleep -Seconds 3" >nul 2>&1
        )

        curl -s -m 5 http://127.0.0.1:11434/api/tags 2>nul | findstr /c:"!VLM_MODEL!" >nul 2>&1
        if errorlevel 1 (
            echo  [!] Ollama is up but !VLM_MODEL! is not installed. Pull it with:
            echo         ollama pull !VLM_MODEL!
        ) else (
            echo  [6/6] VLM ready - !VLM_MODEL!
        )
    )
)

REM --- Host and port ---------------------------------------------------------
set "HOST=0.0.0.0"
set "PORT=8000"
for /f "tokens=2 delims==" %%a in ('findstr /b /c:"API_HOST=" .env') do set "HOST=%%a"
for /f "tokens=2 delims==" %%a in ('findstr /b /c:"API_PORT=" .env') do set "PORT=%%a"

REM --- Port already in use? --------------------------------------------------
netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo.
    echo  [X] Port %PORT% is already in use. Stop the running server first:
    echo.
    echo        netstat -ano ^| findstr :%PORT%
    echo        taskkill /F /PID ^<pid^>
    echo.
    exit /b 1
)

echo.
echo  Listening on http://%HOST%:%PORT%
echo  Health:      http://localhost:%PORT%/api/v1/health
echo               ^(reports vlmReady - if false, scans are single-engine^)
echo.
echo  For a TC22 over USB, forward the port to the device:
echo        adb reverse tcp:%PORT% tcp:%PORT%
echo.
echo  Ctrl+C to stop.
echo.

python -m uvicorn app.main:app --host %HOST% --port %PORT% %RELOAD%

endlocal
