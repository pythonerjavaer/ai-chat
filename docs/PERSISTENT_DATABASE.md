# 冰焰数据库保全与 PostgreSQL 迁移

项目提供两条互不等价的迁移路径：完整 SQLite 数据库迁移，以及在明确同意重新注册的前提下，只恢复公开招聘机会。两条路径都不调用 OpenAI，默认只做离线检查，只有显式 `--apply` 才连接 PostgreSQL。

## 公开机会池恢复：不迁移旧账号和聊天

这条路径只适用于使用者已经明确同意：保留公开招聘机会，旧账号、聊天和私有文档不迁移，切换后重新注册。未经这一选择，不能用公开机会快照替代完整数据库备份。

切换前，从仍在运行的旧实例分页导出完整 `/api/future-radar/opportunities` 结果，使用公共字段白名单，校验总数、唯一 ID、状态及文件 SHA-256。快照存放在代码库和网页静态目录之外，不包含访问令牌、会话链接、个人匹配档案或密码。导出账号与导出凭据也不迁移。

```sh
python scripts/frostfire_public_pool_restore.py \
  --snapshot /secure/public-opportunity-snapshot.json \
  --expected-sha256 <已核对的文件SHA256>
```

离线校验通过后，通过受控环境变量提供目标连接，执行：

```sh
python scripts/frostfire_public_pool_restore.py \
  --snapshot /secure/public-opportunity-snapshot.json \
  --expected-sha256 <已核对的文件SHA256> \
  --apply \
  --target-env FROSTFIRE_PUBLIC_POOL_DATABASE_URL \
  --schema frostfire
```

恢复保持机会的原 ID、链接、日期和核验状态：`pending` 不变成 `verified`，`unknown` 不变成 `open`，截止日期不向后延长。独立的只推送来源保留快照出处，避免空的旧来源表在首次扫描时把已恢复机会误判为消失；后续明确关闭或到期的机会仍遵守正常展示规则。重复恢复必须核对真实目标内容，不允许覆盖不同数据。

这条路径不恢复原扫描日志、AI 缓存或旧账号资料，不能宣称完整数据库已经迁移。上线前还必须：

1. 校验目标机会 ID、公开事实、数量与核验状态，确认没有导入账号或聊天。
2. **更换 `JWT_SECRET`**，使旧会话全部失效。新账号的数字 ID 可能与旧账号相同，保留旧签名密钥会产生错误身份关联。受控招聘同步令牌无需因此更换。
3. 在恢复和首次验收期间暂停自动扫描，避免后台写入混淆比较；验收后恢复原自动扫描设置。
4. 验证重新注册、登录、完整机会池、筛选及详情链接；重启后再次确认数据仍在 PostgreSQL。

## 完整数据库迁移的保全前提

截至本次改动，尚未取得当前 Render 运行实例的完整生产 SQLite 快照。当前服务原有接口没有全库导出能力；账号接口不导出密码哈希，消息接口也不能导出全部历史。Render Free 的 Shell 入口出现升级要求，不能据此声称已经成功导出或备份。

如果选择保留旧账号、聊天和所有应用数据，**不要先重启、重新部署、改变套餐或切换数据库，再声称旧数据已经安全保留**。新增迁移脚本、完成 Supabase 连接检查，均不代表生产数据已迁移。

需要先获得平台支持、且不会重建当前实例的文件访问或一致性导出通道。若暂时没有这样的通道，完整保全仍是阻塞项，不能用部分恢复包替代。

已有的262条招聘来源恢复包仅包含公开招聘候选及部分出处；纠正批次更新其中已有记录。它不包含真实账号、密码哈希、完整会话和文档，也不包含后来发生的所有扫描、事件及状态变更。它不是完整生产备份。此前隔离验收数据库包含合成测试用户，同样不得迁入生产。

以下章节仅描述 `frostfire_database_migrate.py` 的完整数据库迁移流程，不适用于上面的公开机会池恢复。

## 完整迁移范围与不变项

迁移白名单为当前30张应用表：

```text
users                         sessions
messages                      documents
chunks                        spaces
token_usage                   space_runs
recruitment_profiles          recruitment_jobs
recruitment_ingest_candidates  recruitment_ingest_sources
recruitment_ingest_events      recruitment_watches
system_state                  api_usage_events
radar_companies               schema_migrations
monitor_sources               recruitment_programs
radar_jobs                    source_articles
job_sources                   program_sources
radar_events                  radar_runs
radar_sync_batches            radar_locks
radar_ai_cache                 radar_source_snapshots
```

