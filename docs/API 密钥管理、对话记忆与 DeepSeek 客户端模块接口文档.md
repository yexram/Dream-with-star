# API 密钥管理、对话记忆与 DeepSeek 客户端模块接口文档

## 概述

本文档描述了四个对话系统核心模块：

- **`KeyManager`**（位于 `api_manager.py`）：多模块、多 API 密钥管理器，支持加密存储、加权轮询/故障转移策略、热重载及自动禁用失效密钥。
- **`ConversationMemory`**（位于 `conversation_memory.py`）：轻量级对话记忆容器，支持 system prompt、用户/助手/工具消息的追加与清理，并自动过滤推理内容。
- **`DeepSeekClient`**（位于 `deepseek_client.py`）：DeepSeek API 客户端，支持流式/非流式响应、自动工具调用循环、智能重试（指数退避）、本地令牌桶限速及对话记录保存。
- **`PromptBuilder`**（位于 `prompt_builder.py`）：静态方法集合，用于快速构建常用提示词模板（角色扮演、摘要、翻译）。

所有模块均线程安全，适用于 Python 3.8+，已在 Windows 11 / Linux 环境下测试。

---

## 模块依赖

| 依赖库 | 版本 | 用途 | 是否必须 |
|--------|------|------|----------|
| `cryptography` | ≥41.0.0 | API 密钥的加密存储（Fernet 对称加密） | 是 |
| `openai` | ≥1.0.0 | DeepSeek API 调用（兼容 OpenAI SDK） | 仅 `DeepSeekClient` 必需 |
| `src.tools.config_manager` | 项目内置 | 读取加密密钥文件路径等配置 | 是 |
| `src.tools.logger` | 项目内置 | 日志记录 | 是 |

> 注：`cryptography` 需手动安装 `pip install cryptography`；`openai` 需手动安装 `pip install openai`。

---

# 一、密钥管理器 (`KeyManager`)

位于 `src.conversation.api_manager`，全局单例 `key_manager` 已自动创建。

## 1.1 初始化与配置

```python
from src.conversation.api_manager import key_manager
```

管理器启动时自动执行以下步骤：

1. 从 `config_manager` 读取配置项 `encryption.master_key_file`（默认 `data/secrets/master.key`）和 `api_keys_file`（默认 `data/secrets/api_keys.json`）。
2. 若主密钥文件不存在则自动生成并保存。
3. 加载并解密 `api_keys.json` 中的密钥数据。
4. 注册配置热重载回调（当 `api_keys_file` 配置变更时自动重新加载）。

### 密钥文件格式 (`api_keys.json`)

```json
{
  "modules": {
    "1": {
      "strategy": "weighted_round_robin",
      "keys": [
        {
          "id": "primary_key",
          "encrypted": "gAAAAAB...",
          "weight": 3,
          "enabled": true
        }
      ]
    }
  }
}
```

- `modules` 键为模块 ID（整数），每个模块独立配置。
- `strategy`：`"weighted_round_robin"`（加权轮询）或 `"failover"`（故障转移，顺序使用）。
- `keys` 列表中的 `encrypted` 字段使用主密钥加密，存储时不存明文。

## 1.2 核心 API

### 获取模块的下一个密钥
```python
api_key = key_manager.get_next_key(module_id=1)
if api_key is None:
    print("无可用密钥")
```

### 上报密钥调用失败（自动禁用）
```python
key_manager.mark_key_failure(module_id=1, key_plain=api_key)
```
- 连续失败 3 次后，该密钥的 `enabled` 被设为 `false`，并触发选择器重建。
- 失败计数保存在内存中，同时更新到 `api_keys.json`。

### 手动注册新模块
```python
key_manager.register_module(module_id=2, strategy="failover")
```

### 为模块添加新密钥（明文，自动加密存储）
```python
key_manager.add_key(module_id=1, api_key_plain="sk-xxxxx", key_id="my_key", weight=2)
```
- `key_id` 可选，缺省自动生成。
- 添加后自动重建选择器并保存至文件。

