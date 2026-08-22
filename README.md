# 冰焰智研 · FrostFire AI

一个面向合同合规与金融文档研究的证据驱动 AI 工作台，也是一个可创建专属 AI Space 的轻量平台底座。法律工作台采用寒冰蓝视觉和“条款地图”，金融工作台采用烈焰橙红视觉和“信号面板”；两者共享同一套账号、私人资料库、来源引用和流式对话能力。

当前仓库同时包含 Web、iOS 和 Android 工程。后端使用 FastAPI、SQLite 与 OpenAI API；前端使用 Vite、原生 HTML/CSS/JavaScript 和 Capacitor。

## 已实现

- 用户注册、登录、JWT 鉴权、隐私同意记录与应用内永久删号；用户数据相互隔离。
- SQLite 保存会话、消息、文档文本和向量；本地或 Docker volume 可长期保留，Render 免费实例上的数据则可能随重启或重新部署重置。工作区之间不会交叉检索资料。
- 合同与合规、金融研究、通用文档三个工作区。
- “冰火交叉审查”独立工作台：把合同机制与财务后果连接成跨域因果碰撞卡，而不是分别生成两份摘要。
- AI Space Studio：用户可从项目工程师、工作流设计师、文档研究员或空白模板出发，定义独立角色规则、说明和月度 Token 上限，并在 Space Sandbox 中运行任务。
- Token 账本：按账号和按 AI Space 记录模型实际输入/输出 Token，用免费计划额度与每个 Space 的预算在服务端阻止超额调用。
- 个人秋招监控：岗位/职能类型、雇主类型、行业与城市偏好均可只填写其中一部分后匹配。
- 已合并三组个人监控范围：央国企全量秋招、大厂/金融/外企、政策行与平安专项；覆盖监控目标，不代表清单内所有单位当前均有开放岗位。
- 动态源适配器：服务启动后按 `RECRUITMENT_REFRESH_MINUTES`（默认 30 分钟）扫描公开招聘线索，并支持配置 Adzuna API 凭证。公开聚合页只进入后台发现队列，不会直接成为面向用户的岗位卡片。
- 行动卡片准入：只展示已核验的企业官方 ATS/招聘官网直达链接，或通过受保护动态接收 API 写入的岗位；无链接、第三方聚合页、非校招和已过期岗位不会展示。
- 已核验校招直达源：拼多多 2027 提前批、科尔尼大中华区 2027 校招，以及九坤 2027 梧桐计划 8 个官方 Moka 岗位；前两者按官方公告截止日期触发预警，九坤按官方持续开放窗口展示。
- 首页截止预警：登录后直接显示 7 天内到期的已核验校招，无需先打开秋招雷达；过期岗位不会从岗位 API 返回。
- 动态监控接收 API：受 `RECRUITMENT_INGEST_TOKEN` 保护，可由外部监控任务持续推送结构化校招岗位，服务端会拒绝过期、非校招和非目标城市数据。
- 外部监控接入规范见 [`docs/RECRUITMENT_INGEST_OPENAPI.yaml`](docs/RECRUITMENT_INGEST_OPENAPI.yaml)；部署后将其中的 `YOUR_RENDER_DOMAIN` 替换为实际域名，并在发送方配置 Render 生成的监控令牌。
- 订阅能力的服务端边界：已有 Free/Pro 权益模型、额度查询和一个默认关闭的 Apple 交易校验入口；未配置交易校验时接口明确拒绝，不会把演示按钮伪装成已完成收款。
- 三档反事实压力舱（Base / Downside / Breakpoint）、未知事项雷达、证据锁链和输入版本分析指纹；情景明确标注为推演而非预测。
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
- 多阶段 Docker 镜像：构建 Web 前端并由同一个 FastAPI 容器提供前端、法律页面与 API；`docker-compose.yml` 提供本地持久化，`render.yaml` 当前使用免费演示实例。

## 明确边界

合同与合规工作台是文件研究辅助工具，不构成法律意见。金融研究工作台不提供个性化买卖、持仓、税务或投资组合指令，也不执行交易。

