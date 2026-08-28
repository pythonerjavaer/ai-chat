# Future Radar 架构与运行手册

本文只描述仓库当前已实现的 `backend/future_radar` 服务端情报层和前端 Future Radar 界面。旧版 `/api/recruitment/*` 接口仍保留；新模块不会删除旧岗位池，而是通过 `legacy-recruitment-pipeline` 适配器兼容读取它。

## 1. 当前架构

```text
公开 HTTPS / JSON / RSS / 公开招聘索引 / OpenAI 公网 discovery / 受控同步 / Mock
                              │
                              ▼
                     Source Registry
                    monitor_sources
                              │
        scheduled due source / manual Quick or Deep dispatch
                              │
                              ▼
          normalize → stable identity → semantic hash
                              │
                              ▼
       program/job upsert → provenance → diff events
                              │
                              ▼
             SQLite repository + REST read APIs
                              │
                              ▼
          Future Radar UI（30 秒增量读取事件）
```

主要组件：

- `schema.py`：在现有 SQLite 上执行幂等、增量迁移 `future_radar_v1`。
- `seeds.py`：初始化 Source Registry；不保存私有会话 ID、Cookie 或密钥。
- `adapters.py`：公开 HTML、公开 JSON API、公开 RSS/Atom、公开招聘索引、五个公众号逻辑来源的 OpenAI 公网发现、旧岗位池、受控同步和 Mock 等来源适配器。
- `public_discovery.py`：确定性解析国务院国资委招聘列表与银行招聘网公开索引，只输出经过 URL 与文本净化的文章线索，不生成已核验岗位。
- `normalization.py`：文本、日期、HTTPS URL、稳定外部 ID 和语义哈希规范化。
- `service.py`：并发扫描、按扫描类型的运行锁、单来源锁、验证角色、合并、差异事件、关闭确认和幂等同步。
- `repository.py`：SQLite 查询、分页、来源健康、运行记录、事件游标、来源快照和 AI 缓存。
- `ai.py`：对公开网页文本使用 OpenAI Responses API 做有界结构化提取。
- `worker.py`：一次性服务端 CLI；它不是常驻守护进程。
- `frontend/src/app.js`：读取 dashboard、岗位、项目、事件、来源与运行记录；浏览器只轮询已经落库的事件，不负责发起定时抓取。

每个来源失败相互隔离。一次运行可能是 `success`、`partial_success` 或 `failed`；一个来源失败不会阻断其他来源，也不会把该来源的旧岗位误判为已关闭。只有完整且成功的来源快照才推进缺失计数，默认连续缺失两次后才解除该来源关联；当一个岗位已没有任何活跃来源时才关闭。

## 2. SQLite 数据表

迁移是 additive 和 idempotent，现有 `recruitment_jobs` 等旧表继续可用。

| 表 | 用途 |
| --- | --- |
| `schema_migrations` | 记录 `future_radar_v1` 已应用 |
| `radar_companies` | 规范化雇主实体 |
| `monitor_sources` | Source Registry、优先级、扫描间隔、健康状态和内容哈希 |
| `recruitment_programs` | 校招项目/批次；可先于具体岗位存在 |
| `radar_jobs` | 规范化岗位、日期、状态、核验状态和当前主来源 |
| `source_articles` | 来源文章/公告的最小索引，不保存整段会话 |
| `job_sources` | 岗位与多个来源的关联、证据、活跃状态和缺失次数 |
| `program_sources` | 招聘项目与多个来源的关联 |
| `radar_events` | `NEW`、`UPDATED`、`VERIFIED`、`CLOSED`、`REOPENED` 及项目事件；整数 ID 可作增量游标 |
| `radar_runs` | 每次 scheduled/Quick/Deep/worker/sync 运行的模式、Force 标记、成功/失败/跳过来源计数、错误、AI 调用和 Token 计数 |
| `radar_sync_batches` | `FROSTFIRE_SYNC_V1` 幂等键、payload hash 和重放结果 |
| `radar_locks` | 有过期时间的按类型运行锁与单来源锁 |
| `radar_ai_cache` | 按内容哈希、模型和 schema 版本保存结构化提取结果 |
| `radar_source_snapshots` | 每份最多 20,000 字符、每个来源只保留最近 10 份的规范化页面快照与扫描元数据 |

