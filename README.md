# 智能采集质量管控平台

面向数据采集团队的质量管控与效率优化平台。覆盖采集任务编排、抓取结果校验、采集链路监控、数据格式转换、去重比对、异常重试、采集审计与合规管理的完整采集质控链路。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集任务编排 | `taskplan` | 采集计划 | 计划编号、目标站点、采集频率 |
| 采集结果校验 | `verify` | 校验规则 | 规则编号、校验对象、校验类型 |
| 采集链路监控 | `pipeline` | 链路节点 | 节点编号、节点名称、上游节点 |
| 采集格式转换 | `transform` | 转换模板 | 模板编号、输入格式、输出格式 |
| 采集去重比对 | `dedup` | 去重策略 | 策略编号、业务键列、排序字段 |
| 异常重试管理 | `retry` | 重试规则 | 规则编号、异常类型、重试上限 |
| 采集审计日志 | `audit` | 审计记录 | 记录编号、操作类型、采集计划 |
| 采集合规配置 | `compliance` | 合规规则 | 规则编号、站点域名、协议标识 |
| 代理池管理 | `proxy` | 代理节点 | 节点编号、代理地址、代理类型 |
| 采集存储策略 | `storage` | 存储配置 | 配置编号、存储路径、文件格式 |
| 采集告警中心 | `alert` | 告警规则 | 规则编号、监控指标、告警级别 |
| 采集统计报表 | `report` | 统计报表 | 报表编号、统计维度、统计周期 |
| 采集频率控制 | `throttle` | 频率策略 | 策略编号、目标站点、请求间隔 |
| 采集数据标注 | `tag` | 标注记录 | 标注编号、数据记录、标注类型 |
| 采集配置版本 | `version` | 配置版本 | 版本编号、配置名称、版本号 |
| 采集沙箱测试 | `sandbox` | 沙箱实例 | 实例编号、测试脚本、目标站点 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 告警聚合与恢复

采集失败不再逐条提醒。`app/services/incident.py` 是聚合域的唯一写入口，
告警面板（`/alert`）与采集计划处置清单（`/taskplan`）注入同一个服务实例，共用同一聚合结论。

- **聚合口径**：按「监控指标 + 告警级别 + 采集计划」合并相邻事件，记录首次触发、
  最近触发、累计次数与静默截止时间；同一事件编号重复上报只并入同一聚合组，不重复计数。
- **静默与升级取舍**：级别越高静默越短、升级优先级越高——紧急 5 分钟 / P1，
  重要 15 分钟 / P2，一般 30 分钟 / P3；默认累计 3 次达到阈值。静默期内继续累计，
  但不重复通知、不重复升级。
- **升级名额**：全局唯一。达到阈值时当前静默批次申请占用，两个批次并发申请只接受
  一次占用，其余批次标记「升级被拒」；恢复后释放，后续批次可再申请。
- **处置待办**：达到阈值后在采集计划处置清单生成待办，字段实时取自聚合组；
  告警恢复（自动）或人工确认后退出，同组再次活跃会重新进入清单。
- **恢复**：上报恢复事件即关闭聚合组，生成一条带恢复结论的恢复通知；
  原始事件与全部通知长期保留，历史告警在「历史事件 / 通知记录」页可追溯。
- **面板操作**：批量选择、多条合并（冲突时以最近一次有效聚合为准）、逐条确认、
  整组恢复、跳过单条；一次提交后按条分批展示结果，单条失败不阻断其余。

主要接口：`/api/alert/incidents/{stats,groups,notifications,events,batch}`、
`/api/alert/incidents/groups/{id}/{acknowledge,recover}`、
`/api/taskplan/incidents/todos`。

回归（ALER-0002 × TASK-0002，覆盖静默、去重、待办、升级互斥并发、恢复、批量处置）：

```bash
make regression
# 或 cd backend && .venv/bin/python tests/regression_alert.py
```


领域：数据工程
子方向：数据采集、爬虫与RPA
