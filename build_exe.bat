@echo off
setlocal
title Build Callink Standalone Executable

echo ============================================================
echo           BUILDING CALLINK STANDALONE EXECUTABLE
echo ============================================================
echo.

where pyinstaller >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo Installing PyInstaller...
    pip install pyinstaller
)

echo Building standalone folder package in dist\CallinkGateway...
pyinstaller --onedir --clean ^
    --name "CallinkGateway" ^
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
echo [SUCCESS] Build finished!
echo Executable folder created at: dist\CallinkGateway\
echo.
echo To share with a friend:
echo 1. Copy .env into dist\CallinkGateway\ (or let them create one)
echo 2. Zip the dist\CallinkGateway\ folder and send it to them!
echo 3. They can just double-click CallinkGateway.exe to run.
echo ============================================================
pause
