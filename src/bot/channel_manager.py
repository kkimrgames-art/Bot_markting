"""
نظام إدارة القنوات - يدعم 1000+ قناة بدون تضارب
كل قناة لها ملف JSON منفصل في .data/channels/
"""
import os
import json
import uuid
import shutil
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from pathlib import Path
import logging
import concurrent.futures
import asyncio
from ..agent.config import load_config
from ..bot.persistence import load_state, save_state

logger = logging.getLogger(__name__)
_SUPABASE_SYNC_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=2)


def _project_root_dir() -> Path:
    try:
        return Path(__file__).resolve().parents[2]
    except Exception:
        return Path.cwd()


def _resolve_storage_path(path_value: Optional[str]) -> str:
    raw_path = str(path_value or "").strip()
    if not raw_path:
        return ""
    if os.path.isabs(raw_path):
        return raw_path
    return str((_project_root_dir() / raw_path).resolve())


def _delete_file_best_effort(path_value: Optional[str]) -> None:
    abs_path = _resolve_storage_path(path_value)
    if not abs_path:
        return
    try:
        if os.path.isfile(abs_path):
            os.remove(abs_path)
    except Exception:
        pass


def _channel_token_candidates(channel: Optional["Channel"], cfg: Any = None) -> List[str]:
    candidates: List[str] = []
    if channel is None:
        return candidates

    direct_token_path = str((getattr(channel, "extra_data", {}) or {}).get("token_path") or "").strip()
    if direct_token_path:
        candidates.append(direct_token_path)

    creds = getattr(channel, "platform_credentials", None)
    if isinstance(creds, dict):
        creds_token_path = str(creds.get("token_path") or "").strip()
        if creds_token_path:
            candidates.append(creds_token_path)

    extra_creds = (getattr(channel, "extra_data", {}) or {}).get("platform_credentials")
    if isinstance(extra_creds, dict):
        extra_token_path = str(extra_creds.get("token_path") or "").strip()
        if extra_token_path:
            candidates.append(extra_token_path)

    yt_channel_id = str(getattr(channel, "youtube_channel_id", "") or "").strip()
    if yt_channel_id:
        candidates.extend(_youtube_token_candidates_by_channel_id(yt_channel_id, cfg))

    deduped: List[str] = []
    seen = set()
    for path_value in candidates:
        key = _resolve_storage_path(path_value)
        if key and key not in seen:
            seen.add(key)
            deduped.append(path_value)
    return deduped


def _youtube_token_candidates_by_channel_id(youtube_channel_id: str, cfg: Any = None) -> List[str]:
    channel_id = str(youtube_channel_id or "").strip()
    if not channel_id:
        return []

    base_candidates: List[str] = []
    try:
        if cfg is None:
            cfg = load_config()
    except Exception:
        cfg = None

    try:
        base_dir = os.path.dirname(getattr(cfg, "TELEGRAM_DB_PATH", "") or "") or ""
    except Exception:
        base_dir = ""

    if base_dir:
        base_candidates.append(base_dir)
        nested = os.path.join(base_dir, ".data")
        if os.path.normpath(nested) != os.path.normpath(base_dir):
            base_candidates.append(nested)

    project_data_dir = str((_project_root_dir() / ".data").resolve())
    base_candidates.append(project_data_dir)
    nested_project_data = str((_project_root_dir() / ".data" / ".data").resolve())
    if os.path.normpath(nested_project_data) != os.path.normpath(project_data_dir):
        base_candidates.append(nested_project_data)

    candidates: List[str] = []
    seen = set()
    for base in base_candidates:
        norm_base = _resolve_storage_path(base)
        if not norm_base or norm_base in seen:
            continue
        seen.add(norm_base)
        candidates.append(os.path.join(norm_base, "youtube_tokens", f"{channel_id}.json"))
    return candidates


def _canonical_youtube_token_path(youtube_channel_id: str, cfg: Any = None) -> str:
    candidates = _youtube_token_candidates_by_channel_id(youtube_channel_id, cfg)
    return candidates[0] if candidates else ""


def resolve_youtube_token_path(youtube_channel_id: str, cfg: Any = None) -> str:
    channel_id = str(youtube_channel_id or "").strip()
    if not channel_id:
        return ""

    canonical = _canonical_youtube_token_path(channel_id, cfg)
    canonical_abs = _resolve_storage_path(canonical)

    for candidate in _youtube_token_candidates_by_channel_id(channel_id, cfg):
        candidate_abs = _resolve_storage_path(candidate)
        if not candidate_abs or not os.path.exists(candidate_abs):
            continue
        if canonical_abs and candidate_abs != canonical_abs:
            try:
                os.makedirs(os.path.dirname(canonical_abs), exist_ok=True)
                if not os.path.exists(canonical_abs):
                    shutil.copy2(candidate_abs, canonical_abs)
                    logger.info(f"Restored canonical token file for channel: {channel_id}")
                if os.path.exists(canonical_abs):
                    return canonical_abs
            except Exception as e:
                logger.warning(f"Failed to mirror token file for {channel_id}: {e}")
        return candidate_abs

    return canonical_abs or ""


