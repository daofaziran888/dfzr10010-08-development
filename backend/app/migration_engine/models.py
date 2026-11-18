"""分段迁移域模型与内存仓库。

设计要点（对应需求里的可恢复提交边界）：

1. 一个迁移任务切成若干分段（segment），每段有确定的数据范围与校验规则。
2. 每段数据迁移、源目标一致性校验、血缘写入必须落在同一个逻辑迁移批次里：
   只有三步全部成功，分段才进入「已发布」，形成一个完整提交边界；
   血缘写入失败时按任务的冲突策略，要么保留为「已校验待补血缘」，要么撤回该段、
   只发布前一段——下一轮始终只能从一个完整边界续做。
3. 批次提交幂等：同一 (任务, 分段, 批次) 重复提交不会追加第二份分段记录或血缘。
4. 源目标一致性校验结果只生成一份，审计清单与其它业务入口都引用同一份结果，
   处理结论统一回写到迁移审计清单。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 枚举口径（前后端共用同一套中文状态词）
# ---------------------------------------------------------------------------

# 分段状态：只有「已发布」是完整提交边界，恢复续做时从第一个未发布分段开始
SEGMENT_PENDING = "待迁移"
SEGMENT_MIGRATING = "迁移中"
SEGMENT_VERIFIED_WAITING_LINEAGE = "已校验待补血缘"
SEGMENT_ROLLED_BACK = "已撤回"
SEGMENT_PUBLISHED = "已发布"

# 任务整体状态
TASK_DRAFT = "待执行"
TASK_RUNNING = "迁移中"
TASK_PAUSED = "待补血缘"
TASK_DONE = "已完成"
TASK_FAILED = "校验失败"

# 审计条目状态
AUDIT_PENDING = "待审计"
AUDIT_PASSED = "审计通过"
AUDIT_REJECTED = "审计驳回"

# 血缘写入失败与分段校验成功冲突时的两种处置策略
CONFLICT_KEEP = "保留待补血缘"
CONFLICT_ROLLBACK = "撤回分段只发布前一段"

# 批次执行模式
MODE_BATCH = "批量提交"
MODE_GROUP = "整组校验"
MODE_SINGLE = "逐条执行"


@dataclass
class Segment:
    """一个迁移分段：数据范围、源目标统计与提交边界状态。"""

    seq: int
    name: str
    range_start: int
    range_end: int
    rows_per_segment: int
    partition_start: str
    partition_end: str
    # 源端口径（登记任务时确定，作为校验基准）
    source_row_count: int
    source_distinct_key: int
    source_amount_sum: float
    # 目标端口径（批次提交时回填）
    target_row_count: int | None = None
    target_distinct_key: int | None = None
    target_amount_sum: float | None = None
    status: str = SEGMENT_PENDING
    batch_id: str | None = None
    check_result_id: str | None = None
    lineage_record_id: str | None = None
    updated_at: str | None = None
    attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "name": self.name,
            "range_start": self.range_start,
            "range_end": self.range_end,
            "rows_per_segment": self.rows_per_segment,
            "partition_start": self.partition_start,
            "partition_end": self.partition_end,
            "source_row_count": self.source_row_count,
            "source_distinct_key": self.source_distinct_key,
            "source_amount_sum": self.source_amount_sum,
            "target_row_count": self.target_row_count,
            "target_distinct_key": self.target_distinct_key,
            "target_amount_sum": self.target_amount_sum,
            "status": self.status,
            "batch_id": self.batch_id,
            "check_result_id": self.check_result_id,
            "lineage_record_id": self.lineage_record_id,
            "updated_at": self.updated_at,
            "attempts": self.attempts,
        }


@dataclass
class CheckResult:
    """源目标一致性校验结果——一份结果可被审计清单和其它业务入口共同引用。"""

    result_id: str
    task_id: str
    task_code: str
    segment_seq: int
    segment_name: str
    batch_id: str
    rule_desc: str
    source_row_count: int
    target_row_count: int
    source_distinct_key: int
    target_distinct_key: int
    source_amount_sum: float
    target_amount_sum: float
    row_count_match: bool
    distinct_match: bool
    amount_match: bool
    passed: bool
    conclusion: str
    created_at: str
    # 回写结论的入口记录（审计清单 / 其它业务入口都写这里，保证同一口径）
    conclusions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "result_id": self.result_id,
            "task_id": self.task_id,
            "task_code": self.task_code,
            "segment_seq": self.segment_seq,
            "segment_name": self.segment_name,
            "batch_id": self.batch_id,
            "rule_desc": self.rule_desc,
            "source_row_count": self.source_row_count,
            "target_distinct_key": self.target_distinct_key,
            "target_row_count": self.target_row_count,
            "source_distinct_key": self.source_distinct_key,
            "source_amount_sum": self.source_amount_sum,
            "target_amount_sum": self.target_amount_sum,
            "row_count_match": self.row_count_match,
            "distinct_match": self.distinct_match,
            "amount_match": self.amount_match,
            "passed": self.passed,
            "conclusion": self.conclusion,
            "created_at": self.created_at,
            "conclusions": list(self.conclusions),
        }


@dataclass
class MigrationBatch:
    """逻辑迁移批次：同段数据迁移、校验、血缘三类记录必须同属一个批次。"""

    batch_id: str
    task_id: str
    task_code: str
    mode: str
    segment_seqs: list[int]
    submitted_segment_seqs: list[int]
    conflict_policy: str
    created_at: str
    # 每个分段的处理结果摘要（seq -> 结果说明），多选分段分别保留
    segment_outcomes: dict[int, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "task_id": self.task_id,
            "task_code": self.task_code,
            "mode": self.mode,
            "segment_seqs": list(self.segment_seqs),
            "submitted_segment_seqs": list(self.submitted_segment_seqs),
            "conflict_policy": self.conflict_policy,
            "created_at": self.created_at,
            "segment_outcomes": {str(k): v for k, v in self.segment_outcomes.items()},
        }


@dataclass
class AuditItem:
    """迁移审计清单条目：基于校验结果生成，处理结论回写本清单。"""

    audit_id: str
    task_id: str
    task_code: str
    segment_seq: int
    segment_name: str
    batch_id: str
    check_result_id: str
    target_table_mapping: dict[str, str]
    check_conclusion: str
    check_passed: bool
    status: str
    source_entry: str
    conclusion_remark: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class LineageRecord:
    """迁移产生的血缘记录（与分段同批次写入）。"""

    lineage_id: str
    task_id: str
    batch_id: str
    segment_seq: int
    source_table: str
    target_table: str
    transform_type: str
    field_mapping: dict[str, str]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class MigrationTask:
    task_id: str
    task_code: str
    source_table: str
    target_table: str
    target_table_mapping: dict[str, str]
    data_scope: str
    partition_start: str
    partition_end: str
    dedup_key: str
    amount_field: str
    check_rules: list[str]
    segment_count: int
    rows_per_segment: int
    conflict_policy: str
    # 故障注入：在指定分段模拟「目标分段校验成功但血缘写入失败」
    lineage_fail_seqs: list[int]
    # 故障注入：在指定分段模拟目标端行数不一致（校验失败）
    check_fail_seqs: list[int]
    status: str
    created_at: str
    updated_at: str
    segments: list[Segment]
    batches: list[MigrationBatch] = field(default_factory=list)

    # ---- 边界与进度口径 -------------------------------------------------
    def published_seqs(self) -> list[int]:
        return [s.seq for s in self.segments if s.status == SEGMENT_PUBLISHED]

    def next_resume_seq(self) -> int:
        """下一次续做位置：第一个未形成完整边界（未发布）的分段序号。

        「已校验待补血缘」「已撤回」「待迁移」都不是完整边界，续做都会命中，
        保证不会从错误位置继续。
        """
        for seg in self.segments:
            if seg.status != SEGMENT_PUBLISHED:
                return seg.seq
        return self.segment_count + 1  # 全部发布完，返回越界位表示无续做点

    def progress(self) -> dict[str, Any]:
        published = len(self.published_seqs())
        waiting = sum(1 for s in self.segments if s.status == SEGMENT_VERIFIED_WAITING_LINEAGE)
        rolled_back = sum(1 for s in self.segments if s.status == SEGMENT_ROLLED_BACK)
        failed = sum(1 for s in self.segments if s.status == TASK_FAILED)
        return {
            "segment_count": self.segment_count,
            "published_count": published,
            "waiting_lineage_count": waiting,
            "rolled_back_count": rolled_back,
            "check_failed_count": failed,
            "percent": round(published * 100 / self.segment_count, 1),
            "next_resume_seq": self.next_resume_seq(),
            "completed": published == self.segment_count,
        }

    def to_dict(self, *, with_segments: bool = True) -> dict[str, Any]:
        data = {
            "task_id": self.task_id,
            "task_code": self.task_code,
            "source_table": self.source_table,
            "target_table": self.target_table,
            "target_table_mapping": self.target_table_mapping,
            "data_scope": self.data_scope,
            "partition_start": self.partition_start,
            "partition_end": self.partition_end,
            "dedup_key": self.dedup_key,
            "amount_field": self.amount_field,
            "check_rules": list(self.check_rules),
            "segment_count": self.segment_count,
            "rows_per_segment": self.rows_per_segment,
            "conflict_policy": self.conflict_policy,
            "lineage_fail_seqs": list(self.lineage_fail_seqs),
            "check_fail_seqs": list(self.check_fail_seqs),
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "progress": self.progress(),
        }
        if with_segments:
            data["segments"] = [s.to_dict() for s in self.segments]
            data["batches"] = [b.to_dict() for b in self.batches]
        return data


class MigrationRepository:
    """迁移引擎专用内存仓库（与 app.store 里的演示模块互不影响）。"""

    def __init__(self) -> None:
        self.tasks: dict[str, MigrationTask] = {}
        self.check_results: dict[str, CheckResult] = {}
        self.audit_items: dict[str, AuditItem] = {}
        self.lineage_records: dict[str, LineageRecord] = {}
        self._seq_by_prefix: dict[str, int] = {}

    def next_id(self, prefix: str) -> str:
        current = self._seq_by_prefix.get(prefix, 0) + 1
        self._seq_by_prefix[prefix] = current
        return f"{prefix}-{current:06d}"

    # -- task -------------------------------------------------------------
    def save_task(self, task: MigrationTask) -> None:
        task.updated_at = now_text()
        self.tasks[task.task_id] = task

    def get_task(self, task_id: str) -> MigrationTask | None:
        return self.tasks.get(task_id)

    def list_tasks(self) -> list[MigrationTask]:
        return sorted(self.tasks.values(), key=lambda t: t.task_id)

    def get_segment(self, task: MigrationTask, seq: int) -> Segment | None:
        for seg in task.segments:
            if seg.seq == seq:
                return seg
        return None

    # -- check result -----------------------------------------------------
    def save_check_result(self, result: CheckResult) -> None:
        self.check_results[result.result_id] = result

    def get_check_result(self, result_id: str) -> CheckResult | None:
        return self.check_results.get(result_id)

    def list_check_results(self, task_id: str | None = None) -> list[CheckResult]:
        rows = list(self.check_results.values())
        if task_id:
            rows = [r for r in rows if r.task_id == task_id]
        return sorted(rows, key=lambda r: (r.task_id, r.segment_seq))

    # -- audit ------------------------------------------------------------
    def save_audit_item(self, item: AuditItem) -> None:
        item.updated_at = now_text()
        self.audit_items[item.audit_id] = item

    def find_audit_by_result(self, check_result_id: str) -> AuditItem | None:
        for item in self.audit_items.values():
            if item.check_result_id == check_result_id:
                return item
        return None

    def list_audit_items(
        self, *, task_id: str | None = None, status: str | None = None
    ) -> list[AuditItem]:
        rows = list(self.audit_items.values())
        if task_id:
            rows = [r for r in rows if r.task_id == task_id]
        if status:
            rows = [r for r in rows if r.status == status]
        return sorted(rows, key=lambda r: (r.task_id, r.segment_seq))

    # -- lineage ----------------------------------------------------------
    def save_lineage(self, record: LineageRecord) -> None:
        self.lineage_records[record.lineage_id] = record

    def lineage_exists(self, task_id: str, segment_seq: int, batch_id: str) -> bool:
        """幂等判定：同任务同分段同批次的血缘只允许一份。"""
        for rec in self.lineage_records.values():
            if (
                rec.task_id == task_id
                and rec.segment_seq == segment_seq
                and rec.batch_id == batch_id
            ):
                return True
        return False

    def list_lineage(self, task_id: str | None = None) -> list[LineageRecord]:
        rows = list(self.lineage_records.values())
        if task_id:
            rows = [r for r in rows if r.task_id == task_id]
        return sorted(rows, key=lambda r: (r.task_id, r.segment_seq))

    # -- batch ------------------------------------------------------------
    def find_batch(self, task: MigrationTask, batch_id: str) -> MigrationBatch | None:
        for batch in task.batches:
            if batch.batch_id == batch_id:
                return batch
        return None


# 单例仓库
repo = MigrationRepository()


def reset_repository(seed_func: Callable[[MigrationRepository], None] | None = None) -> None:
    """测试钩子：清空仓库并可重新播种。"""
    global repo
    repo = MigrationRepository()
    if seed_func is not None:
        seed_func(repo)
