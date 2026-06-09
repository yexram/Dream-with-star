# src/conversation/conversation_memory.py
from typing import List, Dict, Optional, Any
from copy import deepcopy

class ConversationMemory:
    def __init__(self, system_prompt: Optional[str] = None):
        self.messages: List[Dict[str, Any]] = []
        if system_prompt:
            self.messages.append({"role": "system", "content": system_prompt})

    def add_user_message(self, content: str):
        self.messages.append({"role": "user", "content": content})

    def add_assistant_message(self, content: str, reasoning_content: Optional[str] = None, tool_calls: Optional[List] = None):
        msg = {"role": "assistant", "content": content}
        if reasoning_content:
            msg["reasoning_content"] = reasoning_content
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self.messages.append(msg)

    def add_tool_result(self, tool_call_id: str, content: str):
        self.messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": content})

    def get_messages_for_api(self) -> List[Dict]:
        """返回适合发送给 API 的消息（自动过滤不需要的 reasoning_content）"""
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
        system = None
        if self.messages and self.messages[0].get("role") == "system":
            system = self.messages[0]
        self.messages = []
        if system:
            self.messages.append(system)

    def to_dict(self) -> Dict:
        return {"messages": self.messages}

    @classmethod
    def from_dict(cls, data: Dict):
        conv = cls()
        conv.messages = data.get("messages", [])
        return conv