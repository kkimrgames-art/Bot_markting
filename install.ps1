# ============================================================
# BotMark — مثبّت بوت الأتمتة لـ Windows (PowerShell)
#
# الطريقة الأسرع (أمر واحد في PowerShell):
#   irm https://raw.githubusercontent.com/kkimrgames-art/Bot_markting/main/install.ps1 | iex
#
# أو نزّل الملف وشغّله:  powershell -ExecutionPolicy Bypass -File install.ps1
# ============================================================

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$RepoUrl = "https://github.com/kkimrgames-art/Bot_markting.git"
$ZipUrl  = "https://github.com/kkimrgames-art/Bot_markting/archive/refs/heads/main.zip"
$AppDir  = Join-Path $HOME ".botmark-app"

Write-Host ""
Write-Host "  =========================================="
Write-Host "   BotMark Installer (Windows)"
Write-Host "  =========================================="
Write-Host ""

# ---------- 1) تحميل ملفات البرنامج ----------
if (-not (Test-Path (Join-Path $AppDir "main.py"))) {
    Write-Host "  [1/4] تحميل البرنامج إلى: $AppDir"
    New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
    if (Get-Command git -ErrorAction SilentlyContinue) {
        git clone --depth 1 $RepoUrl $AppDir
    } elseif (Get-Command curl.exe -ErrorAction SilentlyContinue) {
        Push-Location $env:TEMP
        curl.exe -fsSL -o botmark.zip $ZipUrl
        Expand-Archive -Path botmark.zip -DestinationPath botmark-x -Force
        $src = Get-ChildItem (Join-Path $env:TEMP "botmark-x") -Directory | Select-Object -First 1
        Copy-Item -Path (Join-Path $src.FullName "*") -Destination $AppDir -Recurse -Force
        Remove-Item botmark.zip -Force
        Remove-Item botmark-x -Recurse -Force
        Pop-Location
    } else {
        Write-Host "  [1/4] تحميل البرنامج عبر Invoke-WebRequest..."
        $zip = Join-Path $env:TEMP "botmark.zip"
        Invoke-WebRequest -Uri $ZipUrl -OutFile $zip
        Expand-Archive -Path $zip -DestinationPath (Join-Path $env:TEMP "botmark-x") -Force
        $src = Get-ChildItem (Join-Path $env:TEMP "botmark-x") -Directory | Select-Object -First 1
        Copy-Item -Path (Join-Path $src.FullName "*") -Destination $AppDir -Recurse -Force
        Remove-Item $zip -Force
        Remove-Item (Join-Path $env:TEMP "botmark-x") -Recurse -Force
    }
} else {
    if (Get-Command git -ErrorAction SilentlyContinue) {
        git -C $AppDir pull --ff-only 2>$null | Out-Null
    }
}

# ---------- 2) التحقق من بايثون ----------
$UsePyLauncher = $false
if (Get-Command python -ErrorAction SilentlyContinue) {
    # ok
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $UsePyLauncher = $true
} else {
    Write-Host "  [X] Python غير مثبت على جهازك."
    Write-Host "      ثبّته من https://www.python.org/downloads/ (فعّل خيار Add to PATH)"
    Write-Host "      ثم أعد تشغيل المثبّت."
    Read-Host "  اضغط Enter للخروج"
    exit 1
}

function Invoke-Py {
    param([string[]]$PyArgs)
    if ($UsePyLauncher) { & py -3 @PyArgs } else { & python @PyArgs }
}

# ---------- 3) البيئة والمتطلبات ----------
$Venv = Join-Path $AppDir ".venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"

Write-Host "  [2/4] إنشاء بيئة بايثون معزولة..."
if (-not (Test-Path $VenvPy)) {
    Invoke-Py @("-m", "venv", $Venv)
}

Write-Host "  [3/4] تثبيت المتطلبات (قد يستغرق دقيقة أو أكثر)..."
& $VenvPy -m pip install --upgrade pip -q
& $VenvPy -m pip install -r (Join-Path $AppDir "requirements.txt") -q
& $VenvPy -m pip install -e $AppDir -q

# حفظ موقع المشروع (يُطلب ملف .env عند أول تشغيل فعلي)
& $VenvPy -m botmark setup --project-root $AppDir --quiet | Out-Null

# ---------- 4) تفعيل أمر botmark في PATH ----------
Write-Host "  [4/4] تفعيل أمر botmark في النظام..."
$ScriptsDir = Join-Path $Venv "Scripts"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$ScriptsDir*") {
    $newPath = ($userPath.TrimEnd(';')) + ";" + $ScriptsDir
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Host "      تمت إضافة المسار إلى PATH (يتفعّل في النوافذ الجديدة)"
}

Write-Host ""
Write-Host "  =========================================="
Write-Host "   ✓ BotMark installed globally"
Write-Host "  =========================================="
Write-Host ""
Write-Host "  ▶ افتح نافذة طرفية جديدة (cmd أو PowerShell)"
Write-Host "  ▶ شغّل من أي مكان:"
Write-Host "        botmark"
Write-Host ""
Write-Host "     أول تشغيل فقط: سيطلب منك اختيار ملف .env"
Write-Host "     أوامر مفيدة:  botmark setup | doctor | status | stop | logs"
Write-Host ""
