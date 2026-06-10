"""
提示词模板工具
提供常用提示词构建方法
"""

class PromptBuilder:
    @staticmethod
    def role_play(role: str, instruction: str = "") -> str:
        """角色扮演提示词"""
        base = f"你现在扮演 {role}。"
        if instruction:
            base += f" {instruction}"
        return base

    @staticmethod
    def summary(text: str, max_length: int = 200) -> str:
        """摘要提示词"""
        return f"请将以下内容概括为不超过 {max_length} 字：\n{text}"

    @staticmethod
    def translate(text: str, target_lang: str = "中文") -> str:
        """翻译提示词"""
        return f"请将以下文本翻译成{target_lang}：\n{text}"

    @staticmethod
    def safety_guard() -> str:
        """内容安全前置提示"""
        return "你是一个安全、有益、无害的助手。请不要生成任何违反法律法规的内容。"

    @staticmethod
    def tool_recovery(tool_name: str, error: str) -> str:
        """工具调用失败恢复提示"""
        return f"调用工具 {tool_name} 时发生错误：{error}。请尝试其他方法或告知用户。"