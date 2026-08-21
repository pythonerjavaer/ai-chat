# ai-chat

一个最小可运行的全栈 AI 聊天项目。后端使用 FastAPI 调用 OpenAI Chat Completions API（`gpt-4o-mini`），前端是原生 HTML、CSS 和 JavaScript。

## 当前已实现

- `POST /api/chat` 接收用户消息并返回 OpenAI API 的文本回复。
- 前端展示用户消息、等待状态、AI 回复和请求错误。
- 当前页面内的既有对话会随下一次请求一并提交，帮助模型理解本页上下文；刷新页面后这些数据会丢失。
- 后端通过 CORS 允许本地静态前端访问。

本项目目前没有数据库或持久化记忆，也没有 RAG、Agent/工具调用、流式输出、用户认证等功能。

## 目录结构

```text
ai-chat/
├── backend/
│   ├── main.py
│   ├── requirements.txt
│   └── .env.example
├── frontend/
│   └── index.html
├── .gitignore
└── README.md
```

## 本地运行

需要 Python 3.10 或更高版本，以及一个可用的 OpenAI API Key。

### 1. 启动后端

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

编辑 `backend/.env`：

```dotenv
OPENAI_API_KEY=your_key_here
```

然后启动 FastAPI：

```bash
uvicorn main:app --host 127.0.0.1 --port 8000
```

### 2. 启动前端

在另一个终端中，从仓库根目录运行：

```bash
python3 -m http.server 5500 --bind 127.0.0.1 --directory frontend
```

浏览器打开 <http://127.0.0.1:5500/>，前端会请求 `http://127.0.0.1:8000/api/chat`。

## API 示例

`history` 可省略；如果提供，它只代表本次请求携带的页面会话上下文，后端不会保存。

```bash
curl -X POST http://127.0.0.1:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"你好","history":[]}'
```

成功响应：

```json
{"reply":"..."}
```

## 敏感信息

- 真实密钥只放在 `backend/.env`，不要写入代码或提交到 Git。
- `.env` 和常见本地缓存、虚拟环境、编辑器文件已由根目录 `.gitignore` 忽略。
- `backend/.env.example` 只保留配置变量名，不包含真实值。
