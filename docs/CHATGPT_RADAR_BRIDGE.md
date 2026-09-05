# ChatGPT 本机只读桥接 / 公众号公网发现 → 冰焰未来雷达

当前实现有两条彼此独立的 discovery 路径，二者都不能仅凭第三方描述授予“官网已确认”状态：

1. **私有 ChatGPT 本机只读桥接。** 用户在自己的 Mac 浏览器中保持登录，本机 Codex 自动任务只读取页面已经渲染出来的助手消息 DOM，从招聘表格单元格和真实锚点中提取允许字段。它不读取 Cookie、Authorization、页面存储、隐藏 API、完整会话或用户消息，也不向 ChatGPT 发送内容。Render 不登录 ChatGPT，也不直接访问私有会话。
2. **五个公众号逻辑来源的公网发现。** Future Radar 的 `wechat_web_search` 使用 OpenAI Web Search 搜索公网已经索引的公开文章、招聘栏目和企业官方招聘入口。它不是微信公众号后台连接器，不读取登录后文章列表、订阅消息或私有历史，也不绕过登录、验证码和平台限制。

此外，国务院国资委招聘列表及其公开移动版入口、银行招聘网公开索引由确定性解析器生成最小文章线索；公开 RSS/Atom、用户主动提供的公开文章和结构化 JSON 仍可受控导入。这些来源全部只是候选或文章 discovery，不能替代企业官方招聘 HTTPS 页面核验。

公网 Web Search 与 Future Radar 的结构化提取默认使用 `gpt-5.4-mini`；聊天产品本身的默认模型仍由独立的 `AI_MODEL` 配置决定。

## 七个活动逻辑来源

活动 `source_id` 为 `chatgpt-radar-01`、`chatgpt-radar-02`、`chatgpt-radar-03`、`chatgpt-radar-06`、`chatgpt-radar-07`、`chatgpt-radar-08`、`chatgpt-radar-09`，共七个可公开的稳定逻辑槽位。本机自动任务中的页面映射只保留在本地任务配置；私有会话地址、真实会话标识、消息正文和登录信息均不进入 Git、数据库、README、日志或提交 payload。网页内容中的 `source_id` 也不能覆盖本机指定的逻辑来源。

`chatgpt-radar-04`、`chatgpt-radar-05` 已退出活动监控，新输入不再使用这两个槽位；其历史游标、摘要回执、事件、候选与来源记录继续保留，不因调整活动名单而重置。新注册槽位在实际收到成功回执前保持待同步；注册不会伪造已经读取或同步成功。前端优先使用后端返回的 `expected_source_count` 展示活动来源数量。

## Secret 管理

服务端使用 Render 环境变量 `RECRUITMENT_INGEST_TOKEN`；本机提交器读取名称不同但值相同的 `FROSTFIRE_INGEST_TOKEN`。优先把 Token 放入 macOS Keychain 的通用密码项目：

- service：`frostfire-recruitment-ingest`
- account：当前 macOS 用户名
- password：Render 中 `RECRUITMENT_INGEST_TOKEN` 的值

可在“钥匙串访问”图形界面创建，也可以让 `security` 在交互式终端中直接提示输入密码。这样 Token 不会进入 shell 历史、环境变量或进程参数：

```bash
security add-generic-password -U \
  -a "$USER" \
  -s frostfire-recruitment-ingest \
  -w
```

通用 `frostfire_ingest.py` 与 `frostfire_source_import.py` 兼容受控进程环境中的 `FROSTFIRE_INGEST_TOKEN`；浏览器桥接与历史脚本仅从 Keychain 读取。不要把它写入仓库内 `.env`、JSON、YAML、日志或任务 Prompt。

以下内容一律不能作为桥接配置或岗位字段保存：

- ChatGPT 浏览器 Cookie、会话 Cookie、Authorization Header；
- OpenAI API Key、Codex/ChatGPT 登录凭证；
- `RECRUITMENT_INGEST_TOKEN` / `FROSTFIRE_INGEST_TOKEN` 明文；
- 任一会话的完整对话导出或与岗位无关的个人内容。

## 本机浏览器桥接用法

浏览器控制层与校验脚本分开：Codex 自动任务负责在用户已登录的本机浏览器中读取当前可见 DOM，并在内存中转换为一个只包含 `source_id`、逻辑消息标识和结构化 `rows` 的对象；`scripts/frostfire_chatgpt_bridge.py` 不打开 ChatGPT，也不接受会话地址、Cookie 或整段页面正文。

