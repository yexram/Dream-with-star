"""
大模型客户端抽象基类和公共工具
- 抽象接口定义（供 DeepSeek、OpenAI 等实现）
- 令牌桶限流器（线程安全）
- 指数退避重试装饰器
- 工具调用循环公用方法
"""

import time
import threading
import functools
import json
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any, Callable, Union, Generator
from src.tools.logger import log_warning, log_error, log_info


# ==================== 令牌桶限流器 ====================
class TokenBucketLimiter:
    """
    线程安全的令牌桶限速器
    :param rate: 每秒产生的令牌数（例如 10 表示每秒 10 个请求）
    :param capacity: 桶容量，默认为 rate（突发请求数）
    """
    def __init__(self, rate: float, capacity: float = None):
        self.rate = rate
        self.capacity = capacity or rate
        self.tokens = self.capacity
        self.last_refill = time.monotonic()
        self._lock = threading.RLock()

    def acquire(self, block: bool = True) -> bool:
        """
        请求一个令牌
        :param block: 是否阻塞等待
        :return: 是否获得令牌（非阻塞模式下）
        """
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self.last_refill
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                self.last_refill = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return True
                if not block:
                    return False
                # 计算需要等待的时间（关键：释放锁后 sleep）
                wait_time = (1 - self.tokens) / self.rate
            # 释放锁，避免长时间占用
            time.sleep(wait_time)


# ==================== 重试装饰器 ====================
def retry_on_failure(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    exceptions: tuple = (Exception,),
    on_retry: Callable = None
):
    """
    指数退避重试装饰器
    :param max_retries: 最大重试次数
    :param base_delay: 初始等待秒数
    :param max_delay: 最大等待秒数
    :param exceptions: 需要重试的异常类型
    :param on_retry: 每次重试前的回调函数，接收 (attempt, exception) 参数
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt == max_retries:
                        break
                    if on_retry:
                        on_retry(attempt, e)
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    log_warning(f"重试 {attempt}/{max_retries}，等待 {delay:.2f}s，错误: {e}")
                    time.sleep(delay)
            raise last_exception
        return wrapper
    return decorator


# ==================== 抽象基类 ====================
class BaseLLMClient(ABC):
    """所有 LLM 客户端必须实现的接口"""

    @abstractmethod
    def chat_completion(
        self,
        messages: List[Dict],
        model: str,
        user_id: Optional[str] = None,
        stream: bool = False,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        top_p: Optional[float] = None,
        stop: Optional[Union[str, List[str]]] = None,
        tools: Optional[List[Dict]] = None,
        tool_choice: Optional[str] = None,
        **kwargs
    ) -> Union[Dict, Generator[Dict, None, None]]:
        """
        单轮对话补全（支持流式和非流式）
        :return: 非流式返回完整响应字典；流式返回生成器，每元素为增量块字典
        """
        pass

    @abstractmethod
    def chat_with_tools(
        self,
        messages: List[Dict],
        tools: List[Dict],
        tool_executor: Callable[[str, Dict], str],
        max_tool_rounds: int = 5,
        **kwargs
    ) -> Dict:
        """
        自动工具调用循环
        :param tool_executor: 执行工具的回调，签名 (tool_name, arguments) -> str
        :return: 最终的非工具响应字典
        """
        pass

    @abstractmethod
    def get_balance(self) -> Dict:
        """查询账户余额（API 支持则返回，否则返回模拟数据）"""
        pass

    @abstractmethod
    def list_models(self) -> List[Dict]:
        """列出可用模型（API 支持则返回，否则返回静态列表）"""
        pass


# ==================== 工具调用公共逻辑 ====================
def run_tool_loop(
    initial_messages: List[Dict],
    tools: List[Dict],
    tool_executor: Callable[[str, Dict], str],
    client: BaseLLMClient,
    max_tool_rounds: int = 5,
    **kwargs
) -> Dict:
    """
    工具调用循环的公共实现，任何继承 BaseLLMClient 的子类均可调用
    :param client: 实现了 chat_completion 的客户端实例
    :return: 最终响应字典（不包含 tool_calls）
    """
    current_messages = initial_messages.copy()
    for round_num in range(1, max_tool_rounds + 1):
        response = client.chat_completion(
            messages=current_messages,
            tools=tools,
            stream=False,
            **kwargs
        )
        message = response["choices"][0]["message"]
        tool_calls = message.get("tool_calls")
        if not tool_calls:
            return response

        # 添加 assistant 消息
        assistant_msg = {
            "role": "assistant",
            "content": message.get("content", ""),
            "tool_calls": tool_calls
        }
        if "reasoning_content" in message:
            assistant_msg["reasoning_content"] = message["reasoning_content"]
        current_messages.append(assistant_msg)

        # 执行工具并添加结果
        for tc in tool_calls:
            func_name = tc["function"]["name"]
            args = tc["function"].get("arguments", {})
            if isinstance(args, str):
                import json
                args = json.loads(args)
            result = tool_executor(func_name, args)
            current_messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result
            })
    raise RuntimeError(f"工具调用超过最大轮数 {max_tool_rounds}")