### 手动重新加载密钥文件
```python
key_manager.reload()
```

## 1.3 高级组件（可独立使用）

### 加密管理器 `EncryptionManager`
```python
from src.conversation.api_manager import EncryptionManager, MasterKeyManager

# 生成新密钥
key = EncryptionManager.generate_key()

# 加密/解密
crypto = EncryptionManager(key)
enc = crypto.encrypt("my_secret")
dec = crypto.decrypt(enc)
```

### 选择器 `WeightedRoundRobinSelector` / `FailoverSelector`
```python
keys = [{"plain": "key1", "weight": 3, "enabled": True}, ...]
selector = WeightedRoundRobinSelector(keys)
next_key = selector.next_key()
```

## 1.4 功能边界

### ✅ 能做什么
- 多模块独立管理 API 密钥。
- 密钥文件 AES 加密存储（Fernet）。
- 支持加权轮询和故障转移策略。
- 自动禁用连续失败的密钥（失败计数≥3）。
- 配置热重载（监听 `api_keys_file` 变化，或手动调用 `reload()`）。
- 线程安全的密钥获取与更新。

### ❌ 不能做什么
- **不支持动态更改策略**（如需变更必须通过 `register_module` 重新注册，或直接修改文件后 `reload`）。
- **不支持密钥自动轮换**（如按时间过期重新生成）。
- **不支持分布式环境**（多进程各自独立维护自己的密钥状态；失败计数不同步）。
- **加密主密钥以明文文件存储**（生产环境建议使用硬件安全模块或环境变量注入）。

---

# 二、对话记忆 (`ConversationMemory`)

轻量级容器，用于维护单轮或多轮对话的消息列表。

## 2.1 初始化

```python
from src.conversation.conversation_memory import ConversationMemory

# 无 system prompt
mem = ConversationMemory()

# 带 system prompt
mem = ConversationMemory(system_prompt="你是一个乐于助人的助手。")
```

## 2.2 核心 API

### 追加消息
```python
mem.add_user_message("今天天气怎么样？")
mem.add_assistant_message("今天晴天，温度25度。", reasoning_content="用户询问天气...")
mem.add_tool_result(tool_call_id="call_123", content='{"weather": "sunny"}')
```

### 获取发送给 API 的消息列表（自动过滤）
```python
messages = mem.get_messages_for_api()
```
- 自动移除 assistant 消息中的 `reasoning_content` 字段（**仅当该消息之后没有紧跟着 tool 消息时**，因为带工具调用的 reasoning 可能需要保留，但标准 API 要求过滤）。

### 清空对话（保留 system prompt）
```python
mem.clear()
```

### 序列化 / 反序列化
```python
data = mem.to_dict()                      # {"messages": [...]}
new_mem = ConversationMemory.from_dict(data)
```

## 2.3 功能边界

### ✅ 能做什么
- 管理 user / assistant / system / tool 四种角色消息。
- 支持可选的推理内容（`reasoning_content`）和工具调用（`tool_calls`）。
- 自动过滤 API 不接受的字段。
- 浅拷贝消息用于导出（修改返回列表不影响原记忆）。

### ❌ 不能做什么
- **无上下文长度管理**（不自动截断或摘要，由调用方负责）。
- **不支持消息 ID 或时间戳**。
- **不提供差分更新**（每次需传递完整消息列表）。

---

# 三、DeepSeek 客户端 (`DeepSeekClient`)

DeepSeek API 封装，支持多种高级特性。

## 3.1 初始化

```python
from src.conversation.deepseek_client import DeepSeekClient

client = DeepSeekClient(
    module_id=1,                          # 对应 KeyManager 中的模块 ID
    base_url="https://api.deepseek.com",  # 可自定义代理地址
    timeout=60,
    max_retries=3,
    save_conversations_dir="data/conversations",  # 设为 None 则不保存
    rate_limit_per_second=5.0,            # 每秒最多 5 个请求（令牌桶）
)
```

