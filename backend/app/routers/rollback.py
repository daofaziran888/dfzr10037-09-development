"""回滚复核路由：把图谱演化的回滚事实暴露给图谱比对复核场景。

- GET /api/rollback/review-queue            图谱比对待复核清单（反向 B→A 待办）
- GET /api/rollback/snapshots/{snapshot_id} 读取回滚差异快照
- GET /api/rollback/current-revision        查询图版本当前修订图与版本生命周期
- GET /api/rollback/compare/{entry_id}/snapshot  某个比对待办读取自己的快照
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.services.graph_compare import GraphCompareService
from app.services.graph_evolve import SNAPSHOT_MODULE, GraphEvolveService
from app.store import store

router = APIRouter(prefix="/api/rollback", tags=["回滚复核"])


@router.get("/review-queue")
def review_queue() -> dict[str, Any]:
    """演化回滚结果同步到图谱比对后的待复核清单。"""
    service = GraphCompareService()
    items = service.pending_review_queue()
    return {"items": items, "total": len(items)}


@router.get("/snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: str) -> dict[str, Any]:
    """按快照编号读取回滚差异快照，两个原版本始终可读。"""
    snapshot = next(
        (row for row in store.rows(SNAPSHOT_MODULE) if row.get("快照编号") == snapshot_id),
        None,
    )
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"回滚差异快照 {snapshot_id} 不存在或已归档")
    return dict(snapshot)


@router.get("/compare/{entry_id}/snapshot")
def get_compare_snapshot(entry_id: int) -> dict[str, Any]:
    """复核待办读取自己关联的回滚差异快照。"""
    snapshot = GraphCompareService().read_rollback_snapshot(entry_id)
    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail=f"比对任务 {entry_id} 未关联可读的回滚差异快照",
        )
    return snapshot


@router.get("/current-revision")
def current_revision(relation: str | None = Query(default=None)) -> dict[str, Any]:
    """图版本生命周期查询：被撤销版本不再作为当前修订图。"""
    items = GraphEvolveService().current_revision(relation)
    return {"items": items, "total": len(items)}
