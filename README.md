# 冰焰 · Frostfire

一个面向合同合规与金融文档研究的证据驱动 AI 工作台，也是一个可创建 AI Space“成果胶囊”的轻量平台底座。法律工作台采用寒冰蓝视觉和“条款地图”，金融工作台采用烈焰橙红视觉和“信号面板”；两者共享同一套账号、私人资料库、来源引用和流式对话能力。

当前仓库同时包含 Web、iOS 和 Android 工程。后端使用 FastAPI、SQLite 与 OpenAI API；前端使用 Vite、原生 HTML/CSS/JavaScript 和 Capacitor。

## 已实现

- 用户注册、登录、JWT 鉴权、隐私同意记录与应用内永久删号；用户数据相互隔离。
- 管理员汇总使用面板：访问 `/?admin=usage` 后以 `ADMIN_DASHBOARD_TOKEN` 解锁，约每 10 秒刷新注册量、活跃量、会话/消息/文档、API 请求、已记录的普通聊天与 AI Space 模型调用及 Token；不查询或展示用户名、消息正文、文档内容、密码、Prompt 或模型回复。内容无关的 API 使用事件最多保存 30 天。
- SQLite 保存会话、消息、文档文本和向量；本地或 Docker volume 可以持久化，但当前 Render Free 线上实例使用临时文件系统，休眠、重启或重新部署后账号及业务数据可能丢失。工作区之间不会交叉检索资料。
- 合同与合规、金融研究、通用文档三个工作区。
- “冰焰交叉审查”独立工作台：把合同机制与财务后果连接成跨域因果碰撞卡，而不是分别生成两份摘要。
- 八度空间（Music Dimension）：提供四种可继续改写的创作模板、8 类可自由组合的乐器编制、6 种原创合成声线，以及作品/风格/情绪/BPM/声音材质和叙事蓝图；浏览器使用 Web Audio API 在本机生成程序化编曲与合成哼唱，不依赖音乐 App、不调用 OpenAI，消耗 0 Token。创作蓝图与音量保存在当前设备；只有用户主动最小化时才出现迷你播放器，停止或关闭后不再悬浮。
- 光子魅影（Photon Projection）：第九个正式产品，可把自由灵感或一个已有 AI Space 的有限字段显影成文字、视觉、叙事、品牌、界面或声音概念。六组风格光谱与“生成创作骨架”完全在浏览器本地运行，拖动和生成均为 0 Token；只有用户点击“开始显影”才复用 `POST /api/chat` 发起一次 `creative_single_pass` 请求。该模式不启用工具循环、不检索资料库，响应返回实际模型 usage；最近 10 个结果只保存在当前设备的 `bingyan_photon_creations`，不写入新数据库表。
- 遗忘史诗（Oblivion Archive）：第十个正式产品和私人时间层，用于在当前浏览器封存记忆史诗、未完章节与失落世界，并生成完全本地的时间编年。创建、编辑、搜索、筛选、打捞、重启及 JSON 导入导出均为 0 Token，不请求后端；数据使用 `bingyan_oblivion_archive_v1`，界面明确提示浏览器数据被清除后的丢失风险，不声称永久保存。
- 十产品罗盘：公开首页与登录后总览都使用可拖拽、方向按钮和键盘控制的自适应旋转罗盘；产品数量由当前卡片集合动态计算，新增产品无需重写固定九宫格。最近选择的产品保存在当前设备，刷新或重新登录后恢复所选产品，不再统一跳回极光域。
- AI Space 成果胶囊：用户可从项目工程师、工作流设计师、文档研究员或空白模板出发，定义独立的成功标准、边界和月度 Token 上限，再以 `local`、`lean` 或 `deep` 三种模式更新成果。
- `local` 零 Token 模式：只在服务端用确定性规则整理用户明确标注的事实、假设、待确认项和下一步，不调用 OpenAI；`lean` 节能更新最多生成 320 个输出 Token，`deep` 深度重算最多生成 800 个输出 Token。
- 调用前 Token 飞行计划：用 UTF-8 字节数和固定消息开销建立保守输入预留，再叠加明确的最大输出、剩余额度、预计调用次数与执行路径；预算不足时在调用模型之前拒绝任务。它是工程预算门槛，不是所选模型 tokenizer 给出的逐 Token 精确报价；预检本身不调用 OpenAI。
- 应用级零 Token 复用：`lean` 模式下，同一账号、同一 Space 版本、同一规则、模型和规范化输入的完整指纹完全一致时，直接复用已有成果，不再调用模型。`deep` 模式始终跳过缓存重新计算。这里是本应用数据库中的精确命中缓存，不是语义相似缓存，也不是 OpenAI 提供方缓存。
- 成果运行历史：每个 Space 最多保存最近 100 次运行的模式、路径（规则整理、缓存、节能或深度）、预估值、实际 Token、节省量、成果和完成状态；界面默认展示最近记录。
- AI Space Token 账本：只统计成果胶囊的模型实际输入/输出 Token，并用账号级 Space 权益与每个 Space 的预算在服务端阻止超额调用；服务器规则整理和缓存命中的实际模型用量为 0。普通聊天、文档 Embedding 和冰焰交叉审查目前不计入这本账，不能把这里的余额理解为整个应用的 OpenAI 总额度。
- 普通聊天的完整记录保存在数据库，但每次只把最近 16 条消息重新发送给模型，并把每轮最大输出限制为 700 Tokens；冰焰交叉审查最大输出为 1,800 Tokens。这样控制成本，也意味着更早的聊天内容不会自动进入当前模型上下文。
- 未来雷达：岗位/职能、十类雇主星域、行业与城市均为真实筛选条件；同一字段内取并集，不同已填写字段之间同时生效。星域切换会在服务端完整岗位池中先筛选、再分页，因此总数与翻页结果会同步变化。T 级评价对象是具体岗位，而不是公司 Logo：平台质量、岗位职能、职业发展和工作条件分别按 35% / 45% / 10% / 10% 合成最终岗位分，并可展开查看四项分数与简洁依据。T0 ≥90，T0.5 为 85–89，T1 为 80–84，T1.5 为 75–79，T2 为 70–74，T2.5 为 65–69，T3 为 60–64；低于 60 分不进入重点池，缺少具体岗位或 JD 依据时明确显示“未评分”。
- Future Radar 服务端情报层：`backend/future_radar` 在不删除旧 `/api/recruitment/*` 的前提下增加 Source Registry、招聘项目、岗位、多来源证据、运行记录、差异事件、内容哈希、关闭确认、幂等同步和分页 API。FastAPI 进程负责扫描到期来源；前端每 30 秒只增量读取已落库事件，不把浏览器轮询伪装成后台抓取。完整架构、数据表、API、来源配置、Mock 生命周期与部署限制见 [`docs/FUTURE_RADAR.md`](docs/FUTURE_RADAR.md)。
- 已建立十组监控范围：央企能源与资源、央企科技通信与交通、烟草与高等级专卖体系、政策性金融与国有大行、券商公募资管、保险综合金融、互联网大中厂、快消外企咨询、量化私募对冲、四大专业服务。跨属性机构可保留多个行业标签，同时使用一个主星域分类；监控清单是高信号搜索范围，不代表每家单位当前均有开放岗位，只有通过官方页面核验的当前校招机会才进入主池。
- 动态源适配器：服务启动后按 `RECRUITMENT_REFRESH_MINUTES`（默认 30 分钟）扫描国聘网、国资小新、银行招聘网等公开招聘页面，并支持配置 Adzuna API 凭证。公开来源会先做校招标题、重点雇主和城市规则过滤；仍带“待打开核对”或“待官方核验”的候选只留在隔离区，不进入用户主池。
- 可选 OpenAI 网页搜索补漏：设置 `RECRUITMENT_WEB_SEARCH_ENABLED=true` 后，服务按 `RECRUITMENT_WEB_SEARCH_INTERVAL_MINUTES`（默认 360 分钟）搜索十类重点雇主的当前校园岗位，限制网页搜索工具调用次数，过滤非目标单位、非当前目标届别、社招、当天截止/过期/尚未开放条目、搜索结果页和社交媒体链接；模型提交的日期只有在官方页出现同一日期时才会采用。每次调用记录工具次数和实际 Token，该能力会产生 OpenAI API 费用。
- 行动卡片准入：主池只展示带可点击 HTTPS 链接，且官方正文同时支持公司、校园招聘和具体岗位标题的机会；无链接、城市招聘导航页、非校招、未核验、当天截止及已过期岗位不会展示。官方页未写截止日期的已核验开放岗位可以显示，但不会进入截止预警。
- 五源已核验快照：仓库只保存 5 个授权监控会话最近一次通过服务端核验的公开岗位字段，不保存会话 ID、Cookie、接收 Token 或私密聊天内容。Render Free 冷启动后会逐个重新打开官方页面；只有仍通过核验的岗位才恢复，页面已关闭的岗位会下线，临时无法访问的岗位不会在空数据库中盲目恢复。
- 首页截止预警：登录后直接显示 7 天内到期的已核验机会，无需先打开未来雷达；截止日当天及更早的岗位不会从岗位 API 返回。只有原公告明确标注的截止日期才会触发预警。
- 我的官网变化雷达：每个账号可添加最多 12 个公开 HTTPS 企业招聘页和关注关键词。抓取前会拒绝账号信息、本机/内网/保留地址、非标准 HTTPS 端口和不安全跳转，并限制响应类型、大小与超时。
- 官网变化检测完全使用可见文本规范化、SHA-256 指纹和确定性关键词匹配，不把招聘网页发送给模型，也不消耗 OpenAI Token。首次检查只建立基线；服务清醒时会定时比较变化，用户也可手动刷新。最近变化会在应用首页持续显示“去核对”提醒，直到用户标记为已核对。
- 动态监控接收 API：受 `RECRUITMENT_INGEST_TOKEN` 保护，供另行部署并获得授权的外部任务写入结构化校招岗位。单批最多 10 个岗位，也支持 `{"jobs":[],"source_id":"chatgpt-radar-01","source_updated_at":"<ISO 8601>"}` 空结果心跳；批次和岗位均拒绝未声明字段。新契约记录五个逻辑 `source_id`、稳定条目/外部 ID、来源更新时间和简短证据，并提供只含聚合状态的同步状态接口；五个真实会话 ID 不提交、不入库、不进 Git。可选 `source_thread_id` 只用于兼容其他来源，服务端至多保留不可逆短哈希。当前仓库仍不包含一个永不休眠、覆盖全网的外部采集服务。
- 五源受控导入：ChatGPT 的私有 `/c/...` 会话地址不是开放读取 API，冰焰不会依赖登录态、Cookie 或未公开内部接口读取它们。可行路径是把会话更新为不含隐私的 `FROSTFIRE_SYNC_V1`，由用户创建/更新公开 `https://chatgpt.com/share/...` 快照，或直接导出同一 JSON；`scripts/frostfire_source_import.py` 只提取完整结构化对象，并由本机 Keychain Token 幂等提交。分享链接是快照，不会在原会话新增消息后自动变成持续数据流。
- 公众号与公开订阅源：已知、无需登录即可访问的公众号文章 URL 可以作为 discovery 文章受控导入；公开 RSS/Atom 可以配置为 `public_feed` 来源。两者都只产生线索，仍须企业官方招聘 HTTPS 页面核验后才会成为公开岗位；系统不绕过微信登录、验证码、反爬或平台限制。
- 服务端先隔离候选，再区分已核验、待核验、拒绝与关闭；只有服务端可读取的官方 HTTPS 页面正文同时支持校招、公司和岗位身份，且满足有效期与城市规则时，才提升到岗位池。提交日期只有在官网正文出现同一确切日期时才进入正式岗位；每次重复 heartbeat 也会重新读取官方页，若页面显示关闭就下线，暂时访问失败则保留此前的 last-known-good。外部 `evidence` 最多 12 条、每条 1–280 个字符且必须为单行，邮箱或电话号码会被拒绝；它只保留简短来源上下文，不能替代官方页面核验。岗位会按稳定外部 ID、来源条目 ID 或规范化岗位身份去重，更早的来源版本不会覆盖新版本。
- 外部监控 OpenAPI 契约见 [`docs/RECRUITMENT_INGEST_OPENAPI.yaml`](docs/RECRUITMENT_INGEST_OPENAPI.yaml)，五源桥接、Secret、heartbeat 与幂等说明见 [`docs/CHATGPT_RADAR_BRIDGE.md`](docs/CHATGPT_RADAR_BRIDGE.md)。契约已指向 `https://frostfire-ai.onrender.com`；发送方只能配置 Render 生成的接收 Token，不能写入 ChatGPT Cookie 或 OpenAI API Key。
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
- 八度空间当前生成的是原创程序化编曲、合成哼唱与可复用创作描述，不是完成母带的商业歌曲，也不会克隆、模仿或冒充具体艺人；
- 光子魅影当前生成的是文字作品或创作方案；“视觉”“叙事”“声音概念”等轨道不会调用图像、视频或音乐生成模型，也不声称已经产出图片、影片或完成音乐。已有世界来源只发送名称、最多 360 字描述和最多 600 字规则，不读取聊天记录、文档或运行历史；
- 遗忘史诗当前只保存在使用它的浏览器中，没有云端同步、跨设备恢复、文件上传、向量检索或 AI 整理；用户应定期导出 JSON 备份；
- AI Space 成果胶囊当前仍是文本任务运行时；`local` 只做规则化整理，`lean` / `deep` 才调用模型。它尚未提供可视化应用生成、代码执行、外部工具连接或将上传资料自动绑定到任意 Space 的功能；
- 精确缓存只复用完全一致的指纹；输入、Space 规则或版本、模式、模型任一变化都会重新预检，不能把它理解成对相似问题的通用知识缓存；
- 未来雷达可从国聘网、国资小新、银行招聘网、可选 Adzuna API 和限频 OpenAI 网页搜索定时或手动刷新公开线索；官网变化雷达只检查用户主动添加并允许访问的公开 HTTPS 页面。它会扩大覆盖范围，但仍不能保证任何时刻完整覆盖全网，也不能绕过登录、验证码或网站访问限制；
- 岗位源刷新不会抓取需要登录、验证码或违反网站条款的页面；未配置 `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` 时会跳过 Adzuna，只保留已配置的公开源与已核验卡片，不会伪造 Adzuna 结果；
- 官网变化雷达会在每次请求与跳转前校验 HTTPS、端口和公网 DNS，但当前标准库传输层不会把已验证 IP 固定到连接。面向不受信任的公开注册用户正式运营前，仍应增加受控出站代理或可信招聘域名策略，消除 DNS 重绑定时间窗；
- OpenAI 支持的接口有单进程、十分钟窗口的账号级与服务级请求频率熔断，普通聊天和交叉审查也有输出上限；但公开注册目前没有邀请码、验证码或持久化的服务总费用熔断，进程重启也会清空频率窗口。正式公开运营前仍需加入邀请/风控，并在 OpenAI 项目侧设置预算与用量告警；
- 官网变化提醒是用户打开应用后看到的站内首页提醒，不是 iOS/Android 系统推送，也没有声称具备后台通知。服务清醒时，官网雷达会按 `RECRUITMENT_REFRESH_MINUTES` 周期检查，也可以由用户手动刷新；Render Free 休眠期间，应用进程和进程内定时刷新均不会持续运行；
- 不是多人组织协作或企业审计系统；
- Pro 计划目前是权益数据模型，不是已上线的 Apple 内购。必须完成 App Store Connect 商品、StoreKit 客户端交易、服务端签名交易验证和 Sandbox 测试，才可以向用户收费；
- 当前线上部署使用 Render Free 临时文件系统中的单实例 SQLite，不具备账户数据持久化保证；实例休眠后重新启动、重新部署或重启都会丢失本地账号、成果历史、画像和岗位状态。正式上线前必须迁移到外部持久数据库（例如托管 PostgreSQL），不能继续把当前线上 SQLite 当作生产数据库。

