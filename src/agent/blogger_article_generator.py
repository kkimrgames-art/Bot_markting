#!/usr/bin/env python3
"""
Blogger Article Generator - توليد مقالات البلوجر
يدعم ثلاثة أوضاع:
  1. AI Prompt: توليد مقال بالذكاء الاصطناعي بناءً على برومت مخصص
  2. Templates: استخدام مقالات افتراضية محفوظة (ترتيبي أو عشوائي)
  3. Fallback: مقال بسيط بدون AI
"""
import os
import json
import random
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime

logger = logging.getLogger(__name__)


# ==================== وضع AI (برومبت مخصص) ====================

def generate_article_ai(
    ai_prompt: str,
    video_title: str,
    video_description: str = "",
    content_type: str = "",
    source_name: str = "",
    language: str = "ar",
    tags: List[str] = None,
) -> Dict[str, str]:
    """
    توليد مقال بالذكاء الاصطناعي باستخدام برومت مخصص.
    يعيد {"title": "...", "content": "...", "labels": [...]}
    """
    from .ai import generate_ai_article_for_blogger

    context_info = f""
    if video_title:
        context_info += f"عنوان الفيديو: {video_title}\n"
    if video_description:
        context_info += f"وصف الفيديو: {video_description[:500]}\n"
    if content_type:
        context_info += f"نوع المحتوى: {content_type}\n"
    if source_name:
        context_info += f"اسم المصدر: {source_name}\n"
    if tags:
        context_info += f"الهاشتاقات: {', '.join(tags)}\n"

    try:
        result = generate_ai_article_for_blogger(
            custom_prompt=ai_prompt,
            context_info=context_info,
            language=language,
        )
        if result:
            return result
    except Exception as e:
        logger.warning(f"AI article generation failed: {e}")

    # Fallback إذا فشل AI
    return generate_fallback_article(
        video_title=video_title,
        content_type=content_type,
        language=language,
    )


# ==================== وضع القوالب ====================

def get_next_template(link_config: Dict) -> Optional[Dict]:
    """
    الحصول على القالب التالي بناءً على وضع الاختيار.
    يعيد {"title": "...", "content": "...", "labels": [...]}
    أو None إذا لم تكن هناك قوالب.
    """
    templates_raw = link_config.get("templates", "[]")
    try:
        templates = json.loads(templates_raw) if isinstance(templates_raw, str) else templates_raw
    except Exception:
        templates = []

    if not templates:
        return None

    order = link_config.get("templates_order", "sequential")
    current_idx = link_config.get("template_index", 0) or 0

    if order == "random":
        template = random.choice(templates)
    else:
        # ترتيبي
        idx = current_idx % len(templates)
        template = templates[idx]
        # تحديث المؤشر
        from .blogger_db import increment_template_index
        increment_template_index(link_config["id"])

    return template


def generate_article_template(link_config: Dict, video_title: str = "") -> Dict[str, str]:
    """
    توليد مقال من القوالب المحفوظة.
    يستبدل المتغيرات: {video_title}, {date}, {time}
    """
    template = get_next_template(link_config)
    if not template:
        # لا توجد قوالب، نستخدم fallback
        return generate_fallback_article(
            video_title=video_title,
            content_type="",
            language=link_config.get("article_language", "ar"),
        )

    now = datetime.now()
    replacements = {
        "{video_title}": video_title or "",
        "{date}": now.strftime("%Y-%m-%d"),
        "{time}": now.strftime("%H:%M"),
        "{datetime}": now.strftime("%Y-%m-%d %H:%M"),
        "{year}": str(now.year),
        "{month}": now.strftime("%B"),
    }

    title = template.get("title", "مقال جديد")
    content = template.get("content", "محتوى المقال هنا...")
    labels = template.get("labels", [])

    for key, value in replacements.items():
        title = title.replace(key, value)
        content = content.replace(key, value)

    return {
        "title": title,
        "content": content,
        "labels": labels if isinstance(labels, list) else [],
    }


# ==================== نظام الاحتياطي (بدون AI) ====================

