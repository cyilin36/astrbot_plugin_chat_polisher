from __future__ import annotations

from astrbot.api import AstrBotConfig, logger
import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


DEFAULT_POLISH_PROMPT = (
    "你是一个专业的中文文本润色助手。"
    "请在不改变原意的前提下，优化表达的通顺度、清晰度与自然度。"
    "保持原有语气和信息完整，不要增加新事实。"
    "只输出润色后的最终文本，不要解释。\n\n"
    "待润色文本：\n{{text}}"
)


@register(
    "astrbot_plugin_chat_polisher",
    "cyilin36",
    "在回复发送前调用指定提供商进行文本润色",
    "1.0",
    "https://github.com/cyilin36/astrbot_plugin_chat_polisher",
)
class ChatPolisherPlugin(Star):
    """在消息发送前强制二次调用模型进行文本润色。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    @filter.on_decorating_result(priority=100)
    async def force_polish_before_send(self, event: AstrMessageEvent):
        """发送前钩子：提取文本并替换为润色后的结果。"""
        result = event.get_result()
        if not result or not getattr(result, "chain", None):
            return

        # 仅提取消息链中的纯文本内容进行润色。
        plain_text = self._extract_plain_text(result.chain)
        if not plain_text.strip():
            return

        # 优先使用插件配置中的提供商；未配置则回退到会话主 AI。
        provider = self._resolve_polish_provider(event)
        if not provider:
            logger.warning("[chat_polisher] 未找到可用提供商，跳过润色。")
            return

        polished_text = await self._polish_text(provider, plain_text)
        if not polished_text:
            return

        # 替换链中的 Plain 文本，其他消息段（图片/at 等）保持不变。
        result.chain = self._replace_plain_text(result.chain, polished_text)

    def _resolve_polish_provider(self, event: AstrMessageEvent):
        """解析润色使用的提供商。"""
        provider_id = str(self.config.get("polish_provider", "") or "").strip()
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id=provider_id)
            if provider:
                return provider
            logger.warning(
                "[chat_polisher] 配置的提供商不存在: %s，将回退到当前会话主 AI。",
                provider_id,
            )

        return self.context.get_using_provider(umo=event.unified_msg_origin)

    async def _polish_text(self, provider, text: str) -> str | None:
        """调用提供商进行润色，失败时返回 None。"""
        prompt_tpl = str(self.config.get("polish_prompt", "") or "").strip()
        if not prompt_tpl:
            prompt_tpl = DEFAULT_POLISH_PROMPT

        # 兼容两种写法：
        # 1) 用户显式使用 {{text}} 占位符；
        # 2) 用户只写规则，不写占位符（自动拼接原文）。
        if "{{text}}" in prompt_tpl:
            user_prompt = prompt_tpl.replace("{{text}}", text)
        else:
            user_prompt = f"{prompt_tpl}\n\n待润色文本：\n{text}"

        try:
            resp = await provider.text_chat(
                prompt=user_prompt,
                context=[],
                system_prompt="你是一个只输出最终润色文本的助手。",
            )
        except Exception as exc:
            logger.error("[chat_polisher] 调用润色模型失败: %s", exc)
            return None

        polished = (resp.completion_text or "").strip() if resp else ""
        if not polished:
            logger.warning("[chat_polisher] 润色模型返回空文本，保留原文。")
            return None
        return polished

    @staticmethod
    def _extract_plain_text(chain: list) -> str:
        """从消息链中提取并拼接 Plain 文本。"""
        texts: list[str] = []
        for comp in chain:
            if isinstance(comp, Comp.Plain):
                texts.append(comp.text)
        return "".join(texts)

    @staticmethod
    def _replace_plain_text(chain: list, polished_text: str) -> list:
        """将原有 Plain 文本替换为一段润色文本，保留非文本消息段。"""
        first_plain_idx = None
        new_chain: list = []

        for idx, comp in enumerate(chain):
            if isinstance(comp, Comp.Plain):
                if first_plain_idx is None:
                    first_plain_idx = idx
                continue
            new_chain.append(comp)

        polished_comp = Comp.Plain(polished_text)
        if first_plain_idx is None:
            return [polished_comp] + new_chain

        new_chain.insert(min(first_plain_idx, len(new_chain)), polished_comp)
        return new_chain
