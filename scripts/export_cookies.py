"""
أداة تصدير كوكيز YouTube لاستخدامها على Render
===============================================

الخطوات:
1. شغّل هذا السكريبت على جهازك المحلي (حيث تسجل دخول YouTube)
2. النتيجة: ملف cookies.txt + كود Base64 للنسخ إلى Render

الاستخدام:
    python scripts/export_cookies.py

بعد التشغيل:
    - انسخ قيمة YTDLP_COOKIES_B64 التي تظهر
    - الصقها كمتغير بيئي في Render
"""
import subprocess
import sys
import os
import base64


def main():
    print("=" * 60)
    print("  🍪 أداة تصدير كوكيز YouTube")
    print("=" * 60)
    
    # محاولة استخراج الكوكيز من المتصفحات المتاحة
    browsers = ["chrome", "firefox", "edge", "brave", "opera", "chromium"]
    cookies_file = os.path.join(os.path.dirname(__file__), "..", ".data", "yt_cookies.txt")
    os.makedirs(os.path.dirname(cookies_file), exist_ok=True)
    
    success = False
    for browser in browsers:
        print(f"\n🔍 محاولة استخراج من {browser}...")
        try:
            result = subprocess.run(
                [
                    sys.executable, "-m", "yt_dlp",
                    "--cookies-from-browser", browser,
                    "--cookies", cookies_file,
                    "--skip-download",
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
                ],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0 and os.path.exists(cookies_file):
                size = os.path.getsize(cookies_file)
                if size > 100:  # ملف كوكيز حقيقي
                    print(f"✅ تم استخراج الكوكيز من {browser} ({size} bytes)")
                    success = True
                    break
        except Exception as e:
            print(f"   ⚠️ فشل: {e}")

    if not success:
        print("\n❌ لم نتمكن من استخراج الكوكيز تلقائياً.")
        print("\n📖 الطريقة اليدوية:")
        print("1. ثبت إضافة 'Get cookies.txt LOCALLY' في Chrome/Firefox")
        print("2. افتح youtube.com وسجل الدخول")
        print("3. اضغط على الإضافة واختر 'Export'")
        print("4. احفظ الملف باسم cookies.txt في مجلد .data/")
        print(f"\n📍 المسار المتوقع: {os.path.abspath(cookies_file)}")
        
        # السماح بتحديد ملف يدوياً
        manual = input("\nأو اكتب مسار ملف الكوكيز (أو اضغط Enter للخروج): ").strip()
        if manual and os.path.exists(manual):
            import shutil
            shutil.copy2(manual, cookies_file)
            print(f"✅ تم نسخ الملف إلى {cookies_file}")
            success = True
        else:
            return

    if not success:
        return

    # إنشاء Base64
    with open(cookies_file, "r", encoding="utf-8") as f:
        content = f.read()

    b64 = base64.b64encode(content.encode("utf-8")).decode("utf-8")

    print("\n" + "=" * 60)
    print("  ✅ تم تصدير الكوكيز بنجاح!")
    print("=" * 60)
    print(f"\n📁 ملف الكوكيز: {os.path.abspath(cookies_file)}")
    print(f"📏 الحجم: {len(content)} حرف / {len(b64)} Base64")
    
    print("\n" + "=" * 60)
    print("  📋 انسخ القيمة التالية كمتغير بيئي في Render:")
    print("  Environment Variable: YTDLP_COOKIES_B64")
    print("  Legacy Alias Also Supported: YT_COOKIES_B64")
    print("=" * 60)
    print(f"\n{b64}\n")
    
    # حفظ في ملف أيضاً
    b64_file = cookies_file + ".b64"
    with open(b64_file, "w") as f:
        f.write(b64)
    print(f"💾 تم حفظ Base64 أيضاً في: {os.path.abspath(b64_file)}")
    
    print("\n" + "=" * 60)
    print("  🔧 إعداد Render:")
    print("=" * 60)
    print("  1. اذهب إلى Render Dashboard → Service → Environment")
    print("  2. أضف متغير جديد:")
    print("     Name:  YTDLP_COOKIES_B64")
    print("     Value: [الصق القيمة أعلاه]")
    print("  3. اضغط Save Changes ثم أعد نشر الخدمة")
    print()


if __name__ == "__main__":
    main()
