#!/bin/sh
# ============================================================
# BotMark — مثبّت بوت الأتمتة على جهازك (Linux / macOS)
#
# طريقتان للاستخدام:
#   1) أمر واحد مباشر (بعد رفع الملفات إلى GitHub):
#      bash -c "$(curl -fsSL https://raw.githubusercontent.com/kkimrgames-art/Bot_markting/main/install.sh)"
#
#   2) تحميل المشروع وتشغيله محلياً:
#      bash install.sh        (أو  sh install.sh)
#
# بعد التثبيت، يعمل أمر  botmark  من أي مجلد في الطرفية.
# ============================================================
set -e

REPO_URL="https://github.com/kkimrgames-art/Bot_markting.git"
# مجلد تثبيت التطبيق (البرنامج "مثبت" هنا كتطبيق، لا تحتاج لفتحه أبداً)
APP_DIR="${BOTMARK_APP_DIR:-$HOME/.botmark-app}"

echo ""
echo "🚀 BotMark Installer"
echo "   -----------------------------"

# ---------- الوضع المباشر (Streamed): تحميل المشروع ثم المتابعة ----------
if [ ! -f "$0" ]; then
  echo "📡 وضع التحميل المباشر — جارٍ تنزيل البرنامج إلى:"
  echo "   $APP_DIR"
  if command -v git >/dev/null 2>&1; then
    if [ ! -d "$APP_DIR/.git" ]; then
      git clone --depth 1 "$REPO_URL" "$APP_DIR"
    else
      git -C "$APP_DIR" pull --ff-only 2>/dev/null || true
    fi
  elif command -v curl >/dev/null 2>&1 && command -v unzip >/dev/null 2>&1; then
    mkdir -p "$APP_DIR"
    curl -fsSL "$REPO_URL/archive/refs/heads/main.zip" -o /tmp/botmark.zip
    unzip -oq /tmp/botmark.zip -d /tmp/botmark-extract
    cp -R /tmp/botmark-extract/Bot_markting-main/. "$APP_DIR/" 2>/dev/null || true
    rm -rf /tmp/botmark.zip /tmp/botmark-extract
  else
    echo "❌ تحتاج إلى git أو (curl + unzip) لتحميل البرنامج."
    exit 1
  fi
  cd "$APP_DIR"
  echo "✅ تم التنزيل — المتابعة بالتثبيت..."
  exec bash "$APP_DIR/install.sh"
fi

# ---------- الوضع المحلي (تشغيل الملف من داخل المشروع) ----------
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$PROJECT_ROOT/.venv"
BIN_DIR="$HOME/.local/bin"

command -v python3 >/dev/null 2>&1 || {
  echo "❌ Python 3 غير مثبت على جهازك."
  echo "   ثبّته من https://www.python.org/downloads/ ثم أعد التشغيل."
  exit 1
}

echo "   المشروع: $PROJECT_ROOT"
echo "   بايثون:  $(python3 --version 2>/dev/null || echo '?')"
echo ""

command -v ffmpeg >/dev/null 2>&1 || {
  echo "⚠️  ffmpeg غير موجود — لا تقلق، البوت يثبّته تلقائياً عند أول تشغيل."
}

echo "📦 [1/3] إنشاء بيئة بايثون معزولة..."
if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi

echo "📥 [2/3] تثبيت المتطلبات (قد يستغرق دقيقة أو أكثر)..."
"$VENV_DIR/bin/python" -m pip install --upgrade pip -q
"$VENV_DIR/bin/python" -m pip install -r "$PROJECT_ROOT/requirements.txt" -q
"$VENV_DIR/bin/python" -m pip install -e "$PROJECT_ROOT" -q

echo "🔗 [3/3] تفعيل أمر botmark في النظام..."
mkdir -p "$BIN_DIR"
ln -sf "$VENV_DIR/bin/botmark" "$BIN_DIR/botmark"

# حفظ موقع المشروع (يُطلب ملف .env عند أول تشغيل فعلي)
"$VENV_DIR/bin/python" -m botmark setup --project-root "$PROJECT_ROOT" --quiet >/dev/null 2>&1 || true

# إضافة المسار إلى PATH تلقائياً (في ملف إعدادات الشل)
if [ -z "$(command -v botmark 2>/dev/null)" ] && [ -n "$HOME" ]; then
  RC_FILE=""
  for cand in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.bash_profile"; do
    if [ -f "$cand" ]; then
      RC_FILE="$cand"
      break
    fi
  done
  if [ -z "$RC_FILE" ]; then
    RC_FILE="$HOME/.bashrc"
    : > "$RC_FILE"
  fi
  if ! grep -q "\.local/bin" "$RC_FILE" 2>/dev/null; then
    {
      echo ""
      echo "# BotMark"
      echo "export PATH=\"$BIN_DIR:\$PATH\""
    } >> "$RC_FILE"
    echo "✅ أُضيف .local/bin إلى PATH في: $RC_FILE"
  fi
fi

echo ""
echo "=========================================="
echo "   ✓ BotMark installed globally"
echo "=========================================="
echo ""
echo "▶ افتح نافذة طرفية جديدة (أو نفّذ: source ~/.bashrc)"
echo "▶ شغّل من أي مكان:"
echo "      botmark"
echo ""
echo "   أول تشغيل فقط: سيطلب منك اختيار ملف .env"
echo "   أوامر مفيدة:  botmark setup | doctor | status | stop | logs"
echo ""
