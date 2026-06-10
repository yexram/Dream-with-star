"""
对话记忆管理
- 存储消息列表
- 支持保存到文件
- 支持序列化/反序列化
"""

from typing import List, Dict, Optional, Any
from copy import deepcopy
import json
from pathlib import Path
from datetime import datetime


class ConversationMemory:
    """
    管理一个对话上下文的消息历史
    """

    def __init__(self, system_prompt: Optional[str] = None):
        self.messages: List[Dict[str, Any]] = []
        if system_prompt:
            self.messages.append({"role": "system", "content": system_prompt})

    def add_user_message(self, content: str):
        """添加用户消息"""
        self.messages.append({"role": "user", "content": content})

    def add_assistant_message(
        self,
        content: str,
        reasoning_content: Optional[str] = None,
        tool_calls: Optional[List] = None
    ):
        """添加助手消息"""
        msg = {"role": "assistant", "content": content}
        if reasoning_content:
            msg["reasoning_content"] = reasoning_content
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self.messages.append(msg)

    def add_tool_result(self, tool_call_id: str, content: str):
        """添加工具执行结果"""
        self.messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": content})

    def get_messages_for_api(self) -> List[Dict]:
        """
        返回适合发送给 API 的消息（自动过滤不需要的 reasoning_content）
        规则：如果 assistant 消息之后紧跟 tool 消息，则保留 reasoning_content；否则删除
        """
        filtered = deepcopy(self.messages)
        for i, msg in enumerate(filtered):
            if msg.get("role") != "assistant":
                continue
            # 检查下一条是否是 tool 消息
            has_tool_next = (i + 1 < len(filtered) and filtered[i+1].get("role") == "tool")
            if not has_tool_next and "reasoning_content" in msg:
                del msg["reasoning_content"]
        return filtered

    def clear(self):
        """清空所有消息，但保留 system prompt"""
        system = None
        if self.messages and self.messages[0].get("role") == "system":
            system = self.messages[0]
        self.messages = []
        if system:
            self.messages.append(system)

    def to_dict(self) -> Dict:
        """序列化为字典"""
        return {"messages": self.messages}

    @classmethod
    def from_dict(cls, data: Dict):
        """从字典反序列化"""
        conv = cls()
        conv.messages = data.get("messages", [])
        return conv

    def save_to_file(self, file_path: str, append_timestamp: bool = True) -> str:
        """
        将当前对话保存到 JSON 文件
        :param file_path: 文件路径（可以是目录或完整文件路径）
        :param append_timestamp: 是否在文件名后追加时间戳
        :return: 实际保存的文件路径
        """
        path = Path(file_path)
        if path.is_dir():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            filename = f"conversation_{timestamp}.json" if append_timestamp else "conversation.json"
            path = path / filename
        else:
            if append_timestamp:
                stem = path.stem
                suffix = path.suffix
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
                path = path.parent / f"{stem}_{timestamp}{suffix}"

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
        return str(path)