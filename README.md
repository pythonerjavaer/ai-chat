# 冰焰 · Frostfire

一个面向合同合规与金融文档研究的证据驱动 AI 工作台，也是一个可创建 AI Space“成果胶囊”的轻量平台底座。法律工作台采用寒冰蓝视觉和“条款地图”，金融工作台采用烈焰橙红视觉和“信号面板”；两者共享同一套账号、私人资料库、来源引用和流式对话能力。

当前仓库同时包含 Web、iOS 和 Android 工程。后端使用 FastAPI、SQLite（本地）或 PostgreSQL / Supabase（持久部署）与 OpenAI API；前端使用 Vite、原生 HTML/CSS/JavaScript 和 Capacitor。

## 已实现

- 用户注册、登录、JWT 鉴权、隐私同意记录与应用内永久删号；用户数据相互隔离。
- 管理员汇总使用面板：访问 `/?admin=usage` 后以 `ADMIN_DASHBOARD_TOKEN` 解锁，约每 10 秒刷新注册量、活跃量、会话/消息/文档、API 请求、已记录的普通聊天与 AI Space 模型调用及 Token；不查询或展示用户名、消息正文、文档内容、密码、Prompt 或模型回复。内容无关的 API 使用事件最多保存 30 天。
- 会话、消息、文档文本和向量统一保存到所配置的数据库。开发环境保留 SQLite；`DATABASE_BACKEND=postgres` 使用私有 PostgreSQL schema 与小连接池，可接入 Supabase。缺失 PostgreSQL 配置时不静默回退到 SQLite，Render 启动强制要求持久数据库。工作区之间不会交叉检索资料。切换前必须按确认的保留范围备份并校验；完整迁移与仅保留公开机会、重新注册是两条不同路径。
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
- 未来雷达：岗位/职能、十类雇主星域、行业与城市均为真实筛选条件；同一字段内取并集，不同已填写字段之间同时生效。星域切换会在服务端完整岗位池中先筛选、再分页，因此总数与翻页结果会同步变化。T 级评价对象是具体岗位，而不是公司 Logo：按最初规则直接累加平台层级 16、岗位质量 15、复合背景 14、职业方向 12、长期上限 12、迁移能力 8、资格匹配 7、薪酬福利 6、可持续性 5、城市地域 3、继续教育 2，共 100 分；四组摘要只用于界面阅读，不再进行第二次 35 / 45 / 10 / 10 加权。详细页展示 11 维原始分、机构基准与单列的校准调整，不为凑最终分改写证据分项。T0 ≥90，T0.5 为 85–89，T1 为 80–84，T1.5 为 75–79，T2 为 70–74，T2.5 为 65–69，T3 为 60–64；低于 60 分不进入重点池，缺少具体岗位或 JD 依据时明确显示“未评分”。
- 机构层级校正：从实际招聘单位与明确岗位署名区分集团总部/总行、省级/区域分支、地市、县区网点、子公司、研究机构及外包/派遣关系；普通分支不继承母集团的完整平台分。评级详情展示平台基准、实际单位平台分与识别依据，未知层级不加总部分。岗位分优先读取具体职责，兼容 ATS 将“工作描述”放在任职要求字段的情况；集团介绍、可报专业列表和旧角色标签不能代替岗位职责。未披露的薪酬保持中性，央企或运营商标签不自动带来待遇、轻松工作或培养路径加分。评级规则变化自动使旧评分缓存失效，不删除已有机会或缩小监控范围。
- Future Radar 服务端情报层：`backend/future_radar` 在不删除旧 `/api/recruitment/*` 的前提下增加 Source Registry、招聘项目、岗位、多来源证据、运行记录、差异事件、内容哈希、关闭确认、幂等同步和分页 API。FastAPI 进程负责扫描到期来源；前端每 30 秒读取已落库的增量事件和统一机会池，不把浏览器轮询伪装成后台抓取。完整架构、数据表、API、来源配置、Mock 生命周期与部署限制见 [`docs/FUTURE_RADAR.md`](docs/FUTURE_RADAR.md)。
- Future Radar 内置的确定性官网监控会同时核对活动标记与具体岗位名称。官网总入口仅能证明招聘活动时形成招聘项目，不把“校园招聘”总称冒充具体岗位；已接入的运营商 ATS、明确岗位详情和结构化 JobPosting 数据按实际返回内容逐岗处理。国聘等 JavaScript/聚合入口、OpenAI 搜索、RSS 或公众号可提供 discovery 线索；具备公开招聘链接和校招语义的线索可直接供登录用户查看，只有通过官方来源核验后才标记“官网已确认”。
- 已建立十组监控范围：央企能源与资源、央企科技通信与交通、烟草与高等级专卖体系、银行与政策性金融、券商公募资管、保险综合金融、互联网大中厂、快消外企咨询、量化私募对冲、四大专业服务。跨属性机构可保留多个行业标签，同时使用一个主星域分类；监控清单是高信号搜索范围，不代表每家单位当前均有开放岗位。主池合并符合条件的已核验岗位与公开招聘线索，不把企业名录数量当作岗位数量。
- 全名录 Deep Scan：搜索目标与左侧十类名录共用数据源，当前 218 个条目经别名合并为 205 家雇主。每家单独发起联网搜索，最多并发 8 家；只有完成真实搜索调用和结构解析才计为成功，失败逐家记录。统一机会池显示配置范围、最近一轮完成数和失败数，不用模型自报的企业数量冒充覆盖率。
- 官网详情补采：已启用详情发现的官网源，以及逐家搜索得到的真实官方引用，可继续通过确定性读取发现同源列表、分页、岗位详情和 JSON-LD JobPosting。空白 JavaScript 页面、失败页及未读完的分页会记录为部分覆盖；一次搜索成功不等于官网岗位全部读取。单雇主读取预算、页数和超时可配置，任务遵守原有来源锁与安全检查；部分结果不会用于批量关闭此前仍有效的岗位。
- 企业分组浏览：机会池前端默认进入“均衡精选”并按企业展示。该视图只从 T0–T3 与未评分线索中做可逆的浏览投影：十个星域轮转、每个星域最多 24 条、每个展示企业最多 3 条，避免单个运营商或逐岗 ATS 数据淹没首页；同一中性上限适用于所有星域，不会虚构稀缺星域的岗位，也不删除岗位或修改 T 级。“全部重点”保留完整 T0–T3 与未评分线索，“全部记录”和“次级机会”继续查看其余记录。提供“按企业／按岗位”切换，后端先对全池应用星域、城市、来源及搜索条件，再计算均衡/重点/T 级投影、统计和分页；企业展开继承同一投影，不会重新获得配额。侧栏分别标明当前筛选的企业组数和机会条数。运营商品牌归组只用于浏览，不改招聘主体、总部／分支判断、岗位评分或原始链接。API 默认 `view=jobs`、`priority_only=false`、`balanced_only=false` 保持兼容，并支持 `view=companies`、`company_key`、`total_companies` 和 `total_opportunities`。
- 行业分类补全：搜索与入库共用 `backend/recruitment_directory.py` 的雇主名录和明确别名，不把岗位描述中的“科技／量化／咨询”直接当作雇主行业。一次性迁移修复已有岗位的分类与对应内容哈希，不删除数据、不改日期、来源、核验状态或招聘主体；企业层级和评分公式不变，行业资料补全可能使分数按原规则重算。目录外且缺少明确组织信息的企业仍保留未分类。
- 多源职位身份：Workday 链接在招聘站点、雇主、完整职位号和届次相符时合并展示，语言、标题写法或城市待确认不会导致同一职位重复出现；保留所有来源与原 ID 的详情入口。不同职位号、招聘单位、地区专项或项目范围不会仅因共用招聘首页而合并。
- 统一机会池：登录后默认使用 `/api/future-radar/opportunities`，合并已核验岗位、聊天线索和搜索发现；“官网已确认／聊天线索／搜索发现／信息有差异”只说明来源和核验状态，不是查看门槛。具体雇主发布的当届校招或管培项目也可作为“招聘项目”查看；未细分到具体岗位时不生成岗位 T 级，不把项目虚构成多条职位。按企业别名、具体岗位、城市和届别去重，保留出处并优先采用已核验记录；全量匹配后统计 T 级、筛选和分页，不仅筛当前页。默认隐藏已关闭、当天截止、过期、明确非校招或被拒绝的记录；未知日期不等于过期。非空受控同步提交后立即尝试刷新本地投影，不调用 AI；遇到运行锁时保留数据，交由下一次 Quick Scan 更新。页面打开时每 30 秒轻量刷新。原 `/api/future-radar/jobs` 仍为已核验岗位兼容接口，`/api/future-radar/search-updates` 保留搜索档案接口，不再要求用户切换到独立候选池。
- 动态源适配器：服务启动后按 `RECRUITMENT_REFRESH_MINUTES`（默认 30 分钟）扫描国聘网、国资小新、银行招聘网等公开招聘页面，并支持配置 Adzuna API 凭证。公开来源会先做校招标题、重点雇主等规则过滤；写入 Future Radar 的有效公开招聘线索可以在登录后的主池查看，保留真实的来源与核验状态。
- 可选 OpenAI 公网搜索补漏：设置 `RECRUITMENT_WEB_SEARCH_ENABLED=true` 后，服务按 `RECRUITMENT_WEB_SEARCH_INTERVAL_MINUTES`（默认 360 分钟）搜索十类重点雇主及五个公众号逻辑来源的公开网页索引。公众号路径使用 `wechat_web_search` 发现已经被公网索引的公开文章与官方招聘入口，并不是读取微信公众号后台、登录后历史或私有接口。结果会过滤非目标届别、社招、当天截止/过期/尚未开放条目、搜索结果页和社交媒体链接；模型提交的日期只有在官方页出现同一日期时才会采用。网页搜索与 Future Radar 结构化提取默认使用 `gpt-5.4-mini`，每次调用记录工具次数和实际 Token，并产生 OpenAI API 费用。
- 行动卡片准入：主池展示带可点击公开 HTTPS 申请或公告链接，并有公司、岗位与校招语义的机会。无链接、通用招聘导航、明确非校招、被拒绝、当天截止及已过期岗位不会默认展示；不会仅因地点待确认、官网使用 JavaScript 或未完成核验而拒绝一条有效线索。来源标注日期与官网已确认日期分别展示；未知截止日期不触发截止预警。
- 受控同步已核验快照：仓库只保存经受控同步接收、并由服务端重新核验的公开岗位字段，不保存会话 ID、Cookie、接收 Token 或私密聊天内容。Render Free 冷启动后会逐个重新打开官方页面；只有仍通过核验的岗位才恢复，页面已关闭的岗位会下线，临时无法访问的岗位不会在空数据库中盲目恢复。
- 首页截止预警：登录后直接显示 7 天内到期的已核验机会，无需先打开未来雷达；截止日当天及更早的岗位不会从岗位 API 返回。只有原公告明确标注的截止日期才会触发预警。
- 我的官网变化雷达：每个账号可添加最多 12 个公开 HTTPS 企业招聘页和关注关键词。抓取前会拒绝账号信息、本机/内网/保留地址、非标准 HTTPS 端口和不安全跳转，并限制响应类型、大小与超时。
- 官网变化检测完全使用可见文本规范化、SHA-256 指纹和确定性关键词匹配，不把招聘网页发送给模型，也不消耗 OpenAI Token。首次检查只建立基线；服务清醒时会定时比较变化，用户也可手动刷新。最近变化会在应用首页持续显示“去核对”提醒，直到用户标记为已核对。
- 动态监控接收 API：受 `RECRUITMENT_INGEST_TOKEN` 保护，供另行部署并获得授权的外部任务写入结构化校招岗位。每次 HTTP 请求最多 100 个岗位，监控运行没有总条数配额；提交器用 `--batch-size` 控制请求大小，默认 25、最大 100，连续处理全部更新。也支持 `{"jobs":[],"source_id":"chatgpt-radar-01","source_updated_at":"<ISO 8601>"}` 空结果心跳；批次和岗位均拒绝未声明字段。七个活动逻辑来源为 `chatgpt-radar-01`、`02`、`03`、`06`、`07`、`08`、`09`；退役 `04`、`05` 停止新增监控，但保留历史游标、事件与候选。契约记录稳定条目/外部 ID、来源更新时间和简短证据，并提供只含聚合状态的同步状态接口；真实会话 ID 不提交、不入库、不进 Git。可选 `source_thread_id` 只用于兼容其他来源，服务端至多保留不可逆短哈希。当前仓库仍不包含一个永不休眠、覆盖全网的外部采集服务。
- 七源本机只读桥接：用户已经登录的本机浏览器可由 Codex 自动任务读取页面中**当前可见的助手消息 DOM**，只提取招聘表格或具体岗位条目及真实公开 HTTPS 锚点，不要求原消息采用 JSON 格式；引用标记缺少链接时须读取实际锚点，不能猜测 URL。脱敏字段交给 `scripts/frostfire_chatgpt_bridge.py` 分批并维护本机哈希游标。桥接不读取 Cookie、Authorization、隐藏接口、页面存储或完整会话，也不向 ChatGPT 发送消息；Render 服务端不会登录或直接访问这些私有会话。浏览器结果提交到 `/api/recruitment/ingest` 后，服务端核对公司、校招、岗位、日期与关闭状态并保留结果；有有效公开招聘链接的未确认线索可直接进入登录后的统一机会池，不冒充“官网已确认”，明确拒绝、关闭和过期条目不会进入默认列表。
- 多消息历史回填：`scripts/frostfire_chatgpt_history.py` 接收已脱敏的多条招聘记录，按稳定岗位 ID 去重，默认每次 HTTP 请求 25 条，可用 `--batch-size` 调整至 1–100 条，持续处理所有更新。单个输入页保留 10,000 行及字节安全边界，超出后分页续传，不作为每轮监控的总量配额。所有批次先经过真实 ingest dry-run，提交仅从本机 Keychain 读取接收 Token。成功回执写入仓库外的纯摘要账本，中断后只补未确认条目，重试可以调整请求大小；不相交的旧历史片段不能覆盖已提交的新版本。未遍历完整来源时保留 `history_complete=false`，不把“本批处理完毕”称为“全部历史已同步”。
- 原始来源评级：可选 `source_rating` 原样保留明确的 T 级、数值分数、理由及岗位／公司作用范围，并记录来源。具体岗位评级可应用于该岗位，公司评级仅作公司参考；缺失评分或仅有 P 类优先级时不臆造 T 级。评级修正参与内容哈希和增量更新，冲突来源保留待核对。来源评级与官网核验状态独立，不能使待核验线索变成“官网已确认”。
- 公众号与公开索引：五个公众号逻辑来源在 Deep Scan 中通过 OpenAI 公网 Web Search 做 discovery，不直接抓取微信公众号后台，也不绕过微信登录、验证码或反爬。国务院国资委招聘列表（含公开移动版 fallback）和银行招聘网由确定性解析器生成最小文章线索；公开 RSS/Atom 与用户提供的公开文章也可产生 discovery。有效公开招聘线索可直接查看，企业官方招聘 HTTPS 页面核验用于增加“官网已确认”标记，而不是阻止用户查看发现。
- 指定 ChatGPT 监控对话已由用户用于筛选岗位，其有效条目以 `source_screened`（ChatGPT 已筛选）直接入池，不等待官网再次读取成功；原有符合条件的 pending 数据会本地迁移，保留 ID、来源日期与评级。`source_screened` 与官网 `verified` 分开计数，不冒充官方确认。明确过期、关闭或不安全记录不显示为当前开放岗位，历史仍持久保存。其他来源继续按原官网核验规则处理。外部 `evidence` 仅保留单行招聘事实短句（最多 12 条、每条 1–280 字符），不包含邮箱、电话号码或私人对话。稳定身份去重与旧版本不能覆盖新版本的规则不变。
- 外部监控 OpenAPI 契约见 [`docs/RECRUITMENT_INGEST_OPENAPI.yaml`](docs/RECRUITMENT_INGEST_OPENAPI.yaml)，七源桥接、Secret、heartbeat 与幂等说明见 [`docs/CHATGPT_RADAR_BRIDGE.md`](docs/CHATGPT_RADAR_BRIDGE.md)。契约已指向 `https://frostfire-ai.onrender.com`；发送方只能配置 Render 生成的接收 Token，不能写入 ChatGPT Cookie 或 OpenAI API Key。
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
- 旧 Render Free SQLite 实例没有账户数据持久化保证；新配置不会自动从旧实例取出数据库。保留旧账号和聊天需要完整 SQLite 备份；只有明确同意重新注册时，才能单独恢复完整公开机会快照并更换登录签名密钥。公开岗位 JSON 不是账号、会话、文档和全部扫描数据的备份。两条迁移路径见 [`docs/PERSISTENT_DATABASE.md`](docs/PERSISTENT_DATABASE.md)。

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
│   ├── storage.py
│   ├── certs/
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
│   ├── PERSISTENT_DATABASE.md
│   ├── RECRUITMENT_INGEST_OPENAPI.yaml
│   ├── STORE_LISTING_ZH.md
│   └── STORE_RELEASE_CHECKLIST.md
├── scripts/
│   ├── frostfire_chatgpt_bridge.py
│   ├── frostfire_ingest.py
│   ├── frostfire_database_migrate.py
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
FUTURE_RADAR_AI_MODEL=gpt-5.4-mini
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

