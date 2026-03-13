"""
معالج فيديوهات المودات - نظام منفصل عن المحتوى الحالي
يتضمن: قص الثواني الأولى والأخيرة، إضافة نص دعوة، تحويل لشورتس
"""
import os
import subprocess
import logging
import time
import re
import uuid
import hashlib
from typing import Optional, Tuple, Dict, Any
from pathlib import Path
from .ffmpeg_utils import ffmpeg_bin, ffprobe_bin
from .config import load_config

try:
    from fontTools.ttLib import TTFont
    HAS_FONTTOOLS = True
except Exception:
    TTFont = None
    HAS_FONTTOOLS = False

# مكتبات معالجة اللغة العربية 🆕
try:
    import arabic_reshaper
    from bidi.algorithm import get_display
    HAS_ARABIC_SUPPORT = True
except ImportError:
    HAS_ARABIC_SUPPORT = False

logger = logging.getLogger(__name__)


def _parse_volume_ratio(raw: str, default_ratio: float) -> float:
    try:
        s = (raw or "").strip()
        if not s:
            return default_ratio
        v = float(s)
        # Accept 60/70 style percentages
        if v > 1.5:
            v = v / 100.0
        return max(0.0, min(4.0, v))
    except Exception:
        return default_ratio


def _get_random_volume_level(min_percent: int = 90, max_percent: int = 100) -> float:
    """
    الحصول على مستوى صوت عشوائي بين الحد الأدنى والأقصى
    
    يستخدم لتنويع مستوى الصوت بين الفيديوهات لتجنب التكرار
    """
    import random
    percent = random.randint(min_percent, max_percent)
    return percent / 100.0


"""معالج فيديوهات المودات"""


