# Dream-with-star API 调用框架模块接口文档

## 概述

本框架提供了一套完整、可扩展的 DeepSeek API 调用解决方案，包含：

- **统一 LLM 客户端接口**（`BaseLLMClient`）
- **DeepSeek 专业客户端**（`DeepSeekClient`）
- **对话记忆管理**（`ConversationMemory`）
- **API 密钥安全管理**（`KeyManager`）
- **提示词模板**（`PromptBuilder`）
- **配置与日志系统**（基于已有工具）
- **完整的错误处理体系**（`exceptions`）

支持流式/非流式、工具调用、自动重试、限流、余额查询、模型列表等功能，适用于生产环境。

---

## 一、异常模块 (`src.conversation.exceptions`)

### 1.1 异常层次结构

| 异常类 | 父类 | 触发条件 |
|--------|------|----------|
| `LLMError` | `Exception` | 所有 LLM 相关异常的基类 |
| `ConfigurationError` | `LLMError` | 配置错误（缺少 API key、模块未注册） |
| `AuthenticationError` | `LLMError` | API key 无效或过期（HTTP 401） |
| `RateLimitError` | `LLMError` | 请求速率超限（HTTP 429） |
| `InsufficientBalanceError` | `LLMError` | 账户余额不足（HTTP 402） |
| `ValidationError` | `LLMError` | 请求参数错误（HTTP 422） |
| `ModelNotFoundError` | `LLMError` | 模型不存在或不可用（HTTP 404） |
| `CircuitBreakerOpenError` | `LLMError` | 断路器开路，请求被拒绝 |

### 1.2 `RateLimitError` 特有属性

```python
class RateLimitError(LLMError):
    def __init__(self, message: str, is_user_level: bool = False, retry_after: float = None):
        self.is_user_level = is_user_level   # 是否为 user_id 级别的限流
        self.retry_after = retry_after       # 建议等待秒数（从 Retry-After 头解析）
```

### 1.3 使用示例

```python
from src.conversation.exceptions import AuthenticationError, RateLimitError

try:
    client.chat_completion(...)
except AuthenticationError:
    # 切换 API key
except RateLimitError as e:
    if e.is_user_level:
        time.sleep(e.retry_after or 1)
    else:
        # 全局限流，等待后重试或切换 key
```

---

## 二、基础客户端抽象 (`src.conversation.base_client`)

### 2.1 `BaseLLMClient` 抽象类

所有 LLM 客户端必须实现的接口。

#### 方法：`chat_completion`

```python
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
) -> Union[Dict, Generator[Dict, None, None]]
```

- **参数**
  - `messages`: 消息列表，格式 `[{"role": "user", "content": "..."}]`
  - `model`: 模型名称（如 `"deepseek-v4-pro"`）
  - `user_id`: 业务侧用户标识（DeepSeek 通过 `extra_body` 传递）
  - `stream`: 是否流式输出
  - `temperature`: 采样温度 (0~2)
  - `max_tokens`: 最大输出 token 数
  - `top_p`: 核采样参数
  - `stop`: 停止词
  - `tools`: 工具定义列表（OpenAI 格式）
  - `tool_choice`: 强制使用工具，如 `"auto"`, `"none"`, `{"type": "function", "function": {"name": "..."}}`
- **返回**
  - 非流式：`Dict`，结构同 OpenAI 响应
  - 流式：生成器，每个元素为增量块 `Dict`

#### 方法：`chat_with_tools`

```python
def chat_with_tools(
    self,
    messages: List[Dict],
    tools: List[Dict],
    tool_executor: Callable[[str, Dict], str],
    max_tool_rounds: int = 5,
    **kwargs
) -> Dict
```

- **参数**
  - `messages`: 初始消息列表
  - `tools`: 工具定义列表
  - `tool_executor`: 工具执行回调函数，签名 `(tool_name: str, arguments: Dict) -> str`
  - `max_tool_rounds`: 最大工具调用轮数
- **返回**：最终的非工具响应字典（不含 `tool_calls`）

#### 方法：`get_balance`

```python
def get_balance(self) -> Dict
```

- **返回**：余额信息，如 `{"is_available": True, "balance_infos": [...]}`

#### 方法：`list_models`

```python
def list_models(self, use_cache: bool = True) -> List[Dict]
```

- **返回**：模型列表，如 `[{"id": "deepseek-v4-pro", ...}]`

### 2.2 `TokenBucketLimiter` 限流器

```python
class TokenBucketLimiter:
    def __init__(self, rate: float, capacity: float = None)
    def acquire(self, block: bool = True) -> bool
```

- `rate`: 每秒令牌产生数（请求数/秒）
- `capacity`: 桶容量（突发请求数），默认等于 `rate`
- `acquire(block=True)`: 获取一个令牌，阻塞或立即返回

### 2.3 `retry_on_failure` 装饰器

```python
def retry_on_failure(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    exceptions: tuple = (Exception,),
    on_retry: Callable = None
)
```

用于包装需要自动重试的函数。

### 2.4 `run_tool_loop` 公共函数