同一岗位可以由多个来源发现。发现源只提供线索；只有 `trust_level=verification` 的真实官方来源，或数据库中已经可靠标为 `verification_status=verified` 的 `official_html` / `official_api` / `ats` 来源，才具有核验角色。Source API 新建官方来源时，管理员应通过 `trust_level=verification` 明确承担这项信任判断。低信任来源不能用一个 `closed` 字段直接关闭此前仍开放的岗位。

## 3. Source Registry 与适配器

当前支持的适配路径：

| `adapter_config.adapter` | 行为 |
| --- | --- |
| `legacy_database` | 把旧 `recruitment_jobs` 岗位池兼容导入新情报层 |
| `official_html` | 抓取允许访问的公开 HTTPS 页面，做校招标记和内容指纹；可选 AI 结构化提取 |
| `official_api` | 按 `items_path` 和 `field_map` 映射公开 JSON API |
| `public_feed` | 读取公开 RSS/Atom，最多保存受限的文章 discovery 元数据；不直接生成已核验岗位 |
| `public_recruitment_index` | 确定性解析已知的国务院国资委/银行公开招聘索引，生成最小文章线索；不调用模型，不直接生成已核验岗位 |
| `openai_web_search` | 复用现有受限 OpenAI 公共网页补漏管线 |
| `wechat_web_search` | 使用 OpenAI Web Search 搜索指定公众号名称对应的**公网已索引内容**与新官方招聘入口；不读取微信公众号后台、登录态或私有文章历史 |
| `manual` | 只接受受控 push，不主动抓取 |
| `discovery_limited` | 明确报告“发现能力未配置”，不伪造成功 |
| `mock` | 本地确定性 1–5 轮生命周期测试 |

### 已内置的真实公开来源

Source Registry 目前包含以下公开入口。聚合频道仅作为 discovery 线索；企业招聘官网可以明确配置为 verification 核验源：

- `public-iguopin-campus`：国聘网公开校园频道；
- `public-sasac-xiaoxin-existing`：国务院国资委招聘公开列表；确定性解析器优先读取网页入口，失败时尝试其公开移动版入口，只保存文章 discovery 元数据；
- `public-bank-recruitment`：银行招聘网公开索引；确定性解析器过滤导航、商业培训噪声与不安全链接，并把录用公示和在招信号分开标记；
- `official-dji-digital-2027`：大疆招聘官网的 2027 数字管理构建者计划；这是核验角色的真实官方 HTML 来源，使用页面标记做确定性项目/岗位解析，不调用 AI；
- `official-pdd-campus-2027`、`official-china-telecom-campus-2027`、`official-haier-campus-2027`、`official-xiaomi-campus-2027`：官网正文明确存在 2027 校招标记的项目级来源；它们只建立招聘项目，不把“2027 校园招聘”这类项目总称伪装成具体岗位；
- `official-honor-campus-2027`：荣耀官网的 2027 校招项目和官网首页逐字列出的三个“重点校招职位”；只有页面仍同时包含活动标记和具体岗位名称时才生成已核验岗位。由于具体 ATS 详情页是无法由确定性抓取器核验岗位正文的 JavaScript 壳，卡片安全回退到已经核验的荣耀官方概览页；
- `official-xiaomi-top-talent-2027`：小米官网逐字包含“顶尖应届生项目”和“2024年-2027年”时，生成同名的已核验项目入口；页面不再包含这些标记时不会继续生成；
- `legacy-recruitment-pipeline`：现有已核验岗位池；
- `openai-public-web-search`：仅在 `RECRUITMENT_WEB_SEARCH_ENABLED=true` 时启用的公共网页补漏。

公开入口可访问不等于其中每条内容已经成为已核验岗位。系统仍按来源角色、岗位字段和官方 HTTPS 证据分别处理。

