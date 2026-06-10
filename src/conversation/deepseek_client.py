"""
DeepSeek API 专业客户端
- 支持所有标准参数（温度、max_tokens、top_p 等）
- 正确处理 user_id（放入 extra_body）
- 支持流式/非流式、工具调用循环
- 重试 + 指数退避
- 令牌桶限流
- 余额查询、模型列表（含缓存）
- 错误码映射到标准异常
"""

import json
import time
import requests
from typing import Dict, List, Optional, Any, Callable, Union, Generator
from openai import OpenAI, APIError, RateLimitError as OpenAIRateLimitError, APIConnectionError
from src.tools.logger import log_info, log_warning, log_error, log_debug
from src.tools.config_manager import config
from src.conversation.api_manager import key_manager
from src.conversation.base_client import (
    BaseLLMClient, TokenBucketLimiter, retry_on_failure, run_tool_loop
)
from src.conversation.exceptions import (
    AuthenticationError, RateLimitError, InsufficientBalanceError,
    ValidationError, ModelNotFoundError, CircuitBreakerOpenError
)


class DeepSeekClient(BaseLLMClient):
    """DeepSeek API 客户端"""

    def __init__(self, module_id: int):
        """
        :param module_id: 模块标识（用于从 KeyManager 获取对应密钥）
        """
        self.module_id = module_id
        # 从配置管理器读取参数
        self.base_url = config.get(f"deepseek.module_{module_id}.base_url", "https://api.deepseek.com")
        self.timeout = config.get(f"deepseek.module_{module_id}.timeout", 60)
        self.max_retries = config.get(f"deepseek.module_{module_id}.max_retries", 3)
        self.save_conversations_dir = config.get(f"deepseek.module_{module_id}.save_conversations_dir", "data/conversations")
        rate = config.get(f"deepseek.module_{module_id}.rate_limit_per_second")
        self.limiter = TokenBucketLimiter(rate) if rate else None

        # 模型列表缓存
        self._models_cache = None
        self._models_cache_time = 0
        self.models_cache_ttl = config.get(f"deepseek.module_{module_id}.models_cache_ttl_seconds", 3600)

        # 注册配置热重载回调
        config.watch(self._on_config_change)

        log_info("DeepSeekClient 初始化完成", module_id=module_id, base_url=self.base_url)

    def _on_config_change(self, key_path: str, old_value: Any, new_value: Any):
        """配置变更时的热更新处理"""
        if key_path.startswith(f"deepseek.module_{self.module_id}"):
            log_info("DeepSeekClient 配置已更新", key=key_path, new_value=new_value)
            # 动态更新限流器
            if key_path.endswith("rate_limit_per_second"):
                self.limiter = TokenBucketLimiter(new_value) if new_value else None

    def _get_openai_client(self, api_key: str) -> OpenAI:
        """获取 OpenAI SDK 客户端实例"""
        return OpenAI(api_key=api_key, base_url=self.base_url, timeout=self.timeout)

    def _map_exception(self, e: Exception) -> Exception:
        """将 OpenAI SDK 异常映射为自定义异常"""
        if isinstance(e, APIError):
            status_code = getattr(e, 'status_code', None)
            if status_code == 401:
                return AuthenticationError(f"认证失败: {e}")
            elif status_code == 402:
                return InsufficientBalanceError(f"余额不足: {e}")
            elif status_code == 422:
                return ValidationError(f"参数错误: {e}")
            elif status_code == 429:
                # 尝试从响应中获取 Retry-After 和 user_id 信息
                retry_after = None
                is_user_level = False
                if hasattr(e, 'response') and e.response:
                    retry_after = e.response.headers.get('Retry-After')
                    # DeepSeek 的 user_id 限流错误信息中可能包含 "user_id"
                    body = e.response.json() if e.response.content else {}
                    error_msg = body.get('error', {}).get('message', '')
                    if 'user_id' in error_msg.lower():
                        is_user_level = True
                return RateLimitError(str(e), is_user_level=is_user_level, retry_after=retry_after)
            elif status_code == 404:
                return ModelNotFoundError(f"模型不存在: {e}")
        elif isinstance(e, APIConnectionError):
            return ConnectionError(f"网络连接错误: {e}")
        return e

    @retry_on_failure(max_retries=3, base_delay=1.0, max_delay=10.0,
                      exceptions=(APIError, APIConnectionError, RateLimitError))
    def chat_completion(
        self,
        messages: List[Dict],
        model: Optional[str] = None,
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
        调用 DeepSeek Chat Completions API
        """
        # 限流
        if self.limiter:
            self.limiter.acquire(block=True)

        # 从配置读取默认值
        if model is None:
            model = config.get(f"deepseek.module_{self.module_id}.default_model", "deepseek-v4-pro")
        if temperature is None:
            temperature = config.get(f"deepseek.module_{self.module_id}.default_temperature", 1.0)
        if max_tokens is None:
            max_tokens = config.get(f"deepseek.module_{self.module_id}.default_max_tokens", 4096)
        if top_p is None:
            top_p = config.get(f"deepseek.module_{self.module_id}.default_top_p", 1.0)
        reasoning_effort = config.get(f"deepseek.module_{self.module_id}.default_reasoning_effort", "high")
        thinking_enabled = config.get(f"deepseek.module_{self.module_id}.thinking_enabled", True)

        # 构建请求体
        request_body = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": top_p,
        }
        if stop is not None:
            request_body["stop"] = stop
        if tools:
            request_body["tools"] = tools
        if tool_choice:
            request_body["tool_choice"] = tool_choice

        # 处理 extra_body（包含 user_id、thinking 等）
        extra_body = {}
        if user_id:
            extra_body["user_id"] = user_id
        if thinking_enabled:
            extra_body["thinking"] = {"type": "enabled"}
        if reasoning_effort:
            extra_body["reasoning_effort"] = reasoning_effort
        # 合并额外参数
        extra_body.update(kwargs.get("extra_body", {}))
        if extra_body:
            request_body["extra_body"] = extra_body

        # 获取 API key
        api_key = key_manager.get_next_key(self.module_id)
        if not api_key:
            raise RuntimeError(f"模块 {self.module_id} 无可用 API key")

        client = self._get_openai_client(api_key)
        try:
            if stream:
                response = client.chat.completions.create(**request_body)
                return self._stream_generator(response, request_body)
            else:
                response = client.chat.completions.create(**request_body)
                resp_dict = self._response_to_dict(response)
                self._save_conversation(request_body, resp_dict)
                return resp_dict
        except Exception as e:
            mapped = self._map_exception(e)
            # 对于认证失败，通知 KeyManager 标记失败并重试（由装饰器处理）
            if isinstance(mapped, AuthenticationError):
                key_manager.mark_key_failure(self.module_id, api_key)
            raise mapped

    def _stream_generator(self, response, request_body: Dict) -> Generator[Dict, None, None]:
        """流式响应生成器，同时收集完整响应以便保存"""
        chunks = []
        try:
            for chunk in response:
                chunk_dict = self._chunk_to_dict(chunk)
                chunks.append(chunk_dict)
                yield chunk_dict
        finally:
            # 流结束时保存对话
            full_resp = self._reconstruct_from_chunks(chunks)
            if full_resp:
                self._save_conversation(request_body, full_resp)

    def _chunk_to_dict(self, chunk) -> Dict:
        """将 OpenAI SDK 的流式块转换为字典"""
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
                        "tool_calls": getattr(c.delta, "tool_calls", None),
                    },
                    "finish_reason": c.finish_reason
                } for c in chunk.choices
            ],
            "usage": chunk.usage.__dict__ if chunk.usage else None
        }

    def _reconstruct_from_chunks(self, chunks: List[Dict]) -> Optional[Dict]:
        """从流式块重建完整响应"""
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
        """将 OpenAI SDK 的非流式响应转换为字典"""
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
            } if response.usage else None,
            "created": response.created,
        }

    def _save_conversation(self, request: Dict, response: Dict):
        """将对话保存到 JSON 文件（可配置是否脱敏）"""
        if not self.save_conversations_dir:
            return
        from pathlib import Path
        from datetime import datetime
        import os
        path = Path(self.save_conversations_dir)
        path.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        filename = f"{timestamp}.json"
        filepath = path / filename
        data = {
            "timestamp": timestamp,
            "request": request,
            "response": response
        }
        # 可选脱敏（通过配置控制）
        if not config.get(f"deepseek.module_{self.module_id}.save_sensitive", True):
            # 简单脱敏：移除 messages 中的 content（保留角色和工具调用结构）
            if "messages" in data["request"]:
                for msg in data["request"]["messages"]:
                    if "content" in msg:
                        msg["content"] = "[REDACTED]"
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        log_debug("对话已保存", file=str(filepath))

    def chat_with_tools(
        self,
        messages: List[Dict],
        tools: List[Dict],
        tool_executor: Callable[[str, Dict], str],
        max_tool_rounds: int = 5,
        **kwargs
    ) -> Dict:
        """自动工具调用（委托给公共函数）"""
        return run_tool_loop(
            initial_messages=messages,
            tools=tools,
            tool_executor=tool_executor,
            client=self,
            max_tool_rounds=max_tool_rounds,
            **kwargs
        )

    def get_balance(self) -> Dict:
        """查询 DeepSeek 账户余额"""
        api_key = key_manager.get_next_key(self.module_id)
        if not api_key:
            raise RuntimeError(f"模块 {self.module_id} 无可用 API key")
        import requests
        url = f"{self.base_url}/user/balance"
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        try:
            resp = requests.get(url, headers=headers, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log_error("查询余额失败", error=str(e))
            raise

    def list_models(self, use_cache: bool = True) -> List[Dict]:
        """列出可用模型（带缓存）"""
        if use_cache and self._models_cache and (time.time() - self._models_cache_time) < self.models_cache_ttl:
            return self._models_cache
        api_key = key_manager.get_next_key(self.module_id)
        if not api_key:
            raise RuntimeError(f"模块 {self.module_id} 无可用 API key")
        import requests
        url = f"{self.base_url}/models"
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        try:
            resp = requests.get(url, headers=headers, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            models = data.get("data", [])
            self._models_cache = models
            self._models_cache_time = time.time()
            return models
        except Exception as e:
            log_error("获取模型列表失败", error=str(e))
            # 返回静态 fallback 列表（硬编码 DeepSeek 当前模型）
            fallback = [
                {"id": "deepseek-v4-pro", "object": "model", "owned_by": "deepseek"},
                {"id": "deepseek-v4-flash", "object": "model", "owned_by": "deepseek"},
            ]
            return fallback