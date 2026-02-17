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
- `polish_prompt`：润色提示词；留空使用内置默认提示词。
- `polish_timeout_seconds`：润色调用超时秒数。
- `failure_mode`：润色失败后发送原文，或发送失败提示。
- `failure_message`：失败提示文本（仅在发送失败提示模式下生效）。
- `mark_retention_seconds`：AI 回复识别标记保留时长（秒）。
- `mark_check_interval_seconds`：过期标记检查间隔（秒）。

## 实现原理

插件通过 AstrBot 事件钩子工作：

1. 在 `on_llm_request` 记录“该回复来自 AI 对话流程”的识别标记。
2. 在 `on_decorating_result` 检查标记：
   - 有标记：提取消息链中的 `Plain` 文本并调用模型润色。
   - 无标记：直接放过（如指令回复）。
3. 按配置选择润色 provider，调用 `provider.text_chat()`，并替换文本段。
4. 在 `after_message_sent` 立即清理标记；同时后台周期任务会清理过期标记。

识别标记仅存在于插件内存中，不写入消息内容，也不会影响 AI 对文本/图片的正常读取。

## 注意事项

- 二次润色会增加一次模型调用，带来额外延迟和 token 消耗。
- 回复文本会发送到你配置的润色模型提供商，请按实际场景评估隐私与合规。
- 润色调用失败或超时时，按 `failure_mode` 执行回退策略。
