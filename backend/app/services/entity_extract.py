"""实体抽取领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class EntityExtractService(BaseModuleService):
    MODULE = "entity_extract"
    ENTITY = "实体定义"
    OPERATION_TERM = "入库"
    LIST_FIELDS = ["实体编号", "实体类型", "数据来源", "抽取规则", "同义词典", "消岐策略", "置信阈值", "最后抽取"]
    REQUIRED_FIELDS = ["实体编号", "实体类型", "数据来源"]
    STATUS_ORDER = ["草稿", "抽取中", "已入库", "低置信"]
    ACTION_RULES = {"启动抽取": "抽取中", "确认入库": "已入库", "标记低置信": "低置信"}
    NEGATIVE_ACTIONS = []

    # ===== 跨模块数据流实现 =====
    def run_action(
        self, entry_id: int, action: str, remark: str | None = None, **kwargs: Any
    ) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action, remark=remark)
        if entry is None:
            return entry, message
        # 链路 L1: entity_extract → relation_model（实体入库后自动创建关系建模记录）
        if entry.get("status") == "已入库":
            rel_msg = self._push_to_relation_model(entry)
            message += "；" + rel_msg
        # 链路 L2: entity_extract → entity_link（实体入库后预创建链接候选）
        if entry.get("status") == "已入库":
            link_msg = self._push_to_entity_link(entry)
            message += "；" + link_msg
        return entry, message

    def _push_to_relation_model(self, entry: dict[str, Any]) -> str:
        relation_rows = store.rows("relation_model")
        next_id = max((int(r.get("id", 0)) for r in relation_rows), default=0) + 1
        rel_entry: dict[str, Any] = {
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "关系编号": f"RELA-AUTO-{next_id:04d}",
            "关系名": f"auto_derive_{entry.get('实体类型', 'unknown')}",
            "主体类型": str(entry.get("实体类型", "")),
            "客体类型": str(entry.get("数据来源", "")),
            "关系权重": f"from_entity_{entry.get('实体编号','')}",
            "推导规则": str(entry.get("抽取规则", "")),
            "反关系": f"inv_{next_id}",
            "使用次数": "0",
        }
        relation_rows.append(rel_entry)
        return f"已向关系建模推送自动关系 {rel_entry['关系编号']}"

    def _push_to_entity_link(self, entry: dict[str, Any]) -> str:
        link_rows = store.rows("entity_link")
        next_id = max((int(r.get("id", 0)) for r in link_rows), default=0) + 1
        link_entry: dict[str, Any] = {
            "id": next_id,
            "status": "待确认",
            "pending": True,
            "abnormal": False,
            "规则编号": f"ENTI-LINK-{next_id:04d}",
            "源实体": str(entry.get("实体编号", "")),
            "目标实体": str(entry.get("实体类型", "")),
            "链接类型": "自动发现",
            "置信度": str(entry.get("置信阈值", "0.6")),
            "链接证据": str(entry.get("消岐策略", "exact-match")),
            "人工确认": "待确认",
            "链接时间": str(entry.get("最后抽取", "2026-11-18")),
        }
        link_rows.append(link_entry)
        return f"已向实体链接推送候选 {link_entry['规则编号']}"

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["cross_module_consumers"] = ["relation_model", "entity_link"]
        return base