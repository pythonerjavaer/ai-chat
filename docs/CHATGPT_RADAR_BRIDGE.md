# ChatGPT / 公众号公开信源 → 冰焰未来雷达受控导入

这套导入把用户明确公开或主动导出的**结构化招聘结果**，提交到冰焰的受保护接收 API。它不是 ChatGPT 账号直连：冰焰不会登录 ChatGPT、不会读取浏览器 Cookie，也不会自动回溯个人会话历史。

必须区分两类 ChatGPT URL：

- `https://chatgpt.com/c/...` 是账号内的私有会话页面，不是开放 API。即使用户和本机浏览器当前已登录，冰焰服务端也不能把这个登录态当成长期数据接口；本项目明确拒绝用 Cookie、浏览器自动化或未公开内部接口抓取它。
- `https://chatgpt.com/share/...` 是用户主动创建的公开分享快照。任何拿到链接的人都可能看到快照，因此创建前必须去除个人资料和敏感内容。分享页是一个快照；原会话后来新增结果时，必须重新更新分享快照才会包含新内容。

当前支持三条真实路径：

1. **首选：结构化 JSON 文件。** 让监控会话只输出完整 `FROSTFIRE_SYNC_V1`，保存为本地文件，然后导入；
2. **公开 ChatGPT 分享快照。** 分享页必须包含一个完整的 `FROSTFIRE_SYNC_V1` JSON 代码块；导入器只解析该对象，忽略页面中的其他自然语言和指令；
3. **公众号/公开网页。** 无需登录即可访问的单篇公开文章 URL 或公开 RSS/Atom 可作为 discovery 信号导入；它们不能替代企业招聘官网核验。

## 五个逻辑来源

五个真实私有会话 UUID 不提交为 `source_thread_id`，也不写入 Git、数据库或日志。可以继续使用以下五个逻辑槽位，但应用只接收结构化结果，不读取其对应的私有会话：

| 本机配置名 | 提交时的 `source_id` | 私有值 |
| --- | --- | --- |
| `FROSTFIRE_RADAR_THREAD_1` | `chatgpt-radar-01` | 第 1 个监控会话 ID |
| `FROSTFIRE_RADAR_THREAD_2` | `chatgpt-radar-02` | 第 2 个监控会话 ID |
| `FROSTFIRE_RADAR_THREAD_3` | `chatgpt-radar-03` | 第 3 个监控会话 ID |
| `FROSTFIRE_RADAR_THREAD_4` | `chatgpt-radar-04` | 第 4 个监控会话 ID |
| `FROSTFIRE_RADAR_THREAD_5` | `chatgpt-radar-05` | 第 5 个监控会话 ID |

`source_id` 是可公开的稳定逻辑名称。导入命令行的 `--source-id` 是唯一可信映射；它不能直接使用 UUID。分享页内的 `source_id` 会被覆盖，页面也不能注入 `source_name` 或私有会话标识。上游条目标识若呈 UUID 形状，导入器会先用逻辑来源和原值生成稳定摘要，再丢弃原 UUID。

## Secret 管理

服务端使用 Render 环境变量 `RECRUITMENT_INGEST_TOKEN`；本机提交器读取名称不同但值相同的 `FROSTFIRE_INGEST_TOKEN`。优先把 Token 放入 macOS Keychain 的通用密码项目：

- service：`frostfire-recruitment-ingest`
- account：当前 macOS 用户名
- password：Render 中 `RECRUITMENT_INGEST_TOKEN` 的值

可在“钥匙串访问”图形界面创建，也可以在交互式终端中临时读取后写入，避免 Token 进入 shell 历史：

```bash
read -r -s FROSTFIRE_TOKEN
security add-generic-password -U -a "$USER" -s frostfire-recruitment-ingest -w "$FROSTFIRE_TOKEN"
unset FROSTFIRE_TOKEN
```

Keychain 不可用时，自动任务可以在其受控进程环境中设置 `FROSTFIRE_INGEST_TOKEN`。不要把它写入仓库内 `.env`、JSON、YAML、日志或任务 Prompt。

以下内容一律不能作为桥接配置或岗位字段保存：

- ChatGPT 浏览器 Cookie、会话 Cookie、Authorization Header；
- OpenAI API Key、Codex/ChatGPT 登录凭证；
- `RECRUITMENT_INGEST_TOKEN` / `FROSTFIRE_INGEST_TOKEN` 明文；
- 五个会话的完整对话导出或与岗位无关的个人内容。

## 受控导入器用法

先验证一个用户主动导出的结构化文件；默认只打印规范化结果，不联网提交：

```bash
python3 scripts/frostfire_source_import.py \
  --source-id chatgpt-radar-01 \
  --structured-json /path/to/FROSTFIRE_SYNC_V1.json
```

公开分享页必须是 `/share/`，不能是 `/c/`：

```bash
python3 scripts/frostfire_source_import.py \
  --source-id chatgpt-radar-01 \
  --chatgpt-share 'https://chatgpt.com/share/<PUBLIC_SHARE_ID>'
```

确认输出后加 `--submit`；脚本从 `FROSTFIRE_INGEST_TOKEN` 或同一 macOS Keychain service 读取 Token，并向 `/api/future-radar/sync` 提交。它不会把分享链接、页面正文或 Token 放进 payload。

公开公众号文章只建立 discovery 文章记录：