当前版本：

- 不接入实时行情、券商交易或外部法律法规数据库；
- 不具备扫描 PDF 的 OCR；仅支持带可提取文字层的 PDF；
- 不验证上传材料本身的真实性或完整性；“证据透镜”表示检索相关度与覆盖情况，不是准确率或评级；
- AI Space 当前是文本任务运行时，尚未提供可视化应用生成、代码执行、外部工具连接或将上传资料自动绑定到任意 Space 的功能；
- 秋招监控可从国资小新页面、银行招聘网及已配置的 Adzuna API 定时或手动刷新公开岗位；未接入的企业官网不会被声称为已实时抓取，监控范围也不等于当前开放岗位；
- 实时岗位刷新不会抓取需要登录、验证码或违反网站条款的页面；未配置 `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` 时刷新接口会明确返回未配置，不会伪造实时结果；
- 不是多人组织协作或企业审计系统；
- Pro 计划目前是权益数据模型，不是已上线的 Apple 内购。必须完成 App Store Connect 商品、StoreKit 客户端交易、服务端签名交易验证和 Sandbox 测试，才可以向用户收费；
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

### Render 免费演示部署

根目录 `render.yaml` 当前使用 Render Free Web Service，不需要持久化磁盘。它适合公开演示和小规模测试，但空闲后会休眠，重新部署或实例重启可能清空 SQLite 中的账号、会话、文档和秋招动态数据。正式运营前必须升级并挂载持久化磁盘，或迁移到托管 PostgreSQL。

部署时必须在 Render 的环境变量页面填写 `OPENAI_API_KEY` 和 `CORS_ORIGINS`；`JWT_SECRET` 与 `RECRUITMENT_INGEST_TOKEN` 由 Blueprint 自动生成。真实密钥不得写入仓库。

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
| `POST` | `/api/cross-exam` | 法律条款与金融资料的双域交叉审查 |
| `GET` | `/api/platform/templates` | AI Space 模板定义 |
| `GET` | `/api/billing/status` | 当前计划、Token 额度和用量 |
| `GET/POST` | `/api/spaces` | 查询或创建专属 AI Space |
| `POST` | `/api/spaces/{id}/run` | 按 Space 规则运行一次任务并记录实际 Token 用量 |
| `POST` | `/api/billing/apple/verify` | Apple 交易校验预留入口（未配置时返回 503） |
| `GET/PUT` | `/api/recruitment/profile` | 查询或保存秋招求职画像 |
| `GET` | `/api/recruitment/jobs` | 获取岗位、截止日期、匹配度与 T0–T3 分层 |
| `POST` | `/api/recruitment/refresh` | 使用已配置的官方/授权岗位源刷新数据 |
| `POST` | `/api/recruitment/ingest` | 使用 `X-Recruitment-Token` 接收外部监控任务推送的校招岗位 |

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

测试覆盖认证、隐私同意、级联删号、用户隔离、SQLite 持久化、RAG 来源、SSE 保存、工作区隔离、双域交叉审查与证据锁定、专业提示、金融计算、DOCX 提取、工具输入限制，以及 AI Space 的创建、隔离、Token 记录和未配置支付入口。

## 上架状态

- Web 生产构建：已通过。
- Android 调试 APK、Release AAB 与 Lint：已在本机通过构建；Release AAB 必须使用发布方真实密钥签名后才能上传。
- iOS 原生工程、图标、启动图与 Privacy Manifest：已生成并校验；本机当前只有 Command Line Tools，尚未用完整 Xcode 归档和签名。
- App Store / 华为应用市场提交：尚未提交。需要已验证的开发者账号、最终运营主体资料、支持邮箱与公开 HTTPS 后端/政策 URL。
- Apple 订阅：尚未上线或收款。完成商店内购配置后，仍须在真机 Sandbox 用真实交易流程验证，才具备提交订阅功能的条件。

这份状态是工程事实，不等同于商店审核保证。最终提交还会受到账号资质、地区、内容分级、隐私披露和平台审核决定影响。
