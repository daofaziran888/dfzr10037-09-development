"""关系建模领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class RelationModelService(BaseModuleService):
    MODULE = "relation_model"
    ENTITY = "关系类型"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["关系编号", "关系名", "主体类型", "客体类型", "关系权重", "推导规则", "反关系", "使用次数"]
    REQUIRED_FIELDS = ["关系编号", "关系名", "主体类型"]
    STATUS_ORDER = ["草稿", "已定义", "已弃用", "冲突待解"]
    ACTION_RULES = {"确认定义": "已定义", "弃用关系": "已弃用", "标记冲突": "冲突待解"}
    NEGATIVE_ACTIONS = []

    # ===== 跨模块数据流实现 =====
    def run_action(
        self, entry_id: int, action: str, remark: str | None = None, **kwargs: Any
    ) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action, remark=remark)
        if entry is None:
            return entry, message
        # 链路 L1: relation_model → graph_build（关系确认后触发图谱构建）
        if entry.get("status") == "已定义":
            build_msg = self._push_to_graph_build(entry)
            message += "；" + build_msg
        # 链路 L2: relation_model → disambig（冲突待解时通知消岐融合）
        if entry.get("status") == "冲突待解":
            disambig_msg = self._push_to_disambig(entry)
            message += "；" + disambig_msg
        return entry, message

    def _push_to_graph_build(self, entry: dict[str, Any]) -> str:
        build_rows = store.rows("graph_build")
        next_id = max((int(r.get("id", 0)) for r in build_rows), default=0) + 1
        build_entry: dict[str, Any] = {
            "id": next_id,
            "status": "待执行",
            "pending": True,
            "abnormal": False,
            "任务编号": f"GRAP-AUTO-{next_id:04d}",
            "数据范围": str(entry.get("客体类型", "")),
            "图类型": f"{entry.get('主体类型','')}-{entry.get('客体类型','')}-graph",
            "实体数量": 15,
            "关系数量": 8,
            "构建模式": "automatic",
            "增量标记": "true",
            "构建时间": "2026-11-18",
        }
        build_rows.append(build_entry)
        return f"已触图谱构建任务 {build_entry['任务编号']}"

    def _push_to_disambig(self, entry: dict[str, Any]) -> str:
        rows = store.rows("disambig")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        dis_entry: dict[str, Any] = {
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "配置编号": f"DISA-CONF-{next_id:04d}",
            "歧义类型": f"relation_conflict_{entry.get('关系名','')}",
            "候选实体": str(entry.get("主体类型", "")),
            "特征向量": str(entry.get("推导规则", "")),
            "消岐算法": "semantic-similarity",
            "融合策略": "auto-merge",
            "精度指标": "0.85",
            "最近执行": "2026-11-18",
        }
        rows.append(dis_entry)
        return f"已通知消岐融合配置 {dis_entry['配置编号']}"

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["cross_module_consumers"] = ["graph_build", "disambig"]
        return base