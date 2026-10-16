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


pyinstaller --noconfirm Callink.spec

if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Build failed!
    pause
    exit /b 1
)

echo.
echo Copying Callink.exe to root and Desktop...
copy /y dist\Callink.exe .\Callink.exe >nul 2>&1
if exist "C:\Users\tanup\OneDrive\Desktop" (
    copy /y dist\Callink.exe "C:\Users\tanup\OneDrive\Desktop\Callink.exe" >nul 2>&1
    echo [OK] Updated Desktop: C:\Users\tanup\OneDrive\Desktop\Callink.exe
)

echo.
echo ============================================================
echo [SUCCESS] Single standalone executable built successfully!
echo Location: dist\Callink.exe
echo Desktop : C:\Users\tanup\OneDrive\Desktop\Callink.exe
echo.
echo ============================================================
pause

