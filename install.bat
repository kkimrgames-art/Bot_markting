@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo  ===========================================
echo   BotMark Installer (Windows)
echo   مسار المشروع: %CD%
echo  ===========================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo  [X] Python غير مثبت.
  echo      ثبّته من https://www.python.org/downloads/ مع تفعيل خيار "Add Python to PATH"
  pause
  exit /b 1
)

set "VENV_DIR=%CD%\.venv"

if not exist "%VENV_DIR%\Scripts\python.exe" (
  echo  [1/3] إنشاء بيئة بايثون معزولة...
  python -m venv "%VENV_DIR%"
)

echo  [2/3] تثبيت المتطلبات (قد يستغرق دقيقة أو أكثر)...
"%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip -q
"%VENV_DIR%\Scripts\python.exe" -m pip install -r requirements.txt -q
"%VENV_DIR%\Scripts\python.exe" -m pip install -e . -q

echo  [3/3] حفظ إعدادات المشروع...
"%VENV_DIR%\Scripts\python.exe" -m botmark setup --project-root "%CD%" --quiet >nul 2>&1

echo.
echo  ===========================================
echo   ✓ BotMark installed globally
echo  ===========================================
echo.
echo  ▶ أضف مسار الأوامر إلى PATH بشكل دائم:
echo      setx PATH "%PATH%;%VENV_DIR%\Scripts"
echo.
echo  ▶ ثم افتح نافذة طرفية جديدة وشغّل من أي مكان:
echo      botmark
echo.
echo  ▶ أول تشغيل سيطلب منك اختيار ملف .env
echo.
pause
