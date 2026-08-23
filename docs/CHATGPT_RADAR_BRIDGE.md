# ChatGPT 监控源 → 冰焰未来雷达受控桥接

这套桥接把五个已经由用户授权的监控会话所产出的**新岗位结构化结果**，提交到冰焰的受保护接收 API。它不是 ChatGPT 账号直连：冰焰不会登录 ChatGPT、不会读取浏览器 Cookie，也不会自动回溯五个会话的历史内容。

桥接由三部分组成：

1. 五个外部监控会话继续发现机会；本机私有映射把每个会话对应到稳定逻辑 `source_id`，并为新结果保留稳定条目标识；
2. 本机 Codex 自动任务每 60 分钟唤醒一次，整理本轮新增或更新的结构化 JSON；
3. [`scripts/frostfire_ingest.py`](../scripts/frostfire_ingest.py) 从标准输入读取 JSON，并以 `X-Recruitment-Token` 提交到 `https://frostfire-ai.onrender.com/api/recruitment/ingest`。

## 五个会话 ID

五个真实监控会话 UUID 只属于本机私有映射，不提交为 `source_thread_id`，也不写入 Git。建议固定使用以下五个逻辑槽位；实际会话主题可以在本机配置中记录：

| 本机配置名 | 提交时的 `source_id` | 私有值 |
| --- | --- | --- |
| `FROSTFIRE_RADAR_THREAD_1` | `chatgpt-radar-01` | 第 1 个监控会话 ID |
| `FROSTFIRE_RADAR_THREAD_2` | `chatgpt-radar-02` | 第 2 个监控会话 ID |
| `FROSTFIRE_RADAR_THREAD_3` | `chatgpt-radar-03` | 第 3 个监控会话 ID |
| `FROSTFIRE_RADAR_THREAD_4` | `chatgpt-radar-04` | 第 4 个监控会话 ID |
| `FROSTFIRE_RADAR_THREAD_5` | `chatgpt-radar-05` | 第 5 个监控会话 ID |

`source_id` 是可公开的稳定逻辑名称。五个真实会话 ID 只用于本机自动任务判断它应写入哪个 `source_id`，不要提交到冰焰，也不要写入 Git。请求模型仍保留可选 `source_thread_id` 以兼容其他受控来源：对上述五个逻辑来源，服务端不保存该值；对其他来源也只保存不可逆的短 SHA-256 引用，不保存或返回原文。

如自动任务需要一个本机映射文件，可放在仓库外，例如 `~/.config/frostfire-radar/bridge.json`，并执行 `chmod 600 ~/.config/frostfire-radar/bridge.json`。该文件只保存五个来源映射和本机游标，不保存 Cookie、OpenAI API Key 或冰焰接收 Token，也不要复制进项目目录。

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

## 提交器用法

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

## 每 60 分钟的本机 Codex heartbeat

heartbeat 是**本机调度频率**，不是 Render 后台任务，也不是 ChatGPT API 轮询承诺。建议为五个来源建立一个 Codex 自动任务，每 60 分钟执行一次以下流程：

1. 读取仓库外的五个会话 ID 与各自上次成功的 `source_item_id` / `source_updated_at` 游标；
2. 仅处理游标之后由授权监控会话明确产出的新条目或更新；
3. 用本机会话映射写入对应的逻辑 `source_id` 和稳定 `source_item_id`，但不把真实会话 ID 写入 `source_thread_id`；有 ATS 岗位编号时同时写 `external_id`；
4. 生成简短 `evidence`，只保留支持公司、岗位、城市、开放状态和日期的证据句；每批最多 10 个岗位；
5. 先执行 `--dry-run`，再把同一 JSON 提交；只有服务端成功返回后才推进本机游标；
6. 没有新结果时不伪造岗位，也不重复改写旧内容；为对应逻辑源提交空结果心跳，并只在心跳成功后推进本机同步时间：

```json
{
  "jobs": [],
  "source_id": "chatgpt-radar-01",
  "source_updated_at": "2026-08-23T10:00:00+10:00"
}
```

空结果心跳会记录该源最新事件、把可连接来源恢复为 `synced`，并只在 `source_updated_at` 比已存时间更新时推进来源时间；它不会创建岗位，也不会清空历史候选库存。每个逻辑源应各自发送心跳，不能用一个来源代替另外四个。

这份仓库只提供接收契约与安全提交器，不包含绕过 ChatGPT 登录去读取私人会话的抓取器。若本机 Codex 自动任务无法获得某个会话的授权结构化输出，应保留该源为未连接状态，不使用 Cookie 或 UI 自动化绕过限制。

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