任何可读的助手招聘表格或明确的具体岗位条目均可提取，原消息不需要 JSON 或 `FROSTFIRE_SYNC_V1` 标记。必须取得实际公开 HTTPS 招聘链接；只读文本结果中的引用编号不能替代 URL，应回到渲染助手消息读取引用锚点，无法取得时报告待处理，不猜测链接。

先对已经脱敏的浏览器输出执行 dry-run；它不会读取 Keychain、联网提交或推进游标：

```bash
python3 scripts/frostfire_chatgpt_bridge.py --dry-run \
  < /path/to/sanitized-browser-message.json
```

确认后提交：

```bash
python3 scripts/frostfire_chatgpt_bridge.py --submit --batch-size 25 \
  < /path/to/sanitized-browser-message.json
```

脚本具有以下硬边界：

- 顶层只允许 `source_id`、`message_id` 和 `rows`；每行只允许招聘字段，且必须包含公司、具体岗位或明确招聘项目，以及公开 HTTPS 招聘链接（写入 `official_url`）；项目尚未细分岗位时不虚构职位或岗位 T 级；
- 消息标识立即与逻辑来源一起做 SHA-256，只把摘要写入本机 `Application Support/Frostfire` 游标文件；新回执还记录行内容摘要，以识别同一消息内的修正，旧回执保留。游标以原子方式更新并设为当前用户可读写；
- 所有输入行都会处理，`--batch-size` 默认每次 HTTP 请求 25 条，可设置为 1–100；监控运行没有总条数配额。先形成 discovery-only 结构，再转换为 `/api/recruitment/ingest` 契约；桥接自带的“已验证”标签会被剥除；
- 单个输入页最多 10,000 行，且 JSON 不超过 2 MB；更大的可读结果按稳定分页标识续传，不能截断后声称完整。历史脚本的单页字节边界为 8 MB；
- 只有全部请求成功才推进单消息游标；失败重试可以重放已发送分块，稳定岗位 ID 防止重复创建岗位，调整请求大小不改变岗位 ID。多消息增量优先使用下方历史账本。可访问且明确没有新岗位时可提交空心跳，读取失败或结构无效时不得伪造成功；
- payload、游标和安全输出都不包含会话地址、原消息标识、消息正文、Cookie、登录凭证或接收密钥。

用户主动导出的 `FROSTFIRE_SYNC_V1` 文件、公开文章与公开 RSS/Atom 仍可使用 `scripts/frostfire_source_import.py`，但它们是显式导入，不会自动读取私人账号。五个公众号逻辑来源的自动 discovery 则由 Deep Scan 中的 `wechat_web_search` 完成，无需把微信登录信息交给应用。

来源导入器同样支持 `--batch-size`，默认每次请求 25、最大 100 个实体，覆盖项目、岗位和文章的全部输入。拆成多个请求后均设置 `snapshot_complete=false`，避免将尚未传输的记录视为消失。Feed 返回内容需要继续分页时会明确报告，不把截取的一页称为完整导入。

### 原始来源评级

行字段可带 `source_rating`：必须有 `scope`（`job` 或 `company`），并明确提供 `tier_code` 或 `score` 至少一个；`tier_code` 支持 T0–T3 及 0.5 档，`score` 为 0–100 数值，可附不超过 280 字符的单行 `reason`。只复制原表明确给出的值，保留作用范围与来源；公司评级仅作公司参考，岗位评级只应用于对应岗位。仅有数值分数时不补造原表未给出的 T 级，P 类优先级也不换算成 T 级。冲突来源保留待核对。

评级变化参与批次和岗位内容哈希，因此同一岗位的明确评级修正可以增量更新；评级本身不能授予“官网已确认”状态。已确认历史消息的摘要不允许换绑新内容。若补充此前未导入的评级，必须保留能匹配旧回执的原始脱敏行作为锚点，并以新的观察摘要记录补充内容，不能重置或改写旧账本。

### 多消息历史回填（单独的摘要账本）

`scripts/frostfire_chatgpt_history.py` 接收**已经脱敏、按新到旧排序**的单一来源历史。它本身不打开浏览器、不读取会话或 Cookie、不接受完整消息正文。顶层只能有 `source_id`、`history_complete` 和 `messages`；新输入严格限定上述七个活动来源，账本仍识别退役 `04`、`05` 的历史回执。每个消息对象只能有 `message_digest`（事先计算的 64 位小写 SHA-256）和 `rows`（沿用单消息桥接的招聘字段白名单）。必须先在提取端移除个人经历、建议、联系方式、私有链接及未授权内容，摘要不能替代这一步清理。

