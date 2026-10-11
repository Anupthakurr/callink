@echo off
setlocal
title Build Callink Standalone Executable (.exe)

echo ============================================================
echo          BUILDING CALLINK STANDALONE .EXE EXECUTABLE
echo ============================================================
echo.

where pyinstaller >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo Installing PyInstaller...
    pip install pyinstaller
)

taskkill /f /im Callink.exe >nul 2>&1

echo Building single-file executable: dist\Callink.exe ...
echo (This may take 1-2 minutes to package all dependencies)
echo.


pyinstaller --onefile --clean ^
    --name "Callink" ^
    --collect-all sounddevice ^
    --collect-all winrt ^
    --collect-all google.genai ^
    --copy-metadata google-genai ^
    --copy-metadata sounddevice ^
    gsm_gemini_gateway.py

if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Build failed!
    pause
    exit /b 1
)

echo.
echo ============================================================
echo [SUCCESS] Single standalone executable built successfully!
echo Location: dist\Callink.exe
echo.
echo To share with a friend:
echo 1. Simply send them the single file: dist\Callink.exe
echo 2. Your friend can double-click Callink.exe directly!
echo    - No Python required.
echo    - If GEMINI_API_KEY is not set, it will prompt them to enter it on first run.
echo ============================================================
pause

