-- =====================================================
-- إنشاء جدول مصادقة Google Drive
-- setup_gdrive_auth.sql
-- قم بتشغيل هذا السكريبت في Supabase SQL Editor
-- =====================================================

-- إنشاء الجدول
CREATE TABLE IF NOT EXISTS gdrive_auth (
    id TEXT PRIMARY KEY DEFAULT 'gdrive_main',
    token TEXT NOT NULL DEFAULT '',
    refresh_token TEXT NOT NULL DEFAULT '',
    token_uri TEXT NOT NULL DEFAULT 'https://oauth2.googleapis.com/token',
    client_id TEXT NOT NULL DEFAULT '',
    client_secret TEXT NOT NULL DEFAULT '',
    scopes TEXT NOT NULL DEFAULT '[]',
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- تفعيل Row Level Security
ALTER TABLE gdrive_auth ENABLE ROW LEVEL SECURITY;

-- سياسة السماح بالوصول الكامل (لأن البوت هو المستخدم الوحيد)
DROP POLICY IF EXISTS "Allow all for authenticated" ON gdrive_auth;
CREATE POLICY "Allow all for authenticated" ON gdrive_auth
    FOR ALL
    USING (true)
    WITH CHECK (true);

-- إدراج سجل افتراضي (فارغ) لضمان عمل UPSERT
INSERT INTO gdrive_auth (id, token, refresh_token, token_uri, client_id, client_secret, scopes, updated_at)
VALUES ('gdrive_main', '', '', 'https://oauth2.googleapis.com/token', '', '', '[]', NOW())
ON CONFLICT (id) DO NOTHING;

-- =====================================================
-- ملاحظات:
-- 1. الجدول مصمم لتخزين بيانات مصادقة Google Drive
-- 2. فقط سجل واحد موجود (id = 'gdrive_main')
-- 3. يتم حفظ access_token و refresh_token
-- 4. يتم التحديث التلقائي عبر البوت
-- =====================================================