```python
def run_tool_loop(
    initial_messages: List[Dict],
    tools: List[Dict],
    tool_executor: Callable[[str, Dict], str],
    client: BaseLLMClient,
    max_tool_rounds: int = 5,
    **kwargs
) -> Dict
```

实现工具调用循环的通用逻辑，任何 `BaseLLMClient` 子类均可调用。

---

## 三、DeepSeek 专业客户端 (`src.conversation.deepseek_client`)

### 3.1 初始化

```python
client = DeepSeekClient(module_id: int)
```

- `module_id`: 模块 ID（与 `KeyManager` 中的模块对应）

配置通过 `config.json` 读取，支持热重载。可配置项：

| 配置键（点号路径） | 说明 | 默认值 |
|-------------------|------|--------|
| `deepseek.module_{id}.base_url` | API 基础 URL | `https://api.deepseek.com` |
| `deepseek.module_{id}.timeout` | 请求超时秒数 | 60 |
| `deepseek.module_{id}.max_retries` | 最大重试次数 | 3 |
| `deepseek.module_{id}.rate_limit_per_second` | 客户端限流（请求/秒） | `None`（不限流） |
| `deepseek.module_{id}.save_conversations_dir` | 对话保存目录 | `"data/conversations"` |
| `deepseek.module_{id}.save_sensitive` | 是否保存敏感信息 | `true` |
| `deepseek.module_{id}.default_model` | 默认模型 | `"deepseek-v4-pro"` |
| `deepseek.module_{id}.default_temperature` | 默认温度 | 1.0 |
| `deepseek.module_{id}.default_max_tokens` | 默认最大输出 token | 4096 |
| `deepseek.module_{id}.default_top_p` | 默认 top_p | 1.0 |
| `deepseek.module_{id}.default_reasoning_effort` | 推理强度 | `"high"` |
| `deepseek.module_{id}.thinking_enabled` | 是否启用思考模式 | `true` |
| `deepseek.module_{id}.models_cache_ttl_seconds` | 模型列表缓存 TTL | 3600 |

### 3.2 实现的方法

继承并实现 `BaseLLMClient` 的所有抽象方法，此外增加：

#### `_save_conversation` (内部)

自动保存请求/响应到 JSON 文件（可脱敏）。

### 3.3 使用示例

```python
client = DeepSeekClient(module_id=1)
messages = [{"role": "user", "content": "Hello"}]
resp = client.chat_completion(messages, user_id="user_123", temperature=0.7)
print(resp["choices"][0]["message"]["content"])
```

---

## 四、对话记忆模块 (`src.memorys.conversation_memory`)

### 4.1 `ConversationMemory` 类

#### 初始化

```python
memory = ConversationMemory(system_prompt: Optional[str] = None)
```

- `system_prompt`: 系统提示词（可选）

#### 方法

| 方法 | 说明 |
|------|------|
| `add_user_message(content: str)` | 添加用户消息 |
| `add_assistant_message(content, reasoning_content=None, tool_calls=None)` | 添加助手消息 |
| `add_tool_result(tool_call_id: str, content: str)` | 添加工具执行结果 |
| `get_messages_for_api() -> List[Dict]` | 获取适合发给 API 的消息（自动过滤 `reasoning_content`） |
| `clear()` | 清空所有消息，保留 system prompt |
| `to_dict() -> Dict` | 序列化为字典 |
| `from_dict(data: Dict) -> ConversationMemory` | 从字典反序列化 |
| `save_to_file(file_path: str, append_timestamp: bool = True) -> str` | 保存到 JSON 文件，返回实际路径 |

### 4.2 示例

```python
mem = ConversationMemory(system_prompt="You are a helpful assistant")
mem.add_user_message("What's Python?")
mem.add_assistant_message("Python is a programming language.")
mem.save_to_file("data/conversations/", append_timestamp=True)
```

---

## 五、提示词构建器 (`src.conversation.prompt_builder`)

所有方法均为静态方法。

| 方法 | 签名 | 说明 |
|------|------|------|
| `role_play` | `(role: str, instruction: str = "") -> str` | 生成角色扮演提示 |
| `summary` | `(text: str, max_length: int = 200) -> str` | 生成摘要提示 |
| `translate` | `(text: str, target_lang: str = "中文") -> str` | 生成翻译提示 |
| `safety_guard` | `() -> str` | 返回内容安全前置提示 |
| `tool_recovery` | `(tool_name: str, error: str) -> str` | 工具调用失败恢复提示 |

---

## 六、API 密钥管理器 (`src.conversation.api_manager`)

### 6.1 `KeyManager` 单例

全局实例 `key_manager` 已预创建，直接使用。

#### 方法

| 方法 | 说明 |
|------|------|
| `register_module(module_id: int, strategy: str = "weighted_round_robin")` | 注册模块，策略可选 `"weighted_round_robin"` 或 `"failover"` |
| `add_key(module_id: int, api_key_plain: str, key_id: str = None, weight: int = 1)` | 添加 API 密钥（明文，自动加密存储） |
| `get_next_key(module_id: int) -> Optional[str]` | 获取下一个可用的密钥（明文） |
| `mark_key_failure(module_id: int, key_plain: str)` | 标记密钥失败，连续失败3次后自动禁用 |
| `reload()` | 手动重新加载配置文件 |

