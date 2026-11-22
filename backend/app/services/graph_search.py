"""图谱搜索领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class GraphSearchService(BaseModuleService):
    MODULE = "graph_search"
    ENTITY = "搜索模板"
    OPERATION_TERM = "查询"
    LIST_FIELDS = ["模板编号", "搜索类型", "查询语法", "起始实体", "关系过滤", "跳数上限", "排序规则", "使用频次"]
    REQUIRED_FIELDS = ["模板编号", "搜索类型", "查询语法"]
    STATUS_ORDER = ["草稿", "已发布", "性能告警", "已停用"]
    ACTION_RULES = {"发布模板": "已发布", "标记告警": "性能告警", "停用模板": "已停用"}
    NEGATIVE_ACTIONS = ["停用模板"]

    # ===== 跨模块数据流实现 =====
    def run_action(
        self, entry_id: int, action: str, remark: str | None = None, **kwargs: Any
    ) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action, remark=remark)
        if entry is None:
            return entry, message
        # 链路 L5: graph_search → query_cache（发布搜索模板后写入查询缓存）
        if entry.get("status") == "已发布":
            cache_msg = self._push_to_query_cache(entry)
            message += "；" + cache_msg
        # 链路 L6: graph_search → subgraph_exp（已发布模板触发子图探查入口）
        if entry.get("status") == "已发布":
            subgraph_msg = self._push_to_subgraph_exp(entry)
            message += "；" + subgraph_msg
        return entry, message

    def _push_to_query_cache(self, entry: dict[str, Any]) -> str:
        cache_rows = store.rows("query_cache")
        next_id = max((int(r.get("id", 0)) for r in cache_rows), default=0) + 1
        qc_entry: dict[str, Any] = {
            "id": next_id,
            "status": "有效",
            "pending": True,
            "abnormal": False,
            "条目编号": f"QUER-CACHE-{next_id:04d}",
            "查询模板": str(entry.get("查询语法", "")),
            "结果哈希": f"hash_{entry.get('模板编号','')}",
            "命中次数": "0",
            "TTL秒数": "3600",
            "存储大小": f"{len(str(entry))}B",
            "最后命中": "",
            "缓存状态": "warm",
        }
        cache_rows.append(qc_entry)
        return f"已写入查询缓存 {qc_entry['条目编号']}"

    def _push_to_subgraph_exp(self, entry: dict[str, Any]) -> str:
        sub_rows = store.rows("subgraph_exp")
        next_id = max((int(r.get("id", 0)) for r in sub_rows), default=0) + 1
        sb_entry: dict[str, Any] = {
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "配置编号": f"SUBG-SRCH-{next_id:04d}",
            "探查种子": str(entry.get("起始实体", "")),
            "扩展策略": "bfs-traversal",
            "关系过滤": str(entry.get("关系过滤", "")),
            "深度限制": str(entry.get("跳数上限", "3")),
            "结果数量": 15,
            "缓存策略": "auto-warm",
            "上次探查": "2026-11-18",
        }
        sub_rows.append(sb_entry)
        return f"已生成子图探查配置 {sb_entry['配置编号']}"

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["cross_module_consumers"] = ["query_cache", "subgraph_exp"]
        return base