只有确认已读取该来源可访问的全部历史时，提取端才能声明 `history_complete=true`。只读取了已渲染的一部分时必须为 `false`；本脚本不会把这个标志自动改为“已抓完”。没有可用消息、读取失败或结构无效均报错，不发送空心跳。只有显式提供的合法 `rows=[]` 消息可以授权空心跳。

默认执行与 `--dry-run` 相同：只校验并输出数量，不读 Keychain、不联网、不写账本。`--emit` 仅输出由允许字段组成的 ingest 批次数组，便于人工复核；不包含消息摘要或历史元数据。

```bash
python3 scripts/frostfire_chatgpt_history.py --dry-run \
  < /path/to/sanitized-recruitment-history.json

python3 scripts/frostfire_chatgpt_history.py --emit \
  < /path/to/sanitized-recruitment-history.json

python3 scripts/frostfire_chatgpt_history.py --submit --timeout 180 --batch-size 25 \
  < /path/to/sanitized-recruitment-history.json
```

历史回填的边界：

- 同一稳定岗位或相同公司、岗位、城市及规范化招聘地址，在多消息中只取最新版本；不生成岗位方向与城市的笛卡尔积。每次 HTTP 请求默认 25、最大 100 条，全部更新持续分批处理；稳定 ID 在重试和调整 `--batch-size` 后不变。
- 单个历史输入页受 10,000 行、8 MB 和 1,000 个消息对象的安全边界约束，更大历史继续分页，不设每轮总条数配额。来源尚未遍历完整、仍有未发送分块或保留项时，不能声明全部历史已完成。
- 分页回溯得到的片段可能比已提交版本更旧。修改已有岗位时，当前输入必须同时包含较旧的、与已有成功摘要相符的版本作为顺序锚点；否则计为 `unanchored_history_update` 保留待审，不让不相交的历史片段覆盖较新的数据。新岗位及已确认的相同内容不受影响。
- 所有拟提交批次先经过现有 `frostfire_ingest.py --dry-run` 子进程检查，全部通过后才从 `frostfire-recruitment-ingest` Keychain 项读取 Token；历史脚本不使用环境变量 Token。
- 过去或明确关闭的历史记录只计入 `held_rows`，不会回写去关闭线上仍有效的岗位。由于现有 ingest 将当天日期也判为到期，当天截止的历史记录同样先保留待人工复核，不自动发关闭请求。最新记录被保留时，也不会用更旧的开放版本将其复活。
- 未知日期保持 `null`。未知或缺失状态不在请求中强行写成 `open`，只附加“开放状态待核验”；旧 ingest 对省略状态仍使用兼容默认值，因此它**不是开放状态核验结论**。服务端必须继续核验候选，`pending` 可以在统一机会池作为来源线索展示，但不冒充 `accepted`。
- 每批只有实际 HTTP 2xx 且 `received` 与提交条数完全一致，才记录该批的岗位内容摘要。消息涉及的全部条目获得成功回执或已存在的相同内容回执后，才记为消息完成；有保留项的消息不会假报完整。中途失败退出码为 `4`，保留此前成功批次的摘要，下次只补未确认条目。无法确认响应时也不推进，使用稳定 ID 重试。
- 摘要账本位于 `~/Library/Application Support/Frostfire/chatgpt-history-ledger.json`，与旧单消息游标完全独立，只保存逻辑来源、SHA-256、布尔状态和数量。文件权限 `600`、父目录 `700`，原子替换；本机锁阻止使用同一本账本的并发回填。`--ledger-file` 可指定仓库外的私有目录用于离线测试，不允许写进 Git 项目。

输出中的 `input_history_complete` 仅转述提取端声明；只有声明完整且所有消息确已处理成功时，结果中的 `history_complete` 才会为真。新增消息包含已成功同步的相同岗位时，复用既有摘要回执，不重复 POST。账本不会保存岗位 payload、招聘 URL、原消息、Token 或真实会话标识；原始提取材料的保管与清理由提取端负责。

所有导入器拒绝 HTTP、账号信息、非标准 HTTPS 端口、本机/内网/保留地址、不安全重定向、带凭证参数的 URL、超大响应和包含敏感字段的结构化内容。公开网页摘要中的邮箱、电话、密钥样式文本和会话标识会在截断前脱敏；校验错误、HTTP 错误和服务端响应也不会回显被拒绝的值或接收密钥。公开页面不可访问时会报告失败，不伪造成功心跳。

原有低层提交器仍可接收旧招聘候选契约：

