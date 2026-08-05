# ============================================================
# BotMark — مثبّت بوت الأتمتة لـ Windows (PowerShell)
#
# الطريقة الأسرع (أمر واحد في PowerShell):
#   irm https://raw.githubusercontent.com/kkimrgames-art/Bot_markting/main/install.ps1 | iex
#
# أو نزّل الملف وشغّله:  powershell -ExecutionPolicy Bypass -File install.ps1
#
# ملاحظة: BotMark ليس على npm — التثبيت يكون عبر هذا المثبّت فقط.
# ============================================================

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
if (-not $HOME) { $HOME = $env:USERPROFILE }
if (-not $env:LOCALAPPDATA) { $env:LOCALAPPDATA = Join-Path $env:USERPROFILE "AppData\Local" }

$RepoUrl = "https://github.com/kkimrgames-art/Bot_markting.git"
$ZipUrl  = "https://github.com/kkimrgames-art/Bot_markting/archive/refs/heads/main.zip"
$AppDir  = Join-Path $HOME ".botmark-app"

Write-Host ""
Write-Host "  =========================================="
Write-Host "   BotMark Installer (Windows)"
Write-Host "  =========================================="
Write-Host ""

# ---------- 1) تحميل ملفات البرنامج ----------
# ملاحظة مهمة: لا نعتمد على $env:TEMP إطلاقاً (قد يكون معطلاً على بعض الأجهزة)،
# بل نستخدم مجلداً مؤقتاً خاصاً داخل مجلد التطبيق نفسه.
if (-not (Test-Path (Join-Path $AppDir "main.py"))) {
    Write-Host "  [1/4] تحميل البرنامج إلى: $AppDir"
    New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
    if (Get-Command git -ErrorAction SilentlyContinue) {
        git clone --depth 1 $RepoUrl $AppDir
    } else {
        $TmpDir = Join-Path $AppDir ".install-tmp"
        New-Item -ItemType Directory -Force -Path $TmpDir | Out-Null
        $ZipPath = Join-Path $TmpDir "botmark.zip"
        try {
            if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
                & curl.exe -fsSL -o $ZipPath $ZipUrl
            } else {
                Write-Host "  [1/4] تحميل البرنامج عبر Invoke-WebRequest..."
                Invoke-WebRequest -Uri $ZipUrl -OutFile $ZipPath
            }
            Expand-Archive -Path $ZipPath -DestinationPath $TmpDir -Force
            $src = Get-ChildItem (Join-Path $TmpDir "Bot_markting-main") -Directory -ErrorAction SilentlyContinue |
                   Select-Object -First 1
            if (-not $src) {
                $src = Get-ChildItem $TmpDir -Directory | Select-Object -First 1
            }
            if (-not $src) {
                throw "تعذر العثور على ملفات المشروع بعد فك الضغط."
            }
            Copy-Item -Path (Join-Path $src.FullName "*") -Destination $AppDir -Recurse -Force
        } finally {
            Remove-Item $TmpDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
} else {
    if (Get-Command git -ErrorAction SilentlyContinue) {
        git -C $AppDir pull --ff-only 2>$null | Out-Null
    }
}

# ---------- 2) التحقق من بايثون (أو تثبيته تلقائياً) ----------
$PyExe = $null          # مسار مباشر لـ python.exe
$UsePyLauncher = $false # استخدام py -3

if (-not $PyExe -and (Get-Command python -ErrorAction SilentlyContinue)) {
    $PyExe = (Get-Command python).Source
}
if (-not $PyExe -and (Get-Command py -ErrorAction SilentlyContinue)) {
    $UsePyLauncher = $true
}
if (-not $PyExe -and -not $UsePyLauncher) {
    foreach ($p in @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe")
    )) {
        if (Test-Path $p) { $PyExe = $p; break }
    }
}

if (-not $PyExe -and -not $UsePyLauncher) {
    Write-Host "  [X] Python غير مثبت على جهازك."
    $ans = Read-Host "      هل تريد أن يثبّت المثبّت Python تلقائياً؟ (y/n)"
    if ($ans -match '^y') {
        $PyVer = "3.11.9"
        $arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "amd64" }
        $pyUrl = "https://www.python.org/ftp/python/$PyVer/python-$PyVer-$arch.exe"
        $Installer = Join-Path $AppDir ".install-tmp"
        New-Item -ItemType Directory -Force -Path $Installer | Out-Null
        $InstallerExe = Join-Path $Installer "python-setup.exe"
        Write-Host "  ⏳ تحميل Python $PyVer (حوالي 25MB)..."
        & curl.exe -fsSL -o $InstallerExe $pyUrl
        Write-Host "  ⏳ تثبيت Python تلقائياً (يستغرق دقيقة)..."
        Start-Process -Wait -FilePath $InstallerExe -ArgumentList @("/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_launcher=1", "Include_test=0")
        Remove-Item $Installer -Recurse -Force -ErrorAction SilentlyContinue
        $known = Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"
        if (Test-Path $known) { $PyExe = $known }
        if (-not $PyExe) {
            foreach ($p in @(
                (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
                (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
                (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
                (Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe")
            )) {
                if (Test-Path $p) { $PyExe = $p; break }
            }
        }
    }
    if (-not $PyExe -and -not $UsePyLauncher) {
        Write-Host "  ❌ لم يتم العثور على بايثون بعد التثبيت."
        Write-Host "      ثبّته يدوياً من: https://www.python.org/downloads/"
        Write-Host "      ⚠️ أثناء التثبيت فعّل خيار (Add python.exe to PATH)"
        Write-Host "      ثم أعد تشغيل نفس الأمر."
        Read-Host "  اضغط Enter للخروج"
        exit 1
    }
}

function Invoke-Py {
    param([string[]]$PyArgs)
    if ($PyExe) { & $PyExe @PyArgs } else { & py -3 @PyArgs }
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
