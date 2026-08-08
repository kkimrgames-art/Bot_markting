#!/usr/bin/env sh
# ============================================================================
#  BotMark Cloud Launcher — start.sh
#  شغّل البوت على أي مساحة سحابية أو سيرفر Linux بأمر واحد:
#      sh start.sh
#  أو مع ملف إعدادات محدد:
#      sh start.sh --env /path/to/.env
#  الملف الرئيسي المستخدم لتشغيل البوت: main.py
# ============================================================================
set -u

GREEN=""; YELLOW=""; RED=""; CYAN=""; BOLD=""; RESET=""
if [ -t 1 ]; then
  GREEN="\033[0;32m"; YELLOW="\033[1;33m"; RED="\033[0;31m"; CYAN="\033[0;36m"; BOLD="\033[1m"; RESET="\033[0m"
fi

say()  { printf '%b%s%b\n' "$CYAN" "$1" "$RESET"; }
ok()   { printf '%b%s%b\n' "$GREEN" "$1" "$RESET"; }
warn() { printf '%b%s%b\n' "$YELLOW" "$1" "$RESET"; }
fail() { printf '%b%s%b\n' "$RED" "$1" "$RESET"; }

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR" || { fail "❌ تعذّر الوصول إلى مجلد المشروع"; exit 1; }

ENV_FILE=""
NO_INSTALL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --env) ENV_FILE="${2:-}"; shift 2 ;;
    --no-install) NO_INSTALL=1; shift ;;
    *) fail "❌ خيار غير معروف: $1"; exit 1 ;;
  esac
done

echo "============================================================"
echo "  BotMark Cloud Launcher"
say "  📁 المشروع: $PROJECT_DIR"
echo "============================================================"

# ===================== الخطوة 1: Python =====================
PYTHON="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  fail "[✗] Python غير موجود. ثبّته أولاً:"
  echo "      sudo apt update && sudo apt install -y python3 python3-venv python3-pip"
  exit 1
fi
ok "[✓] Python: $("$PYTHON" -V 2>&1)"

# ===================== الخطوة 2: البيئة الافتراضية والمتطلبات =====================
VENV_DIR="$PROJECT_DIR/.venv"
PY_VENV="$VENV_DIR/bin/python"

if [ ! -x "$PY_VENV" ]; then
  say "[1/4] إنشاء بيئة Python معزولة (.venv)..."
  "$PYTHON" -m venv "$VENV_DIR" || { fail "[✗] فشل إنشاء البيئة الافتراضية"; exit 1; }
fi

if [ -z "$NO_INSTALL" ] && ! "$PY_VENV" -c "import telegram, aiohttp, dotenv, supabase" >/dev/null 2>&1; then
  say "[2/4] تثبيت المتطلبات (requirements.txt)..."
  "$VENV_DIR/bin/pip" install --quiet --upgrade pip 2>/dev/null
  "$VENV_DIR/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt" || {
    fail "[✗] فشل تثبيت المتطلبات. أعد المحاولة لاحقاً."
    exit 1
  }
fi
ok "[✓] المتطلبات جاهزة."

# ===================== الخطوة 3: ملف الإعدادات =====================
if [ -z "$ENV_FILE" ]; then
  ENV_FILE="${BOTMARK_ENV_FILE:-}"
fi
if [ -z "$ENV_FILE" ] && [ -f "$PROJECT_DIR/.env" ]; then
  ENV_FILE="$PROJECT_DIR/.env"
fi

if [ -n "$ENV_FILE" ] && [ -f "$ENV_FILE" ]; then
  say "[3/4] تحميل الإعدادات من: $ENV_FILE"
  # تصدير المتغيرات إلى بيئة العملية قبل تشغيل main.py
  while IFS= read -r line; do
    case "$line" in
      ''|\#*) continue ;;
    esac
    export "$line"
  done < "$ENV_FILE"
else
  warn "[3/4] لا يوجد ملف إعدادات. سيتم استخدام متغيرات البيئة فقط."
fi

# ===================== الخطوة 4: فحص البيانات المطلوبة =====================
MISSING=""
check_var() {
  name="$1"; label="$2"
  val="$(printenv "$name" 2>/dev/null || true)"
  if [ -z "$val" ]; then
    fail "  [✗] $name مفقود  ← $label"
    MISSING="$MISSING $name"
  fi
}

say "[4/4] فحص البيانات المطلوبة:"
check_var "TELEGRAM_BOT_TOKEN" "توكن البوت من BotFather"
check_var "SUPABASE_URL"       "رابط قاعدة البيانات Supabase"
check_var "SUPABASE_KEY"       "مفتاح قاعدة البيانات Supabase"

case " $MISSING " in
  *" TELEGRAM_BOT_TOKEN "*)
    echo ""
    fail "❌ البوت لا يمكن تشغيله بدون توكن Telegram (TELEGRAM_BOT_TOKEN)."
    echo "   أنشئ ملف إعدادات من القالب ثم أعد المحاولة:"
    printf '      %bcp env.template .env%b\n' "$BOLD" "$RESET"
    echo "      nano .env      # ضع التوكن وبيانات Supabase ثم احفظ"
    echo "      sh start.sh"
    exit 1
    ;;
esac

if [ -n "$MISSING" ]; then
  warn "⚠️  تحذير: بعض البيانات (Supabase) غير متوفرة."
  echo "   البوت سيعمل بوضع محلي، لكن المزامنة مع قاعدة البيانات لن تعمل حتى توفرها."
fi

echo ""
printf '%b🚀 تشغيل البوت عبر main.py ...%b\n' "$GREEN$BOLD" "$RESET"
echo "------------------------------------------------------------"
export PYTHONUNBUFFERED=1
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
exec "$PY_VENV" -u "$PROJECT_DIR/main.py" "$@"
