#!/usr/bin/env python3
"""
Link Shortener - اختصار الروابط عبر cuty.io API
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# cuty.io API
SHORTEN_API_URL = "https://api.cuty.io/full"
DEFAULT_API_TOKEN = "ed64005d43b623b922196b918"


def shorten_link(
    url: str,
    api_token: str = "",
    title: str = "",
    alias: str = "",
) -> Optional[str]:
    """
    اختصار رابط عبر cuty.io API.
    يعيد الرابط المختصر أو None في حال الفشل.
    """
    if not url:
        return None

    token = api_token or DEFAULT_API_TOKEN
    if not token:
        logger.warning("No API token for link shortener")
        return None

    try:
        import httpx

        payload = {
            "token": token,
            "url": url,
        }
        if title:
            payload["title"] = title[:200]
        if alias:
            payload["alias"] = alias.strip()[:50]

        resp = httpx.post(
            SHORTEN_API_URL,
            data=payload,
            timeout=30,
            follow_redirects=True,
        )

        if resp.status_code == 200:
            # الرد يمكن أن يكون JSON يحتوي على shortURL أو مجرد نص
            content_type = resp.headers.get("content-type", "")
            body = resp.text.strip()

            if "application/json" in content_type:
                try:
                    data = resp.json()
                    short = (
                        data.get("shortURL")
                        or data.get("short_url")
                        or data.get("shorturl")
                        or data.get("url")
                        or data.get("link")
                    )
                    if short and ("cuty.io" in short or "http" in short):
                        logger.info(f"Link shortened: {url[:60]}... -> {short}")
                        return short
                except Exception:
                    pass

            # إذا كان الرد مجرد رابط نصي
            if body.startswith("http"):
                logger.info(f"Link shortened: {url[:60]}... -> {body}")
                return body

        logger.warning(f"cuty.io API error: status={resp.status_code}, body={resp.text[:200]}")
        return None

    except Exception as e:
        logger.error(f"Link shortener failed: {e}")
        return None


def shorten_with_fallback(url: str, api_token: str = "", title: str = "") -> str:
    """
    محاولة اختصار الرابط، وفي حال الفشل يعيد الرابط الأصلي.
    """
    try:
        short = shorten_link(url=url, api_token=api_token, title=title)
        if short:
            return short
    except Exception:
        pass
    logger.debug(f"Link shortener failed, using original URL")
    return url