国聘页面仍可能只返回 SPA 壳；公开索引也可能临时不可访问或混有社招、旧届、导航与录用公示。系统不会把“页面可访问”当作在招岗位，也不会在索引失败时伪造成功心跳。国务院国资委和银行公开索引只负责确定性地产生标题、公开链接、发布时间、届别与分类线索；它们继续保持 discovery 角色。新增的确定性官网来源不依赖这些聚合页，也不绕过 JavaScript、登录、验证码或反爬限制。

`official_html` 的确定性配置支持 `required_markers` 和 `configured_jobs`。项目必须命中全部活动标记；岗位还必须逐字命中各自 `job_marker`。只有管理员明确标为 `trust_level=verification` 的官方来源才能把这些条目升级为 `verified`。配置的截止日期已经过去、或命中显式 `closed_markers` 时，适配器产生关闭观察；页面暂时丢失标记时采用现有连续两次完整快照缺失规则，不会一次失败就下线。OpenAI 网页搜索、RSS、公众号文章和聚合页本身始终是 discovery；模型自述不能构成核验。只有 discovery 适配器随后确定性打开企业官方 HTTPS 页面，并在该页确认公司、校招、具体岗位和关闭状态的单个条目，才可获得逐条核验凭据；文章记录永远不能核验岗位。

### 五个公众号来源的真实状态

以下五个逻辑来源已在 Source Registry 注册：`国央校招`、`国聘`、`国资小新`、`国央求职网`、`银行招聘网`。

当 `RECRUITMENT_WEB_SEARCH_ENABLED=true` 时，它们使用 `adapter=wechat_web_search`。适配器把来源名称作为检索范围，通过 OpenAI Web Search 查找公网已经索引的公开文章、招聘栏目和企业官方招聘入口；它不是微信账号连接器，不读取微信公众号后台、登录后文章列表、订阅消息、Cookie 或隐藏接口，也不保证完整覆盖某个账号的历史。若搜索没有可靠结果就返回空数组；OpenAI 不可用时该来源报告失败，不伪造成功或岗位。

搜索返回的文章只保存经净化的发布者、标题、公共 HTTPS 链接、公开日期和短摘要，并保持 `snapshot_complete=false`。搜索返回的岗位先按当前届别、校招、城市、截止状态与目标雇主过滤，再确定性打开其企业招聘官网或授权 ATS 页面；只有页面可读、未关闭且支持具体岗位标题的条目才具有逐条核验资格。关闭公网搜索时，五个来源回退到 `discovery_limited`，而不是假装已经直连微信。

公开 RSS/Atom 使用 `public_feed`。解析器拒绝 DTD/外部实体、不安全链接、ChatGPT 会话/分享链接、带凭证参数的 URL 和非公共 HTTPS 地址；响应最多 1.5 MB，超时、条目数与域名访问间隔都有上限。保存前会脱敏邮箱、电话、密钥样式文本和 UUID，只保留标题、链接、公开发布者、发布时间和不超过 1,500 字符的摘要；20,000 字符来源快照也使用相同脱敏。Feed 是滚动窗口，所以 `snapshot_complete=false`；条目从 feed 消失不会据此关闭岗位。公众号文章、RSS 和聚合页都只能承担 discovery 角色，岗位仍需企业官方招聘页面独立核验。

### 本机 ChatGPT 只读桥接

私有 ChatGPT 监控结果不由 Render 抓取。唯一支持的自动路径运行在用户本机：用户先在浏览器中保持登录，本机 Codex 自动任务只读取当前页面可见的助手消息 DOM，从招聘表格单元格和真实锚点中提取允许字段，再把已经脱敏的结构化行交给 `scripts/frostfire_chatgpt_bridge.py`。该路径不读取 Cookie、Authorization、页面存储、隐藏 API、完整会话或用户消息，也不向 ChatGPT 发送消息。

桥接脚本立即摘要化消息游标，本机状态文件只保存逻辑来源和不可逆摘要；岗位按每批最多 10 条提交到 `/api/recruitment/ingest`。服务端先把候选放入隔离区，再重新读取企业官方 HTTPS 页面，核对公司、校招、岗位、日期与关闭状态。未核验、被拒绝或已关闭的候选不会进入公开岗位 API。这个本机流程依赖 Mac、Codex、网络和浏览器登录会话持续可用；它不是 Render 内的 24/7 云直连。

