import time
import logging
from typing import Dict, Set, Optional
from collections import defaultdict
from ..agent.config import Config

logger = logging.getLogger(__name__)

# In-memory storage for rate limiting
# In production, this should be stored in a persistent database
_user_requests: Dict[int, list] = defaultdict(list)
_rate_limit_cleanup_timestamps: Dict[int, float] = {}


class SensitiveDataFilter(logging.Filter):
    """Filter that masks sensitive data like the Telegram bot token in logs."""
    
    def __init__(self, sensitive_patterns: Optional[Set[str]] = None):
        super().__init__()
        self.sensitive_patterns = sensitive_patterns or set()

    def filter(self, record):
        if not self.sensitive_patterns:
            return True
        
        msg = str(record.msg)
        for pattern in self.sensitive_patterns:
            if pattern:
                msg = msg.replace(pattern, "[MASKED]")
        
        record.msg = msg
        return True


class SecurityManager:
    """Manages security features for the Telegram bot."""
    
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.allowed_user_ids = set(cfg.TELEGRAM_ALLOWED_USER_IDS) if cfg.TELEGRAM_ALLOWED_USER_IDS else set()
        self._warning_shown = False
        
    def is_user_allowed(self, user_id: int) -> bool:
        """
        Check if a user is allowed to use the bot.
        
        Args:
            user_id: Telegram user ID
            
        Returns:
            True if user is allowed, False otherwise
        """
        # If no allowed users are configured, allow all (not recommended for production)
        if not self.allowed_user_ids:
            if not self._warning_shown:
                logger.warning("⚠️ SECURITY WARNING: No allowed users configured!")
                logger.warning("Anyone can control this bot. To fix:")
                logger.warning("1. Get your Telegram user ID by messaging @userinfobot")
                logger.warning("2. Add to .env: TELEGRAM_ALLOWED_USER_IDS=your_user_id")
                logger.warning("3. For multiple users: TELEGRAM_ALLOWED_USER_IDS=123456789,987654321")
                self._warning_shown = True
            return True
            
        return user_id in self.allowed_user_ids
    
    def check_rate_limit(self, user_id: int, max_requests: int = 10, window_seconds: int = 60) -> bool:
        """
        Check if a user has exceeded the rate limit.
        
        Args:
            user_id: Telegram user ID
            max_requests: Maximum number of requests allowed in the window
            window_seconds: Time window in seconds
            
        Returns:
            True if user is within rate limit, False if rate limited
        """
        current_time = time.time()
        
        # Clean up old requests periodically
        self._cleanup_old_requests(user_id, current_time, window_seconds)
        
        # Add current request
        _user_requests[user_id].append(current_time)
        
        # Check if user has exceeded rate limit
        if len(_user_requests[user_id]) > max_requests:
            logger.warning(f"User {user_id} exceeded rate limit: {len(_user_requests[user_id])} requests in {window_seconds} seconds")
            return False
            
        return True
    
    def _cleanup_old_requests(self, user_id: int, current_time: float, window_seconds: int):
        """
        Clean up old requests for a user.
        
        Args:
            user_id: Telegram user ID
            current_time: Current timestamp
            window_seconds: Time window in seconds
        """
        # Only clean up if it's been more than 10 seconds since last cleanup
        last_cleanup = _rate_limit_cleanup_timestamps.get(user_id, 0)
        if current_time - last_cleanup < 10:
            return
            
        # Remove requests older than the window
        cutoff_time = current_time - window_seconds
        _user_requests[user_id] = [
            timestamp for timestamp in _user_requests[user_id]
            if timestamp > cutoff_time
        ]
        
        # Update cleanup timestamp
        _rate_limit_cleanup_timestamps[user_id] = current_time
    
    def sanitize_input(self, text: str) -> str:
        """
        Sanitize user input to prevent injection attacks.
        
        Args:
            text: User input text
            
        Returns:
            Sanitized text
        """
        if not text:
            return ""
            
        # Remove or escape potentially dangerous characters
        # This is a basic implementation - in production, use a proper sanitization library
        sanitized = text.replace("<", "&lt;").replace(">", "&gt;")
        sanitized = "".join(char for char in sanitized if ord(char) < 127)  # Remove non-ASCII characters
        
        # Limit length
        return sanitized[:1000]  # Limit to 1000 characters
    
    def validate_url(self, url: str) -> bool:
        """
        Validate that a URL is safe to use.
        
        Args:
            url: URL to validate
            
        Returns:
            True if URL is valid and safe, False otherwise
        """
        if not url:
            return False
            
        # Basic URL validation
        # In production, use a proper URL validation library
        url = url.lower().strip()
        allowed_schemes = ["http://", "https://"]
        
        if not any(url.startswith(scheme) for scheme in allowed_schemes):
            return False
            
        # Check for dangerous patterns
        dangerous_patterns = ["javascript:", "data:", "vbscript:"]
        if any(pattern in url for pattern in dangerous_patterns):
            return False
            
        # Check that URL is not too long
        if len(url) > 2048:
            return False
            
        return True