def generate_fallback_article(
    video_title: str = "",
    content_type: str = "",
    language: str = "ar",
) -> Dict[str, str]:
    """
    توليد مقال بسيط بدون ذكاء اصطناعي.
    يعيد {"title": "...", "content": "...", "labels": [...]}
    """
    now = datetime.now()

    if language == "ar" or not language:
        title = video_title or f"مقال جديد - {now.strftime('%Y-%m-%d')}"
        content = _build_fallback_content_ar(video_title, content_type, now)
        labels = [content_type] if content_type else ["مقال"]
    elif language == "en":
        title = video_title or f"New Article - {now.strftime('%Y-%m-%d')}"
        content = _build_fallback_content_en(video_title, content_type, now)
        labels = [content_type] if content_type else ["article"]
    elif language == "tr":
        title = video_title or f"Yeni Makale - {now.strftime('%Y-%m-%d')}"
        content = _build_fallback_content_tr(video_title, content_type, now)
        labels = [content_type] if content_type else ["makale"]
    elif language == "fr":
        title = video_title or f"Nouvel Article - {now.strftime('%Y-%m-%d')}"
        content = _build_fallback_content_fr(video_title, content_type, now)
        labels = [content_type] if content_type else ["article"]
    elif language == "es":
        title = video_title or f"Nuevo Artículo - {now.strftime('%Y-%m-%d')}"
        content = _build_fallback_content_es(video_title, content_type, now)
        labels = [content_type] if content_type else ["artículo"]
    else:
        title = video_title or f"New Article - {now.strftime('%Y-%m-%d')}"
        content = _build_fallback_content_en(video_title, content_type, now)
        labels = [content_type] if content_type else ["article"]

    return {"title": title, "content": content, "labels": labels}


def _build_fallback_content_ar(video_title: str, content_type: str, now: datetime) -> str:
    """بناء محتوى احتياطي بالعربية"""
    html = f"""<div dir="rtl" style="font-family: Arial, sans-serif; line-height: 1.8;">
<h2>{video_title or 'مقال جديد'}</h2>
<p>مرحباً بكم في مقالنا الجديد حول موضوع <b>{content_type or 'المحتوى'}</b>.</p>
<p>في هذا المقال نستعرض أحدث المعلومات والتفاصيل المتعلقة بهذا الموضوع المثير للاهتمام. تابعوا معنا للحصول على أفضل المحتوى.</p>
<h3>💡 لمحة عن المحتوى</h3>
<p>نقدم لكم اليوم محتوى مميزاً ومتنوعاً يتناول مواضيع مهمة في عالم {content_type or 'التكنولوجيا والترفيه'}. هذا المحتوى تم إعداده بعناية ليقدم لكم تجربة فريدة ومفيدة.</p>
<h3>🎯 التفاصيل</h3>
<p>يشمل المحتوى مجموعة من النقاط المهمة التي نرجو أن تنال إعجابكم. لا تترددوا في مشاركة المحتوى مع أصدقائكم.</p>
<h3>📌 الخلاصة</h3>
<p>شكراً لمتابعتكم! لا تنسوا مشاهدة الفيديو المرتبط بهذا المقال ومشاركته مع أصدقائكم.</p>
<p><small>تاريخ النشر: {now.strftime('%Y-%m-%d')}</small></p>
</div>"""
    return html


def _build_fallback_content_en(video_title: str, content_type: str, now: datetime) -> str:
    """Build fallback content in English"""
    html = f"""<div style="font-family: Arial, sans-serif; line-height: 1.8;">
<h2>{video_title or 'New Article'}</h2>
<p>Welcome to our latest article about <b>{content_type or 'this topic'}</b>.</p>
<p>In this article, we cover the latest information and details related to this exciting topic. Stay with us for the best content.</p>
<h3>💡 Overview</h3>
<p>We present unique and diverse content about {content_type or 'technology and entertainment'}. This content has been carefully prepared to give you a unique and useful experience.</p>
<h3>🎯 Details</h3>
<p>The content includes several important points that we hope you'll enjoy. Don't hesitate to share it with your friends.</p>
<h3>📌 Conclusion</h3>
<p>Thank you for following! Don't forget to watch the video linked to this article and share it with your friends.</p>
<p><small>Published: {now.strftime('%Y-%m-%d')}</small></p>
</div>"""
    return html