def _get_shorts_encoder_settings() -> dict:
    """
    Get optimal encoder settings for shorts. Auto-detects GPU encoders.
    Optimized for YouTube quality with faststart and proper B-frames.
    """
    def _env_str(name: str, default: str) -> str:
        return (os.getenv(name, default) or default).strip()
    
    def _env_int(name: str, default: int) -> int:
        try:
            return int((os.getenv(name, str(default)) or str(default)).strip())
        except Exception:
            return default

    def _test_encoder(encoder: str, preset: Optional[str], extra_args: Optional[list] = None) -> bool:
        """Verify that an advertised encoder can actually complete a tiny encode."""
        try:
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                tmp_path = tmp.name

            cmd = [
                ffmpeg_bin(), "-y",
                "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1",
                "-an",
                "-c:v", encoder,
            ]
            if preset:
                cmd.extend(["-preset", preset])
            if extra_args:
                cmd.extend(extra_args)
            cmd.append(tmp_path)

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            return result.returncode == 0
        except Exception as e:
            logger.debug(f"Shorts encoder self-test failed for {encoder}: {e}")
            return False
        finally:
            try:
                if 'tmp_path' in locals() and tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except Exception:
                pass
    
    is_low_res = os.getenv("LOW_RESOURCE_MODE") == "1" or os.getenv("FFMPEG_LOW_CPU") == "1" or os.getenv("RENDER") in ("1", "true")
    # YouTube-optimized settings for shorts
    # Defaults tailored for speed on mobile/low-end devices while maintaining good quality
    settings = {
        "encoder": "libx264",
        "preset": _env_str("SHORTS_X264_PRESET", "ultrafast" if is_low_res else "medium"),
        "crf": _env_str("SHORTS_X264_CRF", "28" if is_low_res else "20"),
        "threads": _env_int("FFMPEG_THREADS", 1 if is_low_res else 0),
        "extra_args": [
            "-profile:v", "high",
            "-level", _env_str("SHORTS_H264_LEVEL", "4.2"), # 4.2 is safer for mobile/TikTok/Shorts
            "-bf", "2",
            "-g", "30",
            "-pix_fmt", "yuv420p",
            # YouTube Critical
            "-movflags", "+faststart",
            "-use_editlist", "0",
            "-map_metadata", "-1",
        ],
        "min_bitrate": _env_str("SHORTS_MIN_BITRATE", "5M"),
        "audio_bitrate": _env_str("AUDIO_BITRATE", "256k"),
        "audio_sample_rate": _env_int("AUDIO_SAMPLE_RATE", 44100),
        "is_gpu": False
    }
    
    hwaccel_mode = _env_str("FFMPEG_USE_HWACCEL", "auto")
    if hwaccel_mode.lower() in {"false", "0", "no", "off", "disabled"}:
        return settings
    
    try:
        cmd = [ffmpeg_bin(), "-hide_banner", "-encoders"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        out = result.stdout if result.returncode == 0 else ""
        
        # 1. Check for Android MediaCodec (Best for Termux)
        if "h264_mediacodec" in out:
            settings["encoder"] = "h264_mediacodec"
            settings["preset"] = None # MediaCodec doesn't use presets like x264
            settings["crf"] = None
            # MediaCodec relies on bitrate mainly
            settings["extra_args"] = [
                "-b:v", "6M", # Target 6Mbps
                "-maxrate", "10M",
                "-bufsize", "12M",
                "-profile:v", "high",
                "-level", "4.2",
                "-movflags", "+faststart",
                "-map_metadata", "-1",
            ]
            settings["is_gpu"] = True
            logger.info("✅ Shorts Hardware Accel: Android MediaCodec (h264_mediacodec)")
            return settings

        # 2. NVIDIA NVENC
        if "h264_nvenc" in out:
            nvenc_preset = _env_str("FFMPEG_NVENC_PRESET", "p4")
            nvenc_args = [
                "-rc", "vbr",
                "-cq", _env_str("SHORTS_X264_CRF", "23"),
                "-b:v", settings["min_bitrate"],
                "-maxrate", "10M",
                "-bufsize", "14M",
                "-profile:v", "high",
                "-movflags", "+faststart",
                "-map_metadata", "-1",
            ]
            if _test_encoder("h264_nvenc", nvenc_preset, nvenc_args):
                settings["encoder"] = "h264_nvenc"
                settings["preset"] = nvenc_preset
                settings["crf"] = None
                settings["extra_args"] = nvenc_args
                settings["is_gpu"] = True
                logger.info("✅ Shorts GPU: NVIDIA NVENC")
                return settings
            logger.warning("⚠️ Shorts NVENC listed but self-test failed; falling back to CPU encoder")
        
        # 3. Intel QuickSync
        if "h264_qsv" in out:
            settings["encoder"] = "h264_qsv"
            settings["preset"] = "faster"
            settings["extra_args"] = [
                "-global_quality", _env_str("SHORTS_X264_CRF", "23"),
                "-look_ahead", "0",
                "-movflags", "+faststart",
                "-map_metadata", "-1",
            ]
            settings["crf"] = None
            settings["is_gpu"] = True
            logger.info("✅ Shorts GPU: Intel QuickSync")
            return settings
            
        # 4. Apple VideoToolbox (Mac)
        if "h264_videotoolbox" in out:
            settings["encoder"] = "h264_videotoolbox"
            settings["preset"] = None
            settings["crf"] = None
            settings["extra_args"] = [
                 "-q:v", "60", # 0-100 quality scale
                 "-profile:v", "high",
                 "-movflags", "+faststart",
                 "-map_metadata", "-1",
            ]
            settings["is_gpu"] = True
            logger.info("✅ Shorts GPU: Apple VideoToolbox")
            return settings

    except Exception as e:
        logger.warning(f"Error checking for hwaccel: {e}")
    
    return settings

class ModVideoProcessor:
    
    def __init__(self, temp_dir: Optional[str] = None):
        if temp_dir is None:
            from .config import load_config
            cfg = load_config()
            temp_dir = os.path.join(cfg.TEMP_DIR, "mods")
        self.temp_dir = Path(temp_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
    
    def process_mod_video(
        self,
        input_video: str,
        output_dir: str,
        video_id: str,
        trim_start: float = 1.0,
        trim_end: float = 1.0,
        add_cta: bool = True,
        cta_text: str = "لتحميل التطبيق المستخدم في الشرح\nحمل تطبيقنا الآن من الرابط في الوصف",
        top_text: Optional[str] = None,
        convert_to_shorts: bool = True,
        custom_font: Optional[str] = None,
        top_text_size: int = 64,
        top_text_y: int = 150,
        is_custom: bool = False,
        enhance: bool = False,
        shorts_format: str = "crop",
        cta_position: Optional[str] = None,
        video_effects: Optional[Dict[str, Any]] = None,
        hflip: bool = False,
        progress_callback: Optional[callable] = None,
    ) -> Tuple[str, dict]:
        """
        معالجة فيديو مود
        
        Args:
            input_video: مسار الفيديو المدخل
            output_dir: مجلد الإخراج
            video_id: معرف الفيديو
            trim_start: عدد الثواني المراد قصها من البداية
            trim_end: عدد الثواني المراد قصها من النهاية
            add_cta: إضافة نص الدعوة في النهاية
            cta_text: نص الدعوة
            top_text: نص يظهر في أعلى الفيديو طوال الوقت
            convert_to_shorts: تحويل الفيديو لصيغة شورتس
            custom_font: مسار ملف خط مخصص
            top_text_size: حجم خط النص العلوي
            top_text_y: موقع النص العلوي (Y)
            is_custom: هل هذا هو النمط المخصص (لتغيير شكل النص)
            video_effects: إعدادات تأثيرات البداية/النهاية الخاصة بالمصدر
            hflip: هل يتم قلب الفيديو أفقياً (Mirror)
        
        Returns:
            tuple: (مسار الفيديو المعالج, معلومات إضافية)
        """
        os.makedirs(output_dir, exist_ok=True)
        
        # التأكد من وجود البرامج اللازمة
        if not ffmpeg_bin() or not ffprobe_bin():
             logger.warning("⚠️ FFmpeg or FFprobe not found. Returning original video without processing.")
             return input_video, {"status": "skipped_no_ffmpeg", "original_path": input_video}
        
        
        # الحصول على معلومات الفيديو
        duration = self._get_video_duration(input_video)
        width, height = self._get_video_dimensions(input_video)
        final_width, final_height = width, height
        
        logger.info(f"Processing mod video: {video_id} (Custom: {is_custom})")
        logger.info(f"Original duration: {duration}s, dimensions: {width}x{height}")
        
        # Start timing for each step
        step_timings = {
            "total": time.time(),
            "trim": None,
            "flip": None,
            "convert": None,
            "overlay": None,
            "effects": None,
            "enhance": None,
            "encode": None
        }
        
        # حساب المدة الجديدة
        new_duration = duration - trim_start - trim_end
        
        if new_duration <= 0:
            raise ValueError(f"Video too short after trimming: {new_duration}s")
        
        # المسارات المؤقتة
        trimmed_path = self.temp_dir / f"{video_id}_trimmed.mp4"
        flipped_path = self.temp_dir / f"{video_id}_flipped.mp4"
        resized_path = self.temp_dir / f"{video_id}_resized.mp4"
        overlay_path = self.temp_dir / f"{video_id}_overlay.mp4"
        final_path = Path(output_dir) / f"{video_id}_mod.mp4"
        try:
            apply_processing_effects = str(os.getenv("SHORTS_EFFECTS_APPLY_DURING_PROCESSING", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}
        except Exception:
            apply_processing_effects = False
        
        try:
            current_path = input_video
            target_fps = self._get_video_fps(current_path)
            
            # الخطوة 1: قص البداية والنهاية (فقط إذا كانت القيم أكبر من 0)
            # 🔧 استخدام stream copy للحفاظ على الجودة الأصلية
            # سيتم الترميز لاحقاً في convert_to_shorts أو الترميز النهائي
            if trim_start > 0 or trim_end > 0:
                step_start = time.time()
                logger.info("✂️ Step 1/5: Trimming video...")
                if progress_callback:
                    try: progress_callback("1/5 ✂️ قص الفيديو...")
                    except Exception: pass
                self._trim_video(current_path, trimmed_path, trim_start, trim_end, force_encode=False)
                current_path = str(trimmed_path)
                step_timings["trim"] = time.time() - step_start
                logger.info(f"✅ Step 1/5 completed in {step_timings['trim']:.2f}s")
            
            # الخطوة 1.5: قلب الفيديو أفقياً (إذا تم طلبه)
            if hflip:
                logger.info("↔️ Flipping video horizontally (mirroring)...")
                self._flip_video(current_path, flipped_path)
                current_path = str(flipped_path)
            
            # الخطوة 2: تحويل لصيغة شورتس (9:16)
            if convert_to_shorts:
                fmt = (shorts_format or "crop").strip().lower()
                try:
                    skip_if_vertical = str(os.getenv("AUTO_MOD_SKIP_SHORTS_CONVERT_IF_VERTICAL", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
                except Exception:
                    skip_if_vertical = True

                should_skip_conversion = False
                if skip_if_vertical and fmt == "crop" and width > 0 and height > 0:
                    try:
                        should_skip_conversion = abs((width / height) - (9.0 / 16.0)) <= 0.02
                    except Exception:
                        should_skip_conversion = False

                if should_skip_conversion:
                    logger.info(f"📐 Step 2/5: Skipping shorts conversion (already 9:16: {width}x{height})")
                    if progress_callback:
                        try: progress_callback("2/5 📐 تخطي التحويل (الفيديو عمودي 9:16 مسبقاً)...")
                        except Exception: pass
                    step_timings["convert"] = 0.0
                else:
                    step_start = time.time()
                    logger.info("📐 Step 2/5: Converting to shorts format...")
                    if progress_callback:
                        try: progress_callback("2/5 📐 تحويل لصيغة شورتس...")
                        except Exception: pass
                    self._convert_to_shorts(current_path, resized_path, width, height, shorts_format=shorts_format)
                    current_path = str(resized_path)
                    final_width, final_height = 1080, 1920
                    step_timings["convert"] = time.time() - step_start
                    logger.info(f"✅ Step 2/5 completed in {step_timings['convert']:.2f}s")
            
            # الخطوة 3: إضافة نص علوي (اختياري)
            if top_text:
                step_start = time.time()
                logger.info("📝 Step 3/5: Adding text overlay...")
                if progress_callback:
                    try: progress_callback("3/5 📝 إضافة نص...")
                    except Exception: pass
                self._add_top_overlay_text(current_path, overlay_path, top_text, custom_font, top_text_size, top_text_y, is_custom)
                current_path = str(overlay_path)
                step_timings["overlay"] = time.time() - step_start
                logger.info(f"✅ Step 3/5 completed in {step_timings['overlay']:.2f}s")

            # الخطوة 4: إضافة تأثيرات البداية/النهاية
            effects_start = time.time()
            if convert_to_shorts:
                explicit_effects = video_effects if isinstance(video_effects, dict) else None
                if explicit_effects:
                    intro_cfg = self._normalize_explicit_video_effect(explicit_effects.get("intro") or {})
                    outro_cfg = self._normalize_explicit_video_effect(explicit_effects.get("outro") or {})
                    if intro_cfg.get("enabled") or outro_cfg.get("enabled"):
                        effects_path = self.temp_dir / f"{video_id}_effects.mp4"
                        self._apply_configured_intro_outro_effects(current_path, effects_path, explicit_effects)
                        current_path = str(effects_path)
                elif apply_processing_effects:
                    # الحفاظ على السلوك الافتراضي القديم فقط عندما لا توجد إعدادات مصدر صريحة
                    effects_path = self.temp_dir / f"{video_id}_effects.mp4"
                    self.add_simple_intro_outro_effects(current_path, effects_path, seed=video_id, apply_outro=False)
                    current_path = str(effects_path)

                    outro_path = self.temp_dir / f"{video_id}_outro.mp4"
                    self._apply_outro_blur_black(current_path, outro_path, 1.0)
                    current_path = str(outro_path)
            step_timings["effects"] = time.time() - effects_start
            if step_timings["effects"] > 0.1:
                logger.info(f"✅ Step 4/5 (effects) completed in {step_timings['effects']:.2f}s")
            
            # الخطوة 5: تحسين سينمائي (اختياري)
            if enhance:
                step_start = time.time()
                enhance_path = self.temp_dir / f"{video_id}_enhanced.mp4"
                ok = self._apply_cinematic_teal_boost(current_path, enhance_path)
                if ok:
                    current_path = str(enhance_path)
                step_timings["enhance"] = time.time() - step_start
                logger.info(f"✅ Step 5/5 (enhance) completed in {step_timings['enhance']:.2f}s")
            
            # الخطوة 6: الترميز النهائي
            encode_start = time.time()
            logger.info("🎬 Step 6/6: Final encoding...")
            if progress_callback:
                try: progress_callback("6/6 🎬 الترميز النهائي...")
                except Exception: pass
            if convert_to_shorts:
                ok_final = self._encode_final_shorts(current_path, str(final_path), target_fps)
                if not ok_final:
                    raise RuntimeError(f"Failed to encode final shorts: {final_path}")
            else:
                self._optimize_for_youtube(current_path, str(final_path))
            step_timings["encode"] = time.time() - encode_start
            logger.info(f"✅ Step 6/6 (final encode) completed in {step_timings['encode']:.2f}s")
            
            # معلومات الفيديو المعالج
            info = {
                "original_duration": duration,
                "new_duration": new_duration,
                "final_size": f"{final_width}x{final_height}",
                "final_path": str(final_path)
            }
            return str(final_path), info


            
        finally:
            # تنظيف الملفات المؤقتة
            for temp_file in [trimmed_path, flipped_path, resized_path, overlay_path, self.temp_dir / f"{video_id}_effects.mp4", self.temp_dir / f"{video_id}_outro.mp4", self.temp_dir / f"{video_id}_enhanced.mp4"]:
                if temp_file.exists():
                    try:
                        temp_file.unlink()
                    except Exception as e:
                        logger.warning(f"Failed to delete temp file {temp_file}: {e}")

    def _normalize_explicit_video_effect(self, raw_effect: Any) -> Dict[str, Any]:
        effect_type = "none"
        duration = 0.0
        enabled = False

        if isinstance(raw_effect, dict):
            effect_type = str(raw_effect.get("type") or raw_effect.get("effect") or "none").strip().lower()
            enabled = bool(raw_effect.get("enabled", effect_type not in {"none", "off", "disabled", "no"}))
            duration = raw_effect.get("duration", 1.0)
        elif isinstance(raw_effect, str):
            effect_type = raw_effect.strip().lower()
            enabled = effect_type not in {"none", "off", "disabled", "no"}
            duration = 1.0

        aliases = {
            "none": "none",
            "off": "none",
            "disabled": "none",
            "no": "none",
            "blur": "blur",
            "normal_blur": "blur",
            "black_blur": "black_blur",
            "blur_black": "black_blur",
            "black": "black_blur",
        }
        effect_type = aliases.get(effect_type, "none")
        try:
            duration = max(0.0, float(duration or 0.0))
        except Exception:
            duration = 0.0

        if effect_type == "none" or not enabled:
            return {"enabled": False, "type": "none", "duration": 0.0}
        return {"enabled": True, "type": effect_type, "duration": min(max(duration, 0.3), 3.0)}

    def _apply_configured_intro_outro_effects(self, input_path: str, output_path: str, video_effects: Optional[Dict[str, Any]]) -> None:
        import shutil

        explicit_effects = video_effects if isinstance(video_effects, dict) else {}
        intro_cfg = self._normalize_explicit_video_effect(explicit_effects.get("intro") or {})
        outro_cfg = self._normalize_explicit_video_effect(explicit_effects.get("outro") or {})

        if not intro_cfg.get("enabled") and not outro_cfg.get("enabled"):
            shutil.copy2(input_path, output_path)
            return

        duration_s = self._get_video_duration(input_path)
        if not duration_s or duration_s <= 0:
            shutil.copy2(input_path, output_path)
            return

        intro_d = min(float(intro_cfg.get("duration", 0.0) or 0.0), max(0.15, duration_s * 0.45)) if intro_cfg.get("enabled") else 0.0
        outro_d = min(float(outro_cfg.get("duration", 0.0) or 0.0), max(0.15, duration_s * 0.45)) if outro_cfg.get("enabled") else 0.0
        total_fx = intro_d + outro_d
        max_total = max(0.3, duration_s * 0.9)
        if total_fx > max_total and total_fx > 0:
            scale = max_total / total_fx
            intro_d *= scale
            outro_d *= scale

        outro_start = max(0.0, duration_s - outro_d)
        vf_parts = ["setpts=PTS-STARTPTS"]

        if intro_cfg.get("enabled") and intro_d > 0:
            vf_parts.append(f"fade=t=in:st=0:d={intro_d:.3f}")
            vf_parts.append(f"boxblur=luma_radius={12 if intro_cfg.get('type') == 'black_blur' else 8}:enable='between(t,0,{intro_d:.3f})'")
            if intro_cfg.get("type") == "black_blur":
                vf_parts.append(f"eq=brightness=-0.18:saturation=0.92:enable='between(t,0,{intro_d:.3f})'")

        if outro_cfg.get("enabled") and outro_d > 0:
            vf_parts.append(f"fade=t=out:st={outro_start:.3f}:d={outro_d:.3f}")
            vf_parts.append(f"boxblur=luma_radius={12 if outro_cfg.get('type') == 'black_blur' else 8}:enable='between(t,{outro_start:.3f},{duration_s:.3f})'")
            if outro_cfg.get("type") == "black_blur":
                vf_parts.append(f"eq=brightness=-0.18:saturation=0.92:enable='between(t,{outro_start:.3f},{duration_s:.3f})'")

        vf_parts.append("format=yuv420p")
        vf = ",".join(vf_parts)
        ff_threads, base_preset, base_crf = self._shorts_x264_settings()
        preset = str(os.getenv("SHORTS_INTERMEDIATE_PRESET", base_preset) or base_preset).strip() or base_preset
        crf = int(os.getenv("SHORTS_INTERMEDIATE_CRF", str(base_crf)) or str(base_crf))
        level = (os.getenv("SHORTS_H264_LEVEL", "5.1") or "5.1").strip() or "5.1"
        has_audio = self._has_audio(input_path)
        fps = self._get_video_fps(input_path)
        if not fps or fps <= 0:
            fps = 30.0
        gop = max(1, int(round(fps)))

        cmd = [
            ffmpeg_bin(),
            "-y",
            "-i", input_path,
            "-vf", vf,
            "-vsync", "cfr",
            "-map", "0:v",
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", str(crf),
            "-profile:v", "high",
            "-level", level,
            "-pix_fmt", "yuv420p",
            "-r", f"{fps:.6f}",
            "-g", str(gop),
            "-keyint_min", str(max(1, gop // 2)),
            "-sc_threshold", "0",
            "-force_key_frames", "expr:gte(t,0)",
            "-threads", str(ff_threads),
        ]
        if has_audio:
            cmd += ["-map", "0:a?", "-c:a", "aac", "-b:a", "384k", "-ar", "48000"]
        else:
            cmd += ["-an"]
        cmd += ["-movflags", "+faststart", str(output_path)]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if res.returncode != 0:
            raise RuntimeError((res.stderr or "")[-2500:])
    
    def _optimize_for_youtube(self, input_path: str, output_path: str) -> bool:
        """
        Final optimization pass for YouTube:
        - Ensures Move Atom is at start (Fast Start)
        - Removes all metadata and Edit Lists
        - Ensures clean container without re-encoding
        """
        try:
            logger.info("🚀 Running final YouTube optimization pass...")
            cmd = [
                ffmpeg_bin(), "-y",
                "-i", input_path,
                "-c", "copy",  # Stream copy (no re-encoding, fast)
                "-map_metadata", "-1",  # Remove all metadata
                "-movflags", "+faststart",  # Move atom to front
                "-use_editlist", "0",  # Disable edit lists
                "-f", "mp4",
                output_path
            ]
            
            result = subprocess.run(cmd, capture_output=True, timeout=300)
            if result.returncode == 0 and os.path.exists(output_path):
                logger.debug("✅ YouTube optimization successful")
                return True
            else:
                logger.warning(f"Optimization failed: {result.stderr.decode()[:200]}")
                return False

        except Exception as e:
            logger.error(f"Error in YouTube optimization: {e}")
            return False

    def _encode_final_shorts(self, input_path: str, output_path: str, target_fps: Optional[float] = None) -> bool:
        try:
            def _run_encode(enc_settings: dict) -> Tuple[bool, str]:
                fps = float(target_fps) if target_fps and target_fps > 0 else self._get_video_fps(input_path)
                if not fps or fps <= 0:
                    fps = 30.0
                gop = int(round(fps))
                if gop < 1:
                    gop = 30
                has_audio = self._has_audio(input_path)

                out_s = str(output_path)
                if out_s.lower().endswith(".mp4"):
                    tmp_out = out_s[:-4] + ".tmp.mp4"
                else:
                    tmp_out = out_s + ".tmp.mp4"
                try:
                    Path(out_s).parent.mkdir(parents=True, exist_ok=True)
                    Path(tmp_out).parent.mkdir(parents=True, exist_ok=True)
                except Exception:
                    pass
                try:
                    if os.path.exists(tmp_out):
                        os.remove(tmp_out)
                except Exception:
                    pass

                cmd = [
                    ffmpeg_bin(),
                    "-y",
                    "-fflags", "+genpts+igndts",
                    "-i", input_path,
                    "-map", "0:v",
                    "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
                    "-c:v", str(enc_settings.get("encoder") or "libx264"),
                ]

                preset = enc_settings.get("preset")
                if preset:
                    cmd += ["-preset", str(preset)]

                crf = enc_settings.get("crf")
                if crf is not None and str(enc_settings.get("encoder") or "").lower() == "libx264":
                    cmd += ["-crf", str(crf)]

                extra_args = list(enc_settings.get("extra_args") or [])
                # Prevent conflicts/duplicates with options we enforce below
                skip_flags = {
                    "-movflags",
                    "-map_metadata",
                    "-use_editlist",
                    "-vsync",
                    "-r",
                    "-g",
                }
                filtered_extra: list[str] = []
                i = 0
                while i < len(extra_args):
                    tok = str(extra_args[i])
                    if tok in skip_flags:
                        i += 2
                        continue
                    filtered_extra.append(tok)
                    i += 1
                cmd += filtered_extra

                cmd += [
                    "-vsync", "cfr",
                    "-r", f"{fps:.6f}",
                    "-g", str(gop),
                    "-keyint_min", str(max(1, gop // 2)),
                    "-sc_threshold", "0",
                    "-force_key_frames", "expr:gte(t,0)",
                    "-pix_fmt", "yuv420p",
                ]

                if enc_settings.get("threads"):
                    cmd += ["-threads", str(int(enc_settings["threads"]))]

                if has_audio:
                    cmd += [
                        "-map", "0:a?",
                        "-c:a", "aac",
                        "-b:a", str(enc_settings.get("audio_bitrate") or "384k"),
                        "-ar", str(enc_settings.get("audio_sample_rate") or 48000),
                    ]
                else:
                    cmd += ["-an"]

                cmd += [
                    "-movflags", "+faststart",
                    "-map_metadata", "-1",
                    "-use_editlist", "0",
                    "-f", "mp4",
                    str(tmp_out),
                ]

                result = subprocess.run(cmd, capture_output=True, timeout=600)
                stderr = (result.stderr or b"").decode(errors="ignore")
                if result.returncode != 0:
                    return False, stderr
                try:
                    self._validate_video_file(str(tmp_out))
                except Exception as e:
                    return False, str(e)

                try:
                    if os.path.exists(str(output_path)):
                        os.remove(str(output_path))
                except Exception:
                    pass
                try:
                    os.replace(str(tmp_out), str(output_path))
                except Exception:
                    import shutil
                    shutil.copy2(str(tmp_out), str(output_path))
                    try:
                        os.remove(str(tmp_out))
                    except Exception:
                        pass
                return os.path.exists(str(output_path)) and os.path.getsize(str(output_path)) > 0, stderr

            settings = _get_shorts_encoder_settings()
            ok, err = _run_encode(settings)
            if ok:
                return True

            if err:
                logger.error(f"Final shorts encode failed (encoder={settings.get('encoder')}), retrying fallback. ffmpeg stderr: {err[-1500:]}")

            # Fallback to libx264 if GPU encoder fails
            if str(settings.get("encoder") or "").lower() != "libx264":
                ff_threads, preset, crf = self._shorts_x264_settings()
                lvl = (os.getenv("SHORTS_H264_LEVEL", "5.1") or "5.1").strip() or "5.1"
                fallback = {
                    "encoder": "libx264",
                    "preset": preset,
                    "crf": str(crf),
                    "threads": ff_threads,
                    "extra_args": ["-profile:v", "high", "-level", lvl],
                    "audio_bitrate": settings.get("audio_bitrate") or "384k",
                    "audio_sample_rate": settings.get("audio_sample_rate") or 48000,
                }
                ok2, err2 = _run_encode(fallback)
                if ok2:
                    return True
                if err2:
                    logger.error(f"Final shorts encode fallback failed. ffmpeg stderr: {err2[-1500:]}")
            return False
        except Exception as e:
            logger.error(f"Error encoding final shorts: {e}")
            return False

    def _apply_cinematic_teal_boost(self, input_path: str, output_path: Path) -> bool:
        """تحسين بسيط قابل للتعديل: تشبع + تباين + تعريض + هايلايت مع مزج بنسبة كثافة"""
        try:
            def _env_float(name: str, default: float) -> float:
                try:
                    raw = os.getenv(name, str(default)) or str(default)
                    import re
                    cleaned = re.sub(r"[^0-9\.\-]", "", raw).strip()
                    if cleaned == "":
                        return default
                    return float(cleaned)
                except Exception:
                    return default
            sat_pct = _env_float("ENHANCE_SAT_PCT", 25.0)
            contrast_pct = _env_float("ENHANCE_CONTRAST_PCT", 15.0)
            bright_pct = _env_float("ENHANCE_BRIGHTNESS_PCT", 10.0)
            highlights_pct = _env_float("ENHANCE_HIGHLIGHTS_PCT", 20.0)
            intensity_pct = _env_float("ENHANCE_INTENSITY_PCT", 70.0)
            sat = max(0.0, 1.0 + sat_pct / 100.0)
            contrast = max(0.0, 1.0 + contrast_pct / 100.0)
            brightness = max(-1.0, min(1.0, bright_pct / 100.0))
            hl = max(0.0, min(1.0, highlights_pct / 100.0))
            opacity = max(0.0, min(1.0, intensity_pct / 100.0))
            logger.info(
                f"Enhance params -> sat_pct={sat_pct}%, contrast_pct={contrast_pct}%, "
                f"bright_pct={bright_pct}%, highlights_pct={highlights_pct}%, intensity_pct={intensity_pct}% "
                f"(mapped: sat={sat}, contrast={contrast}, brightness={brightness}, hl={hl}, opacity={opacity})"
            )
            def _run_filter(filter_complex: str, map_out: bool = True) -> Tuple[bool, str]:
                ff_threads, preset, crf = self._shorts_x264_settings()
                has_audio = self._has_audio(input_path)
                # 🔧 استخدام نفس إعدادات الوسيط المحسّنة
                try:
                    lossless_intermediate = str(os.getenv("SHORTS_INTERMEDIATE_LOSSLESS", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}
                except Exception:
                    lossless_intermediate = False
                fps = self._get_video_fps(input_path)
                if not fps or fps <= 0:
                    fps = 30.0
                gop = int(round(fps))
                if gop < 1:
                    gop = 30
                if lossless_intermediate:
                    preset = str(os.getenv("SHORTS_INTERMEDIATE_PRESET", "ultrafast") or "ultrafast").strip() or "ultrafast"
                    crf = 0
                    v_profile = "high444"
                    v_pix_fmt = "yuv444p"
                else:
                    # 🆕 إعدادات محسّنة لبيئات الموارد المنخفضة
                    _, base_preset, base_crf = self._shorts_x264_settings()
                    preset = str(os.getenv("SHORTS_INTERMEDIATE_PRESET", base_preset) or base_preset).strip() or base_preset
                    crf = int(os.getenv("SHORTS_INTERMEDIATE_CRF", str(base_crf)) or str(base_crf))
                    v_profile = None
                    v_pix_fmt = "yuv420p"
                # 🆕 استخدام مستوى صوت عشوائي (90-100%) لتنويع المحتوى
                shorts_vol = _get_random_volume_level(90, 100)

                cmd = [
                    ffmpeg_bin(), "-y",
                    "-i", input_path,
                    "-filter_complex", filter_complex,
                    "-map", "[outv]" if map_out else "0:v",
                ]
                if has_audio:
                    cmd += [
                        "-map", "0:a?",
                        "-c:a", "aac",
                        "-b:a", "384k",  # YouTube recommended: 384kbps stereo
                        "-af", f"volume={shorts_vol}",
                    ]
                cmd += [
                    "-c:v", "libx264",
                    "-preset", preset,
                    "-crf", str(crf),
                    "-pix_fmt", v_pix_fmt,
                    "-vsync", "cfr",
                    "-r", f"{fps:.6f}",
                    "-g", str(gop),
                    "-threads", str(ff_threads),
                    str(output_path)
                ]
                if v_profile:
                    # x264 lossless is not supported with profile=high/yuv420p
                    # use high444 for intermediates
                    cmd[cmd.index("-c:v") + 2:cmd.index("-c:v") + 2] = ["-profile:v", v_profile]
                result = subprocess.run(cmd, capture_output=True, timeout=600)
                stderr = (result.stderr or b"").decode(errors="ignore")
                ok = (result.returncode == 0) and os.path.exists(output_path) and os.path.getsize(output_path) > 0
                return ok, stderr

            # Try 1: eq + colorbalance (highlights) + blend (intensity)
            fc1 = (
                f"[0:v]split[orig][proc];"
                f"[proc]eq=contrast={contrast}:brightness={brightness}:saturation={sat},"
                f"colorbalance=rh={hl}:gh={hl}:bh={hl}[proc2];"
                f"[orig][proc2]blend=all_mode=normal:all_opacity={opacity}[outv]"
            )
            ok, err = _run_filter(fc1)
            if ok:
                return True
            if err:
                logger.warning(f"Enhance filter (with colorbalance) failed, retrying fallback. ffmpeg stderr: {err[:800]}")

            # Try 2: eq + blend only
            fc2 = (
                f"[0:v]split[orig][proc];"
                f"[proc]eq=contrast={contrast}:brightness={brightness}:saturation={sat}[proc2];"
                f"[orig][proc2]blend=all_mode=normal:all_opacity={opacity}[outv]"
            )
            ok, err = _run_filter(fc2)
            if ok:
                return True
            if err:
                logger.warning(f"Enhance filter (without colorbalance) failed, retrying minimal. ffmpeg stderr: {err[:800]}")

            # Try 3: eq only (no blend)
            fc3 = f"[0:v]eq=contrast={contrast}:brightness={brightness}:saturation={sat}[outv]"
            ok, err = _run_filter(fc3)
            if ok:
                return True
            if err:
                logger.warning(f"Enhance filter (eq only) failed. ffmpeg stderr: {err[:800]}")
            return False
        except Exception as e:
            logger.error(f"Enhance failed: {e}")
            return False
    
    def _get_best_font(self, text: str, custom_font: Optional[str] = None) -> str:
        """اختيار أفضل خط بناءً على النص واللغة والإعدادات العالمية"""

        def _is_arabicish(s: str) -> bool:
            return bool(
                re.search(
                    r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]",
                    s or "",
                )
            )

        def _is_thai(s: str) -> bool:
            return bool(re.search(r"[\u0E00-\u0E7F]", s or ""))

        def _iter_required_codepoints(s: str) -> set[int]:
            cps: set[int] = set()
            for ch in (s or ""):
                o = ord(ch)
                if ch.isspace():
                    continue
                if 0x0600 <= o <= 0x06FF:
                    cps.add(o)
                    continue
                if 0x0750 <= o <= 0x077F:
                    cps.add(o)
                    continue
                if 0x08A0 <= o <= 0x08FF:
                    cps.add(o)
                    continue
                if 0xFB50 <= o <= 0xFDFF:
                    cps.add(o)
                    continue
                if 0xFE70 <= o <= 0xFEFF:
                    cps.add(o)
                    continue
                if 0x0E00 <= o <= 0x0E7F:
                    cps.add(o)
                    continue
            return cps

        def _font_supports_required_chars(font_path: str, required: set[int]) -> bool:
            if not required:
                return True
            if not font_path or not os.path.exists(font_path):
                return False
            if not HAS_FONTTOOLS:
                return True
            try:
                tt = TTFont(font_path, recalcBBoxes=False, recalcTimestamp=False)
                cmap = tt.getBestCmap() or {}
                tt.close()
                if not cmap:
                    return False
                for cp in required:
                    if cp not in cmap:
                        return False
                return True
            except Exception:
                return False

        required_cps = _iter_required_codepoints(text)
        # 1. الأولوية القصوى للخط المخصص (إذا تم رفعه للقناة أو الجلسة)
        if custom_font:
            # Normalize path to handle both forward and backward slashes
            custom_font_norm = os.path.normpath(custom_font)
            if os.path.exists(custom_font_norm):
                cand = os.path.abspath(custom_font_norm)
                if _font_supports_required_chars(cand, required_cps):
                    return cand
                logger.warning(f"Custom font does not support required characters, skipping: {cand}")
            # Also try with current directory if relative path
            elif not os.path.isabs(custom_font_norm):
                custom_font_abs = os.path.abspath(custom_font_norm)
                if os.path.exists(custom_font_abs):
                    cand = custom_font_abs
                    if _font_supports_required_chars(cand, required_cps):
                        return cand
                    logger.warning(f"Custom font does not support required characters, skipping: {cand}")

        # 2. اكتشاف اللغة (يشمل Arabic Presentation Forms)
        is_ar = _is_arabicish(text)
        is_thai = _is_thai(text)
        cfg = load_config()

        # 3. الأولوية الثانية للخطوط العالمية المحددة في الإعدادات
        if is_ar and cfg.GLOBAL_FONT_AR:
            global_ar_norm = os.path.normpath(cfg.GLOBAL_FONT_AR)
            if os.path.exists(global_ar_norm):
                cand = os.path.abspath(global_ar_norm)
                if _font_supports_required_chars(cand, required_cps):
                    return cand
                logger.warning(f"GLOBAL_FONT_AR does not support required characters, skipping: {cand}")
            # Also try with current directory if relative path
            elif not os.path.isabs(global_ar_norm):
                global_ar_abs = os.path.abspath(global_ar_norm)
                if os.path.exists(global_ar_abs):
                    cand = global_ar_abs
                    if _font_supports_required_chars(cand, required_cps):
                        return cand
                    logger.warning(f"GLOBAL_FONT_AR does not support required characters, skipping: {cand}")
        elif not is_ar and cfg.GLOBAL_FONT_EN:
            global_en_norm = os.path.normpath(cfg.GLOBAL_FONT_EN)
            if os.path.exists(global_en_norm):
                return os.path.abspath(global_en_norm)
            # Also try with current directory if relative path
            elif not os.path.isabs(global_en_norm):
                global_en_abs = os.path.abspath(global_en_norm)
                if os.path.exists(global_en_abs):
                    return global_en_abs

        # 4. الأولوية الثالثة للخط المحلي الموثوق (تجنباً لمشاكل النظام)
        local_ar_font = os.path.join(".data", "fonts", "fallback_ar.ttf")
        if is_ar and os.path.exists(local_ar_font):
            cand = os.path.abspath(local_ar_font)
            if _font_supports_required_chars(cand, required_cps):
                return cand

        # 5. قائمة الخطوط الاحتياطية للنظام
        if is_ar:
            font_candidates = []

            # خطوط المستخدم (أولوية عالية)
            user_font_dir = os.path.join("font", "arabic")
            try:
                if os.path.isdir(user_font_dir):
                    for name in os.listdir(user_font_dir):
                        if not name.lower().endswith((".ttf", ".otf")):
                            continue
                        font_candidates.append(os.path.abspath(os.path.join(user_font_dir, name)))
            except Exception:
                pass

            # خطوط النظام
            font_candidates.extend(
                [
                    "C:/Windows/Fonts/tahoma.ttf",
                    "C:/Windows/Fonts/arial.ttf",
                    "C:/Windows/Fonts/arabtype.ttf",
                    "/system/fonts/NotoNaskhArabic-Regular.ttf", # Android
                    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", # Linux
                ]
            )
        else:
            font_candidates = []
            if is_thai:
                font_candidates.extend(
                    [
                        "C:/Windows/Fonts/leelawui.ttf",
                        "C:/Windows/Fonts/LeelawUI.ttf",
                        "C:/Windows/Fonts/tahoma.ttf",
                        "/system/fonts/NotoSansThai-Regular.ttf",
                        "/system/fonts/NotoSansThaiUI-Regular.ttf",
                        "/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",
                        "/usr/share/fonts/truetype/noto/NotoSansThaiUI-Regular.ttf",
                    ]
                )
            font_candidates.extend(
                [
                    "C:/Windows/Fonts/tahoma.ttf",       # يدعم التايلاندية والعربية بشكل جيد
                    "C:/Windows/Fonts/arial.ttf",
                    "C:/Windows/Fonts/segoeui.ttf",
                    "/system/fonts/Roboto-Regular.ttf", # Android
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", # Linux
                ]
            )

        fontfile = None
        for f in font_candidates:
            if not os.path.exists(f):
                continue
            cand = os.path.abspath(f)
            if _font_supports_required_chars(cand, required_cps):
                fontfile = cand
                break
        
        # 6. خط احتياطي نهائي شامل
        if not fontfile:
            global_fallback = os.path.join("data", "fonts", "overlay_fallback.ttf")
            if os.path.exists(global_fallback):
                cand = os.path.abspath(global_fallback)
                if _font_supports_required_chars(cand, required_cps):
                    fontfile = cand

        if not fontfile:
            if required_cps:
                raise RuntimeError(
                    "No suitable font found for overlay text. "
                    "Set GLOBAL_FONT_AR/EN in .env (or in tg_state.json global_fonts) "
                    "or upload a font that supports the required language."
                )
            fontfile = "C:/Windows/Fonts/arial.ttf"
            if not os.path.exists(fontfile):
                # Try generic linux path if Windows doesn't exist
                fontfile = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
            if not os.path.exists(fontfile):
                # Try Android path
                fontfile = "/system/fonts/Roboto-Regular.ttf"

        return os.path.abspath(fontfile)

    def _add_top_overlay_text(self, input_path: str, output_path: str, text: str, custom_font: Optional[str] = None, top_text_size: int = 64, top_text_y: int = 150, is_custom: bool = False):
        """إضافة نص في أعلى الفيديو طوال الوقت"""
        is_ar = bool(re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", text or ""))
        use_text_shaping = (os.getenv("FFMPEG_DRAWTEXT_TEXT_SHAPING", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
        
        if is_ar and use_text_shaping:
            display_text = text
        elif is_ar and HAS_ARABIC_SUPPORT:
            try:
                reshaped_text = arabic_reshaper.reshape(text)
                display_text = get_display(reshaped_text)
            except Exception as e:
                logger.error(f"Error reshaping Arabic text: {e}")
                display_text = text
        else:
            display_text = text

        # استخدام UTF-8 (بدون BOM) لضمان أقصى توافق مع FFmpeg 
        text_file = self.temp_dir / f"text_{uuid.uuid4().hex}.txt"
        try:
            # كتابة النص بتشفير UTF-8 عادي
            with open(text_file, "w", encoding="utf-8") as f:
                f.write(display_text)
            
            # وظيفة مساعدة لهروب المسارات في ويندوز لـ FFmpeg filters
            def ffmpeg_escape_path(path_str):
                p = str(path_str).replace("\\", "/")
                p = p.replace(":", "\\:")
                return p
            
            text_file_esc = ffmpeg_escape_path(text_file)
            fontfile = self._get_best_font(display_text, custom_font)
            font_esc = ffmpeg_escape_path(fontfile)

            logger.info(f"FFmpeg Path Escaped - Text: {text_file_esc}, Font: {font_esc}")

            # الألوان والنمط (بناء على طلب المستخدم: نص أبيض مع خلفية سوداء شبه شفافة)
            if is_custom:
                # في النمط المخصص، نكبر الخط قليلاً ليكون أوضح
                top_text_size = int(top_text_size * 1.2) if top_text_size == 64 else top_text_size
            
            font_color = "white"
            box_opt = "box=1:boxcolor=black@0.6:boxborderw=15:"

            # بناء الفلتر
            # نستخدم : بدلاً من ' ' للمسارات لأننا قمنا بالهروب يدوياً
            shaping_opt = "text_shaping=1:" if (is_ar and use_text_shaping) else ""

            drawtext_filter = (
                f"drawtext="
                f"textfile='{text_file_esc}':"
                f"fontfile='{font_esc}':"
                f"{shaping_opt}"
                f"{box_opt}"
                f"fontsize={top_text_size}:"
                f"fontcolor={font_color}:"
                f"x=(w-text_w)/2:"
                f"y={top_text_y}:"
                f"fix_bounds=1" # ضمان عدم خروج النص عن الشاشة
            )
            
            logger.debug(f"Applying VF filter: {drawtext_filter}")

            # 🔧 استخدام إعدادات وسيط محسّنة (تحترم RENDER / LOW_RESOURCE_MODE)
            ff_threads, base_preset, base_crf = self._shorts_x264_settings()
            preset = str(os.getenv("SHORTS_INTERMEDIATE_PRESET", base_preset) or base_preset).strip() or base_preset
            crf = int(os.getenv("SHORTS_INTERMEDIATE_CRF", str(base_crf)) or str(base_crf))
            level = (os.getenv("SHORTS_H264_LEVEL", "5.1") or "5.1").strip() or "5.1"
            fps = self._get_video_fps(input_path)
            if not fps or fps <= 0:
                fps = 30.0
            
            base_cmd = [
                ffmpeg_bin(),
                "-y",
                "-i", input_path,
                "-vf", "",
                "-c:v", "libx264",
                "-preset", preset,
                "-crf", str(crf),
                "-profile:v", "high",
                "-level", level,
                "-pix_fmt", "yuv420p",
                "-vsync", "cfr",
                "-r", f"{fps:.6f}",
                "-threads", str(ff_threads),
                "-c:a", "copy",
                str(output_path)
            ]

            def _run_filter(vf: str) -> subprocess.CompletedProcess:
                cmd = list(base_cmd)
                cmd[5] = vf
                return subprocess.run(cmd, capture_output=True, text=True, timeout=300)

            attempts = []

            attempts.append(drawtext_filter)
            attempts.append(drawtext_filter.replace(":fix_bounds=1", ""))

            no_font_filter = drawtext_filter.replace(f"fontfile='{font_esc}':", "")
            attempts.append(no_font_filter)
            attempts.append(no_font_filter.replace(":fix_bounds=1", ""))

            direct_text = display_text
            direct_text = direct_text.replace("\\\\", "\\\\\\\\").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
            direct_text = direct_text.replace("'", "\\\\'").replace(":", "\\:").replace(",", "\\,")
            shaping_direct = "text_shaping=1:" if (is_ar and use_text_shaping) else ""
            attempts.append(
                f"drawtext=text='{direct_text}':fontfile='{font_esc}':{shaping_direct}{box_opt}fontsize={top_text_size}:fontcolor={font_color}:x=(w-text_w)/2:y={top_text_y}"
            )
            attempts.append(
                f"drawtext=text='{direct_text}':{shaping_direct}{box_opt}fontsize={top_text_size}:fontcolor={font_color}:x=(w-text_w)/2:y={top_text_y}"
            )

            last_stderr = ""
            for vf in attempts:
                logger.debug(f"Applying VF filter: {vf}")
                result = _run_filter(vf)
                if result.returncode == 0:
                    logger.info("✅ Top overlay text added to video")
                    return
                last_stderr = (result.stderr or "")[-2500:]
                logger.error(f"FFmpeg failed to add top overlay text: {last_stderr}")

            raise RuntimeError(f"Failed to add top overlay text via FFmpeg drawtext. Last error: {last_stderr}")
        finally:
            # حذف ملف النص المؤقت
            if text_file.exists():
                try:
                    text_file.unlink()
                except:
                    pass

    def add_custom_overlay_text(
        self, input_path: str, output_path: str, text: str,
        timing: str = "full",
        duration: float = 2.0,
        screen_position: str = "top",
        intro_animation: Optional[Dict[str, Any]] = None,
        outro_animation: Optional[Dict[str, Any]] = None,
        custom_font: Optional[str] = None,
        font_size: int = 56,
    ) -> None:
        """إضافة نص مخصص على الفيديو بلون أبيض وحدود سوداء سميكة

        Args:
            timing: "start" | "end" | "full"
            duration: مدة الظهور بالثواني (فقط لـ start/end)
            screen_position: "top" | "center" | "bottom"
        """
        def _normalize_animation(raw_animation: Optional[Dict[str, Any]]) -> Dict[str, Any]:
            if not isinstance(raw_animation, dict):
                return {"enabled": False, "type": "none", "duration": 0.0}
            animation_type = str(raw_animation.get("type") or "none").strip().lower()
            if animation_type not in {"fade", "blur"}:
                return {"enabled": False, "type": "none", "duration": 0.0}
            try:
                anim_duration = max(0.0, float(raw_animation.get("duration", 0.0) or 0.0))
            except Exception:
                anim_duration = 0.0
            if not raw_animation.get("enabled") or anim_duration <= 0:
                return {"enabled": False, "type": "none", "duration": 0.0}
            return {"enabled": True, "type": animation_type, "duration": min(anim_duration, 2.0)}

        intro_anim = _normalize_animation(intro_animation)
        outro_anim = _normalize_animation(outro_animation)
        if intro_anim.get("enabled") or outro_anim.get("enabled"):
            return self._add_custom_overlay_text_via_image_overlay(
                input_path=input_path,
                output_path=output_path,
                text=text,
                timing=timing,
                duration=duration,
                screen_position=screen_position,
                intro_animation=intro_anim,
                outro_animation=outro_anim,
                custom_font=custom_font,
                font_size=font_size,
            )

        is_ar = bool(re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", text or ""))
        use_text_shaping = (os.getenv("FFMPEG_DRAWTEXT_TEXT_SHAPING", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}

        if is_ar and use_text_shaping:
            display_text = text
        elif is_ar and HAS_ARABIC_SUPPORT:
            try:
                reshaped_text = arabic_reshaper.reshape(text)
                display_text = get_display(reshaped_text)
            except Exception as e:
                logger.error(f"Error reshaping Arabic text for custom overlay: {e}")
                display_text = text
        else:
            display_text = text

        text_file = self.temp_dir / f"ctext_{uuid.uuid4().hex}.txt"
        try:
            with open(text_file, "w", encoding="utf-8") as f:
                f.write(display_text)

            text_file_esc = str(text_file).replace("\\", "/").replace(":", "\\:")
            fontfile = self._get_best_font(display_text, custom_font)
            font_esc = str(fontfile).replace("\\", "/").replace(":", "\\:")

            # — الموضع —
            pos_key = (screen_position or "top").strip().lower()
            if pos_key in {"bottom", "bottom_center", "bottom-center"}:
                y_expr = "h-text_h-80"
            elif pos_key in {"center", "middle"}:
                y_expr = "(h-text_h)/2"
            else:
                y_expr = "80"

            # — التوقيت (enable) —
            enable_opt = ""
            if timing in ("start", "end"):
                # الحصول على مدة الفيديو
                total_dur = None
                try:
                    prd = subprocess.run(
                        [ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
                         "-of", "default=noprint_wrappers=1:nokey=1", input_path],
                        capture_output=True, text=True, timeout=5,
                    )
                    if prd.stdout.strip():
                        total_dur = float(prd.stdout.strip())
                except Exception:
                    total_dur = None

                if total_dur and total_dur > 0:
                    if timing == "start":
                        end_t = min(duration, total_dur)
                        enable_opt = f":enable='between(t,0,{end_t:.2f})'"
                    else:  # end
                        start_t = max(0, total_dur - duration)
                        enable_opt = f":enable='between(t,{start_t:.2f},{total_dur:.2f})'"
                # إذا لم نتمكن من الحصول على المدة، نعرض النص طول الفيديو

            shaping_opt = "text_shaping=1:" if (is_ar and use_text_shaping) else ""

            # النمط: أبيض مع حدود سوداء سميكة (بدون مربع خلفية)
            drawtext_filter = (
                f"drawtext="
                f"textfile='{text_file_esc}':"
                f"fontfile='{font_esc}':"
                f"{shaping_opt}"
                f"fontsize={font_size}:"
                f"fontcolor=white:"
                f"borderw=4:"
                f"bordercolor=black:"
                f"x=(w-text_w)/2:"
                f"y={y_expr}:"
                f"fix_bounds=1"
                f"{enable_opt}"
            )

            logger.debug(f"Custom overlay VF filter: {drawtext_filter}")

            ff_threads, base_preset, base_crf = self._shorts_x264_settings()
            preset = str(os.getenv("SHORTS_INTERMEDIATE_PRESET", base_preset) or base_preset).strip() or base_preset
            crf = int(os.getenv("SHORTS_INTERMEDIATE_CRF", str(base_crf)) or str(base_crf))
            level = (os.getenv("SHORTS_H264_LEVEL", "5.1") or "5.1").strip() or "5.1"
            fps = self._get_video_fps(input_path)
            if not fps or fps <= 0:
                fps = 30.0

            base_cmd = [
                ffmpeg_bin(),
                "-y",
                "-i", input_path,
                "-vf", "",
                "-c:v", "libx264",
                "-preset", preset,
                "-crf", str(crf),
                "-profile:v", "high",
                "-level", level,
                "-pix_fmt", "yuv420p",
                "-vsync", "cfr",
                "-r", f"{fps:.6f}",
                "-threads", str(ff_threads),
                "-c:a", "copy",
                str(output_path)
            ]

            def _run_filter(vf: str) -> subprocess.CompletedProcess:
                cmd = list(base_cmd)
                cmd[5] = vf
                return subprocess.run(cmd, capture_output=True, text=True, timeout=300)

            attempts = [drawtext_filter]
            attempts.append(drawtext_filter.replace(":fix_bounds=1", ""))

            no_font_filter = drawtext_filter.replace(f"fontfile='{font_esc}':", "")
            attempts.append(no_font_filter)
            attempts.append(no_font_filter.replace(":fix_bounds=1", ""))

            last_stderr = ""
            for vf in attempts:
                logger.debug(f"Custom overlay attempt: {vf}")
                result = _run_filter(vf)
                if result.returncode == 0:
                    logger.info("✅ Custom overlay text added to video")
                    return
                last_stderr = (result.stderr or "")[-2500:]
                logger.error(f"FFmpeg custom overlay failed: {last_stderr}")

            raise RuntimeError(f"Failed to add custom overlay text via FFmpeg. Last error: {last_stderr}")
        finally:
            if text_file.exists():
                try:
                    text_file.unlink()
                except:
                    pass

    def _add_custom_overlay_text_via_image_overlay(
        self,
        input_path: str,
        output_path: str,
        text: str,
        timing: str = "full",
        duration: float = 2.0,
        screen_position: str = "top",
        intro_animation: Optional[Dict[str, Any]] = None,
        outro_animation: Optional[Dict[str, Any]] = None,
        custom_font: Optional[str] = None,
        font_size: int = 56,
    ) -> None:
        from PIL import Image, ImageDraw, ImageFont

        intro_animation = intro_animation or {"enabled": False, "type": "none", "duration": 0.0}
        outro_animation = outro_animation or {"enabled": False, "type": "none", "duration": 0.0}
        video_duration = self._get_video_duration(input_path)
        if video_duration <= 0:
            raise RuntimeError("Invalid video duration for overlay text animation")

        visible_start = 0.0
        visible_end = video_duration
        try:
            window_duration = max(0.5, float(duration or 0.0))
        except Exception:
            window_duration = 2.0

        timing_key = (timing or "full").strip().lower()
        if timing_key == "start":
            visible_end = min(video_duration, window_duration)
        elif timing_key == "end":
            visible_start = max(0.0, video_duration - window_duration)

        visible_window = max(0.05, visible_end - visible_start)
        intro_dur = min(max(0.0, float(intro_animation.get("duration", 0.0) or 0.0)), visible_window)
        outro_dur = min(max(0.0, float(outro_animation.get("duration", 0.0) or 0.0)), visible_window)
        total_anim = intro_dur + outro_dur
        if total_anim > visible_window and total_anim > 0:
            scale = visible_window / total_anim
            intro_dur *= scale
            outro_dur *= scale

        is_ar = bool(re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", text or ""))

        def _shape_line(value: str) -> str:
            if is_ar and HAS_ARABIC_SUPPORT:
                try:
                    return get_display(arabic_reshaper.reshape(value))
                except Exception:
                    return value
            return value

        vw, vh = self._get_video_dimensions(input_path)
        if not vw or not vh:
            vw, vh = 1080, 1920

        fontfile = self._get_best_font(text, custom_font)
        try:
            font = ImageFont.truetype(str(fontfile), font_size)
        except Exception:
            font = ImageFont.load_default()

        max_text_width = max(280, int(vw * 0.82))
        padding_x = 40
        padding_y = 24
        stroke_w = 4
        line_spacing = 10
        measure = ImageDraw.Draw(Image.new("RGBA", (8, 8), (0, 0, 0, 0)))

        def _wrap_lines(src_text: str, max_w: int) -> list:
            raw_lines = (src_text or "").splitlines() or [src_text or ""]
            wrapped = []
            for part in raw_lines:
                words = part.split()
                if not words:
                    wrapped.append("")
                    continue
                current = ""
                for word in words:
                    probe = f"{current} {word}".strip()
                    if measure.textlength(_shape_line(probe), font=font) <= max_w:
                        current = probe
                    else:
                        if current:
                            wrapped.append(current)
                        current = word
                if current:
                    wrapped.append(current)
            return wrapped[:4] or [src_text.strip() or text]

        logical_lines = _wrap_lines(text, max(120, max_text_width - padding_x * 2))
        display_lines = [_shape_line(line) for line in logical_lines]
        text_width = 0
        text_height = 0
        line_heights = []
        for line in display_lines:
            bbox = measure.textbbox((0, 0), line or " ", font=font, stroke_width=stroke_w)
            line_w = max(1, bbox[2] - bbox[0]) if bbox else max(1, int(measure.textlength(line or " ", font=font)))
            line_h = max(font_size, bbox[3] - bbox[1]) if bbox else font_size
            text_width = max(text_width, line_w)
            text_height += line_h
            line_heights.append(line_h)
        if len(display_lines) > 1:
            text_height += line_spacing * (len(display_lines) - 1)

        overlay_w = min(vw - 80, max(320, int(text_width + padding_x * 2)))
        overlay_h = max(120, int(text_height + padding_y * 2))
        img = Image.new("RGBA", (overlay_w, overlay_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        y_cursor = (overlay_h - text_height) // 2
        for idx, line in enumerate(display_lines):
            bbox = draw.textbbox((0, 0), line or " ", font=font, stroke_width=stroke_w)
            line_w = max(1, bbox[2] - bbox[0]) if bbox else max(1, int(draw.textlength(line or " ", font=font)))
            x = max(padding_x, int((overlay_w - line_w) / 2))
            draw.text(
                (x, y_cursor),
                line,
                font=font,
                fill=(255, 255, 255, 255),
                stroke_width=stroke_w,
                stroke_fill=(0, 0, 0, 255),
            )
            y_cursor += line_heights[idx] + line_spacing

        pos_key = (screen_position or "top").strip().lower()
        y_expr = "80"
        if pos_key in {"bottom", "bottom_center", "bottom-center"}:
            y_expr = "H-h-80"
        elif pos_key in {"center", "middle"}:
            y_expr = "(H-h)/2"

        overlay_path = self.temp_dir / f"overlay_text_{uuid.uuid4().hex}.png"
        img.save(overlay_path)

        fade_filters = []
        if intro_animation.get("enabled") and intro_animation.get("type") in {"fade", "blur"} and intro_dur > 0:
            fade_filters.append(f"fade=t=in:st={visible_start:.2f}:d={intro_dur:.2f}:alpha=1")
        if outro_animation.get("enabled") and outro_animation.get("type") in {"fade", "blur"} and outro_dur > 0:
            fade_start = max(visible_start, visible_end - outro_dur)
            fade_filters.append(f"fade=t=out:st={fade_start:.2f}:d={outro_dur:.2f}:alpha=1")

        blur_terms = []
        max_blur = 14.0
        if intro_animation.get("enabled") and intro_animation.get("type") == "blur" and intro_dur > 0:
            blur_terms.append(f"if(between(t,{visible_start:.2f},{visible_start + intro_dur:.2f}),{max_blur:.2f}*(1-((t-{visible_start:.2f})/{intro_dur:.2f})),0)")
        if outro_animation.get("enabled") and outro_animation.get("type") == "blur" and outro_dur > 0:
            outro_start = max(visible_start, visible_end - outro_dur)
            blur_terms.append(f"if(between(t,{outro_start:.2f},{visible_end:.2f}),{max_blur:.2f}*(((t-{outro_start:.2f})/{outro_dur:.2f})),0)")
        blur_expr = "+".join(blur_terms) if blur_terms else "0"

        overlay_chain = ["[1:v]format=rgba", "setpts=PTS-STARTPTS"]
        overlay_chain.extend(fade_filters)
        if blur_terms:
            overlay_chain.append(
                f"boxblur=luma_radius='{blur_expr}':luma_power=1:chroma_radius='{blur_expr}':chroma_power=1"
            )

        filter_complex = ",".join(overlay_chain) + f"[ovl];[0:v][ovl]overlay=x=(W-w)/2:y={y_expr}:shortest=1:enable='between(t,{visible_start:.2f},{visible_end:.2f})'[vout]"

        ff_threads, _, _ = self._shorts_x264_settings()
        preset = str(os.getenv("SHORTS_INTERMEDIATE_PRESET", "veryfast") or "veryfast").strip() or "veryfast"
        crf = int(os.getenv("SHORTS_INTERMEDIATE_CRF", "20") or "20")
        level = (os.getenv("SHORTS_H264_LEVEL", "5.1") or "5.1").strip() or "5.1"
        fps = self._get_video_fps(input_path)
        if not fps or fps <= 0:
            fps = 30.0

        cmd = [
            ffmpeg_bin(),
            "-y",
            "-i", input_path,
            "-loop", "1",
            "-i", str(overlay_path),
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", str(crf),
            "-profile:v", "high",
            "-level", level,
            "-pix_fmt", "yuv420p",
            "-vsync", "cfr",
            "-r", f"{fps:.6f}",
            "-threads", str(ff_threads),
            "-c:a", "copy",
            str(output_path),
        ]

        try:
            logger.debug("Custom animated overlay filter: %s", filter_complex)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                raise RuntimeError((result.stderr or "")[-2500:] or "Animated overlay failed")
            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                raise RuntimeError("Animated overlay output file missing or empty")
            logger.info("✅ Custom animated overlay text added to video")
        finally:
            try:
                if overlay_path.exists():
                    overlay_path.unlink()
            except Exception:
                pass

    def add_watermark_text(self, input_path: str, output_path: str, text: str, seed: Optional[str] = None, custom_font: Optional[str] = None) -> None:
        """إضافة Watermark شفاف (اسم القناة) على فيديو الشورتس"""
        is_ar = bool(re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", text or ""))
        use_text_shaping = (os.getenv("FFMPEG_DRAWTEXT_TEXT_SHAPING", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
        
        if is_ar and use_text_shaping:
            display_text = text
        elif is_ar and HAS_ARABIC_SUPPORT:
            try:
                reshaped_text = arabic_reshaper.reshape(text)
                display_text = get_display(reshaped_text)
            except Exception:
                display_text = text
        else:
            display_text = text

        text_file = self.temp_dir / f"wm_{uuid.uuid4().hex}.txt"
        try:
            with open(text_file, "w", encoding="utf-8") as f:
                f.write(display_text)

            def ffmpeg_escape_path(path_str):
                p = str(path_str).replace("\\", "/")
                p = p.replace(":", "\\:")
                return p

            text_file_esc = ffmpeg_escape_path(text_file)
            fontfile = self._get_best_font(display_text, custom_font)
            font_esc = ffmpeg_escape_path(fontfile)

            positions = [
                "top_center",
                "bottom_center",
                "bottom_left",
                "top_left",
            ]
            seed_s = (seed or "") + "::" + (display_text or "")
            idx = int(hashlib.md5(seed_s.encode("utf-8", errors="ignore")).hexdigest()[:8], 16) % len(positions)
            pos = positions[idx]

            y_expr = "12" if "top" in pos else "h-text_h-28"
            x_expr = "12" if "left" in pos else "(w-text_w)/2"

            try:
                alpha = float(os.getenv("SHORTS_WATERMARK_ALPHA", "0.22") or "0.22")
            except Exception:
                alpha = 0.22
            alpha = max(0.05, min(alpha, 0.9))
            # تنويع لون اسم القناة لكل فيديو مع الحفاظ على نفس الشفافية
            wm_colors = [
                "0xFFFFFF",  # white
                "0x87CEFA",  # light sky blue
                "0xA0522D",  # sienna (brown)
                "0xFF7F7F",  # light red
            ]
            try:
                cidx = int(hashlib.md5((seed_s + "::color").encode("utf-8", errors="ignore")).hexdigest()[:8], 16) % len(wm_colors)
            except Exception:
                cidx = 0
            wm_color = wm_colors[cidx]

            shaping_opt = "text_shaping=1:" if (is_ar and use_text_shaping) else ""

            drawtext_filter = (
                f"drawtext="
                f"textfile='{text_file_esc}':"
                f"fontfile='{font_esc}':"
                f"{shaping_opt}"
                f"fontsize=54:"
                f"fontcolor={wm_color}@{alpha}:"
                f"borderw=2:"
                f"bordercolor=black@{min(0.45, alpha + 0.18)}:"
                f"x={x_expr}:"
                f"y={y_expr}:"
                f"fix_bounds=1"
            )

            # 🔧 استخدام إعدادات وسيط محسّنة
            ff_threads, base_preset, base_crf = self._shorts_x264_settings()
            if os.getenv("LOW_RESOURCE_MODE") == "1":
                base_preset = "ultrafast"
                base_crf = 26
            preset = str(os.getenv("SHORTS_INTERMEDIATE_PRESET", base_preset) or base_preset).strip() or base_preset
            crf = int(os.getenv("SHORTS_INTERMEDIATE_CRF", str(base_crf)) or str(base_crf))
            level = (os.getenv("SHORTS_H264_LEVEL", "5.1") or "5.1").strip() or "5.1"
            base_cmd = [
                ffmpeg_bin(),
                "-y",
                "-i", input_path,
                "-vf", "",
                "-c:v", "libx264",
                "-preset", preset,
                "-crf", str(crf),
                "-profile:v", "high",
                "-level", level,
                "-pix_fmt", "yuv420p",
                "-threads", str(ff_threads),
                "-c:a", "copy",
                str(output_path),
            ]

            def _run_filter(vf: str) -> subprocess.CompletedProcess:
                cmd = list(base_cmd)
                cmd[5] = vf
                return subprocess.run(cmd, capture_output=True, text=True, timeout=300)

            attempts = [
                drawtext_filter,
                drawtext_filter.replace(":fix_bounds=1", ""),
                drawtext_filter.replace(f"fontfile='{font_esc}':", ""),
                drawtext_filter.replace(f"fontfile='{font_esc}':", "").replace(":fix_bounds=1", ""),
            ]

            last_stderr = ""
            for vf in attempts:
                result = _run_filter(vf)
                if result.returncode == 0:
                    return
                last_stderr = (result.stderr or "")[-2500:]

            raise RuntimeError(f"Failed to add watermark via FFmpeg drawtext. Last error: {last_stderr}")
        finally:
            if text_file.exists():
                try:
                    text_file.unlink()
                except Exception:
                    pass

    def add_simple_intro_outro_effects(self, input_path: str, output_path: str, seed: Optional[str] = None, apply_intro: bool = True, apply_outro: bool = True) -> None:
        """إضافة تأثيرات ظهور/اختفاء بسيطة للشورتس (بدون انزلاق/اتجاهات)"""
        try:
            effects_enabled = str(os.getenv("SHORTS_EFFECTS_ENABLED", "1") or "1").strip().lower() not in {"0", "false", "no", "off"}
        except Exception:
            effects_enabled = True
        if not effects_enabled:
            cmd = [
                ffmpeg_bin(), "-y",
                "-i", input_path,
                "-c", "copy",
                "-map_metadata", "-1",
                "-movflags", "+faststart",
                "-use_editlist", "0",
                str(output_path),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if res.returncode != 0:
                raise RuntimeError((res.stderr or "")[-2500:])
            return

        duration_s = None
        try:
            prd = subprocess.run(
                [ffprobe_bin(), "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", input_path],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if prd.stdout.strip():
                duration_s = float(prd.stdout.strip())
        except Exception:
            duration_s = None

        if not duration_s or duration_s <= 0:
            duration_s = 30.0

        try:
            intro_d = float(os.getenv("SHORTS_EFFECTS_INTRO_SECONDS", "0.25") or "0.25")
        except Exception:
            intro_d = 0.25
        try:
            outro_d = float(os.getenv("SHORTS_EFFECTS_OUTRO_SECONDS", "0.25") or "0.25")
        except Exception:
            outro_d = 0.25

        intro_d = max(0.12, min(intro_d, 0.8))
        outro_d = max(0.12, min(outro_d, 0.8))
        outro_start = max(0.0, float(duration_s) - float(outro_d))

        intro_types = ["fade", "noise", "blur", "darken", "desat"]
        outro_types = ["fade", "noise", "blur", "darken", "desat"]
        base_seed = seed or input_path or ""
        
        # 🔧 إضافة مكون عشوائي إذا كان الـ seed فارغاً أو يساوي مسار الملف فقط
        # هذا يضمن تأثيرات مختلفة لكل فيديو حتى لو كان نفس الملف الأساسي
        import random
        if not seed or seed == input_path:
            base_seed = f"{base_seed}::{uuid.uuid4().hex}::{random.random()}"
            logger.info(f"[FX] Using randomized seed for unique effects: {base_seed[:50]}...")
        else:
            logger.info(f"[FX] Using provided seed: {base_seed[:50]}...")
        
        intro_type = None
        outro_type = None
        if apply_intro:
            intro_idx = int(hashlib.md5((base_seed + "::intro").encode("utf-8", errors="ignore")).hexdigest()[:8], 16) % len(intro_types)
            intro_type = intro_types[intro_idx]
        if apply_outro:
            outro_idx = int(hashlib.md5((base_seed + "::outro").encode("utf-8", errors="ignore")).hexdigest()[:8], 16) % len(outro_types)
            outro_type = outro_types[outro_idx]

        def _intro_filter(kind: str) -> str:
            if kind == "fade":
                return f"fade=t=in:st=0:d={intro_d}"
            if kind == "noise":
                return f"noise=alls=20:allf=t+u:enable='between(t,0,{intro_d})'"
            if kind == "blur":
                return f"boxblur=10:1:enable='between(t,0,{intro_d})'"
            if kind == "darken":
                return f"eq=brightness=-0.05:saturation=0.95:enable='between(t,0,{intro_d})'"
            if kind == "desat":
                return f"fade=t=in:st=0:d={intro_d},eq=saturation=0.75:enable='between(t,0,{intro_d})'"
            return f"fade=t=in:st=0:d={intro_d},eq=saturation=0.75:enable='between(t,0,{intro_d})'"

        def _outro_filter(kind: str) -> str:
            if kind == "fade":
                return f"fade=t=out:st={outro_start}:d={outro_d}"
            if kind == "noise":
                return f"noise=alls=22:allf=t+u:enable='between(t,{outro_start},{duration_s})'"
            if kind == "blur":
                return f"boxblur=10:1:enable='between(t,{outro_start},{duration_s})'"
            if kind == "darken":
                return f"eq=brightness=-0.06:saturation=0.9:enable='between(t,{outro_start},{duration_s})'"
            if kind == "desat":
                return f"fade=t=out:st={outro_start}:d={outro_d},eq=saturation=0.75:enable='between(t,{outro_start},{duration_s})'"
            return f"fade=t=out:st={outro_start}:d={outro_d},eq=saturation=0.75:enable='between(t,{outro_start},{duration_s})'"

        vf_parts = []
        vf_parts.append("setpts=PTS-STARTPTS")
        if apply_intro:
            vf_parts.append(f"fade=t=in:st=0:d={intro_d}")
            if intro_type and intro_type != "fade":
                vf_parts.append(_intro_filter(intro_type))
        if apply_outro:
            vf_parts.append(f"fade=t=out:st={outro_start}:d={outro_d}")
            if outro_type and outro_type != "fade":
                vf_parts.append(_outro_filter(outro_type))
        vf_parts.append("format=yuv420p")
        vf = ",".join(vf_parts) if vf_parts else "null"

        # 🔧 استخدام إعدادات وسيط محسّنة
        ff_threads, base_preset, base_crf = self._shorts_x264_settings()
        if os.getenv("LOW_RESOURCE_MODE") == "1":
            base_preset = "ultrafast"
            base_crf = 26
        preset = str(os.getenv("SHORTS_INTERMEDIATE_PRESET", base_preset) or base_preset).strip() or base_preset
        crf = int(os.getenv("SHORTS_INTERMEDIATE_CRF", str(base_crf)) or str(base_crf))
        level = (os.getenv("SHORTS_H264_LEVEL", "5.1") or "5.1").strip() or "5.1"
        has_audio = self._has_audio(input_path)
        fps = self._get_video_fps(input_path)
        if not fps or fps <= 0:
            fps = 30.0
        gop = int(round(fps))
        if gop < 1:
            gop = 30
        cmd = [
            ffmpeg_bin(),
            "-y",
            "-i", input_path,
            "-vf", vf,
            "-vsync", "cfr",
            "-map", "0:v",
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", str(crf),
            "-profile:v", "high",
            "-level", level,
            "-pix_fmt", "yuv420p",
            "-r", f"{fps:.6f}",
            "-g", str(gop),
            "-keyint_min", str(max(1, gop // 2)),
            "-sc_threshold", "0",
            "-force_key_frames", "expr:gte(t,0)",
            "-threads", str(ff_threads),
        ]
        if has_audio:
            cmd += ["-map", "0:a?", "-c:a", "aac", "-b:a", "384k", "-ar", "48000"]
        else:
            cmd += ["-an"]
        cmd += ["-movflags", "+faststart", str(output_path)]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if res.returncode != 0:
            raise RuntimeError((res.stderr or "")[-2500:])
    
    def _get_video_duration(self, video_path: str) -> float:
        """الحصول على مدة الفيديو"""
        if not video_path or (not os.path.exists(video_path)) or os.path.getsize(video_path) <= 0:
            raise RuntimeError(f"Failed to get video duration: file missing or empty: {video_path}")
        cmd = [
            ffprobe_bin(),
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            video_path
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        if result.returncode != 0:
            raise RuntimeError(f"Failed to get video duration: {result.stderr}")
        
        return float(result.stdout.strip())

    def _validate_video_file(self, video_path: str) -> None:
        if not video_path or not os.path.exists(video_path) or os.path.getsize(video_path) <= 0:
            raise RuntimeError(f"Video file empty or missing: {video_path}")
        _ = self._get_video_duration(video_path)
    
    def _get_video_dimensions(self, video_path: str) -> Tuple[int, int]:
        """الحصول على أبعاد الفيديو"""
        cmd = [
            ffprobe_bin(),
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=s=x:p=0",
            video_path
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        if result.returncode != 0:
            raise RuntimeError(f"Failed to get video dimensions: {result.stderr}")
        
        width, height = map(int, result.stdout.strip().split('x'))
        return width, height

    def _get_video_fps(self, video_path: str) -> float:
        try:
            cmd = [
                ffprobe_bin(),
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=avg_frame_rate,r_frame_rate",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                return 30.0
            lines = [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
            def _parse_frac(s: str) -> Optional[float]:
                try:
                    if "/" in s:
                        a, b = s.split("/", 1)
                        a = float(a)
                        b = float(b)
                        if b == 0:
                            return None
                        v = a / b
                        return v if v > 0 else None
                    v = float(s)
                    return v if v > 0 else None
                except Exception:
                    return None
            for s in lines:
                v = _parse_frac(s)
                if v and v > 0:
                    return float(v)
            return 30.0
        except Exception:
            return 30.0

    def _has_audio(self, video_path: str) -> bool:
        """التحقق من وجود مسار صوتي في الفيديو"""
        try:
            cmd = [
                ffprobe_bin(),
                "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=index",
                "-of", "csv=p=0",
                video_path,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return result.returncode == 0 and bool(result.stdout.strip())
        except Exception:
            return False

    def _shorts_x264_settings(self) -> Tuple[int, str, int]:
        def _env_int(name: str, default: int) -> int:
            try:
                raw = (os.getenv(name, str(default)) or str(default)).strip()
                return int(float(raw))
            except Exception:
                return default

        def _env_str(name: str, default: str) -> str:
            val = (os.getenv(name, default) or default).strip()
            return val if val else default

        def _env_bool(name: str, default: bool = False) -> bool:
            val = os.getenv(name)
            if val is None:
                return default
            return val.strip().lower() in {"1", "true", "yes", "on"}

        ff_threads = max(1, _env_int("FFMPEG_THREADS", 0))  # 0 = auto

        # Performance optimization: Default to veryfast for speed, CRF 23 for good enough quality
        # This is much faster than the previous 'slow'/'medium' preset
        preset = _env_str("SHORTS_X264_PRESET", _env_str("FFMPEG_X264_PRESET", "veryfast"))
        crf_default = _env_int("FFMPEG_X264_CRF", 23)
        crf = max(18, min(28, _env_int("SHORTS_X264_CRF", crf_default)))

        if _env_bool("LOW_RESOURCE_MODE", False) or _env_bool("FFMPEG_LOW_CPU", False) or _env_bool("RENDER", False):
            preset = "ultrafast"
            crf_target = 28 if (_env_bool("RENDER", False) or _env_bool("LOW_RESOURCE_MODE", False)) else 26
            crf = _env_int("SHORTS_X264_CRF", crf_target)
            ff_threads = 1

        return ff_threads, preset, crf
    
    def _trim_video(self, input_path: str, output_path: str, start: float, end: float, force_encode: bool = False):
        """قص الفيديو من البداية والنهاية"""
        duration = self._get_video_duration(input_path)
        new_duration = duration - start - end

        def _env_int(name: str, default: int) -> int:
            try:
                raw = (os.getenv(name, str(default)) or str(default)).strip()
                return int(float(raw))
            except Exception:
                return default

        def _env_str(name: str, default: str) -> str:
            val = (os.getenv(name, default) or default).strip()
            return val if val else default

        def _env_bool(name: str, default: bool = False) -> bool:
            val = os.getenv(name)
            if val is None:
                return default
            return val.strip().lower() in {"1", "true", "yes", "on"}

        # 🔧 استخدام _shorts_x264_settings() لاحترام RENDER / LOW_RESOURCE_MODE
        ff_threads, x264_preset, x264_crf = self._shorts_x264_settings()
        trim_mode = _env_str("FFMPEG_TRIM_MODE", "encode").lower()
        if _env_bool("FFMPEG_LOW_CPU", False) and trim_mode == "encode":
            trim_mode = "copy"

        if force_encode and trim_mode == "copy":
            trim_mode = "encode"

        if trim_mode == "copy":
            cmd = [
                ffmpeg_bin(),
                "-y",
                "-ss", str(start),
                "-i", input_path,
                "-t", str(new_duration),
                "-map", "0",
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                "-fflags", "+genpts",
                str(output_path),
            ]
        else:
            fps = self._get_video_fps(input_path)
            if not fps or fps <= 0:
                fps = 30.0
            level = (os.getenv("SHORTS_H264_LEVEL", "5.1") or "5.1").strip() or "5.1"
            cmd = [
                ffmpeg_bin(),
                "-y",
                "-ss", str(start),  # البداية
                "-i", input_path,
                "-t", str(new_duration),  # المدة
                "-map", "0:v",
                "-map", "0:a?",
                "-c:v", "libx264",
                "-preset", x264_preset,
                "-crf", str(x264_crf),
                "-profile:v", "high",
                "-level", level,
                "-pix_fmt", "yuv420p",
                "-vsync", "cfr",
                "-r", f"{fps:.6f}",
                "-threads", str(ff_threads),
                "-c:a", "aac",
                "-b:a", "384k",  # YouTube recommended: 384kbps stereo
                "-shortest",
                str(output_path),
            ]
        
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        
        if result.returncode != 0:
            raise RuntimeError(f"Failed to trim video: {result.stderr.decode()}")
        
        logger.info(f"✅ Video trimmed: {start}s from start, {end}s from end")
    
    def _convert_to_shorts(self, input_path: str, output_path: str, orig_width: int, orig_height: int, shorts_format: str = "crop"):
        """تحويل الفيديو لصيغة شورتس (9:16 - 1080x1920)

        shorts_format:
            - crop: قص/ملء الشاشة (قد يقص أطراف اليمين/اليسار)
            - fit_blur: عرض كامل + خلفية ضبابية من نفس الفيديو
            - partial_blur: تكبير متوسط (إظهار جزء أكبر من الأعلى/الأسفل) + خلفية ضبابية
        """
        ff_threads, x264_preset, x264_crf = self._shorts_x264_settings()
        level = (os.getenv("SHORTS_H264_LEVEL", "5.1") or "5.1").strip() or "5.1"
        
        # 🔧 تحسين: استخدام CRF منخفض جداً (14) بدلاً من lossless (0)
        # هذا يقلل حجم الفيديو الوسيط بنسبة 90% مع الحفاظ على جودة شبه lossless
        # yuv420p متوافق مع جميع المراحل اللاحقة (لا يوجد تحويل مساحة ألوان)
        try:
            lossless_intermediate = str(os.getenv("SHORTS_INTERMEDIATE_LOSSLESS", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}
        except Exception:
            lossless_intermediate = False
        
        fps = self._get_video_fps(input_path)
        if not fps or fps <= 0:
            fps = 30.0
        gop = int(round(fps))
        if gop < 1:
            gop = 30
        
        if lossless_intermediate:
            x264_preset = str(os.getenv("SHORTS_INTERMEDIATE_PRESET", "ultrafast") or "ultrafast").strip() or "ultrafast"
            x264_crf = 0
            # x264 lossless (crf=0) is not compatible with profile=high + yuv420p.
            v_profile = "high444"
            v_pix_fmt = "yuv444p"
        else:
            # 🆕 Improved settings: Lower CRF with fast preset, respecting RENDER mode
            _, base_preset, base_crf = self._shorts_x264_settings()
            x264_preset = str(os.getenv("SHORTS_INTERMEDIATE_PRESET", base_preset) or base_preset).strip() or base_preset
            x264_crf = int(os.getenv("SHORTS_INTERMEDIATE_CRF", str(base_crf)) or str(base_crf))
            v_profile = "high"
            v_pix_fmt = "yuv420p"  # No color space conversion later

        shorts_vol = _parse_volume_ratio(os.getenv("SHORTS_AUDIO_VOLUME", "60"), 0.6)

        target_width = 1080
        target_height = 1920
        
        # حساب نسبة العرض للارتفاع
        input_ratio = orig_width / orig_height
        target_ratio = target_width / target_height
        
        fmt = (shorts_format or "crop").strip().lower()

        try:
            skip_if_vertical = str(os.getenv("AUTO_MOD_SKIP_SHORTS_CONVERT_IF_VERTICAL", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
        except Exception:
            skip_if_vertical = True
        if skip_if_vertical and fmt == "crop" and orig_width > 0 and orig_height > 0:
            try:
                if abs((orig_width / orig_height) - (9.0 / 16.0)) <= 0.02:
                    import shutil
                    shutil.copy2(input_path, output_path)
                    logger.info(f"✅ Video already 9:16 ({orig_width}x{orig_height}); skipped shorts conversion.")
                    return
            except Exception:
                pass

        cmd = [
            ffmpeg_bin(),
            "-y",
            "-i", input_path,
        ]

        if fmt == "fit_blur":
            # خلفية: تكبير لملء 9:16 ثم قص + ضبابية
            # مقدمة: scale ليلائم الإطار (بدون قص) ثم overlay في المنتصف
            filter_complex = (
                f"[0:v]scale={target_width}:{target_height}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"scale=trunc(iw/2)*2:trunc(ih/2)*2,"
                f"crop={target_width}:{target_height},"
                f"boxblur=luma_radius=20:luma_power=1[bg];"
                f"[0:v]scale={target_width}:{target_height}:force_original_aspect_ratio=decrease:flags=lanczos,"
                f"scale=trunc(iw/2)*2:trunc(ih/2)*2[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2,format={v_pix_fmt}[outv]"
            )
            cmd += [
                "-filter_complex", filter_complex,
                "-map", "[outv]",
                "-c:v", "libx264",
                "-preset", x264_preset,
                "-crf", str(x264_crf),
                "-profile:v", v_profile,
                "-level", level,
                "-pix_fmt", v_pix_fmt,
                "-vsync", "cfr",
                "-r", f"{fps:.6f}",
                "-g", str(gop),
                "-threads", str(ff_threads),
            ]
        elif fmt == "partial_blur":
            # نفس فكرة الخلفية الضبابية، لكن نجعل المقدمة أكبر قليلاً (Zoom متوسط)
            # لتقليل الفراغ العلوي/السفلي مع قص جزء من اليمين/اليسار.
            # zoom=1.25 => المقدمة 1350x2400 ثم crop إلى 1080x1920.
            zoom = float(os.getenv("SHORTS_PARTIAL_ZOOM", "1.25") or "1.25")
            if zoom < 1.0:
                zoom = 1.0
            if zoom > 2.0:
                zoom = 2.0
            def _even(n: int) -> int:
                try:
                    n = int(n)
                except Exception:
                    return 2
                if n <= 0:
                    return 2
                return n if (n % 2 == 0) else (n - 1)

            fg_w = _even(int(target_width * zoom))
            fg_h = _even(int(target_height * zoom))
            filter_complex = (
                f"[0:v]scale={target_width}:{target_height}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"scale=trunc(iw/2)*2:trunc(ih/2)*2,"
                f"crop={target_width}:{target_height},"
                f"boxblur=luma_radius=20:luma_power=1[bg];"
                f"[0:v]scale={fg_w}:{fg_h}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"scale=trunc(iw/2)*2:trunc(ih/2)*2,"
                f"crop={target_width}:{target_height}[fg];"
                f"[bg][fg]overlay=0:0,format={v_pix_fmt}[outv]"
            )
            cmd += [
                "-filter_complex", filter_complex,
                "-map", "[outv]",
                "-c:v", "libx264",
                "-preset", x264_preset,
                "-crf", str(x264_crf),
                "-profile:v", v_profile,
                "-level", level,
                "-pix_fmt", v_pix_fmt,
                "-vsync", "cfr",
                "-r", f"{fps:.6f}",
                "-g", str(gop),
                "-threads", str(ff_threads),
            ]
        else:
            # تحديد استراتيجية التحويل (قص/ملء أو pad عند الحاجة)
            def _even(n: int) -> int:
                try:
                    n = int(n)
                except Exception:
                    return 2
                if n <= 0:
                    return 2
                return n if (n % 2 == 0) else (n - 1)

            if abs(input_ratio - target_ratio) < 0.01:
                vf = f"scale={target_width}:{target_height}:flags=lanczos"
            elif input_ratio > target_ratio:
                # IMPORTANT: for yuv420p, crop width/height and offsets should be even
                safe_h = _even(orig_height)
                new_width = _even(int(safe_h * target_ratio))
                crop_x = _even((orig_width - new_width) // 2)
                vf = f"crop={new_width}:{safe_h}:{crop_x}:0,scale={target_width}:{target_height}:flags=lanczos"
            else:
                scale_height = target_height
                scale_width = int(scale_height * input_ratio)
                if scale_width > target_width:
                    scale_width = target_width
                    scale_height = int(scale_width / input_ratio)

                # IMPORTANT: for yuv420p, intermediate scaled dimensions should be even
                scale_width = _even(scale_width)
                scale_height = _even(scale_height)

                pad_x = (target_width - scale_width) // 2
                pad_y = (target_height - scale_height) // 2

                vf = f"scale={scale_width}:{scale_height}:flags=lanczos,pad={target_width}:{target_height}:{pad_x}:{pad_y}:black"

            cmd += [
                "-vf", vf,
                "-map", "0:v",
                "-c:v", "libx264",
                "-preset", x264_preset,
                "-crf", str(x264_crf),
                "-profile:v", v_profile,
                "-level", level,
                "-pix_fmt", v_pix_fmt,
                "-vsync", "cfr",
                "-r", f"{fps:.6f}",
                "-g", str(gop),
                "-threads", str(ff_threads),
            ]
        has_audio = self._has_audio(input_path)
        if has_audio:
            cmd += [
                "-map", "0:a?",
                "-c:a", "aac",
                "-b:a", "384k",  # YouTube recommended: 384kbps stereo
                "-ar", "48000",  # YouTube recommended: 48kHz
                "-af", f"volume={shorts_vol}",
                "-movflags", "+faststart",  # Essential for YouTube streaming
                "-shortest",
            ]
        else:
            cmd += ["-an", "-movflags", "+faststart"]
        out_s = str(output_path)
        if out_s.lower().endswith(".mp4"):
            tmp_out = out_s[:-4] + ".tmp.mp4"
        else:
            tmp_out = out_s + ".tmp.mp4"
        try:
            if os.path.exists(tmp_out):
                os.remove(tmp_out)
        except Exception:
            pass
        # Ensure ffmpeg picks correct container even for temporary paths
        cmd += ["-f", "mp4"]
        cmd.append(str(tmp_out))

        try:
            timeout_s = int((os.getenv("FFMPEG_TIMEOUT_SECONDS", "600") or "600").strip())
        except Exception:
            timeout_s = 600
        if timeout_s <= 0:
            timeout_s = 600

        result = subprocess.run(cmd, capture_output=True, timeout=timeout_s)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to convert to shorts: {result.stderr.decode()}")

        # Validate output container to avoid later 'moov atom not found'
        try:
            self._validate_video_file(str(tmp_out))
        except Exception as e:
            raise RuntimeError(f"Failed to convert to shorts: output invalid: {e}")

        try:
            if os.path.exists(str(output_path)):
                os.remove(str(output_path))
        except Exception:
            pass
        try:
            os.replace(str(tmp_out), str(output_path))
        except Exception:
            # fallback copy if replace fails
            import shutil
            shutil.copy2(str(tmp_out), str(output_path))
            try:
                os.remove(str(tmp_out))
            except Exception:
                pass
        
        logger.info(f"✅ Video converted to shorts format: 1080x1920")
    
    def _add_cta_text(self, input_path: str, output_path: str, text: str, duration: float, custom_font: Optional[str] = None):
        """إضافة نص الدعوة في نهاية الفيديو"""
        is_ar = bool(re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", text or ""))

        use_text_shaping = (os.getenv("FFMPEG_DRAWTEXT_TEXT_SHAPING", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
        
        if is_ar and use_text_shaping:
            display_text = text
        elif is_ar and HAS_ARABIC_SUPPORT:
            try:
                reshaped_text = arabic_reshaper.reshape(text)
                display_text = get_display(reshaped_text)
            except Exception as e:
                logger.error(f"Error reshaping Arabic CTA text: {e}")
                display_text = text
        else:
            display_text = text

        text_start = max(0, duration - 2.5)
        def _find_app_photo() -> Optional[str]:
            try:
                candidates: list[Path] = []
                env_dir = os.getenv("APP_PHOTO_DIR") or os.getenv("APP_PHOTO_PATH")
                if env_dir:
                    candidates.append(Path(env_dir))
                try:
                    repo_root = Path(__file__).resolve().parents[2]
                    candidates.append(repo_root / "app-photo")
                except Exception:
                    pass
                candidates.append(Path("app-photo"))
                for base in candidates:
                    if not base.exists():
                        continue
                    for ext in ("*.png", "*.jpg", "*.jpeg"):
                        files = list(base.glob(ext))
                        if files:
                            return str(files[0])
                return None
            except Exception:
                return None
        app_photo = _find_app_photo()
        try:
            self._add_cta_text_via_image_overlay(input_path, output_path, display_text, text_start, app_photo, custom_font)
        except Exception as e:
            logger.error(f"CTA overlay with fade failed: {e}")
            import shutil
            shutil.copy2(input_path, output_path)

    def _apply_outro_blur_black(self, input_path: str, output_path: str, duration: float = 1.0):
        d = self._get_video_duration(input_path)
        if not d or d <= 0:
            import shutil
            shutil.copy2(input_path, output_path)
            return
        fade_start = max(0.0, d - duration)
        vf = f"boxblur=luma_radius=10:enable='gte(t,{fade_start})',fade=t=out:st={fade_start}:d={duration}"
        has_audio = self._has_audio(input_path)
        ff_threads, preset, crf = self._shorts_x264_settings()
        level = (os.getenv("SHORTS_H264_LEVEL", "5.1") or "5.1").strip() or "5.1"
        try:
            lossless_intermediate = str(os.getenv("SHORTS_INTERMEDIATE_LOSSLESS", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}
        except Exception:
            lossless_intermediate = False
        fps = self._get_video_fps(input_path)
        if not fps or fps <= 0:
            fps = 30.0
        gop = int(round(fps))
        if gop < 1:
            gop = 30
        if lossless_intermediate:
            preset = str(os.getenv("SHORTS_INTERMEDIATE_PRESET", "ultrafast") or "ultrafast").strip() or "ultrafast"
            crf = 0
        v_profile = "high444" if lossless_intermediate else "high"
        v_pix_fmt = "yuv444p" if lossless_intermediate else "yuv420p"
        cmd = [
            ffmpeg_bin(),
            "-y",
            "-i", input_path,
            "-vf", vf,
            "-map", "0:v",
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", str(crf),
            "-profile:v", v_profile,
            "-level", level,
            "-pix_fmt", v_pix_fmt,
            "-vsync", "cfr",
            "-r", f"{fps:.6f}",
            "-g", str(gop),
            "-threads", str(ff_threads),
        ]
        if has_audio:
            shorts_vol = _parse_volume_ratio(os.getenv("SHORTS_AUDIO_VOLUME", "60"), 0.6)
            af = f"volume={shorts_vol},afade=t=out:st={fade_start}:d={duration}"
            cmd += [
                "-af", af,
                "-map", "0:a?",
                "-c:a", "aac",
                "-b:a", "384k",  # YouTube recommended: 384kbps stereo
            ]
        else:
            cmd += ["-an"]
        cmd.append(str(output_path))
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        if result.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            import shutil
            shutil.copy2(input_path, output_path)

    def _add_cta_text_reliable(self, input_path: str, output_path: str, text: str, duration: float, custom_font: Optional[str] = None):
        # شريط أسود في المنتصف مع نص + صورة التطبيق - حجم مناسب للمحتوى
        text_start = max(0.0, duration - 2.5)
        try:
            vw, vh = self._get_video_dimensions(input_path)
        except Exception:
            vw, vh = (1080, 1920)
        
        # 🆕 تصغير الشريط ليناسب المحتوى فقط
        bar_h = max(180, min(300, int(vh * 0.18)))  # تكبير ضخم للارتفاع (كان 0.14)
        side_margin = 60  # هامش من الجوانب لعدم الالتصاق بالحواف
        margin = 20
        
        # اختيار الخط وعرض النص
        is_ar = bool(re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", text or ""))
        fontfile = self._get_best_font(text, custom_font)
        text_file = self.temp_dir / f"cta_{uuid.uuid4().hex}.txt"
        # البحث عن صورة التطبيق
        def _find_app_photo() -> Optional[str]:
            try:
                candidates: list[Path] = []
                env_dir = os.getenv("APP_PHOTO_DIR") or os.getenv("APP_PHOTO_PATH")
                if env_dir:
                    candidates.append(Path(env_dir))
                try:
                    repo_root = Path(__file__).resolve().parents[2]
                    candidates.append(repo_root / "app-photo")
                except Exception:
                    pass
                candidates.append(Path("app-photo"))
                for base in candidates:
                    if not base.exists():
                        continue
                    for ext in ("*.png", "*.jpg", "*.jpeg"):
                        files = list(base.glob(ext))
                        if files:
                            return str(files[0])
                return None
            except Exception:
                return None
        app_photo = _find_app_photo()
        logo_w = 0
        logo_h = 0
        if app_photo and os.path.exists(app_photo):
            try:
                from PIL import Image
                with Image.open(app_photo) as im:
                    logo_w, logo_h = im.size
            except Exception:
                app_photo = None
                logo_w = logo_h = 0
        # حساب مساحة النص الفعلية بعد وضع الشعار يميناً
        # الشعار لن يُكَبَّر أبداً، فقط يُصغَّر إذا تجاوز ارتفاع الشريط
        max_logo_h = bar_h - 2 * margin
        scale_factor = 1.0
        if logo_h > 0:
            scale_factor = min(1.0, max_logo_h / float(logo_h))
        logo_w_scaled = int(logo_w * scale_factor)
        text_area_w = vw - (margin + (logo_w_scaled if logo_w_scaled > 0 else 0) + margin + margin)
        # تجهيز أسطر النص (wrap) بالترتيب المنطقي ثم تشكيل كل سطر (RTL) عند الحاجة
        try:
            from PIL import ImageFont as _ImageFont, ImageDraw as _ImageDraw, Image as _Image

            def _shape_line(s: str) -> str:
                if is_ar and HAS_ARABIC_SUPPORT:
                    try:
                        return get_display(arabic_reshaper.reshape(s))
                    except Exception:
                        return s
                return s

            def _load_font(size: int):
                try:
                    return _ImageFont.truetype(fontfile, size)
                except Exception:
                    return _ImageFont.load_default()

            fnt = _load_font(75)  # تكبير ضخم (كان 56)
            tmp_img = _Image.new("RGB", (max(1, text_area_w), max(1, bar_h)))
            tmp_draw = _ImageDraw.Draw(tmp_img)

            def _wrap_lines_logical(s: str, max_w: int, max_lines: int = 5):
                # Split by existing newlines first
                parts = (s or "").split('\n')
                all_lines = []
                for part in parts:
                    words = part.split()
                    if not words:
                        continue
                    cur = ""
                    for w in words:
                        test = (cur + " " + w).strip()
                        if tmp_draw.textlength(_shape_line(test), font=fnt) <= max_w:
                            cur = test
                        else:
                            if cur:
                                all_lines.append(cur)
                            cur = w
                    if cur:
                        all_lines.append(cur)
                return all_lines[:max_lines]

            logical_lines = _wrap_lines_logical(text, max(1, text_area_w), max_lines=5)
            display_lines = [_shape_line(ln) for ln in logical_lines]
        except Exception:
            display_lines = [text[:100]]

        # كتابة النص لملف (لأغراض التتبع/التوافق)
        try:
            with open(text_file, "w", encoding="utf-8") as f:
                f.write("\n".join(display_lines))
        except Exception:
            pass
        # هروب المسارات
        def ffmpeg_escape_path(path_str):
            return str(path_str).replace("\\", "/").replace(":", "\\:")
        text_file_esc = ffmpeg_escape_path(text_file)
        font_esc = ffmpeg_escape_path(fontfile)
        filter_parts = []
        # 🆕 حساب عرض الشريط بناءً على المحتوى الفعلي (نص + صورة)
        from PIL import Image, ImageDraw, ImageFont
        
        # تحميل الخط أولاً لحساب عرض النص
        try:
            font = ImageFont.truetype(fontfile, 65)  # تكبير ضخم (كان 48)
        except Exception:
            font = None
            for fp in ["C:/Windows/Fonts/tahoma.ttf", "C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/system/fonts/NotoNaskhArabic-Regular.ttf"]:
                try:
                    if os.path.exists(fp):
                        font = ImageFont.truetype(fp, 38)
                        break
                except Exception:
                    continue
            if font is None:
                font = ImageFont.load_default()
        
        # حساب عرض النص
        tmp_img = Image.new("RGB", (1000, 200))
        tmp_draw = ImageDraw.Draw(tmp_img)
        
        text_lines = (text or "").split('\n')
        max_text_width = 0
        for line in text_lines:
            if is_ar and HAS_ARABIC_SUPPORT:
                try:
                    shaped_line = get_display(arabic_reshaper.reshape(line))
                except:
                    shaped_line = line
            else:
                shaped_line = line
            line_width = tmp_draw.textlength(shaped_line, font=font)
            max_text_width = max(max_text_width, line_width)
        
        # 🆕 عرض الشريط = النص + الشعار + هوامش داخلية
        icon_size = max(120, min(220, bar_h - 40))  # تكبير ضخم للأيقونة
        gap = 20  # المسافة بين الأيقونة والنص
        padding_x = 30  # هامش داخلي للشريط
        
        bar_w = int(max_text_width + icon_size + gap + padding_x * 2)
        bar_w = max(300, min(bar_w, vw - side_margin * 2))  # حد أقصى وأدنى للعرض
        
        # إنشاء صورة الشريط بالحجم المحسوب
        overlay_img = Image.new("RGBA", (bar_w, bar_h), (0, 0, 0, int(0.75 * 255)))
        draw = ImageDraw.Draw(overlay_img)
        
        # إدراج الشعار حسب اتجاه اللغة:
        # 🆕 تصحيح: للعربية (RTL): الشعار على اليمين، للإنجليزية (LTR): الشعار على اليسار
        logo_img = None
        logo_w_final = 0
        logo_h_final = 0
        if app_photo and os.path.exists(app_photo) and logo_w > 0 and logo_h > 0:
            try:
                logo_img = Image.open(app_photo).convert("RGBA")
                # تصغير الأيقونة لتناسب الشريط
                target_size = icon_size
                if logo_w > logo_h:
                    new_w = target_size
                    new_h = max(1, int(logo_h * target_size / logo_w))
                else:
                    new_h = target_size
                    new_w = max(1, int(logo_w * target_size / logo_h))
                logo_img = logo_img.resize((new_w, new_h), Image.LANCZOS)
                logo_w_final, logo_h_final = logo_img.size
                
                # 🆕 تصحيح الموضع:
                # - للعربية (RTL): الشعار على اليمين من الشريط
                # - للإنجليزية (LTR): الشعار على اليسار من الشريط
                ly = (bar_h - logo_h_final) // 2
                if is_ar:
                    lx = bar_w - logo_w_final - padding_x  # الشعار على اليمين للعربية
                else:
                    lx = padding_x  # الشعار على اليسار للإنجليزية
                overlay_img.alpha_composite(logo_img, (lx, ly))
            except Exception:
                logo_img = None
                logo_w_final = logo_h_final = 0
        # 🆕 تحديد مساحة النص حسب اتجاه اللغة وموضع الشعار
        if is_ar and logo_w_final > 0:
            # العربية: الشعار على اليمين، النص على اليسار
            text_area_left = padding_x
            text_area_right = bar_w - logo_w_final - gap - padding_x
        elif logo_w_final > 0:
            # الإنجليزية: الشعار على اليسار، النص على اليمين
            text_area_left = logo_w_final + gap + padding_x
            text_area_right = bar_w - padding_x
        else:
            # لا يوجد شعار
            text_area_left = padding_x
            text_area_right = bar_w - padding_x
        text_area_w2 = max(1, int(text_area_right - text_area_left))

        # تجهيز أسطر النص
        spacing = 2
        display_lines = []
        for line in (text or "").split('\n'):
            if is_ar and HAS_ARABIC_SUPPORT:
                try:
                    display_lines.append(get_display(arabic_reshaper.reshape(line)))
                except:
                    display_lines.append(line)
            else:
                display_lines.append(line)
        
        total_h = 0
        line_heights = []
        for ln in display_lines[:3]:  # حد أقصى 3 أسطر
            bb = draw.textbbox((0, 0), ln, font=font)
            lh = (bb[3] - bb[1]) if bb else 0
            line_heights.append(lh)
            total_h += lh
        total_h += spacing * max(0, len(line_heights) - 1)
        text_y = max(0, (bar_h - total_h) // 2)
        y = text_y
        for i, ln in enumerate(display_lines[:3]):
            lw = draw.textlength(ln, font=font)
            if is_ar:
                # محاذاة لليمين للنص العربي
                x = text_area_right - lw
            else:
                # محاذاة لليسار للنص الإنجليزي
                x = text_area_left
            draw.text((x, y), ln, font=font, fill=(255, 255, 255, 255))
            y += line_heights[i] + spacing
        # حفظ صورة overlay مؤقتاً
        overlay_path = self.temp_dir / f"cta_bar_{uuid.uuid4().hex}.png"
        overlay_img.save(overlay_path)
        try:
            ovl_esc = ffmpeg_escape_path(overlay_path)
            overlay_input = str(overlay_path)
            # 🔧 استخدام إعدادات وسيط محسّنة
            ff_threads, base_preset, base_crf = self._shorts_x264_settings()
            preset = str(os.getenv("SHORTS_INTERMEDIATE_PRESET", base_preset) or base_preset).strip() or base_preset
            crf = int(os.getenv("SHORTS_INTERMEDIATE_CRF", str(base_crf)) or str(base_crf))
            # إضافة تأثيري fade-in و fade-out على طبقة الدعوة
            video_duration = self._get_video_duration(input_path)
            if not video_duration:
                video_duration = float(duration or 0.0)
            show_start = max(0.0, float(video_duration) - 2.6)
            show_dur = max(1.2, min(2.6, float(video_duration) - show_start))
            show_end = float(show_start) + float(show_dur)
            # ✅ تسجيل تفصيلي للتوقيتات المحسوبة
            logger.info(f"[CTA_reliable] video_duration={video_duration:.2f}s show_start={show_start:.2f}s show_end={show_end:.2f}s show_dur={show_dur:.2f}s")
            try:
                seed_s = f"{input_path}::{output_path}::{text}"
                r = int(hashlib.md5(seed_s.encode("utf-8", errors="ignore")).hexdigest()[:8], 16)
                fade_in_dur = 0.25 + (r % 35) / 100.0
                fade_out_dur = 0.40 + ((r // 35) % 45) / 100.0
                in_kinds = ["fade", "slideup", "slidedown", "pop"]
                out_kinds = ["fade", "slideup", "slidedown", "pop"]
                in_kind = in_kinds[r % len(in_kinds)]
                out_kind = out_kinds[(r // 11) % len(out_kinds)]
            except Exception:
                fade_in_dur = 0.35
                fade_out_dur = 0.65
                in_kind = "fade"
                out_kind = "fade"
            fade_in_dur = max(0.18, min(fade_in_dur, 0.7))
            fade_out_dur = max(0.22, min(fade_out_dur, 0.9))
            if (fade_in_dur + fade_out_dur) > (show_dur - 0.05):
                fade_in_dur = max(0.18, show_dur * 0.35)
                fade_out_dur = max(0.22, show_dur * 0.45)
            fade_out_start_rel = max(0.0, float(show_dur) - float(fade_out_dur))
            fade_out_start_abs = float(show_start) + float(fade_out_start_rel)

            x_expr = "(W-w)/2"
            y_base = "(H-h)/2"
            slide_px = max(80, min(220, int(vh * 0.10)))
            intro_end_abs = float(show_start) + float(fade_in_dur)

            try:
                logger.info(
                    f"[CTA_fx] in={in_kind} out={out_kind} "
                    f"fade_in={fade_in_dur:.2f}s fade_out={fade_out_dur:.2f}s "
                    f"slide_px={int(slide_px)}"
                )
            except Exception:
                pass

            # حركة y بحسب نوع الدخول/الخروج
            y_expr = y_base
            if in_kind in {"slideup", "slidedown"} or out_kind in {"slideup", "slidedown"}:
                intro_sign = 1 if in_kind == "slideup" else (-1 if in_kind == "slidedown" else 0)
                out_sign = 1 if out_kind == "slidedown" else (-1 if out_kind == "slideup" else 0)
                intro_expr = y_base
                if intro_sign != 0:
                    intro_expr = f"if(lt(t,{intro_end_abs}),{y_base}+({intro_end_abs}-t)/{fade_in_dur}*{intro_sign*slide_px},{y_base})"
                out_expr = y_base
                if out_sign != 0:
                    out_expr = f"{y_base}+(t-{fade_out_start_abs})/{fade_out_dur}*{out_sign*slide_px}"
                # الخروج له أولوية في النهاية
                y_expr = f"if(gte(t,{fade_out_start_abs}),{out_expr},{intro_expr})"

            # تأثير pop (تكبير/تصغير خفيف) على طبقة الدعوة
            overlay_scale_filter = ""
            if in_kind == "pop" or out_kind == "pop":
                intro_scale = "1"
                out_scale = "1"
                if in_kind == "pop":
                    intro_scale = f"if(lt(t,{fade_in_dur}),0.85+0.15*(t/{fade_in_dur}),1)"
                if out_kind == "pop":
                    out_scale = f"if(gte(t,{fade_out_start_rel}),1-0.20*min(1,(t-{fade_out_start_rel})/{fade_out_dur}),1)"
                scale_expr = f"({intro_scale})*({out_scale})"
                # IMPORTANT: commas inside expressions must be escaped for ffmpeg filtergraph parsing
                scale_expr_esc = str(scale_expr).replace(",", "\\,")
                overlay_scale_filter = f",scale=iw*{scale_expr_esc}:ih*{scale_expr_esc}:eval=frame"

            enable_end = float(show_end) + 0.05
            cmd = [
                ffmpeg_bin(), "-y",
                "-i", input_path,
                "-loop", "1", "-i", overlay_input,
                # Quote y expression because it contains commas (if/min) which otherwise break filter parsing
                "-filter_complex", f"[1:v]format=rgba,fade=t=in:st=0:d={fade_in_dur}:alpha=1,fade=t=out:st={fade_out_start_rel}:d={fade_out_dur}:alpha=1{overlay_scale_filter},setpts=PTS-STARTPTS+{show_start}/TB[ovl];[0:v][ovl]overlay=x={x_expr}:y='{y_expr}':shortest=1:enable='between(t,{show_start},{enable_end})'[vout]",
                "-map", "[vout]",
                "-map", "0:a?",
                "-c:v", "libx264",
                "-preset", preset,
                "-crf", str(crf),
                "-threads", str(ff_threads),
                "-vsync", "cfr",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "384k",
                "-ar", "48000",
                "-movflags", "+faststart",
                str(output_path)
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=300)
            if result.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                raise RuntimeError((result.stderr or b"").decode())
        finally:
            try:
                if text_file.exists():
                    text_file.unlink()
            except Exception:
                pass
            try:
                if overlay_path.exists():
                    overlay_path.unlink()
            except Exception:
                pass
    def _add_cta_text_via_image_overlay(self, input_path: str, output_path: str, text: str, text_start: float, app_photo: Optional[str], custom_font: Optional[str] = None):
        from PIL import Image, ImageDraw, ImageFont
        vw, vh = self._get_video_dimensions(input_path)
        if not vw or not vh:
            vw, vh = 1080, 1920
        
        # 1. Prepare Font and Size settings first
        # Bigger font as requested (V3 - Even Bigger)
        target_font_size = 64  # Increased from 56
        font_path = self._get_best_font(text, custom_font)
        try:
            font = ImageFont.truetype(font_path, target_font_size)
        except Exception:
            font = ImageFont.load_default()

        # 2. Text Logic Handling (RTL/Shaping)
        is_ar = bool(re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", text or ""))
        def _shape_line(s: str) -> str:
            if is_ar and HAS_ARABIC_SUPPORT:
                try:
                    return get_display(arabic_reshaper.reshape(s))
                except Exception:
                    return s
            return s

        # 3. Logo Handling - Prepare logo to measure it
        logo_img = None
        logo_w = logo_h = 0
        scaling_factor_logo = 1.5 # Increased again
        padding_x = 40 # Increased padding
        padding_y = 30 
        
        # Target logo height related to font size but constrained
        target_logo_h = 180 # Increased from 160
        
        if app_photo and os.path.exists(app_photo):
            try:
                temp_logo = Image.open(app_photo).convert("RGBA")
                orig_w, orig_h = temp_logo.size
                # Resize keeping aspect ratio
                ratio = min(target_logo_h / orig_h, target_logo_h / orig_w)
                new_w = max(1, int(orig_w * ratio))
                new_h = max(1, int(orig_h * ratio))
                logo_img = temp_logo.resize((new_w, new_h), Image.LANCZOS)
                logo_w, logo_h = logo_img.size
            except Exception:
                logo_img = None
        
        # 4. Calculate Dynamic Box Dimensions
        # Initial max width constraints
        safe_margin = 40
        max_box_w = min(950, vw - safe_margin * 2) 
        
        # Helper to wrap text
        dummy_draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        
        # Available width for text = Total Max Width - Padding - Logo Width - Padding - Padding
        text_avail_w = max_box_w - (padding_x * 2) - (logo_w + padding_x if logo_w > 0 else 0)
        
        def _wrap_lines_logical(s: str, max_w: int, max_lines: int = 4):
            parts = (s or "").split('\n')
            all_lines = []
            for part in parts:
                words = part.split()
                if not words: continue
                cur = ""
                for w in words:
                    test = (cur + " " + w).strip()
                    if dummy_draw.textlength(_shape_line(test), font=font) <= max_w:
                        cur = test
                    else:
                        if cur: all_lines.append(cur)
                        cur = w
                if cur: all_lines.append(cur)
            return all_lines[:max_lines]

        logical_lines = _wrap_lines_logical(text, max(1, text_avail_w), max_lines=4)
        display_lines = [_shape_line(ln) for ln in logical_lines]
        
        # Measure total text block size
        text_total_h = 0
        text_max_w = 0
        line_heights = []
        spacing = 8 
        
        for ln in display_lines:
            bb = dummy_draw.textbbox((0, 0), ln, font=font)
            lh = (bb[3] - bb[1]) if bb else font.size 
            line_heights.append(lh)
            text_total_h += lh
            tw = dummy_draw.textlength(ln, font=font)
            text_max_w = max(text_max_w, tw)
            
        if len(display_lines) > 1:
            text_total_h += spacing * (len(display_lines) - 1)
            
        # 5. Finalize Box Dimensions based on content
        # Total Width = Padding + Text Width + Padding + Logo + Padding
        # But we usually want a fixed width bar or at least wide enough.
        # Let's keep the width reasonably wide for aesthetics, or tighten it?
        # User complained about "big black bar", usually height is the issue.
        # Let's make width dynamic too but with a minimum used width.
        
        content_w = padding_x + text_max_w + padding_x + (logo_w + padding_x if logo_w > 0 else 0)
        final_box_w = max(int(content_w), 600) # Minimum width 600px for good look
        final_box_w = min(final_box_w, max_box_w) # Cap at max
        
        # Total Height = Max(Text Height, Logo Height) + padding_y * 2
        content_h = max(text_total_h, logo_h)
        final_box_h = int(content_h + padding_y * 2)
        
        # 6. Draw Content
        img = Image.new("RGBA", (final_box_w, final_box_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        
        # Background with rounded corners aesthetic or just rect? 
        # Making it slightly transparent black
        draw.rectangle([(0, 0), (final_box_w, final_box_h)], fill=(0, 0, 0, int(0.85 * 255))) # 85% opacity
        
        rtl = bool(is_ar)
        
        # Position Logo
        logo_x = 0
        logo_y = (final_box_h - logo_h) // 2
        
        text_start_x = 0
        
        if logo_img:
            if rtl:
                # [ Text ...  Logo ]
                logo_x = final_box_w - padding_x - logo_w
                img.alpha_composite(logo_img, (logo_x, logo_y))
                text_right_limit = logo_x - padding_x
                text_left_limit = padding_x
            else:
                # [ Logo ... Text ]
                logo_x = padding_x
                img.alpha_composite(logo_img, (logo_x, logo_y))
                text_left_limit = logo_x + logo_w + padding_x
                text_right_limit = final_box_w - padding_x
        else:
             text_left_limit = padding_x
             text_right_limit = final_box_w - padding_x

        # Position Text (Vertically Centered)
        text_y = (final_box_h - text_total_h) // 2
        
        # Draw Text Lines
        for i, ln in enumerate(display_lines):
            lw = draw.textlength(ln, font=font)
            
            # Horizontal align
            if is_ar:
                # Right align within available space
                # x = text_right_limit - lw
                # But if we want it centered in the remaining space:
               avail_space = text_right_limit - text_left_limit
               x = text_left_limit + (avail_space - lw) # Right align
            else:
                # Left align
                x = text_left_limit
            
            draw.text((x, text_y), ln, font=font, fill=(255, 255, 255, 255))
            text_y += line_heights[i] + spacing

        # Save
        overlay_path = self.temp_dir / f"cta_overlay_{uuid.uuid4().hex}.png"
        img.save(overlay_path)
        
        # Get video duration for fade out timing
        video_duration = self._get_video_duration(input_path)
        fade_in_dur = 0.5
        fade_out_dur = 0.8 # Making it quicker to leave
        
        # Calculate fade out start time (end of video - fade duration - 0.2s buffer)
        fade_out_start = max(text_start + 1.0, video_duration - fade_out_dur - 0.2)
        
        def ffmpeg_escape_path(path_str):
            return str(path_str).replace("\\", "/").replace(":", "\\:")
        ovl_esc = ffmpeg_escape_path(overlay_path)
        overlay_input = str(overlay_path)
        ff_threads, preset, crf = self._shorts_x264_settings()
        level = (os.getenv("SHORTS_H264_LEVEL", "5.1") or "5.1").strip() or "5.1"
        cmd = [
            ffmpeg_bin(),
            "-y",
            "-i", input_path,
            "-loop", "1", "-i", overlay_input,
            "-filter_complex", f"[1:v]format=rgba,setpts=PTS-STARTPTS,fade=t=in:st={text_start}:d={fade_in_dur}:alpha=1,fade=t=out:st={fade_out_start}:d={fade_out_dur}:alpha=1[ovl];[0:v][ovl]overlay=x=(W-w)/2:y=(H-h)/2:shortest=1:enable='between(t,{text_start},{fade_out_start + fade_out_dur})'[vout]",
            "-map", "[vout]",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", str(crf),
            "-profile:v", "high",
            "-level", level,
            "-pix_fmt", "yuv420p",
            "-threads", str(ff_threads),
            "-c:a", "copy",
            str(output_path)
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        try:
            if overlay_path.exists():
                overlay_path.unlink()
        except Exception:
            pass
        if result.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise RuntimeError(result.stderr.decode() if result.stderr else "CTA image overlay failed")

    def _flip_video(self, input_path: str, output_path: Path) -> None:
        """قلب الفيديو أفقياً (Mirror)"""
        try:
            ff_threads, preset, crf = self._shorts_x264_settings()
            level = (os.getenv("SHORTS_H264_LEVEL", "5.1") or "5.1").strip() or "5.1"
            
            cmd = [
                ffmpeg_bin(),
                "-y",
                "-i", input_path,
                "-vf", "hflip",
                "-c:v", "libx264",
                "-preset", preset,
                "-crf", str(crf),
                "-profile:v", "high",
                "-level", level,
                "-pix_fmt", "yuv420p",
                "-threads", str(ff_threads),
                "-c:a", "copy", # الحفاظ على الصوت كما هو
                str(output_path)
            ]
            
            result = subprocess.run(cmd, capture_output=True, timeout=300)
            if result.returncode != 0 or not os.path.exists(output_path):
                raise RuntimeError(f"FFmpeg hflip failed: {result.stderr.decode()[:500]}")
                
        except Exception as e:
            logger.error(f"Error flipping video: {e}")
            raise
