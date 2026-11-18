"""分段迁移引擎核心规则验证（不依赖 HTTP）。

运行：backend/.venv/bin/python -I scripts/verify_engine.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.migration_engine.engine import MigrationEngine
from app.migration_engine.models import (
    AUDIT_PASSED,
    AUDIT_PENDING,
    AUDIT_REJECTED,
    CONFLICT_KEEP,
    CONFLICT_ROLLBACK,
    MODE_BATCH,
    MODE_GROUP,
    MODE_SINGLE,
    SEGMENT_PENDING,
    SEGMENT_PUBLISHED,
    SEGMENT_ROLLED_BACK,
    SEGMENT_VERIFIED_WAITING_LINEAGE,
    TASK_DONE,
    TASK_FAILED,
    TASK_PAUSED,
    TASK_RUNNING,
    MigrationRepository,
)

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def main() -> int:
    repo = MigrationRepository()
    engine = MigrationEngine(repo)

    # 1. 任务登记与分段生成（64 段 × 50 万行示例口径）
    task, errors = engine.create_task({
        "task_code": "orders_2026Q4",
        "source_table": "ods.orders",
        "target_table": "dwd.orders_df",
        "segment_count": 64,
        "rows_per_segment": 500_000,
        "conflict_policy": CONFLICT_KEEP,
        "lineage_fail_seqs": [7],
    })
    check("登记无错误", not errors, str(errors))
    check("生成 64 个分段", task.segment_count == len(task.segments) == 64)
    s1 = task.segments[0]
    check("分段数据范围", s1.range_start == 0 and s1.range_end == 499_999)
    s64 = task.segments[-1]
    check("末段范围", s64.range_start == 31_500_000 and s64.range_end == 31_999_999)
    check("源端行数基准", s1.source_row_count == 500_000)
    check("去重数小于行数（有重复口径）", s1.source_distinct_key < s1.source_row_count)
    check("初始续做点为第 1 段", task.next_resume_seq() == 1)
    check("初始未完成", task.progress()["completed"] is False)

    # 2. 批量提交前 6 段：数据+校验+血缘同批，全部发布
    batch, msg = engine.submit_batch(task.task_id, segment_seqs=[1, 2, 3, 4, 5, 6], mode=MODE_BATCH)
    check("前 6 段全部发布", all(s.status == SEGMENT_PUBLISHED for s in task.segments[:6]))
    check("续做位置前进到第 7 段", task.next_resume_seq() == 7)
    check("每段都有校验结果", all(s.check_result_id for s in task.segments[:6]))
    check("每段都有血缘", all(s.lineage_record_id for s in task.segments[:6]))
    check("血缘与分段同批次", all(r.batch_id == batch.batch_id for r in engine.list_lineage(task.task_id)))
    check("血缘共 6 条", len(engine.list_lineage(task.task_id)) == 6)
    check("审计清单生成 6 条", len(engine.list_audit_items(task_id=task.task_id)) == 6)
    check("任务处于迁移中", task.status == TASK_RUNNING)

    # 3. 幂等：同批次号重复提交，不追加血缘/审计，结果回放
    audit_before = len(engine.list_audit_items(task_id=task.task_id))
    lineage_before = len(engine.list_lineage(task.task_id))
    again, msg = engine.submit_batch(
        task.task_id, segment_seqs=[1, 2], mode=MODE_BATCH, batch_id=batch.batch_id
    )
    check("重复批次号幂等回放", msg.startswith("批次") and "幂等" in msg)
    check("血缘未追加", len(engine.list_lineage(task.task_id)) == lineage_before)
    check("审计未追加", len(engine.list_audit_items(task_id=task.task_id)) == audit_before)
    # 不带批次号重复提交已发布分段：逐段幂等，同样不追加
    engine.submit_batch(task.task_id, segment_seqs=[1], mode=MODE_SINGLE)
    check("已发布分段重复提交不追加血缘", len(engine.list_lineage(task.task_id)) == lineage_before)

    # 4. 冲突场景（KEEP 策略）：第 7 段校验成功、血缘失败 → 已校验待补血缘
    b7, _ = engine.submit_batch(task.task_id, segment_seqs=[7], mode=MODE_GROUP)
    seg7 = task.segments[6]
    check("第 7 段保留待补血缘", seg7.status == SEGMENT_VERIFIED_WAITING_LINEAGE)
    check("第 7 段有校验结果但无血缘", seg7.check_result_id and not seg7.lineage_record_id)
    check("续做位置仍停在第 7 段", task.next_resume_seq() == 7)
    check("任务状态为待补血缘", task.status == TASK_PAUSED)
    audit7 = repo.find_audit_by_result(seg7.check_result_id)
    check("审计保留待审计并标注补血缘", audit7.status == AUDIT_PENDING and "补血缘" in (audit7.conclusion_remark or ""))

    # 5. 续做：从第 7 段继续（先解除故障再续做，模拟下一轮血缘服务恢复）
    task.lineage_fail_seqs = []
    chk7_before = seg7.check_result_id
    audit7_before = repo.find_audit_by_result(chk7_before).audit_id
    audit7_count_before = sum(
        1 for a in engine.list_audit_items(task_id=task.task_id) if a.segment_seq == 7
    )
    rb, msg = engine.resume(task.task_id, mode=MODE_BATCH, limit=3)
    check("续做批次包含 7/8/9 段", rb.segment_seqs == [7, 8, 9])
    check("第 7 段补血缘后发布", task.segments[6].status == SEGMENT_PUBLISHED)
    check("第 7 段血缘补齐", bool(task.segments[6].lineage_record_id))
    check("续做位置前进到第 10 段", task.next_resume_seq() == 10)
    check("重跑复用同一份校验结果", task.segments[6].check_result_id == chk7_before)
    check("重跑复用同一条审计条目", repo.find_audit_by_result(chk7_before).audit_id == audit7_before)
    audit7_count_after = sum(
        1 for a in engine.list_audit_items(task_id=task.task_id) if a.segment_seq == 7
    )
    check("第 7 段重跑未产生新审计条目", audit7_count_after == audit7_count_before == 1)

    # 6. 完成状态只在数据与血缘同版（全部发布）后出现
    engine.submit_batch(task.task_id, segment_seqs=list(range(10, 65)), mode=MODE_BATCH)
    check("64 段全部发布", all(s.status == SEGMENT_PUBLISHED for s in task.segments))
    check("续做位置越界（无续做点）", task.next_resume_seq() == 65)
    check("任务完成只在全发布后出现", task.status == TASK_DONE and task.progress()["completed"] is True)
    check("完成百分比 100", task.progress()["percent"] == 100.0)
    check("血缘总数 64", len(engine.list_lineage(task.task_id)) == 64)

    # 7. 校验失败阻断：另建任务，第 5 段目标行数不一致
    repo2 = MigrationRepository()
    eng2 = MigrationEngine(repo2)
    t2, _ = eng2.create_task({
        "task_code": "pay_demo",
        "source_table": "ods.pay",
        "target_table": "dwd.pay",
        "segment_count": 8,
        "rows_per_segment": 10_000,
        "conflict_policy": CONFLICT_ROLLBACK,
        "lineage_fail_seqs": [3],
        "check_fail_seqs": [5],
    })
    eng2.submit_batch(t2.task_id, segment_seqs=[1, 2, 3], mode=MODE_SINGLE)
    seg3 = t2.segments[2]
    check("ROLLBACK 策略：第 3 段撤回", seg3.status == SEGMENT_ROLLED_BACK)
    check("撤回段无血缘", seg3.lineage_record_id is None)
    check("撤回段目标统计清空", seg3.target_row_count is None)
    check("撤回后续做位置仍为第 3 段（只发布前一段）", t2.next_resume_seq() == 3)
    check("前两段保持发布", [s.status for s in t2.segments[:2]] == [SEGMENT_PUBLISHED] * 2)
    audit3 = repo2.find_audit_by_result(seg3.check_result_id)
    check("撤回段审计驳回", audit3.status == AUDIT_REJECTED)

    # 续做撤回段（故障仍在）→ 再次撤回，边界仍停在 3
    eng2.resume(t2.task_id)
    check("撤回段续做仍停在第 3 段边界", t2.next_resume_seq() == 3)
    # 故障恢复后续做 3、4、5：第 5 段校验失败
    t2.lineage_fail_seqs = []
    eng2.submit_batch(t2.task_id, segment_seqs=[3, 4, 5], mode=MODE_GROUP)
    check("第 3/4 段恢复发布", t2.segments[2].status == SEGMENT_PUBLISHED and t2.segments[3].status == SEGMENT_PUBLISHED)
    seg5 = t2.segments[4]
    check("第 5 段校验失败", seg5.status == TASK_FAILED)
    check("失败段不写血缘", seg5.lineage_record_id is None)
    chk5 = eng2.get_check_result(seg5.check_result_id)
    check("失败校验结果三项均不匹配", not (chk5.row_count_match and chk5.distinct_match and chk5.amount_match))
    check("任务状态为校验失败", t2.status == TASK_FAILED)
    check("续做位置停在第 5 段", t2.next_resume_seq() == 5)
    # 故障恢复后重跑第 5 段：同一份校验结果被刷新为通过，不新增结果与审计条目
    chk5_before = seg5.check_result_id
    audit5_before = repo2.find_audit_by_result(chk5_before).audit_id
    t2.check_fail_seqs = []
    eng2.submit_batch(t2.task_id, segment_seqs=[5], mode=MODE_BATCH)
    check("第 5 段恢复后发布", t2.segments[4].status == SEGMENT_PUBLISHED)
    check("校验结果编号保持不变", t2.segments[4].check_result_id == chk5_before)
    check("审计条目编号保持不变", repo2.find_audit_by_result(chk5_before).audit_id == audit5_before)
    check("原失败结果已原地刷新为通过", eng2.get_check_result(chk5_before).passed is True)

    # 8. 双入口回写同一份校验结果，审计清单结论一致
    result_id = t2.segments[0].check_result_id
    audit, msg = eng2.write_audit_conclusion(
        result_id, conclusion=AUDIT_PASSED, remark="审计清单入口通过", source_entry="迁移审计清单"
    )
    check("审计入口回写通过", audit.status == AUDIT_PASSED and audit.source_entry == "迁移审计清单")
    # 其它业务入口再回写驳回
    audit2, msg = eng2.write_audit_conclusion(
        result_id, conclusion=AUDIT_REJECTED, remark="外部入口复核驳回", source_entry="其它业务入口"
    )
    check("外部入口结论同步到同一审计条目", audit2.audit_id == audit.audit_id and audit2.status == AUDIT_REJECTED)
    chk = eng2.get_check_result(result_id)
    check("唯一校验结果保留两次回写痕迹", len(chk.conclusions) == 2)
    check("审计条目保存目标表映射", bool(audit2.target_table_mapping))

    # 9. 多选分段分别保留结果
    repo3 = MigrationRepository()
    eng3 = MigrationEngine(repo3)
    t3, _ = eng3.create_task({
        "task_code": "multi_demo",
        "source_table": "a",
        "target_table": "b",
        "segment_count": 4,
        "rows_per_segment": 1000,
        "conflict_policy": CONFLICT_KEEP,
        "lineage_fail_seqs": [2],
    })
    b, _ = eng3.submit_batch(t3.task_id, segment_seqs=[1, 2, 3], mode=MODE_BATCH)
    outcomes = b.segment_outcomes
    check("批次保留三段独立结果", set(outcomes) == {1, 2, 3})
    check("第 1 段发布 / 第 2 段待补血缘 / 第 3 段发布",
          outcomes[1]["status"] == SEGMENT_PUBLISHED
          and outcomes[2]["status"] == SEGMENT_VERIFIED_WAITING_LINEAGE
          and outcomes[3]["status"] == SEGMENT_PUBLISHED)
    check("待迁移分段未被批次触碰", t3.segments[3].status == SEGMENT_PENDING)

    print()
    if failures:
        print(f"共 {len(failures)} 项失败：{failures}")
        return 1
    print("全部引擎规则验证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