### Source API

所有读取接口都需要用户 JWT。普通用户的手动扫描还要求已同意当前隐私条款；来源写接口继续使用 `X-Admin-Token`，不会把 source config 当作密钥存储；`adapter_config`、`query_config`、`region_config` 中名字包含 `secret`、`token`、`password`、`api_key` 或 `apikey` 的键会被拒绝。来源 URL 必须是公共 HTTPS URL并会被规范化。

创建一个公开 JSON API 来源的示例：

```http
POST /api/future-radar/sources
X-Admin-Token: <ADMIN_DASHBOARD_TOKEN>
Content-Type: application/json
```

```json
{
  "id": "example-official-careers-api",
  "name": "示例企业官方招聘 API",
  "platform": "official",
  "company": "示例企业",
  "source_type": "official_api",
  "url": "https://careers.example.com/api/jobs",
  "enabled": true,
  "priority": 80,
  "trust_level": "verification",
  "interval_minutes": 120,
  "adapter_config": {
    "adapter": "official_api",
    "items_path": "data.jobs",
    "field_map": {
      "external_id": "id",
      "company": "company",
      "title": "title",
      "city": "city",
      "official_url": "detail_url",
      "application_url": "apply_url",
      "opening_date": "opening_date",
      "closing_date": "closing_date",
      "status": "status"
    },
    "snapshot_complete": true,
    "timeout_seconds": 10,
    "domain_delay_seconds": 1
  },
  "query_config": {
    "recruitment_year": 2027,
    "scope": "campus"
  },
  "region_config": {
    "timezone": "Asia/Shanghai",
    "regions": ["中国"]
  }
}
```

公开 RSS/Atom 来源使用同一 Source API，`source_type` 与适配器都设为 `public_feed`，且保持 discovery：

```json
{
  "id": "example-campus-feed",
  "name": "示例公开校园招聘订阅",
  "platform": "rss",
  "source_type": "public_feed",
  "url": "https://example.com/campus.xml",
  "enabled": true,
  "priority": 55,
  "trust_level": "discovery",
  "interval_minutes": 120,
  "adapter_config": {
    "adapter": "public_feed",
    "max_entries": 30,
    "timeout_seconds": 10,
    "domain_delay_seconds": 1
  }
}
```

`trust_level=verification` 只应赋给已确认由招聘主体运营的官方入口；聚合站、镜像、搜索结果和公众号线索应保持 `discovery`。更新使用 `PATCH /api/future-radar/sources/{source_id}`；它接受名称、平台、公司、来源类型、公开 URL、公众号名称/公开账号标识、启用状态、优先级、信任级、间隔和三个 config 对象。`GET /api/future-radar/sources` 只返回可公开的运行健康字段，不回显适配器内部配置、账号标识或密钥。

## 4. API

读取 API 和普通用户手动扫描使用 `Authorization: Bearer <JWT>`；手动扫描同时要求当前版本的隐私同意。来源配置管理 API 使用 `X-Admin-Token`；受控同步使用 `X-Recruitment-Token`。

