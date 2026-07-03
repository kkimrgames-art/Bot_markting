import os
import logging
import asyncio
import shutil
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional, List
from telegram.ext import ContextTypes
from src.agent.config import load_config

logger = logging.getLogger(__name__)

async def smart_download_file(context: ContextTypes.DEFAULT_TYPE, file_obj, destination: str) -> bool:
    """
    تحميل ملف بطريقة ذكية:
    - في local_mode: نسخ الملف من المسار المحلي مباشرة (يدعم ملفات > 20MB)
    - إذا كان المسار غير قابل للوصول (Docker): التحميل عبر HTTP من الخادم المحلي
    - بدون local_mode: تحميل عادي عبر Telegram API
    
    Returns: True إذا نجح التحميل
    """
    cfg = load_config()
    is_local_mode = bool(cfg.LOCAL_BOT_API_URL)

    def _extract_relative_path_for_bot_api(fp: str) -> str:
        if not fp:
            return ""

        if "://" in fp:
            try:
                fp = urlparse(fp).path
            except Exception:
                pass

        if "/var/lib/telegram-bot-api/" in fp:
            fp = fp.split("/var/lib/telegram-bot-api/", 1)[1]

        fp = fp.lstrip("/")

        token = cfg.TELEGRAM_BOT_TOKEN or ""
        if token and fp.startswith(token + "/"):
            fp = fp[len(token) + 1 :]

        parts = [p for p in fp.split("/") if p]
        for type_dir in [
            "videos",
            "documents",
            "photos",
            "voice",
            "music",
            "audio",
            "animations",
            "stickers",
        ]:
            if type_dir in parts:
                idx = parts.index(type_dir)
                return "/".join(parts[idx:])

        if len(parts) >= 2:
            return "/".join(parts[-2:])
        return "/".join(parts)

    def _build_candidate_download_urls(fp: str) -> list[str]:
        urls: list[str] = []

        base_file_url = getattr(context.bot, "base_file_url", None)
        if not base_file_url:
            return urls

        if fp and "://" in fp:
            if "/var/lib/telegram-bot-api/" not in fp:
                urls.append(fp)

        raw_path = fp or ""
        if "://" in raw_path:
            try:
                raw_path = urlparse(raw_path).path
            except Exception:
                pass
        raw_path = raw_path.lstrip("/")

        token = cfg.TELEGRAM_BOT_TOKEN or ""
        stripped_docker = raw_path
        if "var/lib/telegram-bot-api/" in stripped_docker:
            stripped_docker = stripped_docker.split("var/lib/telegram-bot-api/", 1)[1]

        stripped_docker = stripped_docker.lstrip("/")

        if stripped_docker:
            urls.append(f"{base_file_url.rstrip('/')}/{stripped_docker}")

        if token and stripped_docker and not stripped_docker.startswith(token + "/"):
            urls.append(f"{base_file_url.rstrip('/')}/{token}/{stripped_docker}")

        stripped_no_token = stripped_docker
        if token and stripped_no_token.startswith(token + "/"):
            stripped_no_token = stripped_no_token[len(token) + 1 :]
        if stripped_no_token and stripped_no_token != stripped_docker:
            urls.append(f"{base_file_url.rstrip('/')}/{stripped_no_token}")

        rel = _extract_relative_path_for_bot_api(fp)
        if rel:
            urls.append(f"{base_file_url.rstrip('/')}/{rel.lstrip('/')}")

        seen: set[str] = set()
        out: list[str] = []
        for u in urls:
            if u and u not in seen:
                seen.add(u)
                out.append(u)
        return out
    
    try:
        # محاولة الحصول على معلومات الملف
        # ملاحظة: في الخادم المحلي، get_file يجب أن تعمل حتى للملفات الكبيرة إذا تم تمرير --local
        try:
            file = await file_obj.get_file()
        except Exception as get_file_error:
            # إذا فشل get_file، غالباً بسبب الحجم الكبير
            error_str = str(get_file_error).lower()
            if "too big" in error_str or "file is too big" in error_str:
                if is_local_mode:
                    # إذا كنا في الوضع المحلي وفشل get_file، فهذا يعني غالباً أن الخادم لا يعمل بشكل صحيح مع --local
                    logger.error(f"❌ الخادم المحلي رفض get_file بحجة الحجم! تأكد من تشغيل الخادم مع --local. الخطأ: {get_file_error}")
                    pass
                raise get_file_error
            raise get_file_error
        
        if is_local_mode:
            # التحقق مما إذا كان الملف موجوداً في المسار المحدد (Native Mode)
            if file.file_path and os.path.exists(file.file_path):
                # في local_mode (Native)، الملف موجود مباشرة على النظام
                logger.info(f"✅ تم نسخ الملف من الخادم المحلي (نسخ مباشر): {file.file_path}")
                shutil.copy2(file.file_path, destination)
                return True
            else:
                # التحقق من وجود الملف في مجلد البيانات المشترك (Docker Volume Optimization)
                # هذا يتجاوز التحميل عبر HTTP ويحل مشكلة المهلة (Timeout) للملفات الكبيرة
                try:
                    token = cfg.TELEGRAM_BOT_TOKEN
                    # المسار الافتراضي للبيانات
                    data_dir = Path(".data") / "bot-api-server"
                    
                    # تنظيف مسار الملف (يدعم: URL، مسار Docker مطلق، أو مسار نسبي)
                    rel_path = file.file_path or ""

                    if "://" in rel_path:
                        try:
                            rel_path = urlparse(rel_path).path
                        except Exception:
                            pass

                    rel_path = rel_path.lstrip("/")

                    if token:
                        file_prefix = f"file/bot{token}/"
                        if rel_path.startswith(file_prefix):
                            rel_path = rel_path[len(file_prefix) :]
                            rel_path = rel_path.lstrip("/")

                    if rel_path.startswith("var/lib/telegram-bot-api/"):
                        rel_path = rel_path.replace("var/lib/telegram-bot-api/", "", 1)

                    rel_path = rel_path.lstrip("/")

                    rel_variants: list[str] = []
                    if rel_path:
                        rel_variants.append(rel_path)
                        if token and rel_path.startswith(token + "/"):
                            rel_variants.append(rel_path[len(token) + 1 :])
                        if token and not rel_path.startswith(token + "/"):
                            rel_variants.append(f"{token}/{rel_path}")

                    seen_rel: set[str] = set()
                    rel_variants = [rp for rp in rel_variants if rp and not (rp in seen_rel or seen_rel.add(rp))]

                    # بناء مسارات محتملة على المضيف
                    # بعض توزيعات bot-api تُخزن الملف تحت <token>/videos/.. بينما file_path قد يكون videos/..
                    candidates: list[Path] = []
                    bot_id = (token.split(":", 1)[0] if token else "")
                    token_dirs: list[Path] = []
                    try:
                        if data_dir.exists():
                            if bot_id:
                                for d in data_dir.iterdir():
                                    if d.is_dir() and d.name.startswith(bot_id):
                                        token_dirs.append(d)
                    except Exception:
                        token_dirs = [data_dir]

                    for rp in rel_variants:
                        candidates.append(data_dir / rp)
                        if token and rp.startswith(token + "/"):
                            candidates.append(data_dir / rp[len(token) + 1 :])
                        for td in token_dirs:
                            candidates.append(td / (rp[len(token) + 1 :] if (token and rp.startswith(token + "/")) else rp))

                    for potential_host_path in candidates:
                        if potential_host_path.exists():
                            # التحقق من تطابق الحجم لضمان اكتمال الملف
                            current_size = potential_host_path.stat().st_size
                            expected_size = file.file_size

                            if expected_size is None or current_size >= expected_size:
                                logger.info(f"✅ تم العثور على الملف مكتمل في مجلد Docker المشترك: {potential_host_path}")
                                shutil.copy2(potential_host_path, destination)
                                return True
                            else:
                                logger.warning(
                                    f"⚠️ الملف موجود لكن الحجم غير متطابق ({current_size}/{expected_size})... جاري التحميل عبر HTTP"
                                )
                except Exception as map_err:
                    logger.warning(f"⚠️ فشل محاولة الوصول المباشر لملف Docker: {map_err}")

                # في local_mode (Docker)، المسار داخل الحاوية لا يمكن الوصول إليه
                # لذا نستخدم التحميل عبر HTTP (download_to_drive تتكفل بذلك باستخدام base_file_url)
                logger.info("ℹ️ الملف غير موجود محلياً (ربما Docker)، جاري التحميل عبر HTTP من الخادم المحلي...")

                dest_path = Path(destination)
                dest_path.parent.mkdir(parents=True, exist_ok=True)

                candidates = _build_candidate_download_urls(file.file_path or "")
                if not candidates:
                    raise RuntimeError("Bot has no base_file_url configured for local mode")

                logger.info(
                    "🔎 محاولات تنزيل محتملة عبر HTTP: "
                    f"file_path={file.file_path!r} candidates={candidates!r}"
                )

                last_err: Exception | None = None
                for download_url in candidates:
                    try:
                        logger.info(f"⬇️ محاولة تنزيل عبر: {download_url}")
                        buf = None
                        for attempt in range(6):
                            try:
                                buf = await context.bot.request.retrieve(
                                    download_url,
                                    connect_timeout=60,
                                    read_timeout=1200,
                                    write_timeout=1200,
                                    pool_timeout=60,
                                )
                                break
                            except Exception as attempt_err:
                                err_s = str(attempt_err).lower()
                                if ("404" in err_s or "not found" in err_s) and attempt < 5:
                                    await asyncio.sleep(0.5 * (2**attempt))
                                    continue
                                raise

                        if buf is None:
                            raise RuntimeError("Failed to retrieve file buffer")
                        dest_path.write_bytes(buf)
                        return True
                    except Exception as dl_err:
                        last_err = dl_err
                        logger.warning(
                            "⚠️ فشل تنزيل عبر HTTP. "
                            f"file_path={file.file_path!r} url={download_url!r} err={dl_err}"
                        )

                assert last_err is not None
                raise last_err
        else:
            # الطريقة العادية (Cloud API)
            await file.download_to_drive(destination)
            return True
            
    except Exception as e:
        error_str = str(e).lower()
        if "too big" in error_str or "file is too big" in error_str:
            if is_local_mode:
                 logger.error(f"❌ الخادم المحلي رفض الملف بحجة الحجم! تأكد من تشغيل الخادم مع --local")
            raise Exception(f"الملف كبير جداً. تأكد من أن الخادم المحلي يعمل مع خيار --local. الخطأ: {e}")
        
        logger.error(f"❌ فشل تحميل الملف: {e}")
        raise