工具会验证这30张表及当前29条外键；发现额外应用表、缺表、触发器、视图、未知表达式索引或不支持的列类型时停止，避免静默丢失数据或执行源库中未经审查的 SQL。SQLite 的内部 `sqlite_sequence` 仅用于读取自增高水位，不作为应用表导入。

以下内容原样保留：

- 用户ID、密码哈希、隐私同意记录和套餐字段；不会重新注册用户或改成 Supabase Auth。
- 会话、全部现存消息、文档正文、分块、向量字符串及所有权关联。
- 招聘记录、来源、核验/关闭状态、事件、游标、扫描历史、缓存和锁记录。
- 主键、复合键、NULL、文本及JSON原始字符串、时间字符串和数值。
- 64位 SQLite INTEGER → PostgreSQL BIGINT；SQLite REAL → DOUBLE PRECISION，避免32位整数或单精度浮点截断。
- 29条外键，包括 `space_runs.cached_from_run_id` 自引用。外键设为可延迟，数据导入后再次立即验证。
- SQLite `NOCASE` 的 ASCII 大小写不敏感唯一性；不擅自改为范围更广的 Unicode 大小写折叠。
- 自增列的最大ID及 `sqlite_sequence` 高水位，包括已删除行曾占用的ID，避免迁移后重复使用。

浏览器本地保存的产品资料不在服务端 SQLite 内，需要另行通过对应产品导出。源库中早已被正常保留策略清除的记录，也不能由本工具凭空恢复。

## 第一步：在当前实例仍存在时保全

以下路径为示例，应替换为**已获授权、能够直接读取的完整 SQLite 文件**。本工具必须读取到该文件；只提供网站地址或招聘JSON不能代替它。

```sh
python scripts/frostfire_database_migrate.py \
  --source /secure/current-render-instance.sqlite3 \
  --backup-dir /secure/frostfire-backup-20260830 \
  --dry-run
```

`--backup-dir` 必须是新目录，或当前用户拥有且权限为 `0700` 的目录。省略时创建独立的 `0700` 临时目录。快照与清单文件权限为 `0600`；已有文件不会被覆盖。临时目录不是长期备份位置，应把验证后的快照及清单安全转存到受控、持久的备份位置，不要提交 Git 或放入网页静态目录。

执行顺序：

1. 源库以 `mode=ro` 和 `query_only` 打开；不执行 checkpoint、VACUUM、删除或源库结构修改。
2. 在源库读事务中执行 `integrity_check`、`foreign_key_check`。
3. 通过 SQLite online backup API 获取同一读视图的一致性快照，包含已提交的 WAL 数据，不包含未提交事务。
4. 再次检查快照，验证30表/29外键，并计算每张表的行数和规范化行内容哈希。
5. 保存完整快照及摘要清单。输出不包含用户名、密码哈希、文档正文、DSN或其他行内容。

不要在 WAL 模式下只复制主 `.db` 文件而忽略 WAL。不要把无锁文件复制等同于一致性备份。成功的快照只证明其取样时刻的数据完整，不能证明取样之后没有新写入。

默认 dry-run 只需 Python 3.11+ 标准库，不读取目标连接环境变量、不导入应用配置、不加载用户 `.env`、不要求 `OPENAI_API_KEY`，也不连接数据库或调用AI。

## 第二步：连接新的私有目标库

目标连接只从环境变量读取，默认名称为 `DATABASE_URL`。不要把包含密码的 DSN 放在命令行、聊天、日志或 Git 中；通过受控的进程环境或部署密钥设置提供。

Supabase 使用官方 CA 和完整 TLS 主机名验证：

```text
sslmode=verify-full
sslrootcert=<官方CA证书的绝对路径>
```

仓库中的公开证书位于 `backend/certs/supabase-prod-ca-2021.crt`。本地工具使用本机绝对路径，Render/Docker 使用 `/app/backend/certs/supabase-prod-ca-2021.crt`。证书路径作为 URL 参数时需要正确编码。