| 方法 | 路径 | 鉴权 | 作用 |
| --- | --- | --- | --- |
| `GET` | `/api/future-radar/dashboard` | JWT | 汇总近 7 天事件、岗位、项目、来源健康、最近运行和事件游标 |
| `GET` | `/api/future-radar/jobs` | JWT | 岗位分页、个人画像评分与筛选 |
| `GET` | `/api/future-radar/jobs/{job_id}` | JWT | 单岗位、来源链与个人画像评分 |
| `GET` | `/api/future-radar/programs` | JWT | 招聘项目分页 |
| `GET` | `/api/future-radar/programs/{program_id}` | JWT | 单招聘项目与来源链 |
| `GET` | `/api/future-radar/events` | JWT | 事件列表；支持 `after_event_id` 增量游标 |
| `GET` | `/api/future-radar/changes` | JWT | `/events` 的增量兼容别名 |
| `GET` | `/api/future-radar/runs` | JWT | 运行历史分页 |
| `GET` | `/api/future-radar/runs/{run_id}` | JWT | 单次运行结果与错误 |
| `GET` | `/api/future-radar/sources` | JWT | 来源公开健康状态；支持 `enabled` 筛选 |
| `POST` | `/api/future-radar/run` | JWT + 当前隐私同意 | `scan_type=quick`（默认）核对确定性官网、ATS、公开 API、Feed 与旧岗位池，不调用 AI；`scan_type=deep` 运行已配置的 OpenAI Web Search、公众号及新入口发现来源。手动请求不修改也不受 Scheduler 的来源间隔影响；同类型运行冲突返回 409，完成后后端可立即重跑。`source_ids` 只能缩小到对应模式的安全来源集合。`force=true` 还需要 `X-Admin-Token`，可忽略 due time 与 AI 内容缓存，但不能激活禁用来源或绕过并发锁、来源锁、外站限速和安全校验 |
| `POST` | `/api/recruitment/ingest` | Ingest | 接收本机 ChatGPT 桥接的结构化候选；先隔离并重新核验官方 HTTPS 页面，未核验候选不公开 |
| `POST` | `/api/future-radar/sync` | Ingest | 严格接收 `FROSTFIRE_SYNC_V1` 结构化批次 |
| `POST` | `/api/future-radar/sources` | Admin | 创建来源 |
| `PATCH` | `/api/future-radar/sources/{source_id}` | Admin | 更新来源运行配置 |

`GET /jobs` 支持 `page`、`page_size`、`status`、`verification_status`、`company`、`city`、`region`、`employer_type`、`industry`、`program_id`、`source_id`、`q`、`event_type`、`opening_before`、`opening_after`、`closing_before`、`closing_after` 和 `sort=changed|closing|opening|first_seen|company`。公开岗位、项目、详情和相关事件只返回 `verification_status=verified` 的实体；待核验候选仅保留在服务端隔离区和汇总计数中。前端岗位页已经提供搜索、公司、城市、行业、雇主类型、招聘项目、状态、核验、信源、事件、开放/截止日期范围和排序控件；原有雇主星域与 T0–T3（含 0.5 档）筛选继续保留。筛选与分页结果以 Radar API 为准，旧岗位池只为同一岗位补充既有个性化评分，不会再把无关岗位拼回结果集。

来源与运行 API 只返回归一化错误代码和固定安全文案。OpenAI 的原始 429、额度、请求标识及其他 provider 诊断不会写入公开运行记录，也不会从来源健康接口回显；`AI_CREDITS_EXHAUSTED` 只表示 AI 补漏不可用，不影响确定性官网扫描。

`POST /sync` 的 `version` 必须是 `FROSTFIRE_SYNC_V1`。`programs`、`jobs`、`articles` 各最多 10 条，三者合计最多 20 条；现有五源自动桥接可以采用更严格的每批最多 10 条策略。请求可以带 `Idempotency-Key`；未提供时依次使用 `batch_id` 或 payload hash。同一幂等键重放同一 payload 返回原结果，复用到不同 payload 返回 409。未知字段、非 HTTPS URL、包含邮箱/电话号码的 evidence 会被 schema 拒绝。

前端一次并行读取 dashboard、jobs、programs、events、sources 和 runs；某一个接口失败时保留其他已成功区域。只有 Future Radar 对话框可见时才每 30 秒读取增量事件。这个轮询不会触发抓取，也不是后台 scheduler。

## 5. 调度与并发

FastAPI lifespan 启动时会：

1. 初始化数据库与 `future_radar_v1` 迁移；
2. 幂等写入初始 Source Registry；
3. 先执行现有公开来源刷新与最后已核验快照的官网复核；首轮尝试结束后再放行 Radar，避免临时数据库上先把空旧池做成成功快照；
4. 当 `FUTURE_RADAR_ENABLED=true` 时创建进程内调度任务；
5. 调度任务立即运行一次，此后每 `FUTURE_RADAR_DEFAULT_INTERVAL_MINUTES` 分钟唤醒一次；
6. 每次只选择满足各自 `monitor_sources.interval_minutes` 的到期来源；`discovery_limited` 占位不会参加例行运行，也不会被伪装成失败扫描。

