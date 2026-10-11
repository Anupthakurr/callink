@echo off
setlocal enabledelayedexpansion
title Callink - GSM to Gemini Live AI Voice Gateway

echo ============================================================
echo        CALLINK: GSM -^> GEMINI LIVE AI VOICE GATEWAY
echo ============================================================
echo.

:: 1. Check if Python is installed
where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Python is not installed or not in your system PATH!
    echo Please install Python 3.9, 3.10, or 3.11 from https://www.python.org/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

:: 2. Check for .env file and prompt if missing
if not exist ".env" (
    echo [SETUP] No .env file found. Setting up your API key...
    echo.
    set /p API_KEY="Enter your Google Gemini API Key: "
    if "!API_KEY!"=="" (
        echo [ERROR] No API key entered! Exiting.
        pause
        exit /b 1
    )
    echo GEMINI_API_KEY=!API_KEY! > .env
    echo [SUCCESS] .env file created successfully!
    echo.
)

:: 3. Setup Virtual Environment
if not exist "venv\Scripts\activate.bat" (
    echo [SETUP] Creating isolated Python virtual environment (venv)...
    python -m venv venv
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo [SETUP] Installing required dependencies (this only happens once)...
    venv\Scripts\python.exe -m pip install --upgrade pip >nul 2>&1
    venv\Scripts\pip.exe install -r requirements.txt
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] Failed to install dependencies from requirements.txt.
        pause
        exit /b 1
    )
    echo [SUCCESS] Dependencies installed!
    echo.
)

:: 4. Verify dependencies installed in venv
venv\Scripts\python.exe -c "import google.genai, sounddevice, winrt" >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [SETUP] Updating / checking dependencies...
    venv\Scripts\pip.exe install -r requirements.txt
    echo.
)

:: 5. Launch the Voice Gateway
echo Starting AI Gateway...
echo Make sure your Android smartphone is paired to this PC via Bluetooth
echo with "Hands-Free Audio" enabled.
echo.
venv\Scripts\python.exe gsm_gemini_gateway.py

if %ERRORLEVEL% neq 0 (
    echo.
    echo [INFO] Gateway exited with an error.
    pause
)
