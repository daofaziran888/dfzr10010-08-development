"""分段迁移引擎：批次提交、源目标一致性校验、血缘同批写入与可恢复边界。

一个批次对每个分段严格按三步执行，任一步缺失都不会形成完整提交边界：

    1. 数据批次迁移（源端 → 目标端，回填目标行数/去重数/金额汇总）
    2. 源目标一致性校验（行数、去重数、金额汇总三项规则），生成唯一一份校验结果
    3. 血缘记录写入（与分段同批次）

冲突口径：第 2 步成功但第 3 步失败时，按任务冲突策略——
- CONFLICT_KEEP：分段保留为「已校验待补血缘」，审计清单留待补血缘条目；
- CONFLICT_ROLLBACK：撤回该分段（目标数据与校验结果标记撤回），只发布前一段。
两种情况下一轮都只能从 `next_resume_seq`（第一个不完整边界）续做。
"""
from __future__ import annotations

from typing import Any

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
    TASK_DRAFT,
    TASK_FAILED,
    TASK_PAUSED,
    TASK_RUNNING,
    CheckResult,
    AuditItem,
    LineageRecord,
    MigrationBatch,
    MigrationRepository,
    MigrationTask,
    Segment,
    now_text,
    repo as default_repo,
)

VALID_MODES = {MODE_BATCH, MODE_GROUP, MODE_SINGLE}
VALID_POLICIES = {CONFLICT_KEEP, CONFLICT_ROLLBACK}
VALID_CONCLUSIONS = {AUDIT_PASSED, AUDIT_REJECTED}