扫描使用最多 `FUTURE_RADAR_MAX_WORKERS` 个线程。SQLite 为 `quick`、`deep`、`scheduled` 分别建立 30 分钟租约式运行锁；任务存活期间后台心跳会持续续租，因此长任务超过初始租期也不会产生第二个同类 Run。同类型已有运行时，后端返回 HTTP 409。每个来源另有 20 分钟、同样自动续租的租约式锁：来源忙碌时本轮只跳过它，其他未锁来源继续扫描。不同类型可以同时启动，但若碰到同一来源仍由来源锁消除重复工作；刷新页面、换浏览器或直接调用 API 都不能绕过这些数据库锁。失去有效租约的遗留 `running` 记录会被标记为失败，而仍持有有效续租锁的长任务不会被误判。

手动扫描没有固定的服务端完成后冷却。Run 完成并释放锁后即可再次调用；前端只保留 20 秒防误触 debounce，它不是安全边界。`monitor_sources.interval_minutes` 仅决定自动 Scheduler 何时选择到期来源，Quick/Deep 不读取也不改写该间隔。Force Scan 仍要求普通登录、当前隐私同意和有效 `X-Admin-Token`，不会重新启用禁用来源，也不会绕过运行锁、来源锁、外部站点限速或安全校验。扫描请求只记录用户 ID、路由、状态码和耗时等无正文审计元数据。这个设计面向单实例 SQLite，不等同于跨主机分布式调度。

### 手动扫描模式

`Quick Scan` 的流程是：选择已启用的确定性来源 → 获取公开官网/ATS/API/Feed 或读取旧岗位池 → 计算内容指纹并规范化 → 核验、合并和生成差异事件。即使某个官网来源配置了可选 AI 提取，Quick 也会在本轮强制关闭它。

`Deep Scan` 的流程是：选择已启用的发现类来源 → 运行十类重点雇主 OpenAI Web Search，并为五个公众号逻辑来源搜索公网已索引页面与新官方 URL → 保存最小文章线索、过滤岗位候选并逐条访问官方 HTTPS 页面 → 仅让通过确定性官网核验的条目进入公开岗位池。Deep 不访问微信公众号后台，也不读取本机 ChatGPT 会话。Deep 完成后同样没有固定手动冷却；重复点击期间由数据库 Run Lock 防止重复调用 OpenAI。

`Force Scan` 是 Quick/Deep 请求上的管理员选项。它忽略自动来源 due time 和 AI 内容缓存读取，但不允许制造相同并行任务，也不绕过域名/供应商的安全限速。

### 一次性 Worker CLI

```bash
python -m backend.future_radar.worker --once
python -m backend.future_radar.worker --once --source legacy-recruitment-pipeline
python -m backend.future_radar.worker --mock-round 1
```

CLI 初始化同一数据库、写入来源种子、执行一次扫描后退出。它适合本地、人工运维，或未来迁移到可靠共享数据库后的受控调度。当前 `render.yaml` **不会**增加独立 Worker：Render Free Web Service 与另一个 Worker 不能可靠共享这份临时 SQLite，双进程反而会造成状态分叉和重复调度。

## 6. Mock 1–5 轮

Mock 来源默认禁用，只用于测试。CLI 的 `--mock-round` 会临时启用并强制运行该来源。

| 轮次 | 预期变化 |
| --- | --- |
| 1 | 创建 5 个招聘项目和 10 个岗位，发出 `NEW` |
| 2 | 保留前 10 个岗位并新增 2 个岗位 |
| 3 | 更新第 1 个岗位的截止日期，发出 `UPDATED` |
| 4 | 把第 2 个岗位标为关闭，发出 `CLOSED` |
| 5 | 第 2 个岗位重新开放，发出 `REOPENED` |

重复同一轮会命中相同内容哈希，不重复建岗位或发事件。缺失关闭阈值、来源失败隔离和多来源合并由单元测试另外覆盖。