## 3.2 核心 API

### 非流式对话
```python
response = client.chat_completion(
    messages=[{"role": "user", "content": "你好"}],
    model="deepseek-v4-pro",
    user_id="user_123",                   # 可选，用于 API 统计
    reasoning_effort="medium",            # 可选
    thinking_enabled=True,                # 启用思考模式
    temperature=0.7,
    max_tokens=2000,
    extra_body={"stop": ["END"]}          # 额外参数
)
print(response["choices"][0]["message"]["content"])
```

### 流式对话（生成器）
```python
for chunk in client.chat_completion(
    messages=messages,
    model="deepseek-v4-pro",
    stream=True
):
    delta = chunk["choices"][0]["delta"]
    if delta.get("content"):
        print(delta["content"], end="")
    if delta.get("reasoning_content"):
        print(f"[思考]{delta['reasoning_content']}[/思考]")
```

### 带自动工具调用的对话
```python
def execute_tool(tool_name: str, arguments: dict) -> str:
    if tool_name == "get_weather":
        return "晴天，25度"
    return "未知工具"

response = client.chat_with_tools(
    messages=[{"role": "user", "content": "北京天气如何？"}],
    tools=[
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "获取天气",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}
            }
        }
    ],
    tool_executor=execute_tool,
    max_tool_rounds=5
)
```

### 手动限速器（令牌桶，可独立使用）
```python
from src.conversation.deepseek_client import TokenBucketLimiter

limiter = TokenBucketLimiter(rate=10.0)   # 每秒10个令牌
if limiter.acquire(block=True):
    # 执行请求
    pass
```

## 3.3 重试与故障转移机制

- 每次调用前通过 `key_manager.get_next_key(module_id)` 获取一个密钥。
- 若请求失败且状态码为 429/500/503（可重试），则标记该密钥失败（连续失败计数+1），等待指数退避（2^attempt 秒，上限 10 秒），然后切换下一个密钥重试。
- 若状态码为 400/401/402/422（致命错误），同样标记失败并切换密钥，但不等待退避。
- 重试次数由 `max_retries` 控制，耗尽后抛出异常。

## 3.4 对话记录保存

当 `save_conversations_dir` 非 `None` 时，每次非流式调用或流式调用结束后，会将请求体和响应体保存为 JSON 文件，文件名格式 `YYYYMMDD_HHMMSS_mmm.json`。  
保存内容包含 `timestamp`、`request`、`response` 三个字段。

## 3.5 功能边界

### ✅ 能做什么
- 支持流式与非流式响应。
- 自动处理工具调用多轮循环（`chat_with_tools`）。
- 智能重试 + 指数退避 + 密钥切换。
- 本地令牌桶限速，避免触发 API 限流。
- 自动保存请求/响应对，便于调试。
- 支持 `reasoning_content`（思考内容）和 `thinking` 模式。

### ❌ 不能做什么
- **不支持同步流式重试**（一旦流开始，如果中途失败无法自动恢复，需上层处理）。
- **不支持自动处理 `tool_calls` 中的并行调用**（当前顺序执行）。
- **不支持取消进行中的请求**（无超时中断机制，依赖 `timeout` 参数）。
- **`save_conversations_dir` 目录不会自动清理**，需外部维护磁盘空间。

---

# 四、提示词构建器 (`PromptBuilder`)

纯静态工具类，提供几个常用的提示词模板。

## 4.1 API

### 角色扮演
```python
from src.conversation.prompt_builder import PromptBuilder

prompt = PromptBuilder.role_play("一名资深程序员", "回答要专业且简洁")
# 输出: "你现在扮演 一名资深程序员。 回答要专业且简洁"
```

