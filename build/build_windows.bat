@echo off
rem ============================================================
rem  Build the classroom one-file EXE.  Run this ON Windows.
rem  (PyInstaller cannot cross-compile: a Linux build is a Linux binary.)
rem
rem  Prereq: Python 3.11-3.13 (64-bit) installed and on PATH.
rem ============================================================
setlocal
cd /d "%~dp0.."

python -m pip install --upgrade pip
python -m pip install "lxml>=4.9" pyinstaller
if errorlevel 1 goto fail

python build\build.py exe
if errorlevel 1 goto fail

echo.
echo ============================================================
echo  OK -^> dist\ooxml-doctor.exe
echo  Copy it (plus 使用用法.md) to the classroom USB stick.
echo ============================================================
pause
exit /b 0

:fail
echo.
echo [X] Build failed. See messages above.
pause
exit /b 1