def resolve_channel_token_path(channel: Optional["Channel"], cfg: Any = None) -> str:
    if channel is None:
        return ""

    for candidate in _channel_token_candidates(channel, cfg):
        candidate_abs = _resolve_storage_path(candidate)
        if candidate_abs and os.path.exists(candidate_abs):
            yt_channel_id = str(getattr(channel, "youtube_channel_id", "") or "").strip()
            if yt_channel_id:
                resolved = resolve_youtube_token_path(yt_channel_id, cfg)
                if resolved and os.path.exists(resolved):
                    return resolved
            return candidate_abs

    yt_channel_id = str(getattr(channel, "youtube_channel_id", "") or "").strip()
    if yt_channel_id:
        return resolve_youtube_token_path(yt_channel_id, cfg)
    return ""


class Channel:
    """نموذج بيانات القناة"""
    
    def __init__(
        self,
        channel_id: str,
        channel_name: str,
        youtube_channel_id: str,
        platform: str = "youtube",
        platform_channel_id: Optional[str] = None,
        platform_credentials: Optional[Dict[str, Any]] = None,
        enabled: bool = True,
        content_type: str = "minecraft",
        privacy: str = "unlisted",
        publish_interval: int = 3600,
        language: str = "ar",  # 🆕 اللغة الافتراضية
        last_publish: Optional[str] = None,
        next_publish: Optional[str] = None,
        total_published: int = 0,
        created_at: Optional[str] = None,
        # 🆕 إعدادات Intro/Outro
        intro_videos: Optional[List[str]] = None,
        outro_videos: Optional[List[str]] = None,
        intro_transition: str = "fade",
        outro_transition: str = "fade",
        # 🆕 إعدادات الجدولة البشرية
        scheduling_settings: Optional[Dict[str, Any]] = None,
        # 🆕 نصوص مخصصة تظهر على الفيديو
        custom_overlay_texts: Optional[List[Dict[str, Any]]] = None,
        # 🆕 الاسم الحقيقي للقناة على YouTube (يُجلب من API)
        youtube_channel_name: Optional[str] = None,
        **kwargs
    ):
        self.channel_id = channel_id
        self.channel_name = channel_name
        self.youtube_channel_id = youtube_channel_id
        self.youtube_channel_name = youtube_channel_name  # الاسم الفعلي من YouTube
        self.platform = platform
        self.platform_channel_id = platform_channel_id or youtube_channel_id
        self.platform_credentials = platform_credentials or {}
        self.enabled = enabled
        self.content_type = content_type
        self.privacy = privacy
        self.publish_interval = publish_interval
        self.language = language  # 🆕 حفظ اللغة
        self.last_publish = last_publish
        self.next_publish = next_publish or self._calculate_next_publish()
        self.total_published = total_published
        self.created_at = created_at or datetime.now().isoformat()
        
        # 🆕 إعدادات Intro/Outro للقناة
        self.intro_videos = intro_videos or []  # قائمة مسارات فيديوهات البداية
        self.outro_videos = outro_videos or []  # قائمة مسارات فيديوهات النهاية
        self.intro_transition = intro_transition  # تأثير الانتقال من الانترو
        self.outro_transition = outro_transition  # تأثير الانتقال للاوترو
        
        # 🆕 إعدادات الجدولة البشرية
        self.scheduling_settings = scheduling_settings or self._get_default_scheduling_settings()
        
        # 🆕 نصوص مخصصة تظهر على الفيديو
        # كل عنصر: {"text": "...", "timing": "start|end|full", "duration": 2.0, "screen_position": "top|bottom"}
        self.custom_overlay_texts = custom_overlay_texts or []
        
        # حفظ أي حقول إضافية
        self.extra_data = kwargs
    
    def _get_default_scheduling_settings(self) -> Dict[str, Any]:
        """الحصول على الإعدادات الافتراضية للجدولة"""
        return {
            "publish_frequency": "daily",
            "custom_interval_hours": None,
            "jitter_enabled": True,
            "jitter_min_minutes": 10,
            "jitter_max_minutes": 60,
            "jitter_distribution": "uniform",
            "min_spacing_shorts_minutes": 10,
            "min_spacing_long_minutes": 30,
            "peak_hours_enabled": True,
            "peak_hours_start": "16:00",
            "peak_hours_end": "20:00",
            "distribution_strategy": "sequential",
            "auto_distribute_threshold": 10
        }
    
    def _calculate_next_publish(self) -> str:
        """حساب موعد النشر التالي"""
        if self.last_publish:
            try:
                # نستخدم .replace(tzinfo=None) لضمان المقارنة مع datetime.now()
                last = datetime.fromisoformat(self.last_publish).replace(tzinfo=None)
            except ValueError:
                last = datetime.now()
            next_time = last + timedelta(seconds=self.publish_interval)
        else:
            next_time = datetime.now() + timedelta(seconds=self.publish_interval)
        return next_time.isoformat()
    
    def update_after_publish(self):
        """تحديث البيانات بعد النشر"""
        self.last_publish = datetime.now().isoformat()
        self.next_publish = self._calculate_next_publish()
        self.total_published += 1
    
    def is_ready_to_publish(self) -> bool:
        """التحقق من جاهزية القناة للنشر"""
        if not self.enabled:
            return False
        
        if not self.next_publish:
            return True
        
        try:
            next_time = datetime.fromisoformat(self.next_publish).replace(tzinfo=None)
        except ValueError:
            return True
            
        return datetime.now() >= next_time

    @property
    def token_path(self) -> Optional[str]:
        """الحصول على مسار ملف التوكن المتوقع لهذه القناة"""
        if self.platform != "youtube" or not self.youtube_channel_id:
            return None

        try:
            resolved = resolve_channel_token_path(self)
            return resolved or _canonical_youtube_token_path(self.youtube_channel_id)
        except Exception:
            return None
    
    def to_dict(self) -> Dict[str, Any]:
        """تحويل إلى قاموس للحفظ"""
        data = {
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "youtube_channel_id": self.youtube_channel_id,
            "platform": self.platform,
            "platform_channel_id": self.platform_channel_id,
            "platform_credentials": self.platform_credentials,
            "enabled": self.enabled,
            "content_type": self.content_type,
            "privacy": self.privacy,
            "publish_interval": self.publish_interval,
            "language": self.language,  # 🆕 حفظ اللغة
            "last_publish": self.last_publish,
            "next_publish": self.next_publish,
            "total_published": self.total_published,
            "created_at": self.created_at,
            # 🆕 إعدادات Intro/Outro
            "intro_videos": self.intro_videos,
            "outro_videos": self.outro_videos,
            "intro_transition": self.intro_transition,
            "outro_transition": self.outro_transition,
            # 🆕 إعدادات الجدولة
            "scheduling_settings": self.scheduling_settings,
            # 🆕 نصوص مخصصة
            "custom_overlay_texts": self.custom_overlay_texts,
            # 🆕 الاسم الحقيقي للقناة على YouTube
            "youtube_channel_name": getattr(self, "youtube_channel_name", None),
        }
        data.update(self.extra_data)
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Channel':
        """إنشاء من قاموس"""
        # استعادة ملف التوكن محلياً إذا كان موجوداً في البيانات ومفقوداً من القرص
        platform = data.get("platform", "youtube")
        creds = data.get("platform_credentials")
        yt_id = data.get("youtube_channel_id")
        
        if platform == "youtube" and creds and yt_id:
            try:
                import os
                from ..agent.config import load_config
                cfg = load_config()
                # تحديد المسار المتوقع للتوكن
                token_dir = os.path.join(os.path.dirname(cfg.TELEGRAM_DB_PATH) or ".data", "youtube_tokens")
                os.makedirs(token_dir, exist_ok=True)
                token_path = os.path.join(token_dir, f"{yt_id}.json")
                
                if not os.path.exists(token_path):
                    with open(token_path, "w", encoding="utf-8") as f:
                        if isinstance(creds, dict):
                            json.dump(creds, f)
                        else:
                            f.write(str(creds))
                    logger.info(f"🔄 Restored missing token file for channel: {yt_id}")
            except Exception as e:
                logger.warning(f"Failed to restore token file for {yt_id}: {e}")
                
        return cls(**data)


