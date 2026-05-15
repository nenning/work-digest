@echo off
setlocal enabledelayedexpansion
REM Removes WorkDigest-* scheduled tasks registered by schedule-digest.bat.

set CONFIG_PATH=%~dp0digest\config.yaml
python -c "import yaml,os; cfg=yaml.safe_load(open(os.environ['CONFIG_PATH'])); [print(t) for t in cfg.get('schedule',{}).get('times',['08:00'])]" > times.tmp 2>nul
if %errorlevel% neq 0 (
    echo ERROR: Could not read config.yaml at %CONFIG_PATH%
    del times.tmp 2>nul
    exit /b 1
)

echo Removing work-digest scheduled tasks...
echo.

for /f "tokens=*" %%t in (times.tmp) do (
    set TIME=%%t
    set TASKNAME=WorkDigest-!TIME::=-!
    echo Removing !TASKNAME!...
    schtasks /delete /tn "!TASKNAME!" /f >nul 2>&1
    if !errorlevel! neq 0 (
        echo   WARNING: !TASKNAME! not found or could not be removed
    ) else (
        echo   OK
    )
)

del times.tmp
echo.
echo Done. Verify with: schtasks /query /fo TABLE ^| findstr /i "WorkDigest"
echo.
