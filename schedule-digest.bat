@echo off
setlocal enabledelayedexpansion
REM Windows Task Scheduler setup for work-digest.
REM Reads schedule times from config.yaml and registers scheduled tasks.

REM Warn if not running as Administrator (non-fatal; /ru %USERNAME% works without elevation)
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo NOTE: Not running as Administrator.
    echo Tasks will be registered for current user ^(%USERNAME%^) only.
    echo Re-run as Administrator only if registration fails below.
    echo.
)

REM Resolve full Python path so scheduled tasks work without inheriting interactive PATH
for /f "tokens=*" %%p in ('where.exe python 2^>nul') do (
    if not defined PYTHON_EXE set "PYTHON_EXE=%%p"
)
if not defined PYTHON_EXE (
    echo ERROR: python.exe not found. Install Python and re-run.
    exit /b 1
)

REM Read schedule times from config.yaml (lives alongside main.py in digest\)
set CONFIG_PATH=%~dp0digest\config.yaml
"!PYTHON_EXE!" -c "import yaml,os; cfg=yaml.safe_load(open(os.environ['CONFIG_PATH'])); [print(t) for t in cfg.get('schedule',{}).get('times',['08:00'])]" > times.tmp 2>nul
if %errorlevel% neq 0 (
    echo ERROR: Could not read config.yaml at %CONFIG_PATH%
    del times.tmp 2>nul
    exit /b 1
)

set SCRIPT_DIR=%~dp0
echo Registering work-digest scheduled tasks...
echo.

for /f "tokens=*" %%t in (times.tmp) do (
    set TIME=%%t
    set TASKNAME=WorkDigest-!TIME::=-!
    echo Registering !TASKNAME! at %%t...
    schtasks /create /tn "!TASKNAME!" /tr "\"!PYTHON_EXE!\" \"!SCRIPT_DIR!digest\main.py\"" /sc daily /st %%t /ru "%USERNAME%" /f
    if !errorlevel! neq 0 (
        echo   WARNING: Failed to register !TASKNAME!
    ) else (
        set "PS_PYTHON=!PYTHON_EXE!"
        set "PS_MAINPY=!SCRIPT_DIR!digest\main.py"
        set "PS_WORKDIR=!SCRIPT_DIR!"
        powershell -NonInteractive -Command "$t=Get-ScheduledTask '!TASKNAME!'; $a=New-ScheduledTaskAction -Execute $env:PS_PYTHON -Argument ([char]34+$env:PS_MAINPY+[char]34) -WorkingDirectory $env:PS_WORKDIR; $s=$t.Settings; $s.DisallowStartIfOnBatteries=$false; $s.StopIfGoingOnBatteries=$false; $s.StartWhenAvailable=$true; $s.ExecutionTimeLimit='PT1H'; Set-ScheduledTask '!TASKNAME!' -Action $a -Settings $s | Out-Null"
        if !errorlevel! neq 0 (
            echo   WARNING: Task registered but power/timing settings could not be applied
        ) else (
            echo   OK
        )
    )
)

del times.tmp
echo.
echo Registered tasks:
schtasks /query /fo TABLE | findstr /i "WorkDigest"
echo.
echo Run manually with: "!PYTHON_EXE!" digest\main.py --dry-run
echo.
