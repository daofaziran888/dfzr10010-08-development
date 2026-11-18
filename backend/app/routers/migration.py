"""分段迁移接口：任务与数据范围读取、批次提交、整组校验、逐条执行与续做边界。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.migration_engine.engine import (
    VALID_MODES,
    engine,
)
from app.migration_engine.models import MODE_BATCH

router = APIRouter(prefix="/api/migration/tasks", tags=["分段迁移任务"])


class BatchPayload(BaseModel):
    segment_seqs: list[int] = Field(default_factory=list, description="本次提交的分段序号，可多选")
    mode: str = Field(default=MODE_BATCH, description="批量提交 / 整组校验 / 逐条执行")
    batch_id: str | None = Field(default=None, description="携带原批次号重复提交时按幂等回放")


class ResumePayload(BaseModel):
    mode: str = MODE_BATCH
    limit: int | None = Field(default=None, description="本轮最多续做几个分段")


class FaultClearPayload(BaseModel):
    lineage_seqs: list[int] | None = Field(default=None, description="要解除血缘故障的分段，缺省不动该类故障")
    check_seqs: list[int] | None = Field(default=None, description="要解除校验故障的分段，缺省不动该类故障")
    clear_all: bool = Field(default=False, description="为 true 时清除该任务全部故障注入")


class TaskPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


@router.get("")
def list_tasks() -> dict[str, Any]:
    """迁移任务列表（含进度与下一次续做位置，不含明细分段以控制载荷）。"""
    items = [task.to_dict(with_segments=False) for task in engine.list_tasks()]
    return {"items": items, "total": len(items)}


@router.post("")
def create_task(payload: TaskPayload) -> dict[str, Any]:
    """登记迁移任务并生成数据范围分段；缺字段时返回可读原因。"""
    task, errors = engine.create_task(payload.values)
    if errors:
        return {"ok": False, "message": "；".join(errors)}
    return {"ok": True, "message": f"迁移任务 {task.task_code} 已登记并生成 {task.segment_count} 个分段", "entry": task.to_dict()}


@router.get("/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    """读取迁移任务、全量分段与历史批次（数据范围、校验规则、边界状态都在这里）。"""
    task = engine.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"迁移任务 {task_id} 不存在")
    return task.to_dict()


@router.get("/{task_id}/segments")
def list_segments(
    task_id: str,
    status: str | None = Query(default=None, description="按分段状态过滤"),
) -> dict[str, Any]:
    """逐段查看源目标行数、去重数、金额汇总与校验结果编号。"""
    task = engine.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"迁移任务 {task_id} 不存在")
    items = [s.to_dict() for s in task.segments if status is None or s.status == status]
    return {
        "items": items,
        "total": len(items),
        "progress": task.progress(),
        "next_resume_seq": task.next_resume_seq(),
    }


@router.post("/{task_id}/batches")
def submit_batch(task_id: str, payload: BatchPayload) -> dict[str, Any]:
    """一次提交多个分段，分别保留每段结果；重复批次号按幂等回放不追加记录。"""
    if payload.mode not in VALID_MODES:
        raise HTTPException(status_code=400, detail=f"执行模式仅支持：{'、'.join(sorted(VALID_MODES))}")
    batch, message = engine.submit_batch(
        task_id,
        segment_seqs=payload.segment_seqs,
        mode=payload.mode,
        batch_id=payload.batch_id,
    )
    if batch is None:
        return {"ok": False, "message": message}
    task = engine.get_task(task_id)
    return {
        "ok": True,
        "message": message,
        "batch": batch.to_dict(),
        "progress": task.progress(),
        "next_resume_seq": task.next_resume_seq(),
        "task_status": task.status,
    }


@router.post("/{task_id}/resume")
def resume_task(task_id: str, payload: ResumePayload) -> dict[str, Any]:
    """从下一个完整边界续做：始终定位到第一个未发布分段，避免从错误位置继续。"""
    batch, message = engine.resume(task_id, mode=payload.mode, limit=payload.limit)
    if batch is None:
        return {"ok": False, "message": message}
    task = engine.get_task(task_id)
    return {
        "ok": True,
        "message": message,
        "batch": batch.to_dict(),
        "progress": task.progress(),
        "next_resume_seq": task.next_resume_seq(),
        "task_status": task.status,
    }


@router.post("/{task_id}/faults/clear")
def clear_faults(task_id: str, payload: FaultClearPayload) -> dict[str, Any]:
    """模拟下游服务恢复：解除血缘/校验故障注入，使下一轮续做可以跨过当前边界。"""
    task, message = engine.clear_faults(
        task_id,
        lineage_seqs=payload.lineage_seqs,
        check_seqs=payload.check_seqs,
        clear_all=payload.clear_all,
    )
    if task is None:
        return {"ok": False, "message": message}
    return {"ok": True, "message": message, "next_resume_seq": task.next_resume_seq()}


@router.get("/{task_id}/lineage")
def list_task_lineage(task_id: str) -> dict[str, Any]:
    """查看与分段同批次写入的血缘记录。"""
    task = engine.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"迁移任务 {task_id} 不存在")
    items = [r.to_dict() for r in engine.list_lineage(task_id)]
    return {"items": items, "total": len(items)}