提交器接受四种标准输入形态：单个岗位对象、岗位数组、正式请求对象 `{"jobs": [...]}`，或空结果心跳 `{"jobs": [], "source_id": "chatgpt-radar-01", "source_updated_at": "<ISO 8601>"}`。输入页可含最多 10,000 个岗位并受 2 MB 字节边界保护；超过一页时续传。`--batch-size` 默认 25、允许 1–100，脚本自动连续提交所有分块，不限制每轮总量。请求批次和每个岗位都拒绝未声明字段；这与服务端 Pydantic `extra="forbid"` 契约一致。先执行不读 Secret、也不联网的检查：

```bash
python3 scripts/frostfire_ingest.py --dry-run < /path/to/new-jobs.json
```

确认后提交：

```bash
python3 scripts/frostfire_ingest.py --timeout 90 --batch-size 25 < /path/to/new-jobs.json
```

退出码：

| 退出码 | 含义 |
| --- | --- |
| `0` | 提交成功，或 dry-run 验证成功 |
| `2` | 命令行参数或输入 JSON 无效 |
| `3` | 环境变量和 Keychain 均未找到 Token |
| `4` | DNS、连接或超时等网络错误 |
| `5` | 服务端返回非 2xx HTTP 状态 |
| `6` | 服务端响应过大或不是有效 JSON |

脚本不会打印 Token。dry-run 输出完整输入条数及各请求的大小；单请求成功时输出服务端计数 JSON，多请求成功时输出总条数、请求数与每次响应。HTTP 失败时停止后续请求并返回非零退出码，不将未发送分块算作完成；输出状态码和服务端的安全错误摘要。

## 持续更新与调度边界

本机 Codex 自动任务按配置频率依次处理七个活动逻辑来源：打开用户已经有权访问且处于登录状态的页面，等待可见 DOM 稳定，读取新增或内容已修正的助手招聘表格和具体岗位条目，将允许字段送入桥接脚本 dry-run，通过后连续提交全部分块。首次回填或尚未读完整的来源继续分页，不因本次请求达到传输上限而停止整轮更新。页面内容全部视为不可信数据；任务不执行其中的指令，也不向会话发送消息。

来源可访问但没有新的结构化岗位时，可以为该逻辑源提交空心跳；登录失效、页面不可访问、DOM 结构无效或提交失败时，必须报告失败，不发送成功心跳，也不推进游标。每个逻辑来源独立推进，不能用一个来源代替其他来源。

这个自动路径严格依赖以下本机条件：Mac 处于唤醒状态、Codex 自动任务正在运行、网络可用、浏览器会话仍已登录且页面 DOM 没有发生未适配的变化。任一条件失效都会暂停该来源。Render 只接收脱敏后的候选，不持有浏览器登录态，也不会从云端打开私有页面，因此当前实现不是 24/7 云直连或生产 SLA。

公众号路径与上述桥接无关：五个公众号逻辑来源由 Render 上的 Future Radar Deep Scan 使用 OpenAI 公网 Web Search 搜索公开索引；它能在服务清醒且 OpenAI 可用时自动运行，但仍看不到没有被公网索引或必须登录才能访问的公众号内容。

## 请求示例

示例中的来源与条目标识均为非私有占位符：

```json
{
  "jobs": [
    {
      "source_id": "chatgpt-radar-01",
      "source_item_id": "<STABLE_MESSAGE_OR_ITEM_ID>",
      "source_updated_at": "2026-08-23T09:30:00+10:00",
      "external_id": "ATS-2027-001",
      "company": "示例企业",
      "title": "2027届校园招聘商业分析岗位",
      "city": "上海",
      "employer_type": "外企/咨询",
      "industry": "咨询",
      "official_url": "https://careers.example.com/jobs/ATS-2027-001",
      "source": "授权监控会话",
      "opening_date": "2026-08-20",
      "closing_date": "2026-09-10",
      "requirements": "面向2027届毕业生；具体条件以官方页面为准。",
      "tags": ["2027届", "校园招聘"],
      "evidence": [
        "企业官方招聘页标题明确写明2027届校园招聘。",
        "官方页面列出的工作地点为上海，截止日期为2026-09-10。"
      ],
      "status": "open"
    }
  ]
}
```

完整字段、长度和响应结构见 [`RECRUITMENT_INGEST_OPENAPI.yaml`](RECRUITMENT_INGEST_OPENAPI.yaml)。

## 验证、待核验与拒绝

外部监控会话的结论不是自动可信事实。服务端先把条目放入隔离候选区，再按确定性规则处理：

