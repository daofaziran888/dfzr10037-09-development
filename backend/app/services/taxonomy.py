"""分类体系领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class TaxonomyService(BaseModuleService):
    MODULE = "taxonomy"
    ENTITY = "分类树"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["分类编号", "分类名称", "父分类", "层级深度", "关联实体", "分类规则", "自动归类", "最后更新"]
    REQUIRED_FIELDS = ["分类编号", "分类名称", "父分类"]
    STATUS_ORDER = ["草稿", "已发布", "重构中", "已合并"]
    ACTION_RULES = {"发布分类": "已发布", "重构层级": "重构中", "合并分类": "已合并"}
    NEGATIVE_ACTIONS = []

    # ===== 跨模块数据流实现 =====
    def run_action(
        self, entry_id: int, action: str, remark: str | None = None, **kwargs: Any
    ) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action, remark=remark)
        if entry is None:
            return entry, message
        # 链路 L20: taxonomy → govern_dash（分类发布/重构后触发治理看板更新）
        if entry.get("status") in ("已发布", "重构中"):
            dash_msg = self._push_to_govern_dash(entry)
            message += "；" + dash_msg
        # 链路 L21: taxonomy → metadata_tag（分类变更可触发重新标注）
        if entry.get("status") in ("重构中", "已合并"):
            tag_msg = self._push_to_metadata_tag(entry)
            message += "；" + tag_msg
        return entry, message

    def _push_to_govern_dash(self, entry: dict[str, Any]) -> str:
        rows = store.rows("govern_dash")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        gd_entry: dict[str, Any] = {
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "指标编号": f"GOVE-TAX-{next_id:04d}",
            "指标名称": f"分类覆盖-{entry.get('分类名称','')}",
            "评估对象": str(entry.get("分类编号", "")),
            "目标值": "0.90",
            "当前值": str(entry.get("层级深度", "0")),
            "趋势方向": "stable",
            "更新频率": "on-classification-change",
            "最后刷新": str(entry.get("最后更新", "2026-11-18")),
        }
        rows.append(gd_entry)
        return f"治理指标同步: {gd_entry['指标编号']}"

    def _push_to_metadata_tag(self, entry: dict[str, Any]) -> str:
        rows = store.rows("metadata_tag")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        mt_entry: dict[str, Any] = {
            "id": next_id,
            "status": "待分配",
            "pending": True,
            "abnormal": False,
            "任务编号": f"META-TAX-{next_id:04d}",
            "标注对象": str(entry.get("关联实体", "")),
            "标注类型": "re-classify",
            "标注人": "system-auto",
            "标注批次": f"batch-{next_id}",
            "内容标签": str(entry.get("分类名称", "")),
            "审核状态": "pending",
            "标注时间": str(entry.get("最后更新", "2026-11-18")),
        }
        rows.append(mt_entry)
        return f"已发起重新标注任务 {mt_entry['任务编号']}"

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["cross_module_consumers"] = ["govern_dash", "metadata_tag"]
        return base