def _build_fallback_content_tr(video_title: str, content_type: str, now: datetime) -> str:
    """Build fallback content in Turkish"""
    html = f"""<div dir="ltr" style="font-family: Arial, sans-serif; line-height: 1.8;">
<h2>{video_title or 'Yeni Makale'}</h2>
<p>En son <b>{content_type or 'bu konu'}</b> hakkındaki makalemize hoş geldiniz.</p>
<p>Bu makalede, bu heyecan verici konuyla ilgili en son bilgileri ve detayları ele alıyoruz.</p>
<h3>💡 Genel Bakış</h3>
<p>{content_type or 'Teknoloji ve eğlence'} hakkında benzersiz ve çeşitli içerik sunuyoruz.</p>
<h3>🎯 Detaylar</h3>
<p>İçerik, beğeneceğinizi umduğumuz birkaç önemli nokta içeriyor. Arkadaşlarınızla paylaşmaktan çekinmeyin.</p>
<h3>📌 Sonuç</h3>
<p>Takip ettiğiniz için teşekkürler! Bu makaleye bağlı videoyu izlemeyi unutmayın.</p>
<p><small>Yayınlanma: {now.strftime('%Y-%m-%d')}</small></p>
</div>"""
    return html


def _build_fallback_content_fr(video_title: str, content_type: str, now: datetime) -> str:
    """Build fallback content in French"""
    html = f"""<div style="font-family: Arial, sans-serif; line-height: 1.8;">
<h2>{video_title or 'Nouvel Article'}</h2>
<p>Bienvenue dans notre dernier article sur <b>{content_type or 'ce sujet'}</b>.</p>
<p>Dans cet article, nous couvrons les dernières informations liées à ce sujet passionnant.</p>
<h3>💡 Aperçu</h3>
<p>Nous présentons un contenu unique sur {content_type or 'la technologie et le divertissement'}.</p>
<h3>🎯 Détails</h3>
<p>Le contenu comprend plusieurs points importants que nous espérons que vous apprécierez.</p>
<h3>📌 Conclusion</h3>
<p>Merci de nous suivre ! N'oubliez pas de regarder la vidéo liée à cet article.</p>
<p><small>Publié le: {now.strftime('%Y-%m-%d')}</small></p>
</div>"""
    return html


def _build_fallback_content_es(video_title: str, content_type: str, now: datetime) -> str:
    """Build fallback content in Spanish"""
    html = f"""<div style="font-family: Arial, sans-serif; line-height: 1.8;">
<h2>{video_title or 'Nuevo Artículo'}</h2>
<p>Bienvenido a nuestro último artículo sobre <b>{content_type or 'este tema'}</b>.</p>
<p>En este artículo cubrimos la última información relacionada con este emocionante tema.</p>
<h3>💡 Resumen</h3>
<p>Presentamos contenido único sobre {content_type or 'tecnología y entretenimiento'}.</p>
<h3>🎯 Detalles</h3>
<p>El contenido incluye varios puntos importantes que esperamos que disfrutes.</p>
<h3>📌 Conclusión</h3>
<p>¡Gracias por seguirnos! No olvides ver el video vinculado a este artículo.</p>
<p><small>Publicado: {now.strftime('%Y-%m-%d')}</small></p>
</div>"""
    return html


# ==================== الوظيفة الرئيسية ====================

def generate_article(
    link_config: Dict,
    video_title: str = "",
    video_description: str = "",
    content_type: str = "",
    source_name: str = "",
    tags: List[str] = None,
) -> Dict[str, Any]:
    """
    توليد مقال بناءً على إعدادات الرابط.
    يحدد الوضع تلقائياً من link_config["ai_mode"].

    يعيد:
    {
        "title": "...",
        "content": "...",  
        "labels": [...],
        "mode_used": "ai_prompt" | "templates" | "fallback",
        "template_index": 0  # فقط في وضع templates
    }
    """
    ai_mode = link_config.get("ai_mode", "ai_prompt")
    language = link_config.get("article_language", "ar")

    if ai_mode == "ai_prompt":
        ai_prompt = link_config.get("ai_prompt", "")
        if ai_prompt.strip():
            result = generate_article_ai(
                ai_prompt=ai_prompt,
                video_title=video_title,
                video_description=video_description,
                content_type=content_type,
                source_name=source_name,
                language=language,
                tags=tags,
            )
            result["mode_used"] = "ai_prompt"
            return result
        else:
            # لا يوجد برومت، ننتقل لـ fallback
            result = generate_fallback_article(video_title, content_type, language)
            result["mode_used"] = "fallback"
            return result

    elif ai_mode == "templates":
        result = generate_article_template(link_config, video_title)
        result["mode_used"] = "templates"
        result["template_index"] = link_config.get("template_index", 0)
        return result

    else:
        # fallback
        result = generate_fallback_article(video_title, content_type, language)
        result["mode_used"] = "fallback"
        return result
