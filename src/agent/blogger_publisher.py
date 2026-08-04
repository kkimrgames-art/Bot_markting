#!/usr/bin/env python3
"""
Blogger Publisher - التكامل مع سير نشر الفيديو
يُستدعى قبل نشر الفيديو لإنشاء مقال بلوجر وإضافة رابطه للوصف.
"""
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


def create_blog_article_before_publish(
    source_id: str,
    channel_id: str,
    video_title: str,
    video_description: str = "",
    content_type: str = "",
    source_name: str = "",
    tags: list = None,
    download_url: str = "",
    download_link_position: str = "bottom",
) -> Optional[str]:
    """
    إنشاء مقال بلوجر قبل نشر الفيديو.
    يُبحث عن رابط بلوجر نشط لهذا المصدر، ينشئ المقال، ويعيد رابطه.

    يعيد رابط المقال (URL) أو None إذا لم يتم إنشاؤه.
    يجب استدعاؤها من _upload_to_youtube أو ما يعادلها.
    """
    try:
        from .blogger_db import get_active_blogger_links_for_source, save_blogger_article, get_blogger_link
        from .blogger_article_generator import generate_article
        from .blogger_integration import publish_article_to_blogger

        # البحث عن روابط بلوجر نشطة لهذا المصدر
        links = get_active_blogger_links_for_source(source_id)
        if not links:
            logger.debug(f"No active blogger links for source {source_id}")
            return None

        # نأخذ أول رابط نشط
        link = links[0]
        link_id = link["id"]
        blog_id = link.get("blog_id", "")
        link_title = link.get("link_title", "🔗 اقرأ المزيد على المدونة")

        if not blog_id:
            logger.warning(f"Blogger link {link_id} has no blog_id")
            return None

        # الحصول على مسار التوكن
        token_path = link.get("token_path", "")
        if not token_path:
            # محاولة الحصول على التوكن من channel_configs
            try:
                from .blogger_integration import get_channel_token_paths
                token_map = get_channel_token_paths()
                if token_map:
                    token_path = list(token_map.values())[0]
            except Exception:
                pass

        if not token_path:
            logger.warning(f"No token path for blogger link {link_id}")
            return None

        # توليد المقال
        article = generate_article(
            link_config=link,
            video_title=video_title,
            video_description=video_description,
            content_type=content_type,
            source_name=source_name,
            tags=tags,
        )

        # إدراج رابط التحميل في المقال إذا موجود
        if download_url:
            article_content = article.get("content", "")
            article_content = _inject_download_link(article_content, download_url, download_link_position)
            article["content"] = article_content

        article_title = article.get("title", video_title)
        article_content = article.get("content", "")
        article_labels = article.get("labels", [])
        mode_used = article.get("mode_used", "fallback")

        # نشر المقال على بلوجر
        publish_result = publish_article_to_blogger(
            token_path=token_path,
            blog_id=blog_id,
            title=article_title,
            content=article_content,
            labels=article_labels if article_labels else None,
        )

        post_url = publish_result.get("url", "")
        post_id = publish_result.get("post_id", "")

        if not post_url:
            logger.warning(f"Blogger publish returned no URL for link {link_id}")
            return None

        # حفظ سجل المقال
        save_blogger_article({
            "link_id": link_id,
            "source_id": source_id,
            "channel_id": channel_id,
            "blog_id": blog_id,
            "blog_post_id": post_id,
            "blog_post_url": post_url,
            "article_title": article_title,
            "ai_mode_used": mode_used,
            "template_index_used": article.get("template_index"),
        })

        logger.info(f"📝 Blog article published: {post_url}")

        # بناء نص الرابط للوصف
        blog_link_text = f"{link_title}\n{post_url}"
        return blog_link_text

    except Exception as e:
        logger.error(f"Blogger publish failed (non-critical): {e}", exc_info=True)
        return None


def append_blog_link_to_description(
    description: str,
    source_id: str,
    channel_id: str,
    video_title: str,
    video_description: str = "",
    content_type: str = "",
    source_name: str = "",
    tags: list = None,
    download_url: str = "",
    download_link_position: str = "bottom",
) -> str:
    """
    وظيفة مساعدة: تنشئ المقال وتضيف رابطه لنهاية الوصف.
    تُستدعى من _upload_to_youtube لتعديل الوصف قبل النشر.
    """
    blog_link_text = create_blog_article_before_publish(
        source_id=source_id,
        channel_id=channel_id,
        video_title=video_title,
        video_description=video_description,
        content_type=content_type,
        source_name=source_name,
        tags=tags,
        download_url=download_url,
        download_link_position=download_link_position,
    )

    if blog_link_text:
        if description:
            return f"{description}\n\n{blog_link_text}"
        else:
            return blog_link_text

    return description


def _inject_download_link(html_content: str, download_url: str, position: str = "bottom") -> str:
    """
    إدراج رابط تحميل في محتوى HTML للمقال.
    position: top | middle | bottom
    """
    link_html = (
        '<div style="margin: 15px 0; padding: 15px; background: #f0f8ff; '
        'border: 2px solid #2196F3; border-radius: 10px; text-align: center;">'
        '<p style="margin: 0 0 8px 0; font-size: 18px; font-weight: bold; color: #2196F3;">'
        '📥 رابط تحميل الفيديو</p>'
        '<a href="' + download_url + '" style="display: inline-block; padding: 10px 25px; '
        'background: #2196F3; color: white; text-decoration: none; border-radius: 5px; '
        'font-size: 16px; font-weight: bold;">📥 تحميل الآن</a>'
        '</div>'
    )

    import re

    if position == "top":
        match = re.search(r'</h[1-6]>|<p>', html_content, re.IGNORECASE)
        if match:
            idx = match.end()
            return html_content[:idx] + "\n" + link_html + "\n" + html_content[idx:]
        return link_html + "\n" + html_content

    elif position == "middle":
        mid = len(html_content) // 2
        search_area = html_content[mid:mid + 500]
        match = re.search(r'</p>|<br\s*/?>', search_area, re.IGNORECASE)
        if match:
            insert_at = mid + match.end()
            return html_content[:insert_at] + "\n" + link_html + "\n" + html_content[insert_at:]
        return html_content + "\n" + link_html

    else:  # bottom
        return html_content + "\n" + link_html
