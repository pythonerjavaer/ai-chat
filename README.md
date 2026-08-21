# 冰焰智研 · FrostFire AI

一个面向合同合规与金融文档研究的证据驱动 AI 工作台。法律工作台采用寒冰蓝视觉和“条款地图”，金融工作台采用烈焰橙红视觉和“信号面板”；两者共享同一套账号、私人资料库、来源引用和流式对话能力。

当前仓库同时包含 Web、iOS 和 Android 工程。后端使用 FastAPI、SQLite 与 OpenAI API；前端使用 Vite、原生 HTML/CSS/JavaScript 和 Capacitor。

## 已实现

- 用户注册、登录、JWT 鉴权、隐私同意记录与应用内永久删号；用户数据相互隔离。
- SQLite 持久化会话、消息、文档文本和向量；工作区之间不会交叉检索资料。
- 合同与合规、金融研究、通用文档三个工作区。
- PDF、DOCX、TXT、Markdown、CSV、JSON 上传；使用 OpenAI Embeddings 建立私人文档索引。
- PDF 引用显示文件名与页码；其他文件显示文件来源。
- 合同工作台支持条款、义务、期限、风险与证据缺口分析入口。
- 金融工作台支持增长率、净利率、ROA、ROE、流动比率、负债权益比和 CAGR 的可复核计算工具。
- 通用安全算术与 IANA 时区时间工具。
- SSE 流式回答，展示模型调用的工具与检索来源；同时保留非流式 `POST /api/chat`。
- 响应采用 Evidence / Analysis / Gaps / Next checks 结构，引导结论回到来源、口径和假设。
- 自适应三栏桌面界面、移动端抽屉、离线状态、原生触觉反馈和本地偏好。
- 隐私政策、使用条款、支持中心、第三方 AI 数据传输明示同意与账号删除闭环。
- Capacitor iOS/Android 工程、原生图标、启动图、iOS Privacy Manifest 和 Android Release 混淆/资源压缩配置。
- 多阶段 Docker 镜像：构建 Web 前端并由同一个 FastAPI 容器提供前端、法律页面与 API；附带单机持久化部署配置。

## 明确边界

合同与合规工作台是文件研究辅助工具，不构成法律意见。金融研究工作台不提供个性化买卖、持仓、税务或投资组合指令，也不执行交易。

当前版本：

- 不接入实时行情、券商交易或外部法律法规数据库；
- 不具备扫描 PDF 的 OCR；仅支持带可提取文字层的 PDF；
- 不验证上传材料本身的真实性或完整性；“证据透镜”表示检索相关度与覆盖情况，不是准确率或评级；
- 不是多人组织协作、计费或企业审计系统；
- 生产部署目前使用单实例 SQLite，若需要多副本和更高并发应迁移到托管 PostgreSQL。

默认聊天模型为 `gpt-4o-mini`，Embedding 模型为 `text-embedding-3-small`，均可通过环境变量修改。OpenAI API Key 始终只存在于服务端。

## 目录结构

```text
ai-chat/
├── backend/
│   ├── tests/
│   ├── ai_service.py
│   ├── config.py
│   ├── database.py
│   ├── main.py
│   ├── security.py
│   ├── workspaces.py
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── requirements-dev.txt
│   └── .env.example
├── frontend/
│   ├── android/
│   ├── ios/
│   ├── public/
│   ├── src/
│   ├── assets/
│   ├── index.html
│   ├── capacitor.config.json
│   ├── package.json
│   └── .env.example
├── docs/
│   ├── STORE_LISTING_ZH.md
│   └── STORE_RELEASE_CHECKLIST.md
├── docker-compose.yml
├── .gitignore
└── README.md
```

## 本地运行

需要 Python 3.10+、Node.js 22+ 和一个可用的 OpenAI API Key。

### 后端

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

启动：

```bash
uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

健康检查与交互式文档：

- `http://127.0.0.1:8000/api/health`
- `http://127.0.0.1:8000/docs`

### Web 前端

```bash
cd frontend
npm install
npm run dev
```

开发服务器会把 `/api` 代理到 `http://127.0.0.1:8000`。浏览器打开 `http://127.0.0.1:5500`。

