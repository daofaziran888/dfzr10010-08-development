# 湖仓一体化治理平台

面向数据架构师的湖仓一体化治理平台。覆盖数据湖表格式管理、Iceberg/Hudi表优化、小文件合并、数据压缩策略、元数据服务、表快照管理、数据分支合并与数据回滚恢复的完整湖仓治理链路。

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
│   ├── app/migration_engine/ 分段迁移引擎（批次边界、一致性校验、审计回写）
│   ├── app/store.py          内存数据仓库与示例数据
│   └── scripts/              引擎与 HTTP 场景自检脚本
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
| 湖表格式管理 | `tablefmt` | 表格式配置 | 配置编号、表名称、格式类型 |
| 小文件合并 | `compact` | 合并任务 | 任务编号、目标表名、文件范围 |
| 数据压缩策略 | `compress` | 压缩配置 | 配置编号、目标表名、压缩算法 |
| 元数据服务 | `metadata` | 元数据表 | 表编号、表名称、字段清单 |
| 表快照管理 | `snapshot` | 快照记录 | 快照编号、表名称、快照时间 |
| 数据分支合并 | `branch` | 分支记录 | 分支编号、表名称、分支类型 |
| 数据回滚恢复 | `rollback` | 回滚记录 | 记录编号、表名称、回滚版本 |
| 分区演进管理 | `partition` | 演进计划 | 计划编号、表名称、当前分区 |
| 表优化管理 | `iceberg` | 优化任务 | 任务编号、表名称、优化类型 |
| 数据目录同步 | `catalog` | 同步作业 | 作业编号、源路径、目标目录 |
| 数据过期清理 | `cleanup` | 清理策略 | 策略编号、目标表名、过期条件 |
| 湖存储配额 | `quota` | 配额配置 | 配置编号、存储空间、表数量上限 |
| 湖仓血缘追踪 | `lineage` | 血缘记录 | 记录编号、上游表名、下游表名 |
| 治理任务调度 | `schedule` | 治理任务 | 任务编号、任务类型、调度表达式 |
| 表统计信息 | `stats` | 统计报告 | 报告编号、表名称、行总数 |
| 数据迁移管理 | `migrate` | 迁移任务 | 任务编号、源表名、目标表名 |
| 分段迁移工作台 | `migration-workbench` | 迁移任务/逻辑批次/分段 | 数据范围、分段状态、源目标校验口径、续做边界 |
| 迁移审计清单 | `migration-audit` | 审计条目/一致性校验结果 | 审计编号、校验结果编号、目标表映射、回写结论 |

### 分段迁移引擎（可恢复提交边界）

`backend/app/migration_engine/` 实现了分段校验与可恢复提交边界，核心约定：

- **三段同批**：每个分段的数据批次迁移、源目标一致性校验（行数 / 去重数 / 金额汇总）、
  血缘写入必须同属一个逻辑迁移批次；三步全部成功分段才进入「已发布」，形成完整提交边界。
- **幂等提交**：同一（任务、分段、批次）重复提交不追加第二份分段或血缘记录，直接回放原结果。
- **冲突处置**：分段校验成功但血缘写入失败时，按任务策略二选一——
  「保留待补血缘」或「撤回分段只发布前一段」；两种情况下一轮都只能从第一个不完整边界
  （`next_resume_seq`）续做，不会从错误位置继续。
- **唯一校验结果**：源目标一致性校验结果只生成一份，迁移审计清单与其它业务入口都引用同一份结果；
  处理结论统一回写迁移审计清单，并在校验结果上保留各入口的回写痕迹。
- **完成口径**：任务完成状态只在全部分段数据与血缘同版（全部「已发布」）后出现。

演示主场景 `orders_2026Q4`：64 段 × 每段 50 万行，按 `order_id` 去重数与 `order_amount`
金额汇总校验，目标分区覆盖 2026-10-01 至 2026-12-31；另有一个 8 段小任务预置血缘故障
（撤回策略）与校验故障，用于演示冲突与恢复。故障注入可通过
`POST /api/migration/tasks/{id}/faults/clear` 解除，模拟下游服务恢复后续做。

后端自检脚本（不依赖 HTTP）：

```bash
cd backend
.venv/bin/python -I scripts/verify_engine.py   # 引擎规则（幂等/冲突/续做/双入口回写）
.venv/bin/python -I scripts/verify_http.py     # 全量 HTTP 场景（需先启动服务）
```

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数仓与数据湖