## 7. OpenAI 结构化提取、缓存与降级

`official_html` 来源只有显式设置 `adapter_config.ai_extract=true` 时才具备 AI 提取能力；Quick Scan 会临时强制关闭它。自动 scheduled 扫描按页面内容指纹复用结构化提取缓存；用户主动发起的 Deep Scan 每次都会跳过缓存并重新提取，管理员 Force Scan 也会跳过缓存。岗位与事件仍通过业务内容哈希去重，不会因为重新提取而重复入库。实现使用 OpenAI Responses API：

- 页面正文被视为不可信数据，不执行页面中的指令；
- 输入最多截取 32,000 字符；
- `text.format` 使用 strict JSON Schema，最多返回 10 个项目和 30 个岗位；
- `max_output_tokens=1800`，`store=false`；
- 最多尝试两次；
- 缓存键为 `schema_version + model + content_hash` 的 SHA-256；
- 缓存命中不调用模型，记录 0 新 Token；
- 运行表记录 AI 调用数和模型实际 input/output Token 合计。

AI 提取失败时会记录警告并降级：已经完成的确定性页面抓取、校招关键词识别与项目线索仍可继续，AI 派生的岗位不会被伪造。一个来源失败或 OpenAI 暂时不可用也不会停止其他确定性来源。当前实现仍会把已经成功抓取的页面指纹记为该来源最新哈希，因此同一页面内容不变时不会反复花费 Token 重试 AI；需要等待页面发生变化后再提取。`openai_web_search` 是另一条现有补漏适配器，不共享上述页面结构化提取缓存。

## 8. 环境变量

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `FUTURE_RADAR_ENABLED` | `true` | 是否在 FastAPI 进程内启动 Future Radar scheduler |
| `FUTURE_RADAR_DEFAULT_INTERVAL_MINUTES` | `30` | scheduler 唤醒间隔，最小 5 分钟；来源自身间隔仍单独生效 |
| `FUTURE_RADAR_CLOSE_CONFIRMATIONS` | `2` | 完整成功快照连续缺失多少次后解除来源关联，限制 2–10 |
| `FUTURE_RADAR_MAX_WORKERS` | `4` | 单次扫描线程上限，限制 1–8 |
| `FUTURE_RADAR_AI_MODEL` | `RECRUITMENT_WEB_SEARCH_MODEL`，否则 `gpt-5.4-mini` | 公开 HTML 结构化提取模型 |
| `OPENAI_API_KEY` | 无 | AI 提取和可选 OpenAI 网页补漏；必须只放服务端环境 |
| `RECRUITMENT_WEB_SEARCH_ENABLED` | 本地示例 `false`；当前 Render Blueprint `true` | 是否启用 `openai-public-web-search` 与五个 `wechat_web_search` 公网 discovery 来源 |
| `RECRUITMENT_WEB_SEARCH_MODEL` | `gpt-5.4-mini` | 重点雇主与公众号公网 Web Search 模型 |
| `RECRUITMENT_INGEST_TOKEN` | 无 | `/api/recruitment/ingest` 与 `/api/future-radar/sync` 的共享接收密钥 |
| `ADMIN_DASHBOARD_TOKEN` | 无 | 汇总使用面板、Source 配置 API 与 Force Scan 的管理员密钥；普通 Quick/Deep 不使用此 Token |
| `DATABASE_PATH` | 项目现有默认 | Future Radar 与主应用共用的 SQLite 文件 |

所有密钥继续由本机 `.env`、Keychain 或 Render Secret 管理；不得写入 source config、Git、README 日志示例或同步 payload。

## 9. 测试

安装开发依赖后运行专用测试：

```bash
source backend/.venv/bin/activate
python -m pytest -q backend/tests/test_future_radar.py
```

