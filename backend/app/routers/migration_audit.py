"""迁移审计清单与校验结果接口。

两份入口（迁移审计清单、其它业务入口）操作同一份源目标一致性校验结果，
处理结论统一回写到迁移审计清单，刷新后两边批次与结论保持一致。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.migration_engine.engine import VALID_CONCLUSIONS, engine

router = APIRouter(prefix="/api/migration", tags=["迁移审计与一致性校验"])


class ConclusionPayload(BaseModel):
    check_result_id: str = Field(..., description="源目标一致性校验结果编号")
    conclusion: str = Field(..., description="审计通过 / 审计驳回")
    remark: str | None = Field(default=None, description="处理说明")
    source_entry: str = Field(default="迁移审计清单", description="回写结论的业务入口")


@router.get("/check-results")
def list_check_results(
    task_id: str | None = Query(default=None, description="按迁移任务过滤"),
) -> dict[str, object]:
    """源目标一致性校验结果清单：全平台只此一份口径，审计与外部入口共用。"""
    items = [r.to_dict() for r in engine.list_check_results(task_id)]
    return {"items": items, "total": len(items)}


@router.get("/check-results/{result_id}")
def get_check_result(result_id: str) -> dict[str, object]:
    """审计详情：查看单份源目标一致性校验结果及其全部回写痕迹。"""
    result = engine.get_check_result(result_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"校验结果 {result_id} 不存在")
    audit = engine.repo.find_audit_by_result(result_id)
    return {
        "check_result": result.to_dict(),
        "audit_item": audit.to_dict() if audit else None,
    }


@router.get("/audits")
def list_audits(
    task_id: str | None = Query(default=None),
    status: str | None = Query(default=None, description="待审计 / 审计通过 / 审计驳回"),
) -> dict[str, object]:
    """迁移审计清单：基于校验结果生成，保存目标表映射与回写结论。"""
    items = [item.to_dict() for item in engine.list_audit_items(task_id=task_id, status=status)]
    return {"items": items, "total": len(items)}


@router.post("/audits/conclusion")
def write_audit_conclusion(payload: ConclusionPayload) -> dict[str, object]:
    """审计清单入口回写处理结论。"""
    if payload.conclusion not in VALID_CONCLUSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"处理结论仅支持：{'、'.join(sorted(VALID_CONCLUSIONS))}",
        )
    audit, message = engine.write_audit_conclusion(
        payload.check_result_id,
        conclusion=payload.conclusion,
        remark=payload.remark,
        source_entry=payload.source_entry or "迁移审计清单",
    )
    if audit is None:
        return {"ok": False, "message": message}
    return {"ok": True, "message": message, "audit_item": audit.to_dict()}


@router.post("/entries/conclusion")
def write_external_conclusion(payload: ConclusionPayload) -> dict[str, object]:
    """其它业务入口：接收校验结果并把校验结论同步到迁移审计清单。

    与审计清单入口走同一个引擎方法、操作同一份校验结果，
    因此任一侧回写后，刷新两侧看到的批次与结论完全相同。
    """
    if payload.conclusion not in VALID_CONCLUSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"处理结论仅支持：{'、'.join(sorted(VALID_CONCLUSIONS))}",
        )
    source_entry = payload.source_entry if payload.source_entry != "迁移审计清单" else "其它业务入口"
    audit, message = engine.write_audit_conclusion(
        payload.check_result_id,
        conclusion=payload.conclusion,
        remark=payload.remark,
        source_entry=source_entry,
    )
    if audit is None:
        return {"ok": False, "message": message}
    return {"ok": True, "message": message, "audit_item": audit.to_dict()}