### Render + Supabase 持久部署

根目录 `render.yaml` 保留 Render Free Web Service，不增加付费磁盘；数据库改为外部 PostgreSQL。Supabase 的 IPv4 Session Pooler 可用于常驻后端，连接串通过 Render Secret `DATABASE_URL` 配置。使用 `sslmode=verify-full`，并将 `sslrootcert` 指向镜像内 `/app/backend/certs/supabase-prod-ca-2021.crt`。证书是官方公开 CA，不是密钥。不要将数据库密码放进前端环境变量或 Git。

**现有服务不能直接无备份重部署。** 保留全部原数据时，先用 `scripts/frostfire_database_migrate.py --source <完整SQLite备份> --dry-run` 检查，再导入并核对全部表。若明确选择不迁移旧账号和聊天，则先导出完整公开机会快照，使用 `scripts/frostfire_public_pool_restore.py` 验证并恢复原机会 ID、状态和招聘链接，同时更换 `JWT_SECRET` 使旧会话失效。仅在原用户身份完整迁移时才可保留原登录签名密钥。迁移失败不会覆盖不同的目标已有数据，也不会改用临时库。Supabase 项目创建成功不等于数据已迁移。

外部数据库让已经写入的数据不依赖 Render 容器生命周期，但不会消除 Free Web Service 的休眠。休眠期间进程内招聘刷新仍不会执行；Supabase Free 自身也存在资源与暂停限制。这里只启用同一 Web Service 内的 Future Radar scheduler，不声称免费组合是 24/7 扫描服务。