```bash
python3 scripts/frostfire_source_import.py \
  --source-id wechat-public-01 \
  --public-article 'https://mp.weixin.qq.com/s/<PUBLIC_ARTICLE_ID>' \
  --title '文章公开标题' \
  --publisher '公众号公开名称' \
  --submit
```

公开 RSS/Atom 同样只导入最多 10 条文章信号：

```bash
python3 scripts/frostfire_source_import.py \
  --source-id public-campus-feed-01 \
  --public-feed 'https://example.com/campus.xml' \
  --publisher '公开招聘订阅' \
  --submit
```

系统拒绝 HTTP、账号信息、非标准 HTTPS 端口、本机/内网/保留地址、不安全重定向、带凭证参数的 URL、超大响应、RSS DTD/实体和非 RSS/Atom XML。导入 payload 中任何 ChatGPT `/c/` 或 `/share/` URL 都会被拒绝，避免会话/分享 UUID 进入岗位或文章来源。公开网页摘要中的邮箱、电话、密钥样式文本和 UUID 会在截断前脱敏；结构化 JSON 中出现这些内容则拒绝整批。校验错误、HTTP 错误和服务端响应也会脱敏，不回显被拒绝的值或 Token。公开页面不可访问时会报告失败，不伪造成功心跳。

原有低层提交器仍可接收旧招聘候选契约：

提交器接受四种标准输入形态：单个岗位对象、最多 10 个岗位的数组、正式请求对象 `{"jobs": [...]}`，或空结果心跳 `{"jobs": [], "source_id": "chatgpt-radar-01", "source_updated_at": "<ISO 8601>"}`。请求批次和每个岗位都拒绝未声明字段；这与服务端 Pydantic `extra="forbid"` 契约一致。先执行不读 Secret、也不联网的检查：

```bash
python3 scripts/frostfire_ingest.py --dry-run < /path/to/new-jobs.json
```

确认后提交：

```bash
python3 scripts/frostfire_ingest.py --timeout 90 < /path/to/new-jobs.json
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

脚本不会打印 Token。成功时输出服务端的计数 JSON；HTTP 失败时只输出状态码和服务端的安全错误摘要。

## 持续更新与调度边界

定时器只能调度一次导入，不能凭空获得私有 ChatGPT 会话内容。要实现持续更新，必须有一个受支持的数据生产步骤：

1. 外部监控服务通过正式 API/Webhook 直接生成新 JSON；或
2. 用户让 ChatGPT 监控会话生成新的完整 JSON，并更新公开分享快照；或
3. 用户导出新的本地 JSON；或
4. 合法公开 RSS/Atom/文章页产生新内容。

之后才可以按固定频率运行导入。若分享快照没有更新，反复请求同一链接只会得到同一批次；幂等键会阻止重复事件。公开来源可访问但确实没有新结果时，可以提交空心跳；来源不可访问或结构无效时不得伪造成功心跳：

```json
{
  "jobs": [],
  "source_id": "chatgpt-radar-01",
  "source_updated_at": "2026-08-23T10:00:00+10:00"
}
```

空结果心跳会记录该源最新事件、把可连接来源恢复为 `synced`，并只在 `source_updated_at` 比已存时间更新时推进来源时间；它不会创建岗位，也不会清空历史候选库存。每个逻辑源应各自发送心跳，不能用一个来源代替另外四个。

这份仓库不包含、也不应增加绕过 ChatGPT 登录去读取私人会话的抓取器。若没有新的公开快照或结构化输出，应保留该源为未连接/无更新状态，不使用 Cookie 或 UI 自动化绕过限制。

## 请求示例

示例中的会话和条目 ID 都是占位符：

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
- **待核验 / pending**：官方页面暂时无法读取，或页面正文尚不能确认公司/岗位身份时，保留候选与原因，不把本轮未核实内容提升到岗位池；如果同一候选此前已有已核验版本，现有 last-known-good 岗位仍保留，除非官方页面明确关闭或已确认过期；
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

受保护的 `GET /api/recruitment/sync/status` 使用同一个 `X-Recruitment-Token`，返回五个预期来源的连接数量、最近同步时间、来源状态和最近事件。五个逻辑来源的 `source_ref` 为 `null`；其他兼容来源至多返回短哈希引用。状态中不包含真实会话 ID、Token、对话正文或 Cookie。

计数分成两套，不能混用：

- 顶层及各来源的 `accepted` / `pending` / `rejected`（同时提供等义的 `latest_*`）表示**该来源最新一次事件**的处理结果。一次成功的空结果心跳会把该来源的这些本轮计数记为 0；
- `inventory_accepted` / `inventory_pending` / `inventory_rejected` 表示候选库的**当前历史库存**，空结果心跳不会清空它们。

## Render Free 限制

当前 Render Free 只适合测试：

- Web Service 会休眠，heartbeat 的首次 POST 可能遇到冷启动，建议本机超时至少设为 90 秒并按退出码重试；
- Render 休眠时进程内定时任务不会执行，真正的 60 分钟节奏依赖本机 Mac、Codex 自动任务、网络和电源都处于可用状态；
- 当前 SQLite 位于临时文件系统，实例重启或重新部署可能丢失候选、幂等状态、同步事件和已接收岗位；
- Free Web Service 不是 24/7 采集器，也不能保证严格整点执行、全网覆盖或即时通知。

正式使用前，应迁移到持久数据库或受支持的持久存储，并将 heartbeat 放到可靠的常驻调度环境。迁移前即使本机已成功提交，也不能把 Render Free 状态当作永久记录。