官方证书来源：[Supabase production CA](https://supabase-downloads.s3-ap-southeast-1.amazonaws.com/prod/ssl/prod-ca-2021.crt)。证书 SHA-256 指纹：

```text
807025ad50d4ed219d2c9c7d299c004f824eb00cf7f65afef607d07b72e6cafa
```

不要使用 `sslmode=require`、关闭校验或自签名绕过来“解决”证书错误。非本机目标必须提供 `verify-full` 和存在的绝对 CA 路径。测试数据只允许使用 `FROSTFIRE_TEST_POSTGRES_URL` 指向显式本机 PostgreSQL。

## 第三步：事务迁移与验证

先确认已经获得**当前真实生产实例的完整快照**，并保留验证后的原文件。`--source-kind production` 是操作者的明确来源确认，不是工具自动认证；程序还会拒绝把已识别的 `.invalid` / `.test` 测试账号当成生产数据迁入。

```sh
python scripts/frostfire_database_migrate.py \
  --source /secure/verified-current-instance.sqlite3 \
  --backup-dir /secure/frostfire-final-snapshot \
  --apply \
  --source-kind production \
  --schema frostfire
```

工具通过独立的 `backend.storage.connect_postgres` 连接目标。`frostfire` 为私有 schema，不使用 `public`、`auth` 或 `storage`；匿名和普通 Supabase 客户端角色不应直接访问账号、密码哈希或私人文档。只有后端受控数据库连接处理这些数据。

迁移行为：

- 获取目标 schema 的事务级迁移锁，第二个相同迁移不能并发导入。
- 只接受空目标，或已经完成且内容/结构相同的完整迁移。部分表、不同记录或不同结构都会停止；没有覆盖、截断或自动合并模式。
- 所有 schema、表、索引和数据创建都在同一事务中。DDL由白名单元数据重建，不直接执行源库任意 SQL。
- 导入时临时延迟外键约束，支持自引用；随后检查全部外键和引用完整性。
- 比对每表行数、每行类型和值的稳定哈希、整体哈希、列定义、键和索引。
- 校准并检查自增序列，全部通过才提交。任何差异导致回滚。
- 同一快照重复迁移会重新检查真实目标内容，完全相同时跳过，不以一条“曾成功”标记代替验证。

哈希按规范化的类型和值计算，再按二进制行哈希排序，避免 SQLite/PostgreSQL 文本排序规则不同造成误判。JSON继续保存为原始 TEXT，不重排键或重写空格。

工具不自动清空运行锁、不改写 RUNNING 状态，也不删除任何源数据。切换前应停止接纳新的写入、等待已有扫描退出，再取得最终快照；首次快照之后仍在发生的注册/扫描，需要明确的最终同步边界。不能仅要求用户“不点击”，却让自动 Scheduler 持续写入而忽略这些增量。

## 部署切换条件

只有满足以下条件才能更换线上数据库连接：

1. 当前生产实例完整快照已安全取出并验证；不是262条候选重放包或本地QA数据库。
2. 最终写入边界明确，账号及最近扫描数据均已包含。
3. PostgreSQL 的30表、29外键、行数、内容哈希及序列检查通过，备份可恢复。
4. 仅在用户 ID 和密码哈希均完整保留的这条迁移路径中，才可保留现有 JWT Secret；不迁移旧账号的公开机会恢复必须更换签名密钥。
5. 线上只启用一个写入目标和一套有效调度执行者，避免新旧服务同时扫描写库。
6. 配置、证书路径和连接失败策略验证完成；有原库快照及明确回退方案。

在这些条件完成前，应保持生产服务及其原实例不变，并明确报告尚未迁移。创建 Supabase 项目、安装驱动或完成本地隔离测试，都不能替代实际生产保全与迁移验收。

## 隔离回归测试

不设置测试连接时，仅运行本地快照/安全检查；PostgreSQL测试自动跳过。需要真实PG验证时，通过 `FROSTFIRE_TEST_POSTGRES_URL` 提供专用本机临时数据库，再执行：

```sh
python -m pytest -q tests/scripts/test_frostfire_database_migrate.py
```

测试只在随机的 `ff_migrate_test_…` schema 中导入合成数据，并清理各自的测试 schema；连接不是显式 loopback 时拒绝执行。测试不注册真实用户、不调用线上API、不读生产凭据或浏览器会话。