默认聊天模型为 `gpt-4o-mini`，Embedding 模型为 `text-embedding-3-small`，均可通过环境变量修改；替换的聊天模型必须兼容 OpenAI Chat Completions 与 `max_completion_tokens`。OpenAI API Key 始终只存在于服务端。

## 目录结构

```text
ai-chat/
├── backend/
│   ├── future_radar/
│   ├── tests/
│   ├── ai_service.py
│   ├── config.py
│   ├── database.py
│   ├── main.py
│   ├── radar_bootstrap_jobs.json
│   ├── recruitment_search.py
│   ├── recruitment_watch.py
│   ├── security.py
│   ├── space_engine.py
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
│   ├── CHATGPT_RADAR_BRIDGE.md
│   ├── FUTURE_RADAR.md
│   ├── RECRUITMENT_INGEST_OPENAPI.yaml
│   ├── STORE_LISTING_ZH.md
│   └── STORE_RELEASE_CHECKLIST.md
├── scripts/
│   ├── frostfire_ingest.py
│   └── frostfire_source_import.py
├── tests/
│   └── scripts/
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
FUTURE_RADAR_ENABLED=true
FUTURE_RADAR_DEFAULT_INTERVAL_MINUTES=30
FUTURE_RADAR_CLOSE_CONFIRMATIONS=2
FUTURE_RADAR_MAX_WORKERS=4
FUTURE_RADAR_AI_MODEL=gpt-5.4-nano
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

根目录 `render.yaml` 当前使用 Render Free Web Service，且没有持久化磁盘。它只适合公开演示和小规模测试：空闲后会休眠，实例重新启动、重新部署或重启会清空 SQLite 中的账号、会话、文档、成果胶囊历史和未来雷达岗位数据；休眠期间进程内招聘刷新也不会继续执行。Blueprint 只在同一个 Web Service 内启用 Future Radar scheduler，没有增加一个无法共享临时 SQLite 的独立 Worker。正式上线前必须接入外部持久数据库，或升级常开服务并使用受支持的持久存储。

部署时必须在 Render 的环境变量页面填写 `OPENAI_API_KEY`、`CORS_ORIGINS` 和一个自行保存的强随机 `ADMIN_DASHBOARD_TOKEN`；`JWT_SECRET` 与 `RECRUITMENT_INGEST_TOKEN` 由 Blueprint 自动生成。管理员面板入口是 `/?admin=usage`，Token 只保存在当前页面内存。`FUTURE_RADAR_DEFAULT_INTERVAL_MINUTES=30` 只表示清醒进程的调度唤醒间隔；各来源仍按自己的间隔判断是否到期，其中 OpenAI 公共网页补漏默认为 360 分钟。如需零额外模型费用，可将 `RECRUITMENT_WEB_SEARCH_ENABLED` 改为 `false`。真实密钥不得写入仓库。Future Radar 的长期运行边界和最小持久方案见 [`docs/FUTURE_RADAR.md`](docs/FUTURE_RADAR.md)。

需要导入 ChatGPT 监控结果时，将服务端 `RECRUITMENT_INGEST_TOKEN` 保存到本机 macOS Keychain service `frostfire-recruitment-ingest`，再使用公开 `/share/` 快照或本地结构化 JSON 调用 `scripts/frostfire_source_import.py`。私有 `/c/...` 链接会被明确拒绝；分享页或本地文件中的 `source_id` 也不能覆盖命令行指定的本机逻辑来源。若要持续更新，数据生产方必须在每轮产生新的完整 JSON 后更新分享快照并重新运行导入，或直接推送 JSON；目前没有 ChatGPT 个人历史会话读取 API。不要把接收 Token、Cookie、OpenAI API Key、私有会话 URL 或会话全文写入 Git、数据库或任务日志。完整边界和命令见 [`docs/CHATGPT_RADAR_BRIDGE.md`](docs/CHATGPT_RADAR_BRIDGE.md)。Render Free 会休眠且 SQLite 不持久，定时导入不能消除冷启动、漏跑或数据丢失风险。

## 移动端

安卓或 iPhone 浏览器可直接打开同一个 HTTPS Web 地址；分享 `/?start=register` 会自动进入注册模式并定位到可提交的表单。登录后的手机工作台保留轻量固定入口，不继续增加 Dock 高度；“总览/产品罗盘”通过手势或方向按钮旋转选择包括遗忘史诗在内的十个正式产品。

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
| `GET` | `/api/admin/usage` | 使用 `X-Admin-Token` 返回不含用户内容的汇总使用数据 |
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
| `POST` | `/api/spaces/{id}/preflight` | 调用模型前估算 Token、预算、缓存与执行路径 |
| `POST` | `/api/spaces/{id}/run` | 兼容入口：按 `local` / `lean` / `deep` 模式更新成果 |
| `POST` | `/api/spaces/{id}/runs` | 创建一次成果运行并记录执行路径与实际用量 |
| `GET` | `/api/spaces/{id}/runs` | 查询最近成果运行历史 |
| `GET` | `/api/spaces/{id}/runs/{run_id}` | 查询单次成果详情 |
| `POST` | `/api/billing/apple/verify` | Apple 交易校验预留入口（未配置时返回 503） |
| `GET/PUT` | `/api/recruitment/profile` | 查询或保存未来雷达求职画像 |
| `GET` | `/api/recruitment/jobs` | 获取岗位、截止日期、匹配度与七档 T 分层 |
| `GET/POST` | `/api/recruitment/watches` | 查询或添加企业名称监控（旧版也兼容公开官网监控） |
| `POST` | `/api/recruitment/watches/refresh` | 手动抓取并比较已添加官网的文本指纹 |
| `POST` | `/api/recruitment/watches/{watch_id}/acknowledge` | 将一次官网变化标记为已核对 |
| `DELETE` | `/api/recruitment/watches/{watch_id}` | 删除个人官网监控 |
| `POST` | `/api/recruitment/refresh` | 使用已配置的官方/授权岗位源刷新数据 |
| `POST` | `/api/recruitment/ingest` | 使用 `X-Recruitment-Token` 接收最多 10 个校招岗位或一个空结果来源心跳 |
| `GET` | `/api/recruitment/sync/status` | 使用 `X-Recruitment-Token` 查询五源最新事件计数、历史候选库存与最近事件 |
| `GET` | `/api/future-radar/dashboard` | Future Radar 汇总、来源健康、最近运行与事件游标 |
| `GET` | `/api/future-radar/jobs` | Future Radar 岗位分页、筛选与个人画像评分 |
| `GET` | `/api/future-radar/jobs/{job_id}` | 单个 Future Radar 岗位与多来源证据 |
| `GET` | `/api/future-radar/programs` | 招聘项目分页 |
| `GET` | `/api/future-radar/programs/{program_id}` | 单个招聘项目与来源链 |
| `GET` | `/api/future-radar/events` | 事件列表与 `after_event_id` 增量读取 |
| `GET` | `/api/future-radar/changes` | Future Radar 事件增量兼容别名 |
| `GET` | `/api/future-radar/runs` | 扫描运行历史分页 |
| `GET` | `/api/future-radar/runs/{run_id}` | 单次扫描的计数与错误 |
| `GET` | `/api/future-radar/sources` | Source Registry 的公开健康字段 |
| `POST` | `/api/future-radar/run` | 已登录且同意当前隐私条款的用户手动扫描到期或指定的已启用来源；每用户 5 分钟冷却，并发冲突返回 409 |
| `POST` | `/api/future-radar/sync` | 使用 `X-Recruitment-Token` 幂等接收 `FROSTFIRE_SYNC_V1` |
| `POST` | `/api/future-radar/sources` | 使用 `X-Admin-Token` 创建来源 |
| `PATCH` | `/api/future-radar/sources/{source_id}` | 使用 `X-Admin-Token` 更新来源运行配置 |

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

本机未来雷达提交器：

```bash
python3 -m unittest discover -s tests/scripts -v
python3 scripts/frostfire_ingest.py --dry-run < /path/to/new-jobs.json
python3 scripts/frostfire_source_import.py --source-id chatgpt-share-01 \
  --structured-json /path/to/FROSTFIRE_SYNC_V1.json
