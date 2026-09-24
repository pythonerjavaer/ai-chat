# 跃迁域翻译层

跃迁域通过统一的 Translation Provider 处理英中辅助翻译。翻译层不会改变原文；摘录、思想坐标、虫洞和对撞的证据仍指向原始段落与稳定锚点。

## Provider

### `browser_local`

- 引擎：Chrome Built-in Translator API（`Translator`，兼容旧版 `window.translation`）。
- 运行位置：用户浏览器/设备。
- 依赖：无需 npm 翻译包或服务器密钥；Chrome 可能在首次使用时下载语言包。
- 模型与大小：由 Chrome 管理，浏览器没有向应用公开具体模型名称、版本或下载大小，因此界面不会编造这些信息。
- 当前语言：英语到简体中文辅助阅读。
- 质量边界：适合一般理解；不提供可靠词性/多义词词典，文学语气、哲学术语和历史语境可能失真。

### `azure_translator`

- 引擎：Microsoft Azure Translator Text API v3；选词时同时使用 dictionary lookup 与上下文翻译。
- 运行位置：Frostfire 后端。Key 和 Region 不会发送到浏览器。
- 配置：`AZURE_TRANSLATOR_KEY`、`AZURE_TRANSLATOR_REGION`；可选 `AZURE_TRANSLATOR_ENDPOINT`。
- 额度保护：`AZURE_TRANSLATOR_MONTHLY_CHAR_LIMIT` 默认为 2,000,000 字符。达到应用上限后停止新请求，不会自动购买或升级。
- 未配置时：Provider 在 UI 中禁用，本地 Provider 和已缓存译文继续可用。

## 请求与缓存

用户明确点击后才会翻译。双语阅读一次最多处理当前 100 段阅读窗口；滚动不会触发请求，也不会自动预翻整本书。

PostgreSQL 中的缓存键包含：

- `document_id`
- `document_version`
- `document_hash`
- `segment_id`
- `source_language` / `target_language`
- `provider` / `provider_model`
- `translation_mode`
- 原文片段哈希

每次保存前，服务端会确认片段仍可定位回当前版本的原文。原文版本或内容哈希变化后，旧缓存不会命中。

每条译文记录 Provider、模型版本标识和翻译时间。用量按月份和 Provider 统计请求、翻译字符与缓存命中节省字符。云端失败后，当前浏览器会话停止继续发起新的云翻译，用户仍可切换本地模式。

## 隐私与证据

公版书可以由用户选择任一已配置 Provider。私人 PDF、课程材料或个人笔记首次使用云端 Provider 时，界面会说明选中的文本将发送到 Microsoft Azure，并要求确认。系统不会自动上传整本私人文档。

机器译文始终显示“AI/机器翻译，仅供辅助阅读”，不会称为官方译本。保存译文时，原始英文先作为证据保存；中文译文只进入用户的个人理解字段。

## “含义”解读 Provider

内容解读与翻译是两条独立链路。`InterpretationProvider` 当前包含：

- `openrouter`：默认请求 `POST https://openrouter.ai/api/v1/chat/completions`，路由模型为 `openrouter/free`。实际底层模型由 OpenRouter 免费路由选择，服务端保存响应中的真实 `model`，不会把它描述成固定模型。
- `openai`：复用冰焰现有 OpenAI runner、额度控制和用量记录；只在用户手动选择时调用。

默认配置为 `AI_INTERPRET_PROVIDER=openrouter`。OpenRouter 需要服务端环境变量 `OPENROUTER_API_KEY`；可通过 `OPENROUTER_MODEL` 和 `OPENROUTER_ENDPOINT`覆盖路由与官方端点。OpenRouter 失败后不会自动调用可能产生费用的 OpenAI Provider。

解读缓存和请求审计记录 provider、请求路由、实际返回模型、请求时间与成功/失败状态，不保存完整 Provider 错误正文。缓存身份包含文档版本、选区、上下文、Provider、请求模型、实际模型与提示词版本。私人材料首次发送到 OpenRouter 前，前端会明确提示文本将发送给第三方；选择文字或切换范围本身不会触发请求。
