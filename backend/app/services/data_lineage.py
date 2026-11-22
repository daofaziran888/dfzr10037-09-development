"""数据血缘领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class DataLineageService(BaseModuleService):
    MODULE = "data_lineage"
    ENTITY = "血缘链路"
    OPERATION_TERM = "转换"
    LIST_FIELDS = ["链路编号", "上游实体", "下游实体", "转换描述", "关联字段", "影响层级", "链路类型", "最后更新"]
    REQUIRED_FIELDS = ["链路编号", "上游实体", "下游实体"]
    STATUS_ORDER = ["自动推导", "已确认", "待验证", "已废弃"]
    ACTION_RULES = {"确认链路": "已确认", "提交验证": "待验证", "废弃链路": "已废弃"}
    NEGATIVE_ACTIONS = []

    # ===== 跨模块数据流实现 =====
    def run_action(
        self, entry_id: int, action: str, remark: str | None = None, **kwargs: Any
    ) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action, remark=remark)
        if entry is None:
            return entry, message
        # 链路 L12: data_lineage → graph_evolve（血缘变更记录触发演化记录）
        if entry.get("status") in ("已确认", "待验证"):
            evolve_msg = self._push_to_graph_evolve(entry)
            message += "；" + evolve_msg
        # 链路 L13: data_lineage → graph_compare（血缘链路触发图谱版本比对）
        if entry.get("status") in ("已确认", "待验证"):
            compare_msg = self._push_to_graph_compare(entry)
            message += "；" + compare_msg
        return entry, message

    def _push_to_graph_evolve(self, entry: dict[str, Any]) -> str:
        rows = store.rows("graph_evolve")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        ev_entry: dict[str, Any] = {
            "id": next_id,
            "status": "已记录",
            "pending": True,
            "abnormal": False,
            "记录编号": f"GRAP-EVOL-{next_id:04d}",
            "变更类型": f"lineage-{entry.get('链路类型','')}",
            "影响实体": str(entry.get("下游实体", "")),
            "影响关系": str(entry.get("关联字段", "")),
            "版本标签": f"v{next_id}.0",
            "变更描述": f"血缘链路{entry.get('链路编号','')}已变更",
            "回滚版本": f"v{next_id-1}.0",
            "演化时间": str(entry.get("最后更新", "2026-11-18")),
        }
        rows.append(ev_entry)
        return f"已记录图谱演化 {ev_entry['记录编号']}"

    def _push_to_graph_compare(self, entry: dict[str, Any]) -> str:
        rows = store.rows("graph_compare")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        cmp_entry: dict[str, Any] = {
            "id": next_id,
            "status": "待执行",
            "pending": True,
            "abnormal": False,
            "任务编号": f"GRAP-CMP-{next_id:04d}",
            "基准图": str(entry.get("上游实体", "")),
            "对比图": str(entry.get("下游实体", "")),
            "比对维度": "lineage-change-diff",
            "新增实体": "auto-calc",
            "删除实体": "auto-calc",
            "变更关系": "auto-calc",
            "比对时间": str(entry.get("最后更新", "2026-11-18")),
        }
        rows.append(cmp_entry)
        return f"已触发图谱比对 {cmp_entry['任务编号']}"

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["cross_module_consumers"] = ["graph_evolve", "graph_compare"]
        return base