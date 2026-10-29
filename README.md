# 最终回复润色

在 AstrBot 发送回复前，对 AI 生成的文本做一次二次润色。

## 插件作用

- 只处理 AI 对话链路产生的回复文本。
- 指令触发的回复默认跳过，不进入二次润色。
- 只替换 `Plain` 文本段，图片、@、语音等富媒体消息段保留不变。

## 使用方法

### 安装

- 插件市场安装：在 AstrBot WebUI 的插件市场搜索“最终回复润色”并安装。
- 手动安装：

```bash
cd AstrBot/data/plugins
git clone https://github.com/cyilin36/astrbot_plugin_chat_polisher.git
```

安装后重启 AstrBot，或在插件管理页重载插件。

### 配置（WebUI）

在 `插件管理 -> 最终回复润色` 中配置：

- `polish_provider`：润色用模型提供商；留空时使用当前会话主 AI。
- `fallback_providers`：备用模型列表（有序多选）；主润色模型连续报错达到“单模型最大尝试次数”后，按此顺序依次切换。留空表示不启用备用模型。
- `polish_max_attempts`：单个模型的最大尝试次数（含首次调用），默认 3；用满仍失败就切换到下一个备用模型。
- `polish_prompt`：润色提示词；留空使用内置默认提示词。
- `custom_rules`：按 UMO 配置插件自己的润色规则。每条规则可单独设置 `umo`、`polish_provider`、`fallback_providers` 和 `polish_prompt`；UMO 需要与消息来源完全一致。规则中的 provider、备用列表或提示词留空时，分别继承插件统一配置（备用列表为整体覆盖，不与统一列表合并）；没有匹配规则时也使用统一配置。
- `polish_timeout_seconds`：单次调用模型的超时秒数。
- `polish_total_timeout_seconds`：一次润色（含全部重试与备用模型）的总时间上限，0 表示不限制，默认 60 秒。
- `failure_mode`：所有候选模型都失败后发送原文，或发送失败提示。
- `failure_message`：失败提示文本（仅在发送失败提示模式下生效）。
- `mark_retention_seconds`：AI 回复识别标记保留时长（秒）。
- `mark_check_interval_seconds`：过期标记检查间隔（秒）。

## 实现原理

插件通过 AstrBot 事件钩子工作：

1. 在 `on_llm_request` 记录“该回复来自 AI 对话流程”的识别标记。
2. 在 `on_decorating_result` 检查标记：
   - 有标记：先判断整条回复是否为报错正文（主模型请求失败、上游错误透传等），是则跳过润色、原样发送；否则提取消息链中的 `Plain` 文本并调用模型润色。
   - 无标记：直接放过（如指令回复）。
3. 按“UMO 规则 → 统一配置 → 会话主 AI → 备用模型列表”的顺序构建去重后的候选模型列表，调用 `provider.text_chat()`，并替换文本段。
4. 每个模型最多尝试 `polish_max_attempts` 次（默认 3）；同一个模型连续失败到该次数后，按 `fallback_providers` 的顺序切换到下一个备用模型。超时、调用异常、空返回，以及模型返回上游错误文本（如余额不足、限流）或 `role=err` 响应，都算一次失败。
5. 调用 `text_chat()` 时传入 `request_max_retries=1`，由插件统一负责重试，避免插件重试与 AstrBot provider 内置重试层层叠加、放大上游请求数；provider 的 429 换 Key、超长上下文裁剪、图片降级等恢复逻辑不受影响，不支持该参数的第三方 provider 会自动跳过。
6. 一次润色最多消耗 `polish_total_timeout_seconds`（含所有重试与备用模型）；超时后按 `failure_mode` 回退。
7. 同一条消息链里，一旦某个模型调用成功，后续文本段优先继续用该模型，不会把已经失败的模型再重试一遍。
8. 标记由后台周期任务按配置统一清理：每隔一段时间检查并删除过期标记。

识别标记仅存在于插件内存中，不写入消息内容，也不会影响 AI 对文本/图片的正常读取。

## 注意事项

- 二次润色会增加模型调用，带来额外延迟和 token 消耗；启用备用模型后延迟会随模型数量累加，建议同时设置 `polish_total_timeout_seconds` 作为总耗时上限。
- 建议让 `polish_total_timeout_seconds` 大于 `polish_timeout_seconds × polish_max_attempts`，否则单个模型的尝试就可能用满全部预算，备用模型不会被尝试。
- `custom_rules` 只匹配本插件配置中的完整 UMO，不使用 AstrBot 核心自定义规则的通配符；同一 UMO 配置多条时按列表中的第一条生效。
- 回复文本会发送到你配置的润色模型提供商，请按实际场景评估隐私与合规。
- 主模型请求失败时，AstrBot 会把错误文本当成回复正文；插件识别到这类报错会跳过润色、原样发送，不额外消耗润色调用，也不会改写报错内容。
- 所有候选模型都失败或总超时耗尽时，按 `failure_mode` 执行回退策略。

## 更新日志

- 请查看 `CHANGELOG.md`。