该测试覆盖 1–5 轮生命周期、同轮幂等、连续缺失关闭、失败隔离、多来源合并/核验升级、OpenAI 失败降级、sync 幂等冲突、HTML/空白非语义变化、公众号公网 Web Search、关闭搜索时的 `discovery_limited` 回退、国务院国资委/银行公开索引、正文不可访问时的公开文章 metadata 保留与文章事件，以及 Quick/Deep/Force、运行锁、来源锁、刷新不可绕过锁、API 分页/组合筛选/鉴权/严格 schema。本机桥接的脱敏、分批、哈希游标与隔离提交由 `tests/scripts/test_frostfire_chatgpt_bridge.py` 覆盖。

使用独立临时数据库做 CLI 冒烟测试，避免污染开发数据：

```bash
RADAR_SMOKE_DB="$(mktemp /tmp/frostfire-radar.XXXXXX.db)"
for RADAR_ROUND in 1 2 3 4 5; do
  DATABASE_PATH="$RADAR_SMOKE_DB" \
    python -m backend.future_radar.worker --mock-round "$RADAR_ROUND"
done
```

完整回归与前端构建：

```bash
python -m pytest -q
cd frontend
npm run build
```

## 10. Render Free 的真实限制

当前 `render.yaml` 保持一个 `plan: free` 的 Web Service，不附加持久盘，也不创建独立 Worker。

- Free 实例空闲后会休眠；休眠期间 FastAPI 进程不存在，进程内 scheduler 不会执行，因而不能承诺固定间隔或“全天实时”监控。
- `DATABASE_PATH=/app/backend/data/ai_chat.db` 位于实例临时文件系统。重启、重新部署或实例替换可能丢失来源状态、运行游标、事件、缓存和岗位历史。
- 冷启动后 scheduler 会再次启动并运行到期来源，但这不能恢复已经丢失的 SQLite 历史，也不能补偿所有休眠期间的错过扫描。
- 增加一个独立 Free Worker 不能解决问题：它有自己的文件系统，无法可靠共享 Web Service 的 SQLite。
- 现有部署适合演示和小规模测试，不满足长期、可审计、持续监控的生产 SLA。

在不重构数据库的前提下，最小可持续方案是：单个常开 Render 服务、为该服务挂载持久盘、继续只运行一个进程内 scheduler，并对数据库做备份和运行失败告警。仓库目前没有执行这项付费升级。需要多实例或独立 worker 时，应先把 Future Radar 持久层迁移到真正的共享数据库（例如托管 PostgreSQL），再使用单独的调度器/worker、跨进程锁、重试与可观测性；不能直接让多个进程写各自的 SQLite。

## 11. 当前仍存在的边界

- 五个公众号逻辑来源通过 OpenAI 公网搜索发现公开线索，而不是微信公众号后台直连；搜索引擎未索引、需要登录或被平台限制的内容仍不可见，不能据此承诺完整账号历史。
- 私有 ChatGPT 的自动桥接只在用户本机、已登录浏览器和 Codex 在线时读取可见助手消息 DOM；Render 不持有登录态，也不读取 Cookie、隐藏 API 或完整会话，因此该桥接不是 24×7 云服务。
- 通用抓取层目前覆盖公开 JSON API 和普通 HTTPS HTML；没有把 Playwright 作为任意动态 ATS 的通用后端 fallback。完全依赖 JavaScript、需要登录或有技术访问限制的 ATS，需要单独确认公开 API 或开发合规专用适配器。
- OpenAI 补漏是低频 discovery，结构化 AI 只理解发生变化的公开页面；它不是无限自主 Agent，也不会自动把每条第三方线索无条件升级为官方事实。新增未知官网仍需要通过 Source Registry 建立并确认 verification 角色。
- Source Management 第一版是受管理员 Token 保护的写 API 加前端只读健康页，尚未提供完整的可视化来源编辑器。
- 当前使用站内 30 秒增量轮询，没有 SSE、WebSocket、系统级推送或已读红点；这些不影响服务端扫描与落库。
- 公司实体目前使用规范化名称去重，没有面向管理员的可编辑别名表；复杂简称仍可能需要在来源映射或后续 resolver 中补充。
- Render Free、单实例 SQLite 和进程内 scheduler 的持续性限制见上一节；因此当前代码是可运行 MVP，不应宣传为 24×7 生产 SLA。
