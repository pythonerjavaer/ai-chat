# ai-chat

一个可本地运行的全栈 AI 工作空间。后端使用 FastAPI、SQLite 和 OpenAI API，前端使用原生 HTML、CSS 与 JavaScript。

## 已实现功能

- 用户注册、登录与 JWT 身份认证；不同用户的数据相互隔离。
- SQLite 持久化对话、消息和个人知识库，服务重启后仍可读取。
- 多会话管理：创建、读取、继续和删除历史对话。
- RAG：上传 UTF-8 编码的 TXT、Markdown、CSV 或 JSON 文档，使用 OpenAI Embeddings 建立索引，并在回答时检索相关片段。
- Agent 工具调用：模型可以调用安全算术计算器和 IANA 时区时间工具。
- SSE 流式输出，前端逐段展示模型回复、工具调用和知识库来源。
- 保留非流式 `POST /api/chat`，便于服务间调用和自动化测试。

默认聊天模型为 `gpt-4o-mini`，Embedding 模型为 `text-embedding-3-small`，均可通过环境变量修改。

## 目录结构

```text
ai-chat/
├── backend/
│   ├── tests/
│   │   └── test_api.py
│   ├── __init__.py
│   ├── ai_service.py
│   ├── config.py
│   ├── database.py
│   ├── main.py
│   ├── security.py
│   ├── requirements.txt
│   ├── requirements-dev.txt
│   └── .env.example
├── frontend/
│   └── index.html
├── .gitignore
└── README.md
```

运行后生成的 SQLite 文件默认位于 `backend/data/ai_chat.db`。

## 本地运行

需要 Python 3.10 或更高版本，以及可用的 OpenAI API Key。

### 1. 安装后端依赖

```bash
python3 -m venv backend/.venv
source backend/.venv/bin/activate
pip install -r backend/requirements.txt
cp backend/.env.example backend/.env
```

编辑 `backend/.env`：

```dotenv
OPENAI_API_KEY=your_openai_api_key
JWT_SECRET=replace_with_a_long_random_value
AI_MODEL=gpt-4o-mini
EMBEDDING_MODEL=text-embedding-3-small
DATABASE_PATH=
CORS_ORIGINS=http://127.0.0.1:5500,http://localhost:5500
```

`DATABASE_PATH` 留空时使用默认 SQLite 路径。

### 2. 启动后端

在仓库根目录运行：

```bash
uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

健康检查和交互式 API 文档：

- <http://127.0.0.1:8000/api/health>
- <http://127.0.0.1:8000/docs>

### 3. 启动前端

在另一个终端中运行：

```bash
python3 -m http.server 5500 --bind 127.0.0.1 --directory frontend
```

浏览器打开 <http://127.0.0.1:5500/>，注册账号后即可创建持久化对话和上传知识库文档。

## API

除注册和登录外，所有业务接口都需要 `Authorization: Bearer <token>`。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/auth/register` | 注册并获取 Token |
| `POST` | `/api/auth/login` | 登录并获取 Token |
| `GET` | `/api/auth/me` | 获取当前用户 |
| `GET/POST` | `/api/sessions` | 查询或创建对话 |
| `GET` | `/api/sessions/{id}/messages` | 查询对话消息 |
| `DELETE` | `/api/sessions/{id}` | 删除对话及其消息 |
| `GET/POST` | `/api/documents` | 查询或上传知识库文档 |
| `DELETE` | `/api/documents/{id}` | 删除知识库文档及向量 |
| `POST` | `/api/chat` | 非流式聊天 |
| `POST` | `/api/chat/stream` | SSE 流式聊天 |

聊天请求：

```json
{
  "message": "根据我的资料总结项目重点",
  "session_id": "optional-existing-session-id"
}
```

不传 `session_id` 时会自动创建新对话。非流式响应包含 `reply`、`session_id`、`sources` 和 `tools_used`。

## 测试

```bash
source backend/.venv/bin/activate
pip install -r backend/requirements-dev.txt
python -m pytest -q
```

测试覆盖认证和用户隔离、SQLite 持久化、文档索引与 RAG 来源、SSE 流式保存以及计算工具输入限制。
