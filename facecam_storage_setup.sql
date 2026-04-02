-- ============================================================
-- إنشاء حاوية Supabase Storage لفيديوهات الفيس كام
-- ============================================================

-- 1. إنشاء حاوية التخزين (Bucket)
-- ملاحظة: هذا الكود يعمل من Supabase Dashboard > Storage > New Bucket
-- أو يمكن تنفيذه عبر SQL Editor باستخدام:

INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES (
    'facecam_videos',
    'facecam_videos',
    false,
    104857600, -- 100MB حد أقصى
    ARRAY['video/mp4', 'video/quicktime', 'video/webm', 'video/x-matroska', 'image/jpeg', 'image/png', 'image/webp', 'image/bmp']
)
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- 2. إنشاء جدول facecam_storage لفهرسة الفيديوهات
-- ============================================================

CREATE TABLE IF NOT EXISTS public.facecam_storage (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    storage_bucket TEXT DEFAULT 'facecam_videos',
    storage_path TEXT,
    local_path TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- إنشاء فهارس للبحث السريع
CREATE INDEX IF NOT EXISTS idx_facecam_storage_source_id ON public.facecam_storage(source_id);
CREATE INDEX IF NOT EXISTS idx_facecam_storage_created_at ON public.facecam_storage(created_at);

-- ============================================================
-- 3. سياسات الوصول (RLS) لحاوية التخزين
-- ============================================================

-- تفعيل RLS على الجدول
ALTER TABLE public.facecam_storage ENABLE ROW LEVEL SECURITY;

-- سياسة القراءة: السماح للـ anon بقراءة السجلات
CREATE POLICY "Allow anon read facecam_storage"
ON public.facecam_storage FOR SELECT
TO anon
USING (true);

-- سياسة الإدراج: السماح للـ anon بالإدراج
CREATE POLICY "Allow anon insert facecam_storage"
ON public.facecam_storage FOR INSERT
TO anon
WITH CHECK (true);

-- سياسة التحديث: السماح للـ anon بالتحديث
CREATE POLICY "Allow anon update facecam_storage"
ON public.facecam_storage FOR UPDATE
TO anon
USING (true)
WITH CHECK (true);

-- سياسة الحذف: السماح للـ anon بالحذف
CREATE POLICY "Allow anon delete facecam_storage"
ON public.facecam_storage FOR DELETE
TO anon
USING (true);

-- ============================================================
-- 4. سياسات الوصول لحاوية التخزين (Storage Bucket)
-- ============================================================

-- سياسة الرفع
CREATE POLICY "Allow anon upload to facecam_videos"
ON storage.objects FOR INSERT
TO anon
WITH CHECK (bucket_id = 'facecam_videos');

-- سياسة القراءة
CREATE POLICY "Allow anon read from facecam_videos"
ON storage.objects FOR SELECT
TO anon
USING (bucket_id = 'facecam_videos');

-- سياسة التحديث
CREATE POLICY "Allow anon update facecam_videos"
ON storage.objects FOR UPDATE
TO anon
USING (bucket_id = 'facecam_videos')
WITH CHECK (bucket_id = 'facecam_videos');

-- سياسة الحذف
CREATE POLICY "Allow anon delete from facecam_videos"
ON storage.objects FOR DELETE
TO anon
USING (bucket_id = 'facecam_videos');

-- ============================================================
-- 5. سياسات service_role (للوصول الكامل)
-- ============================================================

-- سياسة القراءة لـ service_role
CREATE POLICY "Allow service_role read facecam_storage"
ON public.facecam_storage FOR SELECT
TO service_role
USING (true);

-- سياسة الإدراج لـ service_role
CREATE POLICY "Allow service_role insert facecam_storage"
ON public.facecam_storage FOR INSERT
TO service_role
WITH CHECK (true);

-- سياسة التحديث لـ service_role
CREATE POLICY "Allow service_role update facecam_storage"
ON public.facecam_storage FOR UPDATE
TO service_role
USING (true)
WITH CHECK (true);

-- سياسة الحذف لـ service_role
CREATE POLICY "Allow service_role delete facecam_storage"
ON public.facecam_storage FOR DELETE
TO service_role
USING (true);

-- سياسات الحاوية لـ service_role
CREATE POLICY "Allow service_role upload to facecam_videos"
ON storage.objects FOR INSERT
TO service_role
WITH CHECK (bucket_id = 'facecam_videos');

CREATE POLICY "Allow service_role read from facecam_videos"
ON storage.objects FOR SELECT
TO service_role
USING (bucket_id = 'facecam_videos');

CREATE POLICY "Allow service_role update facecam_videos"
ON storage.objects FOR UPDATE
TO service_role
USING (bucket_id = 'facecam_videos')
WITH CHECK (bucket_id = 'facecam_videos');

CREATE POLICY "Allow service_role delete from facecam_videos"
ON storage.objects FOR DELETE
TO service_role
USING (bucket_id = 'facecam_videos');

-- ============================================================
-- انتهى! ✅
-- ============================================================