```

新 Future Radar 情报层的 1–5 轮生命周期、幂等、关闭确认、失败隔离、多来源合并、AI 降级、公众号受限状态与 API 测试：

```bash
python -m pytest -q backend/tests/test_future_radar.py
```

测试覆盖认证、隐私同意、级联删号、用户隔离、本地 SQLite、RAG 来源、SSE 保存、工作区隔离、双域交叉审查与证据锁定、专业提示、金融计算、DOCX 提取、工具输入限制，以及 AI Space 的预检、三种运行模式、精确缓存、运行历史、Token 记录、官网变化雷达安全校验和未配置支付入口。

## 上架状态

- Web 生产构建：已通过。
- Android 调试 APK、Release AAB 与 Lint：已在本机通过构建；Release AAB 必须使用发布方真实密钥签名后才能上传。
- iOS 原生工程、图标、启动图与 Privacy Manifest：已生成并校验；本机当前只有 Command Line Tools，尚未用完整 Xcode 归档和签名。
- App Store / 华为应用市场提交：尚未提交。需要已验证的开发者账号、最终运营主体资料、支持邮箱与公开 HTTPS 后端/政策 URL。
- Apple 订阅：尚未上线或收款。完成商店内购配置后，仍须在真机 Sandbox 用真实交易流程验证，才具备提交订阅功能的条件。

这份状态是工程事实，不等同于商店审核保证。最终提交还会受到账号资质、地区、内容分级、隐私披露和平台审核决定影响。