class MigrationEngine:
    def __init__(self, repository: MigrationRepository | None = None) -> None:
        self.repo = repository or default_repo

    # ------------------------------------------------------------------
    # 任务登记与分段生成
    # ------------------------------------------------------------------
    def create_task(self, values: dict[str, Any]) -> tuple[MigrationTask | None, list[str]]:
        required = ["source_table", "target_table", "segment_count", "rows_per_segment"]
        missing = [name for name in required if values.get(name) in (None, "", [])]
        if missing:
            return None, [f"缺少必填参数：{name}" for name in missing]

        try:
            segment_count = int(values["segment_count"])
            rows_per_segment = int(values["rows_per_segment"])
        except (TypeError, ValueError):
            return None, ["分段数与每段行数必须是正整数"]
        if segment_count <= 0 or rows_per_segment <= 0:
            return None, ["分段数与每段行数必须是正整数"]
        if segment_count > 512:
            return None, ["单任务分段数不能超过 512"]

        conflict_policy = str(values.get("conflict_policy") or CONFLICT_KEEP)
        if conflict_policy not in VALID_POLICIES:
            return None, [f"冲突策略仅支持：{'、'.join(sorted(VALID_POLICIES))}"]

        task_code = str(values.get("task_code") or "").strip()
        if not task_code:
            task_code = f"MIGR-T{len(self.repo.tasks) + 1:04d}"
        if any(t.task_code == task_code for t in self.repo.list_tasks()):
            return None, [f"任务编号 {task_code} 已存在"]

        source_table = str(values["source_table"]).strip()
        target_table = str(values["target_table"]).strip()
        data_scope = str(values.get("data_scope") or f"{source_table} 全量分区").strip()
        partition_start = str(values.get("partition_start") or "2026-10-01")
        partition_end = str(values.get("partition_end") or "2026-12-31")
        dedup_key = str(values.get("dedup_key") or "order_id")
        amount_field = str(values.get("amount_field") or "amount")
        check_rules = list(values.get("check_rules") or [
            "源目标行数一致",
            f"{dedup_key} 去重数一致",
            f"{amount_field} 金额汇总一致",
        ])
        field_mapping = dict(values.get("target_table_mapping") or {
            dedup_key: dedup_key,
            amount_field: amount_field,
            "dt": "dt",
        })
        lineage_fail_seqs = self._safe_int_list(values.get("lineage_fail_seqs"))
        check_fail_seqs = self._safe_int_list(values.get("check_fail_seqs"))

        task_id = self.repo.next_id("TASK")
        segments = [
            self._build_segment(
                seq=seq,
                task_code=task_code,
                rows_per_segment=rows_per_segment,
                partition_start=partition_start,
                partition_end=partition_end,
                check_fail_seqs=check_fail_seqs,
            )
            for seq in range(1, segment_count + 1)
        ]
        task = MigrationTask(
            task_id=task_id,
            task_code=task_code,
            source_table=source_table,
            target_table=target_table,
            target_table_mapping=field_mapping,
            data_scope=data_scope,
            partition_start=partition_start,
            partition_end=partition_end,
            dedup_key=dedup_key,
            amount_field=amount_field,
            check_rules=check_rules,
            segment_count=segment_count,
            rows_per_segment=rows_per_segment,
            conflict_policy=conflict_policy,
            lineage_fail_seqs=lineage_fail_seqs,
            check_fail_seqs=check_fail_seqs,
            status=TASK_DRAFT,
            created_at=now_text(),
            updated_at=now_text(),
            segments=segments,
        )
        self.repo.save_task(task)
        return task, []

    def _build_segment(
        self,
        *,
        seq: int,
        task_code: str,
        rows_per_segment: int,
        partition_start: str,
        partition_end: str,
        check_fail_seqs: list[int],
    ) -> Segment:
        """按确定口径生成源端统计基准；check_fail 分段预留目标端偏差。"""
        range_start = (seq - 1) * rows_per_segment
        range_end = seq * rows_per_segment - 1
        # 源端口径：每段固定行数，少量重复行使去重数略小于行数（演示去重校验）
        duplicate_rows = rows_per_segment // 20
        source_distinct = rows_per_segment - duplicate_rows
        # 金额按分段序号取一个可复现的口径
        source_amount = round(rows_per_segment * 128.5 + seq * 137.25, 2)
        return Segment(
            seq=seq,
            name=f"{task_code}-S{seq:03d}",
            range_start=range_start,
            range_end=range_end,
            rows_per_segment=rows_per_segment,
            partition_start=partition_start,
            partition_end=partition_end,
            source_row_count=rows_per_segment,
            source_distinct_key=source_distinct,
            source_amount_sum=source_amount,
        )

    @staticmethod
    def _safe_int_list(raw: Any) -> list[int]:
        if not raw:
            return []
        if isinstance(raw, str):
            raw = raw.split(",")
        out: list[int] = []
        for item in raw:
            try:
                out.append(int(item))
            except (TypeError, ValueError):
                continue
        return out

    # ------------------------------------------------------------------
    # 批次提交：多选分段一次提交，分别保留结果
    # ------------------------------------------------------------------
    def submit_batch(
        self,
        task_id: str,
        *,
        segment_seqs: list[int],
        mode: str = MODE_BATCH,
        batch_id: str | None = None,
    ) -> tuple[MigrationBatch | None, str]:
        task = self.repo.get_task(task_id)
        if task is None:
            return None, f"迁移任务 {task_id} 不存在"
        if mode not in VALID_MODES:
            return None, f"执行模式仅支持：{'、'.join(sorted(VALID_MODES))}"
        if not segment_seqs:
            return None, "至少选择一个分段才能提交批次"

        seqs = sorted({int(s) for s in segment_seqs})
        invalid = [s for s in seqs if s < 1 or s > task.segment_count]
        if invalid:
            return None, f"分段序号超出范围：{invalid}"

        # 幂等：带 batch_id 的重复提交直接回放原批次，不追加任何分段/血缘记录
        if batch_id:
            existing = self.repo.find_batch(task, batch_id)
            if existing is not None:
                return existing, f"批次 {batch_id} 已提交，按幂等口径回放原结果，未追加记录"

        new_batch = MigrationBatch(
            batch_id=batch_id or self.repo.next_id("BATCH"),
            task_id=task.task_id,
            task_code=task.task_code,
            mode=mode,
            segment_seqs=seqs,
            submitted_segment_seqs=[],
            conflict_policy=task.conflict_policy,
            created_at=now_text(),
        )
        task.batches.append(new_batch)
        if task.status == TASK_DRAFT:
            task.status = TASK_RUNNING

        for seq in seqs:
            seg = self.repo.get_segment(task, seq)
            assert seg is not None
            outcome = self._process_segment(task, seg, new_batch)
            new_batch.segment_outcomes[seq] = outcome
            new_batch.submitted_segment_seqs.append(seq)

        self._refresh_task_status(task)
        self.repo.save_task(task)
        return new_batch, "批次提交完成"

    def _process_segment(
        self, task: MigrationTask, seg: Segment, batch: MigrationBatch
    ) -> dict[str, Any]:
        """单分段三步管道；返回该分段的独立处理结果（多选分段分别保留）。"""
        seg.attempts += 1
        seg.updated_at = now_text()

        # 已发布分段：完整边界，重复提交不追加同段记录，直接回放
        if seg.status == SEGMENT_PUBLISHED:
            return {
                "seq": seg.seq,
                "status": SEGMENT_PUBLISHED,
                "idempotent": True,
                "message": "分段已发布，重复提交未追加记录",
                "check_result_id": seg.check_result_id,
                "lineage_record_id": seg.lineage_record_id,
                "batch_id": seg.batch_id,
            }

        # ---- 第 1 步：数据批次迁移 ------------------------------------
        seg.status = "迁移中"
        seg.batch_id = batch.batch_id
        target_rows, target_distinct, target_amount = self._migrate_segment(task, seg)
        seg.target_row_count = target_rows
        seg.target_distinct_key = target_distinct
        seg.target_amount_sum = target_amount

        # ---- 第 2 步：源目标一致性校验（同分段跨轮次复用同一份结果） ---
        previous_result = (
            self.repo.get_check_result(seg.check_result_id) if seg.check_result_id else None
        )
        result = self._build_or_refresh_check_result(task, seg, batch, previous_result)
        seg.check_result_id = result.result_id
        self.repo.save_check_result(result)
        # 审计清单基于同一份校验结果生成/刷新，并保存目标表映射
        self._ensure_audit_item(task, seg, result)

        if not result.passed:
            # 校验失败：分段停在失败态，不写血缘，不形成边界
            seg.status = TASK_FAILED
            return {
                "seq": seg.seq,
                "status": TASK_FAILED,
                "idempotent": False,
                "message": "源目标一致性校验未通过，已阻断血缘写入与发布",
                "check_result_id": result.result_id,
            }

        # ---- 第 3 步：血缘写入（与分段同批次） ------------------------
        lineage_ok = seg.seq not in task.lineage_fail_seqs
        if lineage_ok:
            lineage = self._write_lineage(task, seg, batch)
            seg.lineage_record_id = lineage.lineage_id
            seg.status = SEGMENT_PUBLISHED
            return {
                "seq": seg.seq,
                "status": SEGMENT_PUBLISHED,
                "idempotent": False,
                "message": "数据迁移、一致性校验、血缘写入同批完成，分段已发布",
                "check_result_id": result.result_id,
                "lineage_record_id": lineage.lineage_id,
                "batch_id": batch.batch_id,
            }

        # ---- 冲突：校验成功但血缘写入失败 -----------------------------
        return self._handle_lineage_conflict(task, seg, batch, result)

    def _migrate_segment(
        self, task: MigrationTask, seg: Segment
    ) -> tuple[int, int, float]:
        """模拟数据批次迁移；check_fail 分段制造目标端行数偏差。"""
        if seg.seq in task.check_fail_seqs:
            # 目标端少写了一批行，去重数与金额随之偏差
            missing = max(1, seg.rows_per_segment // 10)
            return (
                seg.source_row_count - missing,
                seg.source_distinct_key - missing,
                round(seg.source_amount_sum - missing * 128.5, 2),
            )
        return seg.source_row_count, seg.source_distinct_key, seg.source_amount_sum

    def _build_or_refresh_check_result(
        self,
        task: MigrationTask,
        seg: Segment,
        batch: MigrationBatch,
        previous: CheckResult | None,
    ) -> CheckResult:
        """生成校验结果；分段在后续轮次重跑时复用同一份结果，原地刷新口径与批次。

        复用保证一个分段始终对应同一份源目标一致性校验结果，历史回写痕迹不丢；
        只有从未校验过的分段才分配新的结果编号。
        """
        row_match = seg.target_row_count == seg.source_row_count
        distinct_match = seg.target_distinct_key == seg.source_distinct_key
        amount_match = round(float(seg.target_amount_sum or 0), 2) == round(
            seg.source_amount_sum, 2
        )
        passed = row_match and distinct_match and amount_match
        rule_desc = "；".join(task.check_rules)
        if passed:
            conclusion = (
                f"分段 {seg.name} 源目标行数 {seg.source_row_count} 一致、"
                f"{task.dedup_key} 去重数 {seg.source_distinct_key} 一致、"
                f"{task.amount_field} 金额汇总 {seg.source_amount_sum} 一致，校验通过"
            )
        else:
            conclusion = (
                f"分段 {seg.name} 校验未通过："
                f"行数 {seg.source_row_count}→{seg.target_row_count}、"
                f"去重数 {seg.source_distinct_key}→{seg.target_distinct_key}、"
                f"金额 {seg.source_amount_sum}→{seg.target_amount_sum}"
            )

        if previous is not None:
            previous.batch_id = batch.batch_id
            previous.rule_desc = rule_desc
            previous.target_row_count = int(seg.target_row_count or 0)
            previous.target_distinct_key = int(seg.target_distinct_key or 0)
            previous.target_amount_sum = round(float(seg.target_amount_sum or 0), 2)
            previous.row_count_match = row_match
            previous.distinct_match = distinct_match
            previous.amount_match = amount_match
            previous.passed = passed
            previous.conclusion = conclusion
            return previous

        return CheckResult(
            result_id=self.repo.next_id("CHK"),
            task_id=task.task_id,
            task_code=task.task_code,
            segment_seq=seg.seq,
            segment_name=seg.name,
            batch_id=batch.batch_id,
            rule_desc=rule_desc,
            source_row_count=seg.source_row_count,
            target_row_count=int(seg.target_row_count or 0),
            source_distinct_key=seg.source_distinct_key,
            target_distinct_key=int(seg.target_distinct_key or 0),
            source_amount_sum=seg.source_amount_sum,
            target_amount_sum=round(float(seg.target_amount_sum or 0), 2),
            row_count_match=row_match,
            distinct_match=distinct_match,
            amount_match=amount_match,
            passed=passed,
            conclusion=conclusion,
            created_at=now_text(),
        )

    def _ensure_audit_item(
        self, task: MigrationTask, seg: Segment, result: CheckResult
    ) -> AuditItem:
        """审计清单与校验结果一一对应（upsert）：重跑同分段时刷新同一条目。"""
        existing = self.repo.find_audit_by_result(result.result_id)
        if existing is not None:
            existing.batch_id = result.batch_id
            existing.check_conclusion = result.conclusion
            existing.check_passed = result.passed
            existing.target_table_mapping = dict(task.target_table_mapping)
            # 新一轮重新校验后回到待审计，旧结论保留在校验结果的回写痕迹里
            existing.status = AUDIT_PENDING
            existing.conclusion_remark = None
            self.repo.save_audit_item(existing)
            return existing
        item = AuditItem(
            audit_id=self.repo.next_id("AUD"),
            task_id=task.task_id,
            task_code=task.task_code,
            segment_seq=seg.seq,
            segment_name=seg.name,
            batch_id=result.batch_id,
            check_result_id=result.result_id,
            target_table_mapping=dict(task.target_table_mapping),
            check_conclusion=result.conclusion,
            check_passed=result.passed,
            status=AUDIT_PENDING,
            source_entry="迁移批次提交",
            conclusion_remark=None,
            created_at=now_text(),
            updated_at=now_text(),
        )
        self.repo.save_audit_item(item)
        return item

    def _write_lineage(
        self, task: MigrationTask, seg: Segment, batch: MigrationBatch
    ) -> LineageRecord:
        # 幂等：同任务同分段同批次只允许一份血缘
        if self.repo.lineage_exists(task.task_id, seg.seq, batch.batch_id):
            for rec in self.repo.list_lineage(task.task_id):
                if rec.segment_seq == seg.seq and rec.batch_id == batch.batch_id:
                    return rec
        record = LineageRecord(
            lineage_id=self.repo.next_id("LIN"),
            task_id=task.task_id,
            batch_id=batch.batch_id,
            segment_seq=seg.seq,
            source_table=task.source_table,
            target_table=task.target_table,
            transform_type="分段批量迁移",
            field_mapping=dict(task.target_table_mapping),
            created_at=now_text(),
        )
        self.repo.save_lineage(record)
        return record

    def _handle_lineage_conflict(
        self,
        task: MigrationTask,
        seg: Segment,
        batch: MigrationBatch,
        result: CheckResult,
    ) -> dict[str, Any]:
        audit = self.repo.find_audit_by_result(result.result_id)
        if task.conflict_policy == CONFLICT_KEEP:
            # 保留已校验待补血缘分段，等待下一轮从该边界续做
            seg.status = SEGMENT_VERIFIED_WAITING_LINEAGE
            if audit is not None:
                audit.status = AUDIT_PENDING
                audit.conclusion_remark = "血缘写入失败，分段已校验待补血缘，下一轮从该分段续做"
                self.repo.save_audit_item(audit)
            result.conclusions.append({
                "entry": "迁移引擎",
                "action": "血缘写入失败-保留分段",
                "remark": "保留已校验待补血缘状态，续做位置不前进",
                "at": now_text(),
            })
            self.repo.save_check_result(result)
            return {
                "seq": seg.seq,
                "status": SEGMENT_VERIFIED_WAITING_LINEAGE,
                "idempotent": False,
                "conflict": True,
                "message": "校验成功但血缘写入失败：保留为已校验待补血缘，下一轮从该分段续做",
                "check_result_id": result.result_id,
            }

        # 撤回该分段：回滚目标端数据痕迹，只发布前一段；校验结果保留但标注撤回
        seg.status = SEGMENT_ROLLED_BACK
        seg.target_row_count = None
        seg.target_distinct_key = None
        seg.target_amount_sum = None
        seg.lineage_record_id = None
        result.conclusions.append({
            "entry": "迁移引擎",
            "action": "血缘写入失败-撤回分段",
            "remark": "撤回该分段目标数据，只发布前一段，下一轮从该分段重新续做",
            "at": now_text(),
        })
        self.repo.save_check_result(result)
        if audit is not None:
            audit.status = AUDIT_REJECTED
            audit.conclusion_remark = "血缘写入失败，分段已撤回，只发布前一段"
            self.repo.save_audit_item(audit)
        return {
            "seq": seg.seq,
            "status": SEGMENT_ROLLED_BACK,
            "idempotent": False,
            "conflict": True,
            "message": "校验成功但血缘写入失败：已撤回该分段，只发布前一段",
            "check_result_id": result.result_id,
        }

    # ------------------------------------------------------------------
    # 续做：从第一个不完整边界开始（待补血缘 / 已撤回 / 待迁移 都命中）
    # ------------------------------------------------------------------
    def resume(
        self, task_id: str, *, mode: str = MODE_BATCH, limit: int | None = None
    ) -> tuple[MigrationBatch | None, str]:
        task = self.repo.get_task(task_id)
        if task is None:
            return None, f"迁移任务 {task_id} 不存在"
        next_seq = task.next_resume_seq()
        if next_seq > task.segment_count:
            return None, "所有分段均已发布，无可续做边界"
        seqs = [s.seq for s in task.segments if s.seq >= next_seq]
        if limit:
            seqs = seqs[: max(1, int(limit))]
        return self.submit_batch(task_id, segment_seqs=seqs, mode=mode)

    def _refresh_task_status(self, task: MigrationTask) -> None:
        statuses = {s.status for s in task.segments}
        if task.progress()["completed"]:
            task.status = TASK_DONE
        elif TASK_FAILED in statuses:
            task.status = TASK_FAILED
        elif SEGMENT_VERIFIED_WAITING_LINEAGE in statuses:
            task.status = TASK_PAUSED
        elif SEGMENT_PUBLISHED in statuses or "迁移中" in statuses:
            task.status = TASK_RUNNING
        else:
            task.status = TASK_DRAFT

    def clear_faults(
        self,
        task_id: str,
        *,
        lineage_seqs: list[int] | None = None,
        check_seqs: list[int] | None = None,
        clear_all: bool = False,
    ) -> tuple[MigrationTask | None, str]:
        """模拟下游服务恢复：解除指定分段的血缘/校验故障注入。

        - clear_all=True：清除该任务全部故障注入；
        - 否则只处理显式传入的故障类型，未传的类型保持原状，避免顺手清掉另一类故障；
          传空列表表示该类型一个都不解，传具体序号则解这些分段。
        """
        task = self.repo.get_task(task_id)
        if task is None:
            return None, f"迁移任务 {task_id} 不存在"
        if clear_all:
            task.lineage_fail_seqs = []
            task.check_fail_seqs = []
        else:
            if lineage_seqs is not None:
                cleared = set(int(s) for s in lineage_seqs)
                task.lineage_fail_seqs = [s for s in task.lineage_fail_seqs if s not in cleared]
            if check_seqs is not None:
                cleared = set(int(s) for s in check_seqs)
                task.check_fail_seqs = [s for s in task.check_fail_seqs if s not in cleared]
        self.repo.save_task(task)
        return task, "故障注入已解除，下一轮可从当前边界续做"

    # ------------------------------------------------------------------
    # 审计处理结论回写（审计清单入口 / 其它业务入口共用同一份校验结果）
    # ------------------------------------------------------------------
    def write_audit_conclusion(
        self,
        check_result_id: str,
        *,
        conclusion: str,
        remark: str | None,
        source_entry: str,
    ) -> tuple[AuditItem | None, str]:
        result = self.repo.get_check_result(check_result_id)
        if result is None:
            return None, f"校验结果 {check_result_id} 不存在"
        if conclusion not in VALID_CONCLUSIONS:
            return None, f"处理结论仅支持：{'、'.join(sorted(VALID_CONCLUSIONS))}"

        audit = self.repo.find_audit_by_result(check_result_id)
        if audit is None:
            # 其它业务入口先拿到校验结果、审计清单尚未建档时补建，保证两边始终同一份
            task = self.repo.get_task(result.task_id)
            seg = self.repo.get_segment(task, result.segment_seq) if task else None
            if task is None or seg is None:
                return None, "校验结果对应的迁移任务或分段缺失，无法回写审计清单"
            audit = self._ensure_audit_item(task, seg, result)

        audit.status = conclusion
        audit.conclusion_remark = remark
        audit.source_entry = source_entry
        self.repo.save_audit_item(audit)

        # 同步在唯一一份校验结果上留痕，两个入口看到的批次结论一致
        result.conclusions.append({
            "entry": source_entry,
            "action": conclusion,
            "remark": remark or "",
            "at": now_text(),
        })
        self.repo.save_check_result(result)
        return audit, "处理结论已回写迁移审计清单"

    # ------------------------------------------------------------------
    # 查询口径
    # ------------------------------------------------------------------
    def list_tasks(self) -> list[MigrationTask]:
        return self.repo.list_tasks()

    def get_task(self, task_id: str) -> MigrationTask | None:
        return self.repo.get_task(task_id)

    def list_check_results(self, task_id: str | None = None) -> list[CheckResult]:
        return self.repo.list_check_results(task_id)

    def get_check_result(self, result_id: str) -> CheckResult | None:
        return self.repo.get_check_result(result_id)

    def list_audit_items(
        self, *, task_id: str | None = None, status: str | None = None
    ) -> list[AuditItem]:
        return self.repo.list_audit_items(task_id=task_id, status=status)

    def list_lineage(self, task_id: str | None = None) -> list[LineageRecord]:
        return self.repo.list_lineage(task_id)


engine = MigrationEngine()
