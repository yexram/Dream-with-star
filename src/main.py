# main.py
"""
交互式角色扮演对话程序
用法: python main.py
启动后输入角色提示词，然后进入对话循环
输入 'exit' 或 'quit' 退出
"""

import sys
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from src.tools.config_manager import config
from src.tools.logger import (
    log_init_from_config, log_info, log_debug, log_error, log_warning,
    log_bind, log_unbind, log_set_level
)
from src.conversation.deepseek_client import DeepSeekClient
from src.memorys.conversation_memory import ConversationMemory
from src.conversation.prompt_builder import PromptBuilder


def get_role_prompt() -> str:
    """交互式获取用户提供的角色扮演提示词"""
    print("=" * 60)
    print("欢迎使用 AI 角色扮演对话系统")
    print("=" * 60)
    print("\n请输入角色扮演提示词（例如：你现在扮演一位幽默的英语老师）")
    print("提示词将作为 system prompt，影响 AI 的回复风格。")
    print("-" * 60)
    prompt = input("提示词: ").strip()
    if not prompt:
        # 默认提示词
        prompt = "你是一个乐于助人的AI助手。"
        print(f"使用默认提示词: {prompt}")
    return prompt


def main():
    # 1. 加载配置
    config.load_file("data/configs/config.json")
    config.start_watch_thread(interval=2.0)

    # 2. 初始化日志，设置为 DEBUG 以便查看对话上下文
    log_init_from_config(config)
    log_set_level("DEBUG")

    # 3. 创建 DeepSeek 客户端
    try:
        client = DeepSeekClient(module_id=10001)
    except Exception as e:
        log_error("初始化客户端失败，请先运行 configure_api_keys.py 配置密钥", error=str(e))
        print("\n请先运行 python configure_api_keys.py 配置 API 密钥")
        return

    # 4. 获取角色扮演提示词
    role_prompt = get_role_prompt()

    # 5. 创建对话记忆（使用角色提示作为 system prompt，并添加安全前置）
    system_prompt = role_prompt + " " + PromptBuilder.safety_guard()
    memory = ConversationMemory(system_prompt=system_prompt)

    # 绑定 request_id 用于日志追踪
    log_bind(request_id="interactive_session")

    log_info("角色扮演对话开始", system_prompt=role_prompt[:100])

    print("\n" + "=" * 60)
    print("对话已开始！输入 'exit' 或 'quit' 退出，输入 'clear' 清空对话历史。")
    print("=" * 60 + "\n")

    # 6. 对话循环
    while True:
        try:
            user_input = input("\n[你]: ").strip()
            if not user_input:
                continue
            if user_input.lower() in ('exit', 'quit'):
                print("再见！")
                break
            if user_input.lower() == 'clear':
                memory.clear()
                print("对话历史已清空（保留 system prompt）。")
                continue

            # 添加用户消息
            memory.add_user_message(user_input)

            # 打印当前对话上下文（DEBUG 级别）
            log_debug("对话上下文", messages=memory.get_messages_for_api())

            # 调用 API
            response = client.chat_completion(
                messages=memory.get_messages_for_api(),
                user_id="interactive_user",   # 可根据需要修改
                temperature=0.7,
                max_tokens=1024,
                stream=False
            )

            # 提取回复内容和推理内容（如有）
            assistant_msg = response["choices"][0]["message"]["content"]
            reasoning = response["choices"][0]["message"].get("reasoning_content")

            # 添加到记忆
            memory.add_assistant_message(assistant_msg, reasoning_content=reasoning)

            # 输出回复
            print(f"\n[AI]: {assistant_msg}")

            # 可选：如果有推理内容且需要显示，可取消注释下面一行
            # if reasoning:
            #     print(f"\n[推理过程]: {reasoning}")

            # DEBUG 输出完整响应结构（可选）
            log_debug("API 完整响应", response_id=response.get("id"), usage=response.get("usage"))

        except KeyboardInterrupt:
            print("\n\n用户中断，正在保存对话...")
            break
        except Exception as e:
            log_error("对话出错", error=str(e))
            print(f"\n出错了: {e}，是否继续？(y/n)")
            if input().lower() != 'y':
                break

    # 7. 保存对话记录
    if len(memory.messages) > 1:  # 至少有一条用户消息
        saved_path = memory.save_to_file("data/conversations/", append_timestamp=True)
        log_info(f"对话已保存至 {saved_path}")
        print(f"\n对话记录已保存至: {saved_path}")
    else:
        print("\n没有对话内容，未保存。")

    log_unbind("request_id")
    print("程序结束。")


if __name__ == "__main__":
    main()