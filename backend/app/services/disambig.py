"""消岐融合领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class DisambigService(BaseModuleService):
    MODULE = "disambig"
    ENTITY = "消岐配置"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["配置编号", "歧义类型", "候选实体", "特征向量", "消岐算法", "融合策略", "精度指标", "最近执行"]
    REQUIRED_FIELDS = ["配置编号", "歧义类型", "候选实体"]
    STATUS_ORDER = ["草稿", "消岐中", "已融合", "需人工"]
    ACTION_RULES = {"启动消岐": "消岐中", "确认融合": "已融合", "标记需人工": "需人工"}
    NEGATIVE_ACTIONS = []

    # ===== 跨模块数据流实现 =====
    def run_action(
        self, entry_id: int, action: str, remark: str | None = None, **kwargs: Any
    ) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action, remark=remark)
        if entry is None:
            return entry, message
        # 链路 L8: disambig → path_reason（消岐融合后触发路径推理）
        if entry.get("status") == "已融合":
            reason_msg = self._push_to_path_reason(entry)
            message += "；" + reason_msg
        # 链路 L9: disambig → reco_engine（融合结果推送推荐引擎）
        if entry.get("status") == "已融合":
            reco_msg = self._push_to_reco_engine(entry)
            message += "；" + reco_msg
        return entry, message

    def _push_to_path_reason(self, entry: dict[str, Any]) -> str:
        rows = store.rows("path_reason")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        pr_entry: dict[str, Any] = {
            "id": next_id,
            "status": "待推理",
            "pending": True,
            "abnormal": False,
            "任务编号": f"PATH-DISA-{next_id:04d}",
            "起始实体": str(entry.get("候选实体", "")),
            "目标实体": f"fused_{entry.get('歧义类型','')}",
            "推理模式": "path-deduction",
            "最大跳数": "4",
            "路径数量": 12,
            "置信阈值": str(entry.get("精度指标", "0.8")),
            "推理耗时": "",
        }
        rows.append(pr_entry)
        return f"已触路径推理任务 {pr_entry['任务编号']}"

    def _push_to_reco_engine(self, entry: dict[str, Any]) -> str:
        rows = store.rows("reco_engine")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        reco_entry: dict[str, Any] = {
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "规则编号": f"RECO-DISA-{next_id:04d}",
            "推荐场景": f"融合实体关联推荐",
            "源实体": str(entry.get("候选实体", "")),
            "推荐算法": str(entry.get("消岐算法", "simrank")),
            "候选数量": 20,
            "过滤条件": str(entry.get("融合策略", "")),
            "命中次数": "0",
            "采纳率": "0%",
        }
        rows.append(reco_entry)
        return f"已推送推荐引擎规则 {reco_entry['规则编号']}"

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["cross_module_consumers"] = ["path_reason", "reco_engine"]
        return base