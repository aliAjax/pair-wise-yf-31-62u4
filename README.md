# 航班中断恢复系统

独立的 Python 标准库项目，用 SQLite 保存机场、飞机、机组、航线许可、航班、中断事件和恢复方案。系统会校验维护间隔、执勤时限、机场宵禁、航线许可、资源重叠，并计算取消、延误、受影响旅客和错失衔接成本。

## 运行

```bash
python3 app.py --db airline_recovery.db
```

默认监听 `127.0.0.1:8202`，首页为 `/`，健康检查为 `/health`。

身份头：`X-User-Id`、`X-Role`。角色包括 `viewer`、`scheduler`、`ops_manager`、`auditor`。

## 主要接口

- `POST /api/airports`、`/api/aircraft`、`/api/crew`、`/api/permits`：基础资源与约束。
- `POST /api/flights`、`POST /api/disruptions`：创建航班和中断。
- `POST /api/recovery-plans`：一次提交方案及航班调整。
- `POST /api/plans/{id}/assignments`：用 `expected_revision` 临时改派。
- `POST /api/plans/{id}/validate`、`/lock`：校验并原子锁定方案。
- `GET /api/disruptions/{id}/compare`：比较恢复方案成本。
- `POST /api/flights/{id}/cancel`、`/recover`：取消和人工恢复。
- `POST /api/rolling/preview`：恢复滚动预览。以一趟航班为起点，沿同一架飞机后续航段顺延或保留取消，逐段核对维护、执勤、宵禁和航线许可，冲突写清航班号和约束；预览落库为调整记录。
- `POST /api/rolling/{id}/confirm`：确认后写入执行方案，只改预览涉及的航段，其他航班保持原样；有冲突或预览后航班版本已变会被拒绝。
- `GET /api/rolling`、`/api/rolling/{id}`：查询滚动调整记录。
- `GET /api/state`、`GET /api/plans/{id}`：查询状态和影响。
- `GET /rolling`：恢复滚动台页面入口。

## 文件结构

- `app.py`：HTTP 服务、恢复方案与既有校验。
- `common.py`：共享错误类型与时间工具。
- `rolling.py`：恢复滚动计算（顺延/保留取消、逐段约束核对），只读不写。
- `adjustments.py`：滚动调整记录（预览落库、确认写入执行方案、历史查询）。
- `static/rolling.html`：恢复滚动台页面入口。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

时区和机场本地时刻没有引入完整时区数据库；模型使用简化航线许可与宵禁规则。身份头、SQLite 和单进程 HTTP 服务适合原型演示，正式运行需要外部身份系统、共享数据库和更强的跨实例锁。
