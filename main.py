from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
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

# 重试与备用模型切换由插件统一负责，因此把 AstrBot provider 内部的传输层
# 重试次数压到 1（仍保留 429 换 Key、超长上下文裁剪等智能恢复逻辑），
# 避免「插件重试 × provider 重试 × SDK 自带重试」三层相乘放大上游请求数。
INNER_REQUEST_MAX_RETRIES = 1

# 部分中转站会把上游错误当成正常回复正文返回（HTTP 200 + 错误文本），
# 这类响应必须按失败处理，否则会把报错原样发给用户。
UPSTREAM_ERROR_PREFIXES = (
    "上游错误",
    "上游异常",
    "upstream error",
    "error:",
)


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
    "1.6.1",
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

        custom_rule = self._get_custom_rule(event.unified_msg_origin)
        # 候选顺序：UMO 规则 / 插件配置 / 会话主 AI，其后依次是备用模型列表。
        providers = self._resolve_polish_providers(event, custom_rule)
        if not providers:
            logger.warning("[chat_polisher] 未找到可用提供商，跳过润色。")
            return
        prompt_tpl = self._resolve_polish_prompt(custom_rule)

        token = _POLISHING_GUARD.set(True)
        try:
            success, new_chain = await self._polish_chain_segments(
                providers, result.chain, prompt_tpl
            )
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

    def _get_custom_rule(self, umo: str) -> dict[str, Any] | None:
        """Find the first plugin rule whose UMO exactly matches the event UMO."""
        rules = self.config.get("custom_rules", [])
        if not isinstance(rules, list):
            return None

        umo = str(umo or "").strip()
        if not umo:
            return None

        for rule in rules:
            if not isinstance(rule, dict):
                continue
            raw_rule_umo = rule.get("umo", "")
            if not isinstance(raw_rule_umo, str):
                continue
            rule_umo = raw_rule_umo.strip()
            if rule_umo == umo:
                return rule
        return None

    @staticmethod
    def _normalize_provider_ids(raw_value: Any) -> list[str]:
        """把配置中的模型 ID 列表规整为非空字符串列表。"""
        if not isinstance(raw_value, list):
            return []

        provider_ids: list[str] = []
        for item in raw_value:
            if isinstance(item, str) and item.strip():
                provider_ids.append(item.strip())
        return provider_ids

    def _get_fallback_provider_ids(
        self, custom_rule: dict[str, Any] | None
    ) -> list[str]:
        """解析备用模型列表：UMO 规则非空时整体覆盖全局配置。"""
        rule_ids = self._normalize_provider_ids(
            (custom_rule or {}).get("fallback_providers")
        )
        if rule_ids:
            return rule_ids
        return self._normalize_provider_ids(self.config.get("fallback_providers"))

    @staticmethod
    def _provider_identifier(provider: Any) -> str:
        """取模型标识用于去重；取不到配置 ID 时退回对象身份。"""
        provider_config = getattr(provider, "provider_config", None)
        if isinstance(provider_config, dict):
            provider_id = provider_config.get("id")
            if isinstance(provider_id, str) and provider_id:
                return provider_id
        return f"obj:{id(provider)}"

    @staticmethod
    def _supports_text_chat(provider: Any) -> bool:
        """只接受具备 text_chat 能力的聊天模型。"""
        return callable(getattr(provider, "text_chat", None))

    @staticmethod
    def _supports_request_max_retries(provider: Any) -> bool:
        """判断提供商是否接受 request_max_retries 参数。

        AstrBot 内置的 OpenAI/Anthropic/Gemini/Responses 源都支持该参数；
        旧版本或第三方自定义提供商可能不支持，此时只能沿用其内部重试策略。
        """
        try:
            parameters = inspect.signature(provider.text_chat).parameters
        except (TypeError, ValueError):
            return False
        if "request_max_retries" in parameters:
            return True
        return any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )

    def _append_candidate(
        self,
        candidates: list[TextChatProviderProtocol],
        seen: set[str],
        provider: Any,
        source: str,
    ) -> None:
        if provider is None:
            return
        if not self._supports_text_chat(provider):
            logger.warning(
                "[chat_polisher] 忽略不支持 text_chat 的提供商（%s）。", source
            )
            return

        identifier = self._provider_identifier(provider)
        if identifier in seen:
            return
        seen.add(identifier)
        candidates.append(provider)

    def _resolve_polish_providers(
        self,
        event: AstrMessageEvent,
        custom_rule: dict[str, Any] | None = None,
    ) -> list[TextChatProviderProtocol]:
        """构建有序去重的润色模型候选列表：主模型 + 备用模型。"""
        if custom_rule is None:
            custom_rule = self._get_custom_rule(event.unified_msg_origin)

        raw_rule_provider = (custom_rule or {}).get("polish_provider", "")
        raw_global_provider = self.config.get("polish_provider", "")
        configured_ids = [
            raw_rule_provider.strip() if isinstance(raw_rule_provider, str) else "",
            raw_global_provider.strip()
            if isinstance(raw_global_provider, str)
            else "",
        ]

        primary: Any = None
        for provider_id in configured_ids:
            if not provider_id:
                continue
            provider = self.context.get_provider_by_id(provider_id=provider_id)
            if provider:
                primary = provider
                break
            logger.warning(
                "[chat_polisher] Configured provider not found: %s.", provider_id
            )

        if primary is None:
            # 没有可用配置时回落到会话主 AI。
            try:
                primary = self.context.get_using_provider(
                    umo=event.unified_msg_origin
                )
            except ValueError:
                logger.exception("[chat_polisher] 获取会话主 AI 失败。")
                primary = None

        candidates: list[TextChatProviderProtocol] = []
        seen: set[str] = set()
        self._append_candidate(candidates, seen, primary, "主润色模型")

        for fallback_id in self._get_fallback_provider_ids(custom_rule):
            provider = self.context.get_provider_by_id(provider_id=fallback_id)
            if not provider:
                logger.warning(
                    "[chat_polisher] 备用润色模型不存在，已跳过：%s。", fallback_id
                )
                continue
            self._append_candidate(
                candidates, seen, provider, f"备用模型 {fallback_id}"
            )

        return candidates

    def _resolve_polish_provider(
        self,
        event: AstrMessageEvent,
        custom_rule: dict[str, Any] | None = None,
    ) -> TextChatProviderProtocol | None:
        """兼容入口：仅返回候选列表中的第一个模型。"""
        providers = self._resolve_polish_providers(event, custom_rule)
        return providers[0] if providers else None

    def _resolve_polish_prompt(self, custom_rule: dict[str, Any] | None) -> str:
        """Resolve the prompt from UMO, plugin, and built-in defaults."""
        raw_rule_prompt = (custom_rule or {}).get("polish_prompt", "")
        raw_global_prompt = self.config.get("polish_prompt", "")
        prompt_tpl = raw_rule_prompt.strip() if isinstance(raw_rule_prompt, str) else ""
        if not prompt_tpl:
            prompt_tpl = (
                raw_global_prompt.strip()
                if isinstance(raw_global_prompt, str)
                else ""
            )
        return prompt_tpl or DEFAULT_POLISH_PROMPT

    @staticmethod
    def _build_user_prompt(prompt_tpl: str, text: str) -> str:
        """支持 {{text}} 占位符；无占位符时自动拼接原文。"""
        if "{{text}}" in prompt_tpl:
            return prompt_tpl.replace("{{text}}", text)
        return f"{prompt_tpl}\n\n待润色文本：\n{text}"

    @staticmethod
    def _looks_like_upstream_error(text: str) -> bool:
        """识别「把上游错误当正文返回」的响应，避免把报错发给用户。"""
        stripped = text.strip()
        if not stripped:
            return False
        # 去掉中转站常见的包裹符号，例如 [上游错误: {...}]、【...】
        if len(stripped) >= 2 and (stripped[0], stripped[-1]) in {
            ("[", "]"),
            ("【", "】"),
        }:
            stripped = stripped[1:-1].strip()

        lowered = stripped.lower()
        for prefix in UPSTREAM_ERROR_PREFIXES:
            if lowered.startswith(prefix):
                return True

        try:
            payload = json.loads(stripped)
        except (TypeError, ValueError):
            return False
        return isinstance(payload, dict) and "error" in payload

    async def _call_provider_once(
        self,
        provider: TextChatProviderProtocol,
        user_prompt: str,
        timeout: float,
    ) -> str | None:
        """单次调用模型；超时、异常或空文本都视为本次失败并返回 None。"""
        chat_kwargs: dict[str, Any] = {
            "prompt": user_prompt,
            "context": [],
            "system_prompt": "你是一个只输出最终润色文本的助手。",
        }
        if self._supports_request_max_retries(provider):
            # 重试权收归插件，避免与 provider 内部的传输层重试相乘。
            chat_kwargs["request_max_retries"] = INNER_REQUEST_MAX_RETRIES

        try:
            resp = await asyncio.wait_for(
                provider.text_chat(**chat_kwargs),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("[chat_polisher] 润色超时。")
            return None
        except asyncio.CancelledError:
            # 取消不重试，直接向上抛出。
            raise
        except Exception:
            logger.exception("[chat_polisher] 调用润色模型失败")
            return None

        if resp is not None and getattr(resp, "role", "assistant") == "err":
            logger.warning("[chat_polisher] 润色模型返回错误响应（role=err）。")
            return None

        polished = (resp.completion_text or "").strip() if resp else ""
        if not polished:
            logger.warning("[chat_polisher] 润色模型返回空文本。")
            return None
        if self._looks_like_upstream_error(polished):
            logger.warning(
                "[chat_polisher] 润色模型返回上游错误文本，按失败处理：%s",
                polished[:200],
            )
            return None
        return polished

    async def _polish_text(
        self,
        provider: TextChatProviderProtocol,
        text: str,
        prompt_tpl: str | None = None,
    ) -> str | None:
        """单模型单次调用，失败时返回 None（保留原有语义）。"""
        if prompt_tpl is None:
            prompt_tpl = self._resolve_polish_prompt(None)

        user_prompt = self._build_user_prompt(prompt_tpl, text)
        return await self._call_provider_once(
            provider, user_prompt, self._get_timeout_seconds()
        )

    async def _polish_text_with_fallback(
        self,
        providers: list[TextChatProviderProtocol],
        text: str,
        prompt_tpl: str,
        start_index: int = 0,
        deadline: float | None = None,
    ) -> tuple[str | None, int]:
        """按顺序尝试候选模型，每个模型最多尝试配置的次数。

        返回 ``(润色文本, 成功的模型下标)``；全部失败时文本为 None。
        ``start_index`` 用于在同一消息链里粘滞已经成功过的模型下标。
        """
        user_prompt = self._build_user_prompt(prompt_tpl, text)
        attempt_limit = self._get_max_attempts()
        per_attempt_timeout = self._get_timeout_seconds()

        for index in range(start_index, len(providers)):
            provider = providers[index]
            provider_id = self._provider_identifier(provider)

            for attempt in range(1, attempt_limit + 1):
                timeout = per_attempt_timeout
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        logger.warning(
                            "[chat_polisher] 润色总超时耗尽，停止尝试剩余模型。"
                        )
                        return None, index
                    timeout = min(per_attempt_timeout, remaining)

                polished = await self._call_provider_once(
                    provider, user_prompt, timeout
                )
                if polished:
                    return polished, index

                logger.warning(
                    "[chat_polisher] 润色模型 %s 第 %s/%s 次调用失败。",
                    provider_id,
                    attempt,
                    attempt_limit,
                )

            if index + 1 < len(providers):
                logger.warning(
                    "[chat_polisher] 润色模型 %s 连续 %s 次失败，切换到备用模型 %s。",
                    provider_id,
                    attempt_limit,
                    self._provider_identifier(providers[index + 1]),
                )

        return None, len(providers)

    async def _polish_chain_segments(
        self,
        providers: list[TextChatProviderProtocol],
        chain: MessageChain,
        prompt_tpl: str | None = None,
    ) -> tuple[bool, MessageChain]:
        """按连续 Plain 段润色，保持非文本组件的位置与顺序不变。

        同一条消息链共享一次总超时预算，并记住已经成功过的模型下标，
        避免后续段落反复重试已经失败的模型。
        """
        new_chain: MessageChain = []
        buffer: list[Comp.Plain] = []
        has_plain = False
        current_index = 0

        total_timeout = self._get_total_timeout_seconds()
        deadline = (
            time.monotonic() + total_timeout if total_timeout > 0 else None
        )

        async def flush_plain_buffer() -> bool:
            nonlocal buffer, current_index
            if not buffer:
                return True

            original_text = "\n".join(comp.text for comp in buffer).strip()
            if not original_text:
                new_chain.extend(buffer)
                buffer = []
                return True

            logger.info("[chat_polisher] 润色输入: %s", original_text)
            polished_text, current_index = await self._polish_text_with_fallback(
                providers,
                original_text,
                prompt_tpl,
                start_index=current_index,
                deadline=deadline,
            )
            if not polished_text:
                if self._get_failure_mode() == "fallback_original":
                    new_chain.extend(buffer)
                    buffer = []
                    return True
                buffer = []
                return False

            logger.info("[chat_polisher] 润色输出: %s", polished_text)
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

    def _get_total_timeout_seconds(self) -> float:
        """一次润色（含全部重试与备用模型）的总时间上限；<=0 表示不限制。"""
        raw_value = self.config.get("polish_total_timeout_seconds", 60)
        try:
            total_timeout = float(raw_value)
        except (TypeError, ValueError):
            total_timeout = 60.0
        return max(total_timeout, 0.0)

    def _get_max_attempts(self) -> int:
        """单个模型的最大尝试次数（含首次调用）。"""
        raw_value = self.config.get("polish_max_attempts", 3)
        try:
            attempts = int(raw_value)
        except (TypeError, ValueError):
            attempts = 3
        return max(1, min(attempts, 10))

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