### 6.2 配置存储

- 主密钥：`data/secrets/master.key`（自动生成）
- 加密密钥库：`data/secrets/api_keys.json`

支持热重载（监听文件变化）。

### 6.3 示例

```python
key_manager.register_module(1)
key_manager.add_key(1, "sk-xxxx", key_id="primary", weight=2)
key = key_manager.get_next_key(1)
```

---

## 七、配置管理器 (`src.tools.config_manager`)

全局单例 `config`，提供配置加载、热重载、变更监听功能。

### 7.1 核心方法

| 方法 | 说明 |
|------|------|
| `load_file(file_path, format="auto")` | 加载 JSON/YAML 配置文件 |
| `load_chain(defaults, file_path, env_prefix)` | 链式加载：默认值 → 文件 → 环境变量 |
| `load_from_env(prefix)` | 从环境变量加载（`PREFIX_KEY__SUBKEY` 映射） |
| `set_defaults(defaults)` | 设置默认配置 |
| `get(key_path, default=None)` | 获取配置（支持点号路径） |
| `set(key_path, value)` | 动态修改配置（内存） |
| `get_all()` | 获取完整配置（深拷贝） |
| `reload()` | 重新加载配置文件 |
| `watch(callback)` | 注册变更监听器，回调参数 `(key_path, old, new)` |
| `start_watch_thread(interval=1.0)` | 启动后台文件监控线程 |

### 7.2 示例

```python
config.load_file("data/configs/config.json")
config.start_watch_thread()
timeout = config.get("deepseek.module_1.timeout", default=60)
```

---

## 八、日志系统 (`src.tools.logger`)

已在文档中详细说明，此处仅列出常用 API：

| 函数 | 说明 |
|------|------|
| `log_init_from_config(config)` | 从配置管理器初始化日志 |
| `log_debug(msg, **kwargs)` | 调试日志 |
| `log_info(msg, **kwargs)` | 信息日志 |
| `log_warning(msg, **kwargs)` | 警告日志 |
| `log_error(msg, **kwargs)` | 错误日志 |
| `log_critical(msg, **kwargs)` | 严重错误 |
| `log_bind(**kwargs)` | 绑定线程局部上下文（如 `request_id`） |
| `log_unbind(*keys)` | 解绑字段 |
| `log_clear_context()` | 清空当前线程上下文 |
| `log_set_level(level)` | 动态修改日志级别 |
| `log_flush()` | 刷新缓冲区 |
| `log_close()` | 关闭日志系统 |

---

## 九、完整调用流程示例

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from src.tools.config_manager import config
from src.tools.logger import log_init_from_config, log_info, log_bind
from src.conversation.deepseek_client import DeepSeekClient
from src.memorys.conversation_memory import ConversationMemory

def main():
    # 1. 加载配置
    config.load_file("data/configs/config.json")
    config.start_watch_thread()
    log_init_from_config(config)

    # 2. 创建客户端
    client = DeepSeekClient(module_id=1)

    # 3. 对话记忆
    mem = ConversationMemory(system_prompt="You are a helpful assistant")
    mem.add_user_message("Hello")
    log_bind(request_id="abc123")

    # 4. 调用 API
    resp = client.chat_completion(
        messages=mem.get_messages_for_api(),
        user_id="demo_user",
        temperature=0.7
    )
    reply = resp["choices"][0]["message"]["content"]
    mem.add_assistant_message(reply)

    # 5. 保存
    mem.save_to_file("data/conversations/")
    log_info("对话完成", reply=reply[:100])

if __name__ == "__main__":
    main()
```

---

## 十、常见问题

**Q: 运行 `python src/main.py` 报 `ModuleNotFoundError: No module named 'src'`？**  
A: 请使用 `python -m src.main` 或在脚本开头添加 `sys.path.insert(0, str(Path(__file__).parent.parent))`。

**Q: 如何配置 API 密钥？**  
A: 运行 `python configure_api_keys.py` 交互式添加。

**Q: 如何调试对话消息？**  
A: 设置日志级别为 `DEBUG`，框架会自动打印每次请求的完整消息列表。

**Q: 工具调用失败怎么办？**  
A: 可使用 `PromptBuilder.tool_recovery` 生成恢复提示，或让模型重新尝试。

**Q: 如何接入其他 LLM（如 OpenAI）？**  
A: 实现 `BaseLLMClient` 子类，复用 `KeyManager` 和 `ConversationMemory`。

---

## 十一、版本与兼容性

- Python 3.14+
- 依赖：`openai`, `cryptography`, `requests`, `pyyaml`（可选）, `portalocker`（可选）, `colorama`（可选）
- 操作系统：Windows 11 / Linux / macOS

---

**文档版本**：1.0  
**最后更新**：2026-06-10