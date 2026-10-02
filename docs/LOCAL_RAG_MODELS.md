# 跃迁域本地 RAG 模型

跃迁域可以在不调用付费云端模型的情况下使用本机 Ollama 完成语义嵌入与引用式回答。

## 本地运行

此开发环境的 `backend/.env.local`（Git 忽略文件，不包含密钥）已选择：

```dotenv
LEAP_MODEL_PROVIDER=ollama
LEAP_OLLAMA_BASE_URL=http://127.0.0.1:11434
LEAP_OLLAMA_CHAT_MODEL=qwen3:1.7b
LEAP_OLLAMA_EMBEDDING_MODEL=qwen3-embedding:0.6b
```

在 macOS 打开 Ollama 后端，然后下载一次模型：

```sh
open /Applications/Ollama.app
ollama pull qwen3:1.7b
ollama pull qwen3-embedding:0.6b
```

重启 FastAPI 后，材料切块、嵌入、检索与答案生成都在本机执行；Qwen3 嵌入模型支持多语言检索。Ollama 官方模型库列出的下载大小分别约为 1.4 GB 和 639 MB；磁盘占用以当前标签实际下载为准。模型在本机运行，不会产生 OpenAI API 费用。

隐私同意和主动点击仍然是使用入口；演示数据不会交给模型。模型服务未运行时，接口会明确返回本地服务不可用，而不会静默改发云端请求。

## PostgreSQL + pgvector

Frostfire 已支持 PostgreSQL/pgvector；目标数据库需由管理员执行 `CREATE EXTENSION vector;`。索引按实际向量维度建立定长 vector 列和 HNSW 索引；SQLite 保留兼容存储。

本机隔离验证使用 PostgreSQL 17 与 pgvector 0.8.6。原有应用仍使用 `.env` 指定的数据库；没有将它切到测试库，也没有把现有 SQLite 资料复制或迁入新数据库。切换应用数据库前必须先按 [`PERSISTENT_DATABASE.md`](PERSISTENT_DATABASE.md) 完成一致性备份与完整迁移。

## 云端可选模式

如需使用 OpenAI Embeddings/回答模型，将 `LEAP_MODEL_PROVIDER` 设为 `openai`，并在受控的 `backend/.env` 中配置有可用模型权限与额度的 `OPENAI_API_KEY`。密钥不应写入仓库、聊天或日志。当前这台机器发现的密钥额度不足，因此开发配置优先使用本地模型，不会尝试消耗云端额度。
