# ai-chat

一个可本地运行的专业文档智能平台。后端使用 FastAPI、SQLite 和 OpenAI API，前端使用原生 HTML、CSS 与 JavaScript。应用同时提供合同与合规、金融研究和通用文档三个相互隔离的工作区。

## 已实现功能

- 用户注册、登录与 JWT 身份认证；不同用户的数据相互隔离。
- SQLite 持久化对话、消息和个人知识库，服务重启后仍可读取。
- 三类工作区：`legal`（合同与合规）、`finance`（金融研究）和 `general`（通用文档）。每个会话和文档都归属于指定工作区，检索时不会跨区混用资料。
- 多会话管理：创建、读取、继续和删除历史对话，并保存会话所属的专业工作区。
- RAG：上传 PDF、DOCX、TXT、Markdown、CSV 或 JSON 文档，使用 OpenAI Embeddings 建立索引，并在回答时检索相关片段。
- PDF 来源包含页码，前端会显示命中文件和页码；DOCX 和文本文件显示文件来源。
- 合同与合规模式使用独立提示约束，支持通过快捷分析入口检查主要条款、各方义务、期限、风险和合规证据缺口。
- 金融研究模式使用独立提示约束，并提供增长率、净利率、ROA、ROE、流动比率、负债权益比和 CAGR 的可复核计算工具。
- 通用 Agent 工具包括安全算术计算器和 IANA 时区时间工具；金融工具只在金融研究工作区提供。
- SSE 流式输出，前端逐段展示模型回复、工具调用以及知识库文件和页码来源。
- 保留非流式 `POST /api/chat`，便于服务间调用和自动化测试。

合同与合规工作区用于文件审阅辅助，不构成正式法律意见。金融研究工作区用于资料研究和计算，不提供个性化买卖、持仓、税务或投资组合指令。应用不会把文档中的历史数字描述为实时市场数据。

当前 PDF 支持可提取文字的文件；扫描件尚未集成 OCR。单个上传文件最大 10 MB，提取文本过大的文件需要拆分后上传。系统也尚未接入实时行情、证券交易或外部法律法规数据库。

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
│   ├── workspaces.py
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

浏览器打开 <http://127.0.0.1:5500/>，注册账号后选择工作区，即可上传资料并开始专业文档问答。

## API

除注册和登录外，所有业务接口都需要 `Authorization: Bearer <token>`。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/auth/register` | 注册并获取 Token |
| `POST` | `/api/auth/login` | 登录并获取 Token |
| `GET` | `/api/auth/me` | 获取当前用户 |
| `GET` | `/api/workspaces` | 获取工作区及快捷分析配置 |
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
  "message": "提取付款、续约和终止条款，并注明来源",
  "session_id": "optional-existing-session-id",
  "workspace": "legal"
}
```

`workspace` 可取 `general`、`legal` 或 `finance`。不传 `session_id` 时会在指定工作区自动创建新对话；继续已有对话时必须使用该会话原有的工作区。上传文档时通过 multipart 表单的 `workspace` 字段指定归属，`GET /api/documents?workspace=legal` 可以按工作区查询。

非流式响应包含 `reply`、`session_id`、`workspace`、`sources` 和 `tools_used`。命中 PDF 时，每项 `sources` 还包含 `page`。

## 测试

```bash
source backend/.venv/bin/activate
pip install -r backend/requirements-dev.txt
python -m pytest -q
```

测试覆盖认证和用户隔离、SQLite 持久化、文档索引与 RAG 来源、SSE 流式保存、工作区隔离、专业提示约束、金融指标计算、DOCX 提取以及计算工具输入限制。