class ChannelManager:
    """مدير القنوات - يدعم 1000+ قناة"""
    
    _instance: Optional['ChannelManager'] = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(ChannelManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, data_dir: str = ".data/channels"):
        if getattr(self, '_initialized', False):
            return
        
        # تحسين المسار: اجعله مطلقاً بالنسبة لجذر المشروع دائماً
        try:
            # محاولة الوصول لجذر المشروع بناءً على موقع هذا الملف
            current_file = Path(__file__).resolve()
            # src/bot/channel_manager.py -> src/bot -> src -> root
            project_root = current_file.parent.parent.parent
            
            # إذا كان المستخدم قد مرر مساراً نسبياً، ندمجه مع جذر المشروع
            if not os.path.isabs(data_dir):
                self.data_dir = project_root / data_dir
            else:
                self.data_dir = Path(data_dir)
        except Exception:
            # احتياطي في حال فشل تحديد المسار
            self.data_dir = Path(data_dir).resolve()

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._channels_cache: Dict[str, Channel] = {}
        self._cache_last_updated = 0.0
        self._cache_ttl = 60.0  # 60 ثانية
        self._initialized = True
        logger.info(f"ChannelManager singleton initialized with data_dir: {self.data_dir}")
    
    def _get_channel_path(self, channel_id: str) -> Path:
        """الحصول على مسار ملف القناة"""
        return self.data_dir / f"{channel_id}.json"
    
    def add_channel(
        self,
        channel_name: str,
        youtube_channel_id: str,
        platform: str = "youtube",
        platform_channel_id: Optional[str] = None,
        platform_credentials: Optional[Dict[str, Any]] = None,
        content_type: str = "minecraft",
        privacy: str = "unlisted",
        publish_interval: int = 3600,
        language: str = "ar",  # 🆕 اللغة
        **kwargs
    ) -> Channel:
        """
        إضافة قناة جديدة
        
        Args:
            channel_name: اسم القناة
            youtube_channel_id: معرف قناة YouTube
            content_type: نوع المحتوى (minecraft, other)
            privacy: الخصوصية (public, unlisted, private)
            publish_interval: فترة النشر بالثواني (3600 = ساعة)
            language: لغة المحتوى (ar, en, es, etc.)
            **kwargs: حقول إضافية
        
        Returns:
            Channel: القناة المضافة
        """
        # توليد معرف فريد
        channel_id = str(uuid.uuid4())
        
        # إنشاء القناة
        channel = Channel(
            channel_id=channel_id,
            channel_name=channel_name,
            youtube_channel_id=youtube_channel_id or (platform_channel_id or ""),
            platform=platform,
            platform_channel_id=platform_channel_id or youtube_channel_id,
            platform_credentials=platform_credentials,
            content_type=content_type,
            privacy=privacy,
            publish_interval=publish_interval,
            language=language,  # 🆕 تمرير اللغة
            **kwargs
        )
        
        # حفظ القناة
        self._save_channel(channel)
        try:
            self._sync_publish_channel(channel)
        except Exception:
            pass
        
        logger.info(f"Added channel: {channel_name} (ID: {channel_id}, Lang: {language})")
        return channel
    
    def get_channel(self, channel_id: str) -> Optional[Channel]:
        """الحصول على قناة بواسطة المعرف (مع استخدام التخزين المؤقت)"""
        # التحقق من الذاكرة أولاً
        if channel_id in self._channels_cache:
            return self._channels_cache[channel_id]

        path = self._get_channel_path(channel_id)
        if not path.exists():
            return None
        
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            channel = Channel.from_dict(data)
            self._channels_cache[channel_id] = channel
            return channel
        except Exception as e:
            logger.error(f"Error loading channel {channel_id}: {e}")
            return None
    
    def list_channels(
        self,
        enabled_only: bool = False,
        content_type: Optional[str] = None,
        offset: int = 0,
        limit: int = 10
    ) -> tuple[List[Channel], int]:
        """
        عرض قائمة القنوات مع pagination
        
        Args:
            enabled_only: عرض القنوات المفعلة فقط
            content_type: تصفية حسب نوع المحتوى
            offset: البداية (للـ pagination)
            limit: عدد القنوات في الصفحة
        
        Returns:
            tuple: (قائمة القنوات, العدد الكلي)
        """
        all_channels = self.list_all_channels(enabled_only=enabled_only, content_type=content_type)
        total = len(all_channels)
        paginated = all_channels[offset:offset + limit]
        return paginated, total

    def list_all_channels(
        self,
        enabled_only: bool = False,
        content_type: Optional[str] = None,
    ) -> List[Channel]:
        """قائمة جميع القنوات (مع تحسين الأداء عبر التخزين المؤقت)"""
        import time
        now = time.time()
        
        # إعادة التحميل من القرص فقط إذا انتهت مدة الـ TTL أو الكاش فارغ
        if not self._channels_cache or (now - self._cache_last_updated > self._cache_ttl):
            logger.debug("🔄 Reloading Channels Cache from disk...")
            new_cache = {}
            for path in self.data_dir.glob("*.json"):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    channel = Channel.from_dict(data)
                    new_cache[channel.channel_id] = channel
                except Exception as e:
                    logger.error(f"Error loading channel from {path}: {e}")
            
            self._channels_cache = new_cache
            self._cache_last_updated = now

        all_channels = list(self._channels_cache.values())

        if enabled_only:
            all_channels = [c for c in all_channels if c.enabled]
        if content_type:
            all_channels = [c for c in all_channels if c.content_type == content_type]

        all_channels.sort(key=lambda c: c.created_at, reverse=True)
        return all_channels
    
    def _validate_auth_token(self, youtube_channel_id: str) -> tuple[bool, str]:
        """
        التحقق من صلاحية توكن المصادقة للقناة:
        - وجود ملف التوكن لكل قناة
        - إمكان تحميل Credentials بهذه الصلاحيات وتجديدها عند الحاجة
        """
        try:
            try:
                cfg = load_config()
            except Exception:
                cfg = None
            token_path = resolve_youtube_token_path(youtube_channel_id, cfg)
            token_file = Path(token_path) if token_path else None
            if not token_file or not token_file.exists() or token_file.stat().st_size <= 0:
                return False, "🔒 لا يوجد توكن مصادقة (Re-auth مطلوب)"
            try:
                from ..agent.uploader import _creds_from_token_file, AuthenticationRequiredError
                _creds_from_token_file(str(token_file))
                return True, ""
            except AuthenticationRequiredError as e:
                msg = str(e) or "فشل التحقق من المصادقة"
                return False, f"🔒 المصادقة غير صالحة: {msg}"
            except Exception:
                return False, "🔒 فشل قراءة ملف المصادقة"
        except Exception:
            return False, "🔒 خطأ غير متوقع أثناء التحقق من المصادقة"

    def _validate_platform_auth(self, channel: Channel) -> tuple[bool, str]:
        """
        التحقق من صلاحية المصادقة حسب المنصة.
        يدعم YouTube افتراضياً ويستخدم نظام المنصات الجديد لباقي المنصات.
        """
        if channel.platform == "youtube":
            return self._validate_auth_token(channel.youtube_channel_id)

        try:
            from ..agent.platforms import get_platform, PlatformCredentials
        except Exception:
            return False, "🔒 منصة غير مهيأة"

        platform = get_platform(channel.platform)
        if not platform:
            return False, "🔒 منصة غير مدعومة"

        creds_data = {}
        if isinstance(channel.platform_credentials, dict):
            creds_data.update(channel.platform_credentials)
        extra_creds = getattr(channel, "extra_data", {}).get("platform_credentials")
        if isinstance(extra_creds, dict):
            creds_data.update(extra_creds)

        access_token = creds_data.get("access_token")
        if not access_token:
            return False, "🔒 لا توجد بيانات مصادقة"

        expires_at = None
        try:
            if creds_data.get("expires_at"):
                expires_at = datetime.fromisoformat(creds_data["expires_at"]).replace(tzinfo=None)
        except Exception:
            expires_at = None

        credentials = PlatformCredentials(
            platform=channel.platform,
            channel_id=channel.platform_channel_id or channel.youtube_channel_id,
            access_token=access_token,
            refresh_token=creds_data.get("refresh_token"),
            token_path=creds_data.get("token_path"),
            expires_at=expires_at,
            extra_data=creds_data.get("extra_data", {}),
        )

        return platform.validate_credentials(credentials)

    def fetch_youtube_channel_name(self, youtube_channel_id: str) -> Optional[str]:
        """
        جلب الاسم الحقيقي لقناة YouTube من API باستخدام التوكن الخاص بها.
        يُرجع الاسم الفعلي أو None في حال الفشل.
        """
        channel_id = str(youtube_channel_id or "").strip()
        if not channel_id:
            return None
        try:
            cfg = load_config()
        except Exception:
            cfg = None
        token_path = resolve_youtube_token_path(channel_id, cfg)
        if not token_path or not os.path.exists(token_path):
            return None
        try:
            from ..agent.uploader import _creds_from_token_file
            creds = _creds_from_token_file(token_path)
            from googleapiclient.discovery import build
            youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
            # Try channels.list with mine=True first (works with standard OAuth scopes)
            resp = youtube.channels().list(part="snippet", mine=True).execute()
            items = resp.get("items") or []
            if items:
                return items[0].get("snippet", {}).get("title")
            # Fallback: list by ID
            resp2 = youtube.channels().list(part="snippet", id=channel_id).execute()
            items2 = resp2.get("items") or []
            if items2:
                return items2[0].get("snippet", {}).get("title")
        except Exception as e:
            logger.debug(f"Failed to fetch YouTube channel name for {channel_id}: {e}")
        return None

    def resolve_channel_url(self, channel: Optional["Channel"]) -> str:
        """
        بناء رابط قناة YouTube الأصلي بناءً على معرف القناة.
        """
        if channel is None:
            return ""
        yt_id = str(getattr(channel, "youtube_channel_id", "") or "").strip()
        if not yt_id:
            return ""
        # If it's already a URL, return as-is
        if yt_id.startswith("http"):
            return yt_id
        return f"https://www.youtube.com/channel/{yt_id}"

    def refresh_all_youtube_names(self) -> Dict[str, str]:
        """
        تحديث أسماء جميع القنوات من YouTube API (للقنوات التي تفتقر للاسم الحقيقي).
        يُرجع قاموس {channel_id: real_name} للقنوات التي تم تحديثها.
        """
        updated: Dict[str, str] = {}
        channels = self.list_all_channels(enabled_only=False)
        for ch in channels:
            if ch.platform != "youtube" or not ch.youtube_channel_id:
                continue
            if getattr(ch, "youtube_channel_name", None):
                continue  # الاسم موجود مسبقاً
            real_name = self.fetch_youtube_channel_name(ch.youtube_channel_id)
            if real_name:
                ch.youtube_channel_name = real_name
                self._save_channel(ch)
                updated[ch.channel_id] = real_name
                logger.info(f"🔄 Refreshed YouTube name: {ch.youtube_channel_id} -> {real_name}")
        return updated

    def update_channel(self, channel_id: str, **updates) -> Optional[Channel]:
        """
        تحديث إعدادات قناة
        
        Args:
            channel_id: معرف القناة
            **updates: الحقول المراد تحديثها
        
        Returns:
            Channel: القناة المحدثة أو None
        """
        channel = self.get_channel(channel_id)
        if not channel:
            return None
        
        # تحديث الحقول
        for key, value in updates.items():
            if hasattr(channel, key):
                setattr(channel, key, value)
            else:
                channel.extra_data[key] = value
        
        # إعادة حساب موعد النشر التالي إذا تغيرت الفترة
        if 'publish_interval' in updates:
            channel.next_publish = channel._calculate_next_publish()
        
        # حفظ التحديثات
        self._save_channel(channel)
        try:
            self._sync_publish_channel(channel)
        except Exception:
            pass
        
        logger.info(f"Updated channel: {channel_id}")
        return channel
    
    def delete_channel(self, channel_id: str) -> bool:
        """حذف قناة"""
        path = self._get_channel_path(channel_id)
        channel = self.get_channel(channel_id)
        
        # إزالة من الذاكرة
        if channel_id in self._channels_cache:
            del self._channels_cache[channel_id]

        if not path.exists():
            return False
        
        try:
            path.unlink()
            try:
                cfg = load_config()
            except Exception:
                cfg = None

            try:
                state = load_state(cfg)
                state_changed = False

                clips_map = state.get("facecam_clips_by_channel")
                if isinstance(clips_map, dict):
                    removed_clips = clips_map.pop(channel_id, None)
                    if removed_clips is not None:
                        state_changed = True
                        for clip in removed_clips if isinstance(removed_clips, list) else []:
                            _delete_file_best_effort((clip or {}).get("path"))

                publish_channels = state.get("publish_channels")
                if isinstance(publish_channels, list):
                    yt_channel_id = str(getattr(channel, "youtube_channel_id", "") or "")
                    filtered_publish_channels = [
                        entry for entry in publish_channels
                        if str((entry or {}).get("internal_id") or "") != str(channel_id)
                        and str((entry or {}).get("channel_id") or "") != yt_channel_id
                    ]
                    if len(filtered_publish_channels) != len(publish_channels):
                        state["publish_channels"] = filtered_publish_channels
                        state_changed = True

                if state_changed:
                    save_state(state, cfg)
            except Exception as e:
                logger.warning(f"Failed to cleanup state for deleted channel {channel_id}: {e}")

            for token_path in _channel_token_candidates(channel, cfg):
                _delete_file_best_effort(token_path)
            logger.info(f"Deleted channel: {channel_id}")
            return True
        except Exception as e:
            logger.error(f"Error deleting channel {channel_id}: {e}")
            return False
    
    def get_channels_ready_to_publish(self) -> List[Channel]:
        """الحصول على القنوات الجاهزة للنشر"""
        channels = self.list_all_channels(enabled_only=True)
        ready: List[Channel] = []
        for c in channels:
            if not c.is_ready_to_publish():
                continue
            ok, _reason = self._validate_platform_auth(c)
            if ok:
                ready.append(c)
        return ready
    
    def get_ready_and_unready_channels(self) -> tuple[List[Channel], List[tuple[Channel, str]]]:
        """
        إرجاع القنوات الجاهزة وغير الجاهزة مع سبب عدم الجاهزية
        
        Returns:
            (ready, unready_with_reason)
        """
        channels = self.list_all_channels(enabled_only=False)
        ready: List[Channel] = []
        unready: List[tuple[Channel, str]] = []
        for ch in channels:
            if not ch.enabled:
                unready.append((ch, "❌ القناة معطلة"))
                continue
            ok, reason = self._validate_platform_auth(ch)
            if not ok:
                unready.append((ch, reason or "🔒 المصادقة غير جاهزة"))
                continue
            if not ch.is_ready_to_publish():
                unready.append((ch, "⏰ موعد النشر لم يصل بعد"))
                continue
            ready.append(ch)
        return ready, unready
    
    def mark_published(self, channel_id: str) -> Optional[Channel]:
        """تحديث القناة بعد النشر"""
        channel = self.get_channel(channel_id)
        if not channel:
            return None
        
        channel.update_after_publish()
        self._save_channel(channel)
        
        logger.info(f"Marked channel as published: {channel_id}")
        return channel
    
    def _save_channel(self, channel: Channel):
        """حفظ القناة في ملف JSON و Supabase (مع مزامنة خلفية)"""
        path = self._get_channel_path(channel.channel_id)

        # تحديث الذاكرة فوراً
        self._channels_cache[channel.channel_id] = channel

        # 🔄 جلب الاسم الحقيقي للقناة من YouTube API إذا لم يكن محفوظاً
        if channel.platform == "youtube" and channel.youtube_channel_id:
            if not getattr(channel, "youtube_channel_name", None):
                try:
                    real_name = self.fetch_youtube_channel_name(channel.youtube_channel_id)
                    if real_name:
                        channel.youtube_channel_name = real_name
                        logger.info(f"✅ Fetched real YouTube name: {real_name} for {channel.youtube_channel_id}")
                except Exception as e:
                    logger.debug(f"Could not fetch YouTube name for {channel.youtube_channel_id}: {e}")

        try:
            data = channel.to_dict()

            # محاولة تضمين محتوى التوكن للمزامنة مع Supabase
            if channel.platform == "youtube" and channel.youtube_channel_id:
                try:
                    import os
                    from ..agent.config import load_config
                    cfg = load_config()
                    token_path = os.path.join(os.path.dirname(cfg.TELEGRAM_DB_PATH) or ".data", "youtube_tokens", f"{channel.youtube_channel_id}.json")
                    if os.path.exists(token_path):
                        with open(token_path, 'r', encoding='utf-8') as f:
                            data["platform_credentials"] = json.load(f)
                except Exception:
                    pass

            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            def _sync_bg():
                try:
                    from ..agent.supabase_storage import save_channel_config
                    save_channel_config(data)
                except Exception as e:
                    logger.warning(f"Background sync failed for channel {channel.channel_id}: {e}")

            try:
                loop = asyncio.get_running_loop()
                loop.run_in_executor(_SUPABASE_SYNC_EXECUTOR, _sync_bg)
            except Exception:
                _SUPABASE_SYNC_EXECUTOR.submit(_sync_bg)
                
        except Exception as e:
            logger.error(f"Error saving channel {channel.channel_id}: {e}")
            raise

    def _sync_publish_channel(self, channel: Channel):
        if channel.platform != "youtube":
            return
        cfg = load_config()
        st = load_state(cfg)
        pubs = st.get("publish_channels", [])
        found = None
        for ch in pubs:
            if ch.get("channel_id") == channel.youtube_channel_id:
                found = ch
                break
        token_guess = resolve_channel_token_path(channel, cfg)
        if token_guess:
            token_guess = _resolve_storage_path(token_guess)

        # إعدادات إضافية لكل قناة نشر (تُخزَّن في حالة البوت)
        extra = getattr(channel, "extra_data", {}) or {}
        quality = extra.get("video_quality") or "720p"
        overlay_font_path = extra.get("overlay_font_path")
        if not overlay_font_path:
            try:
                candidates = [
                    "C:/Windows/Fonts/ARIALUNI.TTF",
                    "C:/Windows/Fonts/arial.ttf",
                    "C:/Windows/Fonts/Tahoma.ttf",
                    "C:/Windows/Fonts/segoeui.ttf",
                    "C:/Windows/Fonts/Traditional_Arabic.ttf",
                ]
                for f in candidates:
                    if os.path.exists(f):
                        overlay_font_path = f
                        break
            except Exception:
                overlay_font_path = None
        overlay_position = extra.get("overlay_position") or "bottom_center"
        custom_description = extra.get("custom_description")
        custom_description_mode = extra.get("custom_description_mode") or "append"
        description_sections = extra.get("description_sections")
        sections_mode = extra.get("sections_mode") or "append"
        facecam_enabled = bool(extra.get("facecam_enabled", False))
        facecam_clip_id = extra.get("facecam_clip_id")
        facecam_position = extra.get("facecam_position") or "top_right"
        facecam_scale = extra.get("facecam_scale")

        entry = {
            "channel_id": channel.youtube_channel_id,
            "internal_id": channel.channel_id,  # 🆕 المعرف الداخلي (UUID) للمطابقة مع المكتبة
            "title": getattr(channel, "youtube_channel_name", None) or channel.channel_name,
            "channel_name": getattr(channel, "youtube_channel_name", None) or channel.channel_name,
            "channel_url": self.resolve_channel_url(channel),
            "enabled": channel.enabled,
            "content_type": channel.content_type,
            "lang": channel.language,
            "privacy": channel.privacy,
            "token_path": token_guess,
            "quality": quality,
            "overlay_font_path": overlay_font_path,
            "overlay_position": overlay_position,
            "custom_description": custom_description,
            "custom_description_mode": custom_description_mode,
            "description_sections": description_sections,
            "sections_mode": sections_mode,
            "facecam_enabled": facecam_enabled,
            "facecam_clip_id": facecam_clip_id,
            "facecam_position": facecam_position,
            "facecam_scale": facecam_scale,
            "custom_overlay_texts": channel.custom_overlay_texts or [],
        }
        if found:
            found.update(entry)
        else:
            pubs.append(entry)
        st["publish_channels"] = pubs
        save_state(st, cfg)
    
    def get_stats(self) -> Dict[str, Any]:
        """الحصول على إحصائيات القنوات"""
        channels = self.list_all_channels(enabled_only=False)
        total = len(channels)
        enabled = sum(1 for c in channels if c.enabled)
        ready = len(self.get_channels_ready_to_publish())
        total_published = sum(int(c.total_published or 0) for c in channels)
        
        return {
            "total_channels": total,
            "enabled_channels": enabled,
            "ready_to_publish": ready,
            "total_videos_published": total_published
        }
    
    # ==================== إدارة Intro/Outro ====================
    
    def add_intro_video(self, channel_id: str, video_path: str) -> bool:
        """
        إضافة فيديو انترو للقناة
        
        Args:
            channel_id: معرف القناة
            video_path: مسار فيديو الانترو
            
        Returns:
            True إذا نجحت الإضافة
        """
        channel = self.get_channel(channel_id)
        if not channel:
            return False
        
        if video_path not in channel.intro_videos:
            channel.intro_videos.append(video_path)
            self._save_channel(channel)
            logger.info(f"تمت إضافة انترو للقناة {channel_id}: {video_path}")
            return True
        return False
    
    def remove_intro_video(self, channel_id: str, video_index: int) -> bool:
        """
        حذف فيديو انترو من القناة
        
        Args:
            channel_id: معرف القناة
            video_index: فهرس الفيديو في القائمة
            
        Returns:
            True إذا نجح الحذف
        """
        channel = self.get_channel(channel_id)
        if not channel:
            return False
        
        if 0 <= video_index < len(channel.intro_videos):
            removed = channel.intro_videos.pop(video_index)
            self._save_channel(channel)
            logger.info(f"تم حذف انترو من القناة {channel_id}: {removed}")
            return True
        return False
    
    def get_random_intro(self, channel_id: str, seed: int = None) -> Optional[str]:
        """
        الحصول على فيديو انترو عشوائي للقناة
        
        Args:
            channel_id: معرف القناة
            seed: بذرة العشوائية (اختياري)
            
        Returns:
            مسار فيديو الانترو أو None
        """
        import random
        channel = self.get_channel(channel_id)
        if not channel or not channel.intro_videos:
            return None
        
        # تصفية الملفات الموجودة فقط
        valid_intros = [p for p in channel.intro_videos if os.path.exists(p)]
        if not valid_intros:
            return None
        
        if seed is not None:
            rng = random.Random(seed)
            return rng.choice(valid_intros)
        return random.choice(valid_intros)
    
    def add_outro_video(self, channel_id: str, video_path: str) -> bool:
        """
        إضافة فيديو اوترو للقناة
        
        Args:
            channel_id: معرف القناة
            video_path: مسار فيديو الاوترو
            
        Returns:
            True إذا نجحت الإضافة
        """
        channel = self.get_channel(channel_id)
        if not channel:
            return False
        
        if video_path not in channel.outro_videos:
            channel.outro_videos.append(video_path)
            self._save_channel(channel)
            logger.info(f"تمت إضافة اوترو للقناة {channel_id}: {video_path}")
            return True
        return False
    
    def remove_outro_video(self, channel_id: str, video_index: int) -> bool:
        """
        حذف فيديو اوترو من القناة
        
        Args:
            channel_id: معرف القناة
            video_index: فهرس الفيديو في القائمة
            
        Returns:
            True إذا نجح الحذف
        """
        channel = self.get_channel(channel_id)
        if not channel:
            return False
        
        if 0 <= video_index < len(channel.outro_videos):
            removed = channel.outro_videos.pop(video_index)
            self._save_channel(channel)
            logger.info(f"تم حذف اوترو من القناة {channel_id}: {removed}")
            return True
        return False
    
    def get_random_outro(self, channel_id: str, seed: int = None) -> Optional[str]:
        """
        الحصول على فيديو اوترو عشوائي للقناة
        
        Args:
            channel_id: معرف القناة
            seed: بذرة العشوائية (اختياري)
            
        Returns:
            مسار فيديو الاوترو أو None
        """
        import random
        channel = self.get_channel(channel_id)
        if not channel or not channel.outro_videos:
            return None
        
        # تصفية الملفات الموجودة فقط
        valid_outros = [p for p in channel.outro_videos if os.path.exists(p)]
        if not valid_outros:
            return None
        
        if seed is not None:
            rng = random.Random(seed)
            return rng.choice(valid_outros)
        return random.choice(valid_outros)
    
    def set_intro_transition(self, channel_id: str, transition: str) -> bool:
        """تعيين تأثير الانتقال للانترو"""
        return self.update_channel(channel_id, intro_transition=transition) is not None
    
    def set_outro_transition(self, channel_id: str, transition: str) -> bool:
        """تعيين تأثير الانتقال للاوترو"""
        return self.update_channel(channel_id, outro_transition=transition) is not None