### 摘要
```python
prompt = PromptBuilder.summary("很长的一段文字...", max_length=100)
# 输出: "请将以下内容概括为不超过 100 字：\n很长的一段文字..."
```

### 翻译
```python
prompt = PromptBuilder.translate("Hello world", target_lang="法语")
# 输出: "请将以下文本翻译成法语：\nHello world"
```

## 4.2 功能边界

### ✅ 能做什么
- 快速生成符合常用场景的提示词。

### ❌ 不能做什么
- **不提供模板变量替换**（如 `{name}` 占位符）。
- **不支持自定义模板加载**（仅三个固定模板）。

---

# 五、快速上手示例

```python
# example.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from src.tools.config_manager import config
from src.tools.logger import log_init_from_config, log_info
from src.conversation.api_manager import key_manager
from src.conversation.deepseek_client import DeepSeekClient
from src.conversation.conversation_memory import ConversationMemory

def main():
    # 1. 加载全局配置（需提前准备 data/configs/config.json）
    config.load_file("data/configs/config.json")
    log_init_from_config(config)

    # 2. 注册模块并添加密钥（首次使用）
    key_manager.register_module(module_id=1, strategy="weighted_round_robin")
    key_manager.add_key(module_id=1, api_key_plain="sk-your-actual-key", key_id="main_key", weight=3)

    # 3. 创建客户端和对话记忆
    client = DeepSeekClient(module_id=1, save_conversations_dir="data/conversations")
    memory = ConversationMemory(system_prompt="你是一个有帮助的AI助手。")

    # 4. 发起对话
    memory.add_user_message("介绍一下Python装饰器")
    response = client.chat_completion(messages=memory.get_messages_for_api())
    assistant_reply = response["choices"][0]["message"]["content"]
    memory.add_assistant_message(assistant_reply)

    log_info("对话完成", reply=assistant_reply[:100])

if __name__ == "__main__":
    main()
```

---

# 六、常见问题（FAQ）

**Q: `key_manager.get_next_key()` 返回 `None` 怎么办？**  
A: 检查对应模块是否已注册，且至少有一个密钥的 `enabled` 为 `true`。可使用 `key_manager.register_module()` 和 `key_manager.add_key()` 添加。

**Q: 密钥文件丢失或损坏会怎样？**  
A: 程序启动时会自动创建空的密钥文件（仅包含 `{"modules":{}}`）。若解密已存在的文件失败，会抛出异常并记录错误日志，需检查主密钥是否匹配。

**Q: 如何更换主密钥？**  
A: 删除 `data/secrets/master.key` 文件后重启程序，会自动生成新密钥，**但旧加密的 api_keys.json 将无法解密**。需提前用旧密钥解密所有密钥，再用新密钥重新加密保存。

**Q: 流式对话中途网络断开，如何恢复？**  
A: 客户端不提供自动恢复，建议上层捕获异常后使用 `chat_completion`（非流式）重试，或重新建立流式请求。

**Q: `chat_with_tools` 中工具执行出错怎么办？**  
A: `tool_executor` 应捕获异常并返回错误字符串（如 `"执行失败：连接超时"`），DeepSeek 模型会看到该内容并可能给出相应回复。

**Q: 对话记忆中的 `reasoning_content` 为什么有时被过滤掉？**  
A: 根据 DeepSeek API 规范，assistant 消息如果紧接着 tool 消息，必须保留 `reasoning_content`；否则应移除。`get_messages_for_api()` 实现了这一规则。

**Q: 限速器 `TokenBucketLimiter` 是进程安全的吗？**  
A: 不是，仅线程安全。多进程场景需各自维护自己的限速器实例。

---

## 版本与兼容性

- Python 版本：3.8 – 3.14
- 操作系统：Windows 11 / Linux / macOS（部分功能如独立控制台窗口仅 Windows，本模块不涉及）
- 测试依赖：`openai>=1.0.0`, `cryptography>=41.0.0`

**文档更新日期**：2026-06-09