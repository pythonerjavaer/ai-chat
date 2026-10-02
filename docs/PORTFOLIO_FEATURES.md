# 项目能力与实现映射

本页只记录现有产品中已经可以运行、可以由接口和测试定位的能力。新增能力不会替换原有业务流程。

## 五个项目在冰焰中的定位

| 项目 | 冰焰领域 | 数据 / 算法职责 |
|---|---|---|
| 跃迁域知识库 RAG | 跃迁域 | 业务主库保存原文及记录（本地 SQLite、持久部署 PostgreSQL）；PostgreSQL/pgvector 的独立私有 schema 承担已索引材料的向量近邻检索，故障时回退；MongoDB 归档已索引版本的段落和层级锚点；本机 Ollama/嵌入模型用于语义理解 |
| 养老金生命周期预测 | 极光域 | 可复现 Monte Carlo、Bear/Base/Bull 情景和生命周期降风险算法；结果及假设进入现有关系数据库的用户隔离历史 |
| 智能招聘信息监测 | 未来雷达 | 现有 SQLite 主库保存权威岗位与申请状态；Neo4j 表示公开企业—机会—技能关系图，供图谱探索 |
| 企业并购财务与风险测算 | 烈火域 | 交易融资结构、WACC、利率敏感性、EBIT 压力矩阵与可选现金流；现有 SQLite 主库保存用户隔离权威历史，GraphDB 投影并购运行—融资结构—压力情景 RDF 关系 |
| 脉冲域财务 BI / EDA | 脉冲域 | 现有 SQLite 主库仍为账务事实来源；DuckDB 读取限定范围的事实数据作列式聚合；均值/中位数/箱线图统计、标准化 PCA 特征投影和 KMeans 客户分群 |

## 岗位技能在五个项目中的可验证落点

| 岗位能力 | 项目中的实际实现 |
|---|---|
| NLP / LLM | 跃迁域结构分块、语义嵌入、检索增强问答与引用；未来雷达职位文本抽取和字段归一化 |
| AI Agent / 工作流 | 跃迁域“检索→证据整理→生成/回退”的有界问答链；未来雷达“接入→抽取→校验→排序→推送”任务链；模型输出不直接执行资金、申请等不可逆操作 |
| 算法工程 | 极光域固定种子的 Monte Carlo 与生命周期降风险；烈火域 WACC、融资敏感性及压力矩阵；脉冲域 PCA/KMeans；未来雷达图关系同步 |
| 数据工程 | 多源接入、结构化校验、来源溯源、用户范围、持久化、版本键、服务状态和失败隔离；Mongo/Neo4j 是分工明确的扩展存储，不取代核心关系库 |
| 数据分析 | 脉冲域描述统计/趋势/箱线图/特征降维分群；养老金分位数、回撤及目标率；并购融资和偿债敏感性报告 |

极光域聚焦“用已有知识和数据推演未来”；未来雷达聚焦“向外发现未来的机会与变化”。养老金属于前者，招聘监测属于后者。PCA/KMeans 是基于当前用户授权业务聚合的无监督分析，不需要伪造标签；RAG 使用预训练语义嵌入，不声称用个人数据训练了深度模型。

本机无云端费用的模型配置与 PostgreSQL 验证说明见 [`LOCAL_RAG_MODELS.md`](LOCAL_RAG_MODELS.md)。

本次本地验收为保护既有资料，保留应用当前 SQLite 主库；另建 loopback-only `frostfire_test` PostgreSQL 验收库并启用 pgvector 0.8.6，运行 PostgreSQL API/存储集成测试。MongoDB、Neo4j、GraphDB 使用独立本机实例和项目专属存储空间。运行应用的主库切换仍须先完成既有 SQLite 的可恢复完整备份与逐表校验，不以“测试通过”代替迁移。

## 跃迁域：知识库理解与问答

