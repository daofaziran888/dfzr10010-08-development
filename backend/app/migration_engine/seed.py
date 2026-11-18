"""分段迁移演示数据播种。

主场景对需求示例：orders_2026Q4 分为 64 段、每段 50 万行，按 order_id 去重数与
金额汇总校验，目标分区覆盖 2026-10-01 到 2026-12-31。
另备一个 8 段的小任务，预置血缘故障与校验故障，方便直接演示冲突与恢复。
"""
from __future__ import annotations

from app.migration_engine.engine import MigrationEngine
from app.migration_engine.models import (
    CONFLICT_KEEP,
    CONFLICT_ROLLBACK,
    MODE_BATCH,
    MODE_SINGLE,
    SEGMENT_PUBLISHED,
    MigrationRepository,
)


def seed_demo_tasks(engine: MigrationEngine) -> None:
    # 场景一：orders_2026Q4，64 段 × 50 万行，冲突时保留待补血缘
    main_task, errors = engine.create_task({
        "task_code": "orders_2026Q4",
        "source_table": "ods.orders",
        "target_table": "dwd.orders_df",
        "data_scope": "order_id 全量 3200 万行，按序号均分 64 段",
        "partition_start": "2026-10-01",
        "partition_end": "2026-12-31",
        "dedup_key": "order_id",
        "amount_field": "order_amount",
        "check_rules": [
            "源目标行数一致",
            "order_id 去重数一致",
            "order_amount 金额汇总一致",
        ],
        "target_table_mapping": {
            "order_id": "order_id",
            "order_amount": "order_amt",
            "cust_id": "customer_id",
            "dt": "dt",
        },
        "segment_count": 64,
        "rows_per_segment": 500_000,
        "conflict_policy": CONFLICT_KEEP,
        "lineage_fail_seqs": [7],
        "check_fail_seqs": [],
    })
    if not errors and main_task is not None:
        # 预跑前 6 段，形成 6 个完整边界，打开页面即有进度
        engine.submit_batch(
            main_task.task_id,
            segment_seqs=list(range(1, 7)),
            mode=MODE_BATCH,
        )

    # 场景二：小任务 8 段，预置撤回策略 + 血缘故障（第 3 段）+ 校验故障（第 5 段）
    small_task, errors = engine.create_task({
        "task_code": "payments_2026Q4_demo",
        "source_table": "ods.payments",
        "target_table": "dwd.payments_df",
        "data_scope": "payment_id 全量 8 万行，按序号均分 8 段",
        "partition_start": "2026-10-01",
        "partition_end": "2026-12-31",
        "dedup_key": "payment_id",
        "amount_field": "pay_amount",
        "target_table_mapping": {
            "payment_id": "payment_id",
            "pay_amount": "pay_amt",
            "dt": "dt",
        },
        "segment_count": 8,
        "rows_per_segment": 10_000,
        "conflict_policy": CONFLICT_ROLLBACK,
        "lineage_fail_seqs": [3],
        "check_fail_seqs": [5],
    })
    if not errors and small_task is not None:
        # 逐条执行前 3 段：前两段发布，第 3 段触发血缘失败并被撤回
        engine.submit_batch(
            small_task.task_id,
            segment_seqs=[1, 2, 3],
            mode=MODE_SINGLE,
        )


def build_seeded_repository() -> MigrationRepository:
    repository = MigrationRepository()
    seed_demo_tasks(MigrationEngine(repository))
    return repository
