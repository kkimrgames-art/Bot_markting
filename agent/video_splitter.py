"""
Video Splitter - نظام تقسيم الفيديوهات الكبيرة

يستخدم كحل احتياطي عندما لا يمكن استخدام خادم Bot API المحلي
يقسم الفيديوهات إلى أجزاء أصغر من 20 ميجا لرفعها عبر API الرسمي
"""
import os
import logging
import subprocess
import shutil
from pathlib import Path
from typing import List, Tuple, Optional

logger = logging.getLogger(__name__)


def get_ffmpeg_path() -> str:
    """الحصول على مسار FFmpeg"""
    try:
        from ..agent.ffmpeg_utils import ffmpeg_bin
        return ffmpeg_bin()
    except:
        return shutil.which("ffmpeg") or "ffmpeg"


def get_ffprobe_path() -> str:
    """الحصول على مسار ffprobe"""
    try:
        from ..agent.ffmpeg_utils import ffprobe_bin
        return ffprobe_bin()
    except:
        return shutil.which("ffprobe") or "ffprobe"


def get_video_duration(video_path: str) -> float:
    """الحصول على مدة الفيديو بالثواني"""
    try:
        ffprobe = get_ffprobe_path()
        cmd = [
            ffprobe,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return float(result.stdout.strip())
    except Exception as e:
        logger.error(f"خطأ في الحصول على مدة الفيديو: {e}")
        return 0.0


def get_video_bitrate(video_path: str) -> int:
    """الحصول على معدل البت للفيديو (بت/ثانية)"""
    try:
        file_size = os.path.getsize(video_path)  # بايت
        duration = get_video_duration(video_path)  # ثواني
        if duration > 0:
            return int((file_size * 8) / duration)  # بت/ثانية
        return 0
    except:
        return 0


def estimate_segment_duration(target_size_mb: float, bitrate: int) -> float:
    """تقدير مدة الجزء بناءً على الحجم المطلوب"""
    if bitrate <= 0:
        return 60.0  # افتراضي: دقيقة واحدة
    
    target_size_bits = target_size_mb * 1024 * 1024 * 8
    duration = target_size_bits / bitrate
    
    # حد أدنى 10 ثواني، أقصى 5 دقائق
    return max(10.0, min(duration * 0.9, 300.0))  # 90% للأمان


def split_video_by_size(
    video_path: str,
    output_dir: str,
    max_size_mb: float = 19.0,  # أقل من 20 للأمان
    prefix: str = "part"
) -> Tuple[bool, List[str]]:
    """
    تقسيم الفيديو إلى أجزاء بحجم محدد
    
    Returns:
        Tuple[bool, List[str]]: (نجاح, قائمة مسارات الأجزاء)
    """
    if not os.path.exists(video_path):
        logger.error(f"الملف غير موجود: {video_path}")
        return False, []
    
    file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
    
    # إذا الملف أصغر من الحد، لا حاجة للتقسيم
    if file_size_mb <= max_size_mb:
        logger.info(f"الملف أصغر من {max_size_mb}MB، لا حاجة للتقسيم")
        return True, [video_path]
    
    # إنشاء مجلد الإخراج
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    
    # الحصول على معلومات الفيديو
    duration = get_video_duration(video_path)
    bitrate = get_video_bitrate(video_path)
    
    if duration <= 0:
        logger.error("فشل في الحصول على مدة الفيديو")
        return False, []
    
    # حساب مدة كل جزء
    segment_duration = estimate_segment_duration(max_size_mb, bitrate)
    
    # حساب عدد الأجزاء
    num_segments = int(duration / segment_duration) + 1
    
    logger.info(f"📊 معلومات الفيديو:")
    logger.info(f"   • الحجم: {file_size_mb:.1f} MB")
    logger.info(f"   • المدة: {duration:.1f} ثانية")
    logger.info(f"   • معدل البت: {bitrate / 1000:.0f} kbps")
    logger.info(f"   • سيتم تقسيمه إلى ~{num_segments} أجزاء")
    
    # استخدام FFmpeg للتقسيم
    try:
        ffmpeg = get_ffmpeg_path()
        
        # اسم الملف الأصلي بدون امتداد
        base_name = Path(video_path).stem
        ext = Path(video_path).suffix or ".mp4"
        
        output_pattern = os.path.join(output_dir, f"{prefix}_{base_name}_%03d{ext}")
        
        cmd = [
            ffmpeg,
            "-y",
            "-i", video_path,
            "-c", "copy",  # نسخ بدون إعادة ترميز (أسرع)
            "-f", "segment",
            "-segment_time", str(int(segment_duration)),
            "-reset_timestamps", "1",
            "-map", "0",
            output_pattern
        ]
        
        logger.info("✂️ جاري تقسيم الفيديو...")
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600  # 10 دقائق حد أقصى
        )
        
        if result.returncode != 0:
            logger.error(f"فشل التقسيم: {result.stderr}")
            return False, []
        
        # جمع الأجزاء الناتجة
        parts = []
        for i in range(100):  # حد أقصى 100 جزء
            part_path = os.path.join(
                output_dir, 
                f"{prefix}_{base_name}_{i:03d}{ext}"
            )
            if os.path.exists(part_path):
                parts.append(part_path)
            else:
                break
        
        if parts:
            logger.info(f"✅ تم تقسيم الفيديو إلى {len(parts)} أجزاء:")
            for i, part in enumerate(parts):
                size = os.path.getsize(part) / (1024 * 1024)
                logger.info(f"   {i+1}. {Path(part).name} ({size:.1f} MB)")
            return True, parts
        else:
            logger.error("لم يتم إنشاء أي أجزاء!")
            return False, []
            
    except subprocess.TimeoutExpired:
        logger.error("⏰ انتهت مهلة تقسيم الفيديو")
        return False, []
    except Exception as e:
        logger.error(f"❌ خطأ في تقسيم الفيديو: {e}")
        return False, []


