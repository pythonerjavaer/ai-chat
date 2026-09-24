# 跃迁域与脉冲域接入说明

两个参考仓库仅用于能力盘点，没有成为生产依赖，也没有被修改或部署。

| 原型能力 | 冰焰产品能力 | 实现位置 |
|---|---|---|
| Literature Learning Platform 的材料阅读、学习记录 | 跃迁域材料库、分段阅读、阅读进度 | `backend/product_domains/leap.py`、`frontend/src/product-domains.js` |
| 文学学习中的笔记与讨论 | 独立摘录、笔记、原文证据搜索 | 同上 |
| 新产品要求的跨材料学习 | 思想虫洞双侧证据、思想对撞卡、真实关系图数据 | 同上 |
| Restaurant Menu API 的客户、商品、订单和预约边界 | 脉冲域客户、SKU、礼服实物资产、预约订单 | `backend/product_domains/pulse.py`、`frontend/src/product-domains.js` |
| 原型的服务端定价和事务 | 成交价格快照、服务端重叠占用检查、事务写入 | `backend/product_domains/pulse.py` |
| Oia 礼服租赁的真实流程 | 收款/押金、交付/归还、检查、清洗/维修、资产履历 | 同上 |
| 财务与经营分析升级 | 会计事件、双重记账、总账、试算平衡、三张报表、指标字典 | 同上 |

## 运行边界

- 两个产品复用冰焰现有账号认证和 PostgreSQL/SQLite 数据访问层，所有业务表均含 `user_id`。
- 核心功能不调用 OpenAI、Embedding、搜索或其他付费 API。
- 列表与聚合在服务端执行；跃迁域正文按段读取，脉冲域分析不把完整业务表下载到浏览器计算。
- 当前没有税务模块。预测、完整 RFM/Cohort/Funnel 和动态定价仍属于后续能力，不显示伪造结果。
- 生产迁移仅增量创建 `leap_*` 和 `pulse_*` 表，不删除或改写现有招聘、Bridge、Future Radar 数据。

## 数据恢复

上线前应沿用生产 PostgreSQL 的既有备份策略。新增表的回滚方式是停止路由使用并保留数据；本次发布不包含自动删除表的 down migration，避免误删私人材料或 Oia 业务记录。
