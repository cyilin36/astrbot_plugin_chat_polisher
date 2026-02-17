from __future__ import annotations

import asyncio
import contextvars
import time
from typing import Any, Protocol, TypeAlias

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

DEFAULT_MARK_RETENTION_SECONDS = 300.0
DEFAULT_MARK_CHECK_INTERVAL_SECONDS = 60.0


class ProviderResponseProtocol(Protocol):
    """Provider.text_chat 的最小返回协议。"""

    completion_text: str


class TextChatProviderProtocol(Protocol):
    """仅约束本插件用到的提供商能力（text_chat）。"""

    async def text_chat(
        self,
        *,
        prompt: str,
        context: list[dict[str, Any]],
        system_prompt: str,
    ) -> ProviderResponseProtocol: ...


MessageChain: TypeAlias = list[object]

_POLISHING_GUARD: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "chat_polisher_polishing",
    default=False,
)


@register(
    "astrbot_plugin_chat_polisher",
    "cyilin36",
    "在回复发送前调用指定提供商进行文本润色",
    "1.3",
    "https://github.com/cyilin36/astrbot_plugin_chat_polisher",
)
class ChatPolisherPlugin(Star):
    """在发送前润色 AI 回复文本。"""

    # 保留 @register 以兼容仍依赖装饰器注册的 AstrBot 版本。

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # key: 事件标识，value: 打标时间(monotonic)
        self._llm_marks: dict[str, float] = {}
        self._mark_cleanup_task: asyncio.Task[None] | None = None

    @filter.on_llm_request()
    async def mark_ai_reply_flow(self, event: AstrMessageEvent, _req: Any):
        """仅在默认 AI 对话链路触发时记录标记。"""
        self._ensure_mark_cleanup_task()
        key = self._build_event_mark_key(event)
        self._llm_marks[key] = time.monotonic()

    @filter.on_decorating_result(priority=100)
    async def force_polish_before_send(self, event: AstrMessageEvent):
        """发送前钩子：对消息链中的文本段进行润色并原位替换。"""
        if _POLISHING_GUARD.get():
            return

        self._ensure_mark_cleanup_task()
        # 无 AI 链路标记时直接跳过（如指令回复）。
        if not self._has_valid_llm_mark(event):
            return

        result = event.get_result()
        if not result or not getattr(result, "chain", None):
            return

        # 优先使用插件配置；未配置则回退当前会话主 AI。
        provider = self._resolve_polish_provider(event)
        if not provider:
            logger.warning("[chat_polisher] 未找到可用提供商，跳过润色。")
            return

        token = _POLISHING_GUARD.set(True)
        try:
            success, new_chain = await self._polish_chain_segments(provider, result.chain)
        finally:
            _POLISHING_GUARD.reset(token)

        if not success:
            if self._get_failure_mode() == "send_error":
                result.chain = self._replace_plain_text(result.chain, self._get_failure_message())
            return

        if new_chain:
            result.chain = new_chain

    async def terminate(self):
        """插件停用时清理后台任务。"""
        task = self._mark_cleanup_task
        self._mark_cleanup_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._llm_marks.clear()

    def _resolve_polish_provider(self, event: AstrMessageEvent) -> TextChatProviderProtocol | None:
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

    async def _polish_text(self, provider: TextChatProviderProtocol, text: str) -> str | None:
        """调用提供商进行润色，失败时返回 None。"""
        prompt_tpl = str(self.config.get("polish_prompt", "") or "").strip()
        if not prompt_tpl:
            prompt_tpl = DEFAULT_POLISH_PROMPT

        # 支持 {{text}} 占位符；无占位符时自动拼接原文。
        if "{{text}}" in prompt_tpl:
            user_prompt = prompt_tpl.replace("{{text}}", text)
        else:
            user_prompt = f"{prompt_tpl}\n\n待润色文本：\n{text}"

        try:
            timeout = self._get_timeout_seconds()
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=user_prompt,
                    context=[],
                    system_prompt="你是一个只输出最终润色文本的助手。",
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("[chat_polisher] 润色超时。")
            return None
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[chat_polisher] 调用润色模型失败")
            return None

        polished = (resp.completion_text or "").strip() if resp else ""
        if not polished:
            logger.warning("[chat_polisher] 润色模型返回空文本，保留原文。")
            return None
        return polished

    async def _polish_chain_segments(
        self, provider: TextChatProviderProtocol, chain: MessageChain
    ) -> tuple[bool, MessageChain]:
        """按连续 Plain 段润色，保持非文本组件的位置与顺序不变。"""
        new_chain: MessageChain = []
        buffer: list[Comp.Plain] = []
        has_plain = False

        async def flush_plain_buffer() -> bool:
            nonlocal buffer
            if not buffer:
                return True

            original_text = "\n".join(comp.text for comp in buffer).strip()
            if not original_text:
                new_chain.extend(buffer)
                buffer = []
                return True

            polished_text = await self._polish_text(provider, original_text)
            if not polished_text:
                if self._get_failure_mode() == "fallback_original":
                    new_chain.extend(buffer)
                    buffer = []
                    return True
                buffer = []
                return False

            new_chain.append(Comp.Plain(polished_text))
            buffer = []
            return True

        for comp in chain:
            if isinstance(comp, Comp.Plain):
                has_plain = True
                buffer.append(comp)
                continue

            ok = await flush_plain_buffer()
            if not ok:
                return False, chain
            new_chain.append(comp)

        ok = await flush_plain_buffer()
        if not ok:
            return False, chain

        if not has_plain:
            return True, chain
        return True, new_chain

    @staticmethod
    def _replace_plain_text(chain: MessageChain, polished_text: str) -> MessageChain:
        """将原有 Plain 文本替换为一段润色文本，保留非文本消息段。"""
        new_chain: MessageChain = []
        replaced = False

        for comp in chain:
            if isinstance(comp, Comp.Plain):
                if not replaced:
                    new_chain.append(Comp.Plain(polished_text))
                    replaced = True
                continue
            new_chain.append(comp)

        if not replaced:
            new_chain.insert(0, Comp.Plain(polished_text))

        return new_chain

    def _get_timeout_seconds(self) -> float:
        raw_value = self.config.get("polish_timeout_seconds", 12)
        try:
            timeout = float(raw_value)
        except (TypeError, ValueError):
            timeout = 12.0
        return max(timeout, 0.1)

    def _get_failure_mode(self) -> str:
        mode = str(self.config.get("failure_mode", "发送原文（推荐）") or "").strip()
        mode_mapping = {
            "fallback_original": "fallback_original",
            "send_error": "send_error",
            "发送原文（推荐）": "fallback_original",
            "发送失败提示": "send_error",
        }
        return mode_mapping.get(mode, "fallback_original")

    def _get_failure_message(self) -> str:
        message = str(self.config.get("failure_message", "润色失败，请检查日志。") or "").strip()
        return message or "润色失败，请检查日志。"

    def _build_event_mark_key(self, event: AstrMessageEvent) -> str:
        # 优先使用消息来源 + message_id，避免并发会话互相影响。
        message_id = str(
            getattr(getattr(event, "message_obj", None), "message_id", "") or ""
        ).strip()
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()

        if message_id or umo:
            return f"{umo}::{message_id}"
        return f"event::{id(event)}"

    def _has_valid_llm_mark(self, event: AstrMessageEvent) -> bool:
        key = self._build_event_mark_key(event)
        marked_at = self._llm_marks.get(key)
        if marked_at is None:
            return False

        ttl = self._get_mark_retention_seconds()
        if time.monotonic() - marked_at > ttl:
            self._llm_marks.pop(key, None)
            return False
        return True

    def _ensure_mark_cleanup_task(self):
        if self._mark_cleanup_task and not self._mark_cleanup_task.done():
            return

        try:
            self._mark_cleanup_task = asyncio.create_task(self._mark_cleanup_loop())
        except RuntimeError:
            # 事件循环未就绪时，后续钩子会再次尝试启动。
            self._mark_cleanup_task = None

    async def _mark_cleanup_loop(self):
        # 周期清理过期标记，防止异常流程导致残留。
        try:
            while True:
                interval = self._get_mark_check_interval_seconds()
                await asyncio.sleep(interval)
                self._cleanup_expired_marks()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[chat_polisher] 标记清理任务异常退出")

    def _cleanup_expired_marks(self):
        if not self._llm_marks:
            return

        now = time.monotonic()
        ttl = self._get_mark_retention_seconds()
        expired_keys = [key for key, ts in self._llm_marks.items() if now - ts > ttl]
        for key in expired_keys:
            self._llm_marks.pop(key, None)

    def _get_mark_retention_seconds(self) -> float:
        raw_value = self.config.get("mark_retention_seconds", DEFAULT_MARK_RETENTION_SECONDS)
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            value = DEFAULT_MARK_RETENTION_SECONDS
        return max(value, 10.0)

    def _get_mark_check_interval_seconds(self) -> float:
        raw_value = self.config.get(
            "mark_check_interval_seconds", DEFAULT_MARK_CHECK_INTERVAL_SECONDS
        )
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            value = DEFAULT_MARK_CHECK_INTERVAL_SECONDS
        return max(value, 1.0)