部署时必须在 Render 的环境变量页面填写 `DATABASE_URL`、`OPENAI_API_KEY`、`CORS_ORIGINS` 和一个自行保存的强随机 `ADMIN_DASHBOARD_TOKEN`；全新部署的 `JWT_SECRET` 与 `RECRUITMENT_INGEST_TOKEN` 可由 Blueprint 生成。迁移已有服务时保留接收凭证 `RECRUITMENT_INGEST_TOKEN`；`JWT_SECRET` 仅在完整保留原用户身份时保持原值，重置账号库时必须主动更换，不能依赖 Blueprint 的生成配置自动轮换。管理员面板入口是 `/?admin=usage`，Token 只保存在当前页面内存。`FUTURE_RADAR_DEFAULT_INTERVAL_MINUTES=30` 只表示清醒进程的调度唤醒间隔；各来源仍按自己的间隔判断是否到期，其中 OpenAI 公共网页补漏默认为 360 分钟。如需零额外模型费用，可将 `RECRUITMENT_WEB_SEARCH_ENABLED` 改为 `false`。Future Radar 的长期运行边界见 [`docs/FUTURE_RADAR.md`](docs/FUTURE_RADAR.md)。

需要同步 ChatGPT 监控结果时，由本机 Codex 自动任务在用户已登录的浏览器中只读可见助手消息，将脱敏后的结构化行传给 `scripts/frostfire_chatgpt_bridge.py`。脚本先 dry-run，再从 macOS Keychain 读取接收凭证并提交；游标文件只保存逻辑来源和消息摘要，不保存会话地址、消息正文或官方链接。也可继续使用用户主动导出的结构化 JSON 或公开分享快照，但二者都是显式导入，不等于云端账号直连。本机自动同步依赖 Mac、Codex、网络和浏览器登录会话持续可用；Render 不会替它读取私有页面，因此这不是 24/7 云直连。完整边界见 [`docs/CHATGPT_RADAR_BRIDGE.md`](docs/CHATGPT_RADAR_BRIDGE.md)。外部持久数据库与定时同步分别解决保存和导入问题，不消除免费实例的冷启动和漏跑。

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
| `POST` | `/api/recruitment/ingest` | 使用 `X-Recruitment-Token` 接收每个 HTTP 请求最多 100 个校招岗位或一个空结果来源心跳；多请求处理全量更新 |
| `GET` | `/api/recruitment/sync/status` | 使用 `X-Recruitment-Token` 查询七个活动来源最新事件计数、历史候选库存与最近事件 |
| `GET` | `/api/future-radar/dashboard` | Future Radar 汇总、来源健康、最近运行与事件游标 |
| `GET` | `/api/future-radar/opportunities` | 登录用户统一机会池：已核验岗位与有效公开招聘线索；全量去重、筛选、均衡精选/重点/T 级投影、计数和分页 |
| `GET` | `/api/future-radar/opportunities/{job_id}` | 单个机会、来源标签、核验状态与脱敏出处 |
| `GET` | `/api/future-radar/jobs` | 已核验岗位兼容接口，保留分页、筛选与个人画像评分 |
| `GET` | `/api/future-radar/jobs/{job_id}` | 单个已核验岗位与多来源信息 |
| `GET` | `/api/future-radar/search-updates` | 搜索档案兼容接口，保留候选及核验状态，不是默认主池 |
| `GET` | `/api/future-radar/programs` | 招聘项目分页 |
| `GET` | `/api/future-radar/programs/{program_id}` | 单个招聘项目与来源链 |
| `GET` | `/api/future-radar/events` | 事件列表与 `after_event_id` 增量读取 |
| `GET` | `/api/future-radar/changes` | Future Radar 事件增量兼容别名 |
| `GET` | `/api/future-radar/runs` | 扫描运行历史分页 |
| `GET` | `/api/future-radar/runs/{run_id}` | 单次扫描的计数与错误 |
| `GET` | `/api/future-radar/sources` | Source Registry 的公开健康字段 |
| `POST` | `/api/future-radar/run` | 已登录且同意当前隐私条款的用户启动 `quick`（默认）或 `deep` 扫描；手动扫描不受 Scheduler 间隔影响，同类型运行并发冲突返回 409，完成后后端可立即再次运行；管理员可附带 `X-Admin-Token` 使用 `force` |
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
npm test
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

新 Future Radar 情报层的 1–5 轮生命周期、幂等、关闭确认、失败隔离、多来源合并、AI 降级、公众号公网发现、公开索引与 API 测试：

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
