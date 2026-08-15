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
    echo  [5/5] Warming OCR models...
    python scripts\warmup_models.py
    if errorlevel 1 (
        echo  [!] Warmup failed. Starting anyway; the first scan will be slow.
    )
) else (
    echo  [5/5] Warmup skipped
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
echo.
echo  For a TC22 over USB, forward the port to the device:
echo        adb reverse tcp:%PORT% tcp:%PORT%
echo.
echo  Ctrl+C to stop.
echo.

python -m uvicorn app.main:app --host %HOST% --port %PORT% %RELOAD%

endlocal