| 简历能力 | 实现位置 |
|---|---|
| 文档解析 | `backend/product_domains/leap.py`：TXT、Markdown、PDF、DOCX；公共领域 EPUB 继续使用原导入流程 |
| 长段落及层级切块 | `backend/product_domains/knowledge.py::build_chunks`，保留章节、Markdown 标题、段落范围和稳定锚点 |
| 向量化入库 | `leap_knowledge_chunks`；默认可用本机 Ollama 的 `qwen3-embedding:0.6b`，无需云端 API 费用；也可显式切换到 OpenAI Embeddings。PostgreSQL 启用 pgvector 时按向量维度写入原生 vector 列并建立 HNSW 索引；SQLite 保留 JSON 向量回退。原段落表不受影响 |
| 检索匹配 | `/api/leap/knowledge/search`：语义向量与关键词分数融合；PostgreSQL+pgvector 可用 HNSW 近邻检索，其他数据库使用可移植向量回退；严格按用户隔离 |
| 答案生成与引用 | `/api/leap/knowledge/ask`：本机 Ollama `qwen3:1.7b` 或明确配置的云端模型执行证据约束 RAG；模型不可用时返回可追溯的抽取式答案 |
| MongoDB 文档归档 | `backend/portfolio_datastores.py::MongoDocumentArchive`：段落元数据与分块向量按版本拆分保存；PostgreSQL 主检索记录保持权威，Mongo 不可用时不阻断现有检索 |
| 翻译与语义解读 | 保留原 `/translation/*` 与 `/reading-assistant/interpret` 链路，新增问答不改变原文证据 |

## 未来雷达：招聘信息监测与分析

| 简历能力 | 现有实现位置 |
|---|---|
| 多源数据接入 | `backend/future_radar/adapters.py`、`public_discovery.py`、监测信源注册表 |
| 结构化抽取与规则校验 | `normalization.py`、`schemas.py`、`service.py` 的候选合并与核验状态 |
| 岗位池、筛选、排序 | `/api/future-radar/opportunities`，支持岗位、企业、地区、行业、状态、层级和优先范围 |
| 来源溯源 | `job_sources`、`source_ratings`、公开证据 URL 与核验状态 |
| 截止预警 | `deadline_opportunities`、`closing_soon` 与通知接口 |
| 投递状态跟踪 | `/application-records`、`/opportunities/{id}/application`、保存岗位 |
| 结果推送 | 事件/通知增量接口与微信公开来源监测；不会在无变化时重复生成岗位 |
| Neo4j 机会关系图 | `backend/portfolio_datastores.py::Neo4jOpportunityGraph` 与 `/api/future-radar/graph`：公开雇主、职位、技能关系；个人申请状态不写入图数据库 |
| 链路可视化 | 未来雷达页的“数据处理链路”展示接入、抽取、核验、优先级与待推送状态；来自 `/api/future-radar/pipeline-summary` |

## 极光域预测模型与烈火域并购模型

两个模型在各自领域独立显示。极光域生命周期模型可调整年龄、余额、收入、缴费率、收益、波动和冲击年龄，输出 1,000 路径 Monte Carlo 分位数、波动、回撤、目标达成率、年度路径和 Bear/Base/Bull 确定性情景。烈火域并购模型计算新增债务/现金对价、利息、WACC、融资结构敏感性、利率冲击及 EBIT 压力情景；缺少 EBIT 或现金流输入时会明确留空，不捏造覆盖倍数。

运行历史保存在 `/api/finance/models/history`，按登录用户隔离；所有结果是用户输入下的研究情景，明确列出假设，不构成投资建议。

## 脉冲域统计与机器学习

`backend/product_domains/pulse_analytics.py` 对服务端用户范围的汇总执行描述统计，返回箱线图四分位数、均值、中位数、须线和 1.5×IQR 离群值；使用 `StandardScaler + PCA` 输出 PC1/PC2、解释方差与载荷，并使用固定 `random_state=2026` 的 KMeans 展示客户特征分群。关系型 OLTP 按用户与日期界限提取后交给 DuckDB 内存 OLAP 做月度聚合。月收入达到至少 3 个月时，输出 OLS 线性趋势基线与 95%预测区间；至少 36 个月才比较随机森林、AdaBoost 与贝叶斯岭回归，至少 120 个月时额外训练 PyTorch CPU LSTM 候选，并以时间顺序滚动回测 MAE/RMSE 选模。144 个月合成演示会实际运行神经网络预测链路，金额按账号租金月收入中位数校准并明确标注为合成数据。缺失月份显式按零收入处理，并提示趋势基线不建模季节性。PCA 与 KMeans 只对脱敏后的用户范围聚合特征运行，不导出原始客户记录。