生产构建必须设置公开的 HTTPS 后端地址：

```bash
cd frontend
VITE_API_BASE_URL=https://api.example.com/api npm run build
```

不要把 `/api` 结尾重复写入调用路径，也不要在客户端环境变量中放 OpenAI API Key。

### Docker 后端

先创建 `backend/.env`，然后运行：

```bash
docker compose up --build
```

`ai-chat-data` volume 会持久化 SQLite 数据。生产环境必须由反向代理或托管平台终止 HTTPS，并把 `CORS_ORIGINS` 限制为实际 Web 域名。

Docker 镜像会在构建阶段执行 Vite 生产构建。运行后，`/` 提供 Web 应用，`/privacy.html`、`/terms.html` 和 `/support.html` 提供公开政策页面，`/api/*` 提供后端接口，因此可以只维护一个 HTTPS 域名。

## 移动端

Capacitor 应用 ID 当前为 `com.pythonerjavaer.frostfireai`，正式创建商店记录前应确认它就是最终不可变标识。

同步 Web 产物：

```bash
cd frontend
VITE_API_BASE_URL=https://api.example.com/api npm run mobile:sync
```

Android 需要 Android SDK 与 JDK 21：

```bash
cd frontend/android
./gradlew assembleDebug bundleRelease lint
```

iOS 需要最新版完整 Xcode 与有效 Apple Developer 签名：

```bash
cd frontend
npm run ios:open
```

仓库不包含 `.jks`、`.keystore`、`.p12`、Provisioning Profile 或任何商店凭证。发布前逐项执行 `docs/STORE_RELEASE_CHECKLIST.md`。

## API 摘要

除健康检查、工作区列表、注册与登录外，业务接口使用 `Authorization: Bearer <token>`。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/health` | 健康检查 |
| `GET` | `/api/workspaces` | 工作区与快捷分析配置 |
| `POST` | `/api/auth/register` | 注册并记录隐私同意 |
| `POST` | `/api/auth/login` | 登录 |
| `GET` | `/api/auth/me` | 当前账号 |
| `POST` | `/api/auth/privacy-consent` | 更新隐私同意 |
| `DELETE` | `/api/auth/account` | 验证密码并永久删号 |
| `GET` | `/api/legal/disclosures` | 当前隐私版本和专业边界 |
| `GET/POST` | `/api/sessions` | 查询或创建会话 |
| `GET/DELETE` | `/api/sessions/{id}` | 查询消息或删除会话 |
| `GET/POST` | `/api/documents` | 查询或上传工作区文档 |
| `DELETE` | `/api/documents/{id}` | 删除文档与向量 |
| `POST` | `/api/chat` | 非流式聊天 |
| `POST` | `/api/chat/stream` | SSE 流式聊天 |

聊天请求示例：

```json
{
  "message": "提取付款、续约和终止条款，并注明来源",
  "session_id": "optional-existing-session-id",
  "workspace": "legal"
}
```

## 验证

后端：

```bash
source backend/.venv/bin/activate
pip install -r backend/requirements-dev.txt
python -m pytest -q
```

前端与依赖：

```bash
cd frontend
npm run build
npm audit
```

测试覆盖认证、隐私同意、级联删号、用户隔离、SQLite 持久化、RAG 来源、SSE 保存、工作区隔离、专业提示、金融计算、DOCX 提取和工具输入限制。

## 上架状态

- Web 生产构建：已通过。
- Android 调试 APK、Release AAB 与 Lint：已在本机通过构建；Release AAB 必须使用发布方真实密钥签名后才能上传。
- iOS 原生工程、图标、启动图与 Privacy Manifest：已生成并校验；本机当前只有 Command Line Tools，尚未用完整 Xcode 归档和签名。
- App Store / 华为应用市场提交：尚未提交。需要已验证的开发者账号、最终运营主体资料、支持邮箱与公开 HTTPS 后端/政策 URL。

这份状态是工程事实，不等同于商店审核保证。最终提交还会受到账号资质、地区、内容分级、隐私披露和平台审核决定影响。