def split_video_by_duration(
    video_path: str,
    output_dir: str,
    segment_seconds: int = 60,
    prefix: str = "part"
) -> Tuple[bool, List[str]]:
    """
    تقسيم الفيديو حسب المدة (بالثواني)
    """
    if not os.path.exists(video_path):
        return False, []
    
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    
    try:
        ffmpeg = get_ffmpeg_path()
        base_name = Path(video_path).stem
        ext = Path(video_path).suffix or ".mp4"
        output_pattern = os.path.join(output_dir, f"{prefix}_{base_name}_%03d{ext}")
        
        cmd = [
            ffmpeg,
            "-y",
            "-i", video_path,
            "-c", "copy",
            "-f", "segment",
            "-segment_time", str(segment_seconds),
            "-reset_timestamps", "1",
            "-map", "0",
            output_pattern
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        
        if result.returncode != 0:
            return False, []
        
        parts = []
        for i in range(100):
            part_path = os.path.join(output_dir, f"{prefix}_{base_name}_{i:03d}{ext}")
            if os.path.exists(part_path):
                parts.append(part_path)
            else:
                break
        
        return bool(parts), parts
        
    except Exception as e:
        logger.error(f"خطأ: {e}")
        return False, []


def cleanup_split_parts(parts: List[str]) -> None:
    """حذف الأجزاء المؤقتة بعد الرفع"""
    for part in parts:
        try:
            if os.path.exists(part):
                os.remove(part)
                logger.debug(f"تم حذف: {part}")
        except Exception as e:
            logger.warning(f"فشل حذف {part}: {e}")


async def send_video_parts(
    bot,
    chat_id: int,
    parts: List[str],
    caption: str = "",
    reply_to_message_id: Optional[int] = None
) -> List[str]:
    """
    إرسال أجزاء الفيديو إلى المحادثة
    
    Returns:
        List[str]: قائمة file_ids للأجزاء المرفوعة
    """
    file_ids = []
    total = len(parts)
    
    for i, part in enumerate(parts):
        try:
            part_caption = ""
            if i == 0 and caption:
                part_caption = f"{caption}\n\n📎 الجزء {i+1} من {total}"
            elif i == total - 1:
                part_caption = f"📎 الجزء الأخير ({i+1} من {total})"
            else:
                part_caption = f"📎 الجزء {i+1} من {total}"
            
            with open(part, "rb") as f:
                msg = await bot.send_video(
                    chat_id=chat_id,
                    video=f,
                    caption=part_caption,
                    supports_streaming=True,
                    reply_to_message_id=reply_to_message_id if i == 0 else None
                )
                
                if msg.video:
                    file_ids.append(msg.video.file_id)
                    
        except Exception as e:
            logger.error(f"فشل رفع الجزء {i+1}: {e}")
            continue
    
    return file_ids


def estimate_parts_count(file_size_bytes: int, max_size_mb: float = 19.0) -> int:
    """تقدير عدد الأجزاء المتوقعة"""
    file_size_mb = file_size_bytes / (1024 * 1024)
    if file_size_mb <= max_size_mb:
        return 1
    return int(file_size_mb / max_size_mb) + 1
