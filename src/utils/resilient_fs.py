import os
import time
import shutil
import logging
from typing import Any, Callable, TypeVar, Generic

logger = logging.getLogger(__name__)

T = TypeVar('T')

class ResilientFS:
    """
    محيط مرن لعمليات نظام الملفات للتعامل مع انقطاعات الشبكة (WinError 53/3/5).
    """
    
    @staticmethod
    def run(func: Callable[..., T], *args, **kwargs) -> T:
        """تشغيل دالة مع محاولات إعادة في حالة فشل الشبكة"""
        max_retries = 5
        retry_delay = 1.0
        last_error = None
        
        for i in range(max_retries):
            try:
                return func(*args, **kwargs)
            except (OSError, IOError) as e:
                last_error = e
                # أخطاء الشبكة الشائعة في ويندوز
                # 53: The network path was not found
                # 3: The system cannot find the path specified
                # 5: Access is denied (أحياناً بسبب انقطاع جزئي)
                is_network_error = any(str(code) in str(e) for code in [53, 3, 5])
                
                if is_network_error and i < max_retries - 1:
                    logger.warning(f"⚠️ فشل الشبكة ({e}). محاولة {i+1}/{max_retries} بعد {retry_delay} ثانية...")
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    raise last_error
        
        raise last_error

    @staticmethod
    def makedirs(path: str, exist_ok: bool = True):
        return ResilientFS.run(os.makedirs, path, exist_ok=exist_ok)

    @staticmethod
    def exists(path: str) -> bool:
        try:
            return ResilientFS.run(os.path.exists, path)
        except:
            return False

    @staticmethod
    def open(file: str, mode: str = 'r', **kwargs):
        return ResilientFS.run(open, file, mode, **kwargs)

    @staticmethod
    def write_text(path: str, content: str, encoding: str = 'utf-8', newline=None) -> int:
        parent_dir = os.path.dirname(os.path.abspath(path))
        if parent_dir:
            ResilientFS.makedirs(parent_dir, exist_ok=True)
        with ResilientFS.open(path, 'w', encoding=encoding, newline=newline) as f:
            return f.write(content)

    @staticmethod
    def copy2(src: str, dst: str):
        return ResilientFS.run(shutil.copy2, src, dst)

    @staticmethod
    def remove(path: str):
        if ResilientFS.exists(path):
            return ResilientFS.run(os.remove, path)

    @staticmethod
    def rmtree(path: str, ignore_errors: bool = False):
        if ResilientFS.exists(path):
            return ResilientFS.run(shutil.rmtree, path, ignore_errors=ignore_errors)

    @staticmethod
    def listdir(path: str) -> list:
        return ResilientFS.run(os.listdir, path)

    @staticmethod
    def getsize(path: str) -> int:
        return ResilientFS.run(os.path.getsize, path)

    @staticmethod
    def isdir(path: str) -> bool:
        try:
            return ResilientFS.run(os.path.isdir, path)
        except:
            return False

    @staticmethod
    def isfile(path: str) -> bool:
        try:
            return ResilientFS.run(os.path.isfile, path)
        except:
            return False
