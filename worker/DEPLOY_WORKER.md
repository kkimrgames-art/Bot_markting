# 🚀 نشر Worker التنزيل على Koyeb (مجاني 100%)

## المتطلبات
- حساب GitHub يحتوي على مشروع AutoModBot
- حساب Koyeb مجاني ([koyeb.com](https://app.koyeb.com))

---

## الخطوة 1: رفع الكود لـ GitHub

تأكد أن مجلد `worker/` يحتوي على:
```
worker/
├── downloader_worker.py   ← الخادم المحسّن
├── Dockerfile
└── requirements.txt
```

```bash
git add worker/
git commit -m "feat: optimized download worker for Koyeb free tier"
git push origin main
```

---

## الخطوة 2: إنشاء خدمة على Koyeb

1. سجل دخول على [app.koyeb.com](https://app.koyeb.com)
2. اضغط **Create Service** → **Web Service**
3. اختر **GitHub** كمصدر
4. اختر Repository الخاص بـ AutoModBot
5. في إعدادات الخدمة:
   - **Builder**: Docker
   - **Dockerfile path**: `worker/Dockerfile`
   - **Working directory**: `worker`
   - **Instance**: ⚠️ اختر **Free** فقط!
   - **Port**: `8080`
   - **Region**: Frankfurt أو Washington فقط

---

## الخطوة 3: إعداد المتغيرات البيئية في Koyeb

### متغيرات مطلوبة:

| المتغير | القيمة | ملاحظة |
|---|---|---|
| `DOWNLOADER_WORKER_TOKEN` | أي نص سري | لحماية API |
| `YTDLP_FORCE_IPV4` | `1` | موصى به |

### متغيرات اختيارية (حدود الأمان):

| المتغير | القيمة الافتراضية | ملاحظة |
|---|---|---|
| `WORKER_MAX_FILE_MB` | `50` | حد أقصى لحجم الفيديو (MB) |
| `WORKER_MAX_DURATION` | `120` | حد أقصى لمدة الفيديو (ثواني) |
| `WORKER_BW_LIMIT_GB` | `80` | حد شهري للنطاق الترددي (GB) |
| `WORKER_RATE_LIMIT` | `10` | أقصى عدد طلبات في الدقيقة |
| `WORKER_MIN_DISK_MB` | `200` | أقل مساحة قرص مطلوبة (MB) |

> ⚠️ **لا تغير هذه القيم إلا إذا كنت متأكداً!** القيم الافتراضية مصممة لتبقيك ضمن الحدود المجانية.

---

## الخطوة 4: تحديث إعدادات Render

بعد نجاح النشر، ستحصل على رابط مثل:
```
https://your-service-xxxxx.koyeb.app
```

أضف في **Render**:

| المتغير | القيمة |
|---|---|
| `DOWNLOADER_WORKER_URL` | `https://your-service-xxxxx.koyeb.app/download` |
| `DOWNLOADER_WORKER_TOKEN` | نفس التوكن |

---

## الخطوة 5: التحقق

```bash
# فحص الصحة
curl https://your-service-xxxxx.koyeb.app/healthz

# فحص الحالة والموارد
curl https://your-service-xxxxx.koyeb.app/status

# تنزيل تجريبي
curl -X POST https://your-service-xxxxx.koyeb.app/download \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/shorts/VIDEO_ID"}' \
  --output test.mp4
```

---

## 🛡️ أنظمة الحماية المدمجة

| الحماية | التفاصيل |
|---|---|
| **حجم الملف** | رفض أي ملف > 50MB |
| **مدة الفيديو** | رفض أي فيديو > 120 ثانية |
| **طلب واحد** | تنزيل واحد فقط في نفس الوقت |
| **Rate Limit** | 10 طلبات/دقيقة كحد أقصى |
| **مساحة القرص** | فحص تلقائي قبل كل تنزيل |
| **Bandwidth** | عدّاد شهري بحد 80GB (من 100GB) |
| **تنظيف تلقائي** | حذف الملفات المؤقتة بعد كل طلب |

---

## ⚠️ ملاحظات مهمة

- **النوم التلقائي:** Free Tier ينام بعد ساعة بدون طلبات (أول طلب ~30 ثانية)
- **اضبط Billing Alert على $5** في إعدادات Koyeb لاحتياط إضافي
- **لا تُنشئ خدمة ثانية** — خدمة واحدة فقط مجانية
- **لا تُنشئ قاعدة بيانات** — Worker لا يحتاجها
