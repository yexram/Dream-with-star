"""
LLM 客户端异常类定义
统一错误处理体系
"""

class LLMError(Exception):
    """所有 LLM 相关异常的基类"""
    pass

class ConfigurationError(LLMError):
    """配置错误（如缺少 API key）"""
    pass

class AuthenticationError(LLMError):
    """认证失败（API key 无效）"""
    pass

class RateLimitError(LLMError):
    """速率限制超限（含 user_id 级别和全局限流）"""
    def __init__(self, message: str, is_user_level: bool = False, retry_after: float = None):
        super().__init__(message)
        self.is_user_level = is_user_level
        self.retry_after = retry_after

class InsufficientBalanceError(LLMError):
    """账户余额不足"""
    pass

class ValidationError(LLMError):
    """请求参数错误（422）"""
    pass

class ModelNotFoundError(LLMError):
    """模型不存在或不可用"""
    pass

class CircuitBreakerOpenError(LLMError):
    """断路器开路，请求被拒绝"""
    pass