"""实体链接领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class EntityLinkService(BaseModuleService):
    MODULE = "entity_link"
    ENTITY = "链接规则"
    OPERATION_TERM = "归并"
    LIST_FIELDS = ["规则编号", "源实体", "目标实体", "链接类型", "置信度", "链接证据", "人工确认", "链接时间"]
    REQUIRED_FIELDS = ["规则编号", "源实体", "目标实体"]
    STATUS_ORDER = ["待确认", "已链接", "已驳回", "需复核"]
    ACTION_RULES = {"确认链接": "已链接", "驳回归并": "已驳回", "提交复核": "需复核"}
    NEGATIVE_ACTIONS = ["驳回归并"]

    # ===== 跨模块数据流实现 =====
    def run_action(
        self, entry_id: int, action: str, remark: str | None = None, **kwargs: Any
    ) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action, remark=remark)
        if entry is None:
            return entry, message
        # 链路 L7: entity_link → disambig（确认链接后更新消岐融合状态）
        if entry.get("status") in ("已链接", "需复核"):
            disambig_msg = self._push_to_disambig(entry)
            message += "；" + disambig_msg
        return entry, message

    def _push_to_disambig(self, entry: dict[str, Any]) -> str:
        rows = store.rows("disambig")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        dis_entry: dict[str, Any] = {
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "配置编号": f"DISA-LINK-{next_id:04d}",
            "歧义类型": f"link_resolve_{entry.get('链接类型','')}",
            "候选实体": str(entry.get("目标实体", "")),
            "特征向量": str(entry.get("链接证据", "")),
            "消岐算法": "co-reference-resolution",
            "融合策略": "linked-entity-merge",
            "精度指标": str(entry.get("置信度", "0.7")),
            "最近执行": str(entry.get("链接时间", "2026-11-18")),
        }
        rows.append(dis_entry)
        return f"消岐融合受实体链接影响: {dis_entry['配置编号']}"

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["cross_module_consumers"] = ["disambig"]
        return base