# Global security manager instance
_security_manager: Optional[SecurityManager] = None


def get_security_manager(cfg: Optional[Config] = None) -> SecurityManager:
    """
    Get a global security manager instance.
    
    Args:
        cfg: Configuration object (optional if already initialized)
        
    Returns:
        SecurityManager instance
    """
    global _security_manager
    if _security_manager is None:
        if cfg is None:
            raise ValueError("Configuration required for first initialization")
        _security_manager = SecurityManager(cfg)
    else:
        # Refresh allowed users dynamically if a cfg is provided
        if cfg is not None:
            _security_manager.cfg = cfg
            _security_manager.allowed_user_ids = set(cfg.TELEGRAM_ALLOWED_USER_IDS) if cfg.TELEGRAM_ALLOWED_USER_IDS else set()
    return _security_manager


def apply_sensitive_data_filter(cfg: Config):
    """Apply the sensitive data filter to all active loggers."""
    sensitive_patterns = set()
    if cfg.TELEGRAM_BOT_TOKEN:
        sensitive_patterns.add(cfg.TELEGRAM_BOT_TOKEN)
    if cfg.MISTRAL_API_KEY:
        sensitive_patterns.add(cfg.MISTRAL_API_KEY)
    
    if not sensitive_patterns:
        return

    f = SensitiveDataFilter(sensitive_patterns)
    
    # Apply to root logger and all registered loggers
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(f)
        
    for logger_name in logging.root.manager.loggerDict:
        log = logging.getLogger(logger_name)
        for handler in log.handlers:
            handler.addFilter(f)


def is_user_allowed(user_id: int, cfg: Optional[Config] = None) -> bool:
    """
    Check if a user is allowed to use the bot.
    
    Args:
        user_id: Telegram user ID
        cfg: Configuration object (optional if already initialized)
        
    Returns:
        True if user is allowed, False otherwise
    """
    manager = get_security_manager(cfg)
    return manager.is_user_allowed(user_id)


def check_rate_limit(user_id: int, max_requests: int = 10, window_seconds: int = 60, 
                    cfg: Optional[Config] = None) -> bool:
    """
    Check if a user has exceeded the rate limit.
    
    Args:
        user_id: Telegram user ID
        max_requests: Maximum number of requests allowed in the window
        window_seconds: Time window in seconds
        cfg: Configuration object (optional if already initialized)
        
    Returns:
        True if user is within rate limit, False if rate limited
    """
    manager = get_security_manager(cfg)
    return manager.check_rate_limit(user_id, max_requests, window_seconds)


def sanitize_input(text: str, cfg: Optional[Config] = None) -> str:
    """
    Sanitize user input to prevent injection attacks.
    
    Args:
        text: User input text
        cfg: Configuration object (optional if already initialized)
        
    Returns:
        Sanitized text
    """
    manager = get_security_manager(cfg)
    return manager.sanitize_input(text)


def validate_url(url: str, cfg: Optional[Config] = None) -> bool:
    """
    Validate that a URL is safe to use.
    
    Args:
        url: URL to validate
        cfg: Configuration object (optional if already initialized)
        
    Returns:
        True if URL is valid and safe, False otherwise
    """
    manager = get_security_manager(cfg)
    return manager.validate_url(url)