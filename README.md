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
- `POST /api/rolling/preview`、`/api/rolling/confirm`：换机/延误后沿同一架飞机后续航段滚动顺延或保留取消，逐段核对维护、执勤、宵禁、航线许可与机位衔接；冲突写清航班号和约束，确认无冲突后才写入执行方案。
- `GET /api/rolling-records`：滚动调整记录（可带 `?plan_id=`）。
- 调度台页面：首页 `/` 与滚动恢复页 `/rolling`。
- `GET /api/state`、`GET /api/plans/{id}`：查询状态和影响。

## 滚动恢复的三个业务文件

- `rolling.py`：纯滚动计算。给定航班/资源快照与首班调整，按同一架飞机后续航段时间顺序逐段顺延；前一班未落地则后班不得沿用原起飞时刻；已取消航段保留取消并中断滚动链。逐段核对维护到期、机组执勤、起降宵禁、航线许可和机位衔接，冲突均带航班号、约束类型与中文说明；不做任何写入。
- `adjustments.py`：调整记录业务服务。提供试算（preview，不写入）与确认（confirm，事务内重算，有冲突整体回滚；仅受影响航段写入方案，其他航班保持原样），并在 `rolling_adjustments` 表与审计日志留痕，支持追加到草案（乐观版本号）和调整记录查询。
- `static/rolling.html`：页面入口（`/rolling`），选择触发航班、试算逐段核对结果、确认写入并查看调整记录；首页 `/` 提供链接。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

时区和机场本地时刻没有引入完整时区数据库；模型使用简化航线许可与宵禁规则。身份头、SQLite 和单进程 HTTP 服务适合原型演示，正式运行需要外部身份系统、共享数据库和更强的跨实例锁。
