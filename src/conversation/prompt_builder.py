# src/conversation/prompt_builder.py
class PromptBuilder:
    @staticmethod
    def role_play(role: str, instruction: str = "") -> str:
        base = f"你现在扮演 {role}。"
        if instruction:
            base += f" {instruction}"
        return base

    @staticmethod
    def summary(text: str, max_length: int = 200) -> str:
        return f"请将以下内容概括为不超过 {max_length} 字：\n{text}"

    @staticmethod
    def translate(text: str, target_lang: str = "中文") -> str:
        return f"请将以下文本翻译成{target_lang}：\n{text}"