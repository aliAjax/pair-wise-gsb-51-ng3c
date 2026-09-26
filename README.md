# 住房贷款纾困申请与履约跟踪

纯Python标准库实现的住房贷款纾困申请与履约跟踪原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、偿付能力、方案阈值和履约状态和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8327
```

默认端口为`8327`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情，`payload`中的`current_payment`、`current_remaining_months`、`current_effective_date`为现行方案。
- `GET /api/records/{id}/audit`：审计时间线，方案变更事件保留调整前后值。
- `GET /api/records/{id}/modifications`：方案变更申请列表，可带`status=pending/approved/rejected`参数。
- `POST /api/records/{id}/modifications`：登记方案变更，请求体为`{"new_payment":3000,"remaining_months":6,"reason":"...","effective_date":"YYYY-MM-DD"}`；仅履约中（`active`）记录可登记，同一贷款最多一笔待审申请。
- `POST /api/records/{id}/modifications/{mid}/review`：审批方案变更，请求体为`{"approve":true,"review_note":"..."}`；同意时新月供不得高于现方案、剩余期数不得超过原期限，不符合自动驳回并保留原方案。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
