# src/conversation/deepseek_client.py
"""
DeepSeek API 客户端（专业完整版）
- 支持流式和非流式响应
- 自动工具调用循环（支持多轮工具调用）
- 智能重试 + 指数退避
- 本地限速器（可选，基于令牌桶）
- 自动保存对话记录
"""

import json
import time
import threading
from typing import Dict, List, Optional, Any, Callable, Generator, Union
from pathlib import Path
from datetime import datetime
from openai import OpenAI
from src.tools.logger import log_info, log_warning, log_error, log_debug
from src.conversation.api_manager import key_manager


class TokenBucketLimiter:
    """简单的令牌桶限速器（线程安全）"""
    def __init__(self, rate: float, capacity: float = None):
        """
        :param rate: 每秒产生的令牌数（例如 10 表示每秒10个请求）
        :param capacity: 桶容量，默认为 rate
        """
        self.rate = rate
        self.capacity = capacity or rate
        self.tokens = self.capacity
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, block: bool = True) -> bool:
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
            # 计算需要等待的时间
            wait_time = (1 - self.tokens) / self.rate
            time.sleep(wait_time)
            # 重试一次
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last_refill = now
            if self.tokens >= 1:
                self.tokens -= 1
                return True
            return False


class DeepSeekClient:
    def __init__(
        self,
        module_id: int,
        base_url: str = "https://api.deepseek.com",
        timeout: int = 60,
        max_retries: int = 3,
        save_conversations_dir: Optional[str] = "data/conversations",
        rate_limit_per_second: float = None,  # 例如 5.0 表示每秒最多5个请求
    ):
        self.module_id = module_id
        self.base_url = base_url
        self.timeout = timeout
        self.max_retries = max_retries
        self.save_dir = save_conversations_dir
        if self.save_dir:
            Path(self.save_dir).mkdir(parents=True, exist_ok=True)
        self.limiter = TokenBucketLimiter(rate_limit_per_second) if rate_limit_per_second else None

    def _get_client(self, api_key: str) -> OpenAI:
        return OpenAI(api_key=api_key, base_url=self.base_url, timeout=self.timeout)

    def _should_retry(self, status_code: int) -> bool:
        return status_code in {429, 500, 503}

    def _is_fatal(self, status_code: int) -> bool:
        return status_code in {400, 401, 402, 422}

    def _extract_status_code(self, exception: Exception) -> Optional[int]:
        try:
            if hasattr(exception, 'status_code'):
                return exception.status_code
            if hasattr(exception, 'response') and hasattr(exception.response, 'status_code'):
                return exception.response.status_code
        except:
            pass
        return None

    def _save_conversation(self, request: Dict, response_dict: Dict):
        if not self.save_dir:
            return
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        filename = f"{timestamp}.json"
        filepath = Path(self.save_dir) / filename
        data = {
            "timestamp": timestamp,
            "request": request,
            "response": response_dict
        }
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def chat_completion(
        self,
        messages: List[Dict],
        model: str = "deepseek-v4-pro",
        user_id: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        thinking_enabled: bool = True,
        tools: Optional[List[Dict]] = None,
        tool_choice: Optional[str] = None,
        stream: bool = False,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        extra_body: Optional[Dict] = None,
    ) -> Union[Dict, Generator[Dict, None, None]]:
        """
        非流式返回完整响应字典；流式返回生成器，每个元素是字典（增量）
        """
        # 限流
        if self.limiter:
            self.limiter.acquire(block=True)

        request_body = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }
        if user_id:
            request_body["user_id"] = user_id
        if reasoning_effort:
            request_body["reasoning_effort"] = reasoning_effort
        if thinking_enabled:
            if "extra_body" not in request_body:
                request_body["extra_body"] = {}
            request_body["extra_body"]["thinking"] = {"type": "enabled"}
        if tools:
            request_body["tools"] = tools
        if tool_choice:
            request_body["tool_choice"] = tool_choice
        if temperature is not None:
            request_body["temperature"] = temperature
        if max_tokens:
            request_body["max_tokens"] = max_tokens
        if extra_body:
            request_body["extra_body"] = {**request_body.get("extra_body", {}), **extra_body}

        last_exception = None
        for attempt in range(1, self.max_retries + 1):
            api_key = key_manager.get_next_key(self.module_id)
            if not api_key:
                raise RuntimeError(f"模块 {self.module_id} 无可用密钥")

            client = self._get_client(api_key)
            try:
                if stream:
                    response = client.chat.completions.create(**request_body)
                    # 返回生成器，但需要在这里捕获异常较难，简化：先不实现流式重试
                    return self._stream_generator(response, request_body)
                else:
                    response = client.chat.completions.create(**request_body)
                    resp_dict = self._response_to_dict(response)
                    self._save_conversation(request_body, resp_dict)
                    return resp_dict
            except Exception as e:
                status_code = self._extract_status_code(e)
                log_warning(f"API调用失败 (attempt {attempt})", error=str(e), status_code=status_code)
                if status_code and self._is_fatal(status_code):
                    key_manager.mark_key_failure(self.module_id, api_key)
                    continue  # 切换密钥重试
                elif status_code and self._should_retry(status_code):
                    key_manager.mark_key_failure(self.module_id, api_key)
                    wait = min(2 ** attempt, 10)
                    time.sleep(wait)
                    continue
                else:
                    last_exception = e
                    continue
        raise Exception(f"经过 {self.max_retries} 次重试仍失败") from last_exception

    def _stream_generator(self, response, request_body):
        """流式响应的生成器，同时收集完整响应用于保存"""
        collected_chunks = []
        try:
            for chunk in response:
                chunk_dict = self._chunk_to_dict(chunk)
                collected_chunks.append(chunk_dict)
                yield chunk_dict
        finally:
            # 流结束后，尝试重建完整响应并保存（可选）
            full_response = self._reconstruct_from_chunks(collected_chunks)
            if full_response:
                self._save_conversation(request_body, full_response)

    def _chunk_to_dict(self, chunk) -> Dict:
        return {
            "id": chunk.id,
            "model": chunk.model,
            "choices": [
                {
                    "index": c.index,
                    "delta": {
                        "role": getattr(c.delta, "role", None),
                        "content": getattr(c.delta, "content", None),
                        "reasoning_content": getattr(c.delta, "reasoning_content", None),
                    },
                    "finish_reason": c.finish_reason
                } for c in chunk.choices
            ],
            "usage": chunk.usage.__dict__ if chunk.usage else None
        }

    def _reconstruct_from_chunks(self, chunks: List[Dict]) -> Optional[Dict]:
        """从流式chunks重建完整响应（用于保存）"""
        if not chunks:
            return None
        full_content = ""
        full_reasoning = ""
        final_chunk = chunks[-1]
        for c in chunks:
            for choice in c.get("choices", []):
                delta = choice.get("delta", {})
                full_content += delta.get("content", "")
                full_reasoning += delta.get("reasoning_content", "")
        return {
            "id": chunks[0].get("id"),
            "model": chunks[0].get("model"),
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": full_content,
                    "reasoning_content": full_reasoning,
                },
                "finish_reason": final_chunk["choices"][0].get("finish_reason")
            }],
            "usage": final_chunk.get("usage")
        }

    def _response_to_dict(self, response) -> Dict:
        return {
            "id": response.id,
            "model": response.model,
            "choices": [
                {
                    "index": c.index,
                    "message": {
                        "role": c.message.role,
                        "content": c.message.content,
                        "reasoning_content": getattr(c.message, "reasoning_content", None),
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": tc.type,
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments
                                }
                            } for tc in (c.message.tool_calls or [])
                        ] if c.message.tool_calls else None
                    },
                    "finish_reason": c.finish_reason
                } for c in response.choices
            ],
            "usage": {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
                "prompt_cache_hit_tokens": getattr(response.usage, "prompt_cache_hit_tokens", None),
                "prompt_cache_miss_tokens": getattr(response.usage, "prompt_cache_miss_tokens", None),
            } if response.usage else None,
            "created": response.created,
        }

    # ========== 高级方法：带自动工具调用循环 ==========
    def chat_with_tools(
        self,
        messages: List[Dict],
        tools: List[Dict],
        tool_executor: Callable[[str, Dict], str],
        model: str = "deepseek-v4-pro",
        user_id: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        thinking_enabled: bool = True,
        max_tool_rounds: int = 5,
        **kwargs
    ) -> Dict:
        """
        自动处理工具调用的多轮对话
        :param tools: 工具定义列表
        :param tool_executor: 执行工具的回调函数，签名为 (tool_name, arguments_dict) -> str
        :param max_tool_rounds: 最多工具调用轮数，防止无限循环
        :return: 最终的非工具响应字典
        """
        current_messages = messages.copy()
        for round_num in range(1, max_tool_rounds + 1):
            response = self.chat_completion(
                messages=current_messages,
                model=model,
                user_id=user_id,
                reasoning_effort=reasoning_effort,
                thinking_enabled=thinking_enabled,
                tools=tools,
                stream=False,
                **kwargs
            )
            message = response["choices"][0]["message"]
            tool_calls = message.get("tool_calls")
            if not tool_calls:
                # 没有工具调用，返回最终结果
                return response
            # 有工具调用：添加 assistant 消息，然后执行工具并添加 tool 结果
            current_messages.append({
                "role": "assistant",
                "content": message.get("content", ""),
                "reasoning_content": message.get("reasoning_content"),
                "tool_calls": tool_calls
            })
            for tc in tool_calls:
                func_name = tc["function"]["name"]
                args = json.loads(tc["function"]["arguments"])
                result = tool_executor(func_name, args)
                current_messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result
                })
        raise Exception(f"工具调用超过最大轮数 {max_tool_rounds}")