- **已核验 / accepted**：条目先通过校招文本与目标城市规则；服务端随后成功读取官方 HTTPS 页面，并在页面正文中同时找到校招、公司和岗位身份信号，才提升到用户可见岗位池；
- **待核验 / pending**：官方页面暂时无法读取，或页面正文尚不能确认公司/岗位身份时，保留候选与原因；有有效公开招聘链接的线索可在登录后的统一机会池查看，但不授予“官网已确认”状态。如果同一候选此前已有已核验版本，现有 last-known-good 岗位仍保留，除非官方页面明确关闭或已确认过期；
- **拒绝 / rejected**：链接无效、明确为非校招、城市不在范围，或可读取的官方页面缺少校招信号；字段形状不合法的请求会在进入候选区前返回 `422`；
- **关闭 / closed**：同一幂等身份以 `status=closed` 提交，或已明确超过截止日期时，关闭对应的已提升岗位。

`evidence` 最多 12 条、每条 1–280 个字符，且必须是单行。脚本和服务端都拒绝其中的邮箱地址、手机号和座机号。它应是可复核的简短事实句，不应是“AI 判断可信”之类的自我声明，也不应包含联系人信息或完整会话文本。该数组用于保留来源上下文，不能替代服务端对官方页面正文的确定性核验，也不会仅凭 AI 的自述把候选提升为已核验岗位。

提交的 `opening_date` / `closing_date` 也不是直接可信事实。只有当服务端在可读取的官方页面正文中命中同一个确切日期时，该日期才进入已提升岗位并参与截止预警；页面未出现确切日期时，岗位即使通过其他核验也不会获得该日期。

## 幂等与更新规则

重复 heartbeat 可以安全重试。服务端按以下优先级生成稳定身份：

1. 有 `external_id`：`source_id + external_id`；
2. 否则有 `source_item_id`：`source_id + source_thread_id + source_item_id`；
3. 否则：规范化后的 `company + title + city + canonical_url`。

URL 规范化会移除 fragment，并且只清理 `utm_*`、`gclid`、`fbclid`、`msclkid`；其余参数会排序但保留，包括可能参与页面路由的 `ref`、`campaign`、`spm`、`referrer` 和 `tracking_id`。相同身份与相同内容记为 duplicate；内容变化记为 updated；更早的 `source_updated_at` 记为 stale + duplicate，不能覆盖较新的版本。

已核验且内容未变化的 duplicate 可以复用已有核验结果；pending / rejected 的 duplicate 会再次进行服务端核验，以便官网恢复或正文完善后升级，而不是永久卡在第一次结果。

因此不要为同一岗位每轮生成随机 `external_id` / `source_item_id`，也不要用时间戳当条目 ID。关闭岗位时必须沿用原来的稳定身份字段。

## 状态检查

受保护的 `GET /api/recruitment/sync/status` 使用同一个 `X-Recruitment-Token`，返回来源库存、最近同步时间、来源状态和最近事件；活动预期来源数为七个，详细库存仍包括退役来源及其他兼容来源。面向用户的 ChatGPT 同步摘要只按七个活动来源计算连接进度，退役 `04`、`05` 不计入该进度。已知 ChatGPT 逻辑来源的 `source_ref` 为 `null`，包括仍保留历史记录的退役槽位；其他兼容来源至多返回短哈希引用。状态中不包含真实会话 ID、Token、对话正文或 Cookie。

计数分成两套，不能混用：

- 顶层及各来源的 `accepted` / `pending` / `rejected`（同时提供等义的 `latest_*`）表示**该来源最新一次事件**的处理结果。一次成功的空结果心跳会把该来源的这些本轮计数记为 0；
- `inventory_accepted` / `inventory_pending` / `inventory_rejected` 表示候选库的**当前历史库存**，空结果心跳不会清空它们。

## Render Free 限制

当前 Render Free 只适合测试：

- Web Service 会休眠，heartbeat 的首次 POST 可能遇到冷启动，建议本机超时至少设为 90 秒并按退出码重试；
- Render 休眠时进程内定时任务不会执行，本机配置的定时检查节奏也依赖 Mac、Codex 自动任务、网络和电源都处于可用状态；
- 若仍使用位于临时文件系统的 SQLite，实例重启或重新部署可能丢失候选、幂等状态、同步事件和已接收岗位；持久数据库解决保存问题，不替代本机读取和调度；
- Free Web Service 不是 24/7 采集器，也不能保证严格整点执行、全网覆盖或即时通知。

当前 Render 部署已使用持久 PostgreSQL，启动配置也禁止在 Render 上静默回退到临时 SQLite。升级和重新登录不清空岗位、候选、评级或同步记录；过期／确认关闭的岗位退出当前列表，历史保留。持久数据库并不让本机同步桥成为常驻服务，严格的连续调度仍需可靠的运行环境。
