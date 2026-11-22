"""图谱演化领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。

本模块承载「图版本回滚 → 回滚差异快照 → 图谱比对待复核」的完整流程：

1. 提交评审（A 版本 → B 版本）：把正向 A→B 比对任务推送到 graph_compare，
   比对证据带起止版本、比对方向，供回滚前领取复盘。
2. 执行回滚（撤销 B，恢复 A）：
   - 计算并持久化「回滚差异快照」（rollback_snapshot 表），把回滚事实
     交给图谱比对处理图版本切换；
   - 回滚状态与图版本生命周期对应：被撤销版本 B 标记为「已撤销」，
     不能再作为当前修订图，当前修订图切回 A；
   - 同一次回滚（同一回滚批次）重试不重复发出整改项：已经生成过反向
     B→A 比对任务时直接复用。
3. 图谱比对确认结论后，由 graph_compare 服务回写本模块，且回写指针
   始终指向快照里两个仍可读取的原版本 A、B。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 回滚差异快照表名（store 按 key 动态建表，无需迁移）
SNAPSHOT_MODULE = "rollback_snapshot"
# 回滚事实推送到图谱比对时使用的比对维度
ROLLBACK_DIMENSION = "rollback-diff"
EVOLUTION_DIMENSION = "evolution-diff"


class GraphEvolveService(BaseModuleService):
    MODULE = "graph_evolve"
    ENTITY = "演化记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = [
        "记录编号", "变更类型", "影响实体", "影响关系", "版本标签", "变更描述",
        "回滚版本", "当前修订图", "版本状态", "回滚批次", "正向比对编号",
        "反向比对编号", "回滚快照编号", "复核结论编号", "演化时间",
    ]
    REQUIRED_FIELDS = ["记录编号", "变更类型", "影响实体"]
    STATUS_ORDER = ["已记录", "已评审", "已回滚", "已确认"]
    ACTION_RULES = {"提交评审": "已评审", "执行回滚": "已回滚", "确认演化": "已确认"}
    NEGATIVE_ACTIONS = []

    # ===== 动作入口：状态流转只允许发生在服务层 =====
    def run_action(
        self,
        entry_id: int,
        action: str,
        remark: str | None = None,
        **kwargs: Any,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if self.ACTION_RULES.get(action) is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        if action == "提交评审":
            return self._submit_for_review(entry, remark)
        if action == "执行回滚":
            return self._execute_rollback(entry, remark)
        # 确认演化走通用流转
        entry, message = super().run_action(entry_id, action, remark)
        if entry is None:
            return entry, message
        if entry.get("status") == "已确认":
            message += "；" + self._push_to_quality_audit(entry)
        return entry, message

    # ===== 提交评审：A → B，正向比对证据带版本区间与方向 =====
    def _submit_for_review(
        self, entry: dict[str, Any], remark: str | None
    ) -> tuple[dict[str, Any] | None, str]:
        entry["status"] = "已评审"
        entry["pending"] = True
        entry["abnormal"] = False

        # 图版本生命周期：评审中的新版本 B 成为候选当前修订图
        version_b = str(entry.get("版本标签") or "curr-version")
        version_a = str(entry.get("回滚版本") or "prev-version")
        entry.setdefault("当前修订图", version_b)
        entry["版本状态"] = "生效中"
        if remark and remark.strip():
            entry["操作备注"] = remark.strip()

        messages = [f"{self.ENTITY}已提交评审"]
        # 每条演化记录只发一次正向比对，重试不重复制造证据
        if not entry.get("正向比对编号"):
            cmp_msg = self._push_forward_compare(entry, version_a, version_b)
            messages.append(cmp_msg)
        return entry, "；".join(messages)

    def _push_forward_compare(
        self, entry: dict[str, Any], version_a: str, version_b: str
    ) -> str:
        rows = store.rows("graph_compare")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        cmp_entry: dict[str, Any] = {
            "id": next_id,
            "status": "待执行",
            "pending": True,
            "abnormal": False,
            "任务编号": f"GRAP-EVOL-CMP-{next_id:04d}",
            # 正向：基准图 A → 对比图 B
            "基准图": version_a,
            "对比图": version_b,
            "比对方向": "正向 A→B",
            "比对维度": EVOLUTION_DIMENSION,
            "新增实体": "auto-detected",
            "删除实体": "auto-detected",
            "变更关系": str(entry.get("变更类型", "")),
            "来源演化记录": str(entry.get("记录编号", "")),
            "影响关系": str(entry.get("影响关系", "")),
            "回滚快照编号": "",
            "复核人": "",
            "复核结论": "",
            "回写演化记录": "",
            "比对时间": str(entry.get("演化时间", "2026-11-18")),
        }
        rows.append(cmp_entry)
        entry["正向比对编号"] = cmp_entry["任务编号"]
        return f"已触发正向版本比对 {cmp_entry['任务编号']}（{version_a}→{version_b}）"

    # ===== 执行回滚：计算快照、切换版本、同步反向待复核（幂等） =====
    def _execute_rollback(
        self, entry: dict[str, Any], remark: str | None
    ) -> tuple[dict[str, Any] | None, str]:
        version_b = str(entry.get("版本标签") or "curr-version")
        version_a = str(entry.get("回滚版本") or "prev-version")

        # 幂等：同一次回滚（同一回滚批次）重试，不重复计算快照、不重复发整改项
        retry = bool(entry.get("回滚批次"))
        if retry:
            return entry, (
                f"{self.ENTITY}已处于已回滚状态，回滚批次 {entry['回滚批次']} 已存在，"
                f"复用回滚差异快照 {entry.get('回滚快照编号','')} 与反向比对 "
                f"{entry.get('反向比对编号','')}，不重复发出整改项"
            )

        # 1) 回滚状态与图版本生命周期对应：B 撤销，A 恢复为当前修订图
        entry["status"] = "已回滚"
        entry["pending"] = True
        entry["abnormal"] = False
        entry["版本状态"] = "已撤销"          # 被撤销版本 B 不能继续作为当前修订图
        entry["当前修订图"] = version_a       # 当前修订图切回 A
        if remark and remark.strip():
            entry["操作备注"] = remark.strip()

        # 2) 计算回滚差异快照（回滚事实交给图谱比对处理图版本切换）
        snapshot = self._build_rollback_snapshot(entry, version_a, version_b, remark)
        entry["回滚快照编号"] = snapshot["快照编号"]
        entry["回滚批次"] = snapshot["回滚批次"]

        # 3) 同步到图谱比对待复核清单：反向 B → A 整改项
        cmp_msg = self._push_reverse_compare(entry, snapshot)

        message = (
            f"{self.ENTITY}已执行回滚：图版本 {version_b} 已撤销，当前修订图恢复为 "
            f"{version_a}；已计算回滚差异快照 {snapshot['快照编号']}；{cmp_msg}"
        )
        return entry, message

    def _build_rollback_snapshot(
        self,
        entry: dict[str, Any],
        version_a: str,
        version_b: str,
        remark: str | None,
    ) -> dict[str, Any]:
        """计算回滚差异快照：固定回滚事实，两个原版本始终保留、可读、可回查。"""
        rows = store.rows(SNAPSHOT_MODULE)
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        batch_no = f"RB-{entry.get('记录编号', next_id)}-{next_id:04d}"
        snapshot_id = f"SNAP-RB-{next_id:04d}"
        # 回滚方向与正向变化相反：正向 A→B 曾新增的内容，回滚 B→A 即为删除
        snapshot: dict[str, Any] = {
            "id": next_id,
            "快照编号": snapshot_id,
            "回滚批次": batch_no,
            "来源演化记录": str(entry.get("记录编号", "")),
            "影响关系": str(entry.get("影响关系", "")),
            # 两个原版本：被撤销版本 + 恢复版本，均不删除、保持可读
            "被撤销版本": version_b,
            "恢复版本": version_a,
            "当前修订图": version_a,
            "回滚方向": "反向 B→A",
            "原比对方向": "正向 A→B",
            "变更类型": str(entry.get("变更类型", "")),
            # 差异事实：回滚把 B 相对 A 的变化整体反向冲销
            "回滚新增": str(entry.get("删除实体", "")) or "0 项（B 相对 A 的删除被恢复）",
            "回滚删除": str(entry.get("新增实体", "")) or "B 相对 A 的新增整体撤回",
            "回滚变更关系": str(entry.get("变更类型", "")),
            "撤销原因": (remark if isinstance(remark, str) else "")
            or str(entry.get("变更描述", "")) or "人工执行图版本回滚",
            "快照时间": str(entry.get("演化时间", "2026-11-18")),
            "可读原版本": f"{version_a} | {version_b}",
        }
        rows.append(snapshot)
        return snapshot

    def _push_reverse_compare(
        self, entry: dict[str, Any], snapshot: dict[str, Any]
    ) -> str:
        """把回滚事实同步到图谱比对的待复核清单（反向 B→A，待办读取快照）。"""
        rows = store.rows("graph_compare")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        cmp_entry: dict[str, Any] = {
            "id": next_id,
            "status": "待复核",
            "pending": True,
            "abnormal": False,
            "任务编号": f"GRAP-ROLLBACK-CMP-{next_id:04d}",
            # 反向：基准图 B → 对比图 A（与回滚前已领取的 A→B 方向相反）
            "基准图": snapshot["被撤销版本"],
            "对比图": snapshot["恢复版本"],
            "比对方向": "反向 B→A",
            "比对维度": ROLLBACK_DIMENSION,
            "新增实体": str(snapshot["回滚新增"]),
            "删除实体": str(snapshot["回滚删除"]),
            "变更关系": str(snapshot["回滚变更关系"]),
            "来源演化记录": str(entry.get("记录编号", "")),
            "影响关系": str(snapshot["影响关系"]),
            "回滚快照编号": snapshot["快照编号"],
            "回滚批次": snapshot["回滚批次"],
            "复核人": "",
            "复核结论": "",
            "回写演化记录": "",
            "比对时间": str(snapshot["快照时间"]),
        }
        rows.append(cmp_entry)
        entry["反向比对编号"] = cmp_entry["任务编号"]
        return (
            f"已同步反向待复核比对 {cmp_entry['任务编号']}"
            f"（{snapshot['被撤销版本']}→{snapshot['恢复版本']}，待复核清单可读快照 "
            f"{snapshot['快照编号']}）"
        )

    # ===== 图谱比对确认后回写：指针指向两个可读原版本 =====
    def writeback_compare_result(
        self,
        record_no: str,
        compare_no: str,
        reviewer: str,
        conclusion: str,
        snapshot: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        target = next(
            (row for row in store.rows(self.MODULE) if row.get("记录编号") == record_no),
            None,
        )
        if target is None:
            return None
        target["复核结论编号"] = compare_no
        target["复核人"] = reviewer
        target["复核结论"] = conclusion
        if snapshot is not None:
            # 回写时显式指向快照里两个仍可读取的原版本
            target["回写基准版本"] = str(snapshot.get("被撤销版本", ""))
            target["回写对比版本"] = str(snapshot.get("恢复版本", ""))
            target["回写快照编号"] = str(snapshot.get("快照编号", ""))
        return target

    def current_revision(self, relation: str | None = None) -> list[dict[str, Any]]:
        """查询图版本当前修订图；可按影响关系过滤，供复核人核对版本切换结果。"""
        rows = store.rows(self.MODULE)
        result = [
            {
                "记录编号": row.get("记录编号", ""),
                "影响关系": row.get("影响关系", ""),
                "版本标签": row.get("版本标签", ""),
                "回滚版本": row.get("回滚版本", ""),
                "当前修订图": row.get("当前修订图", ""),
                "版本状态": row.get("版本状态", ""),
                "状态": row.get("status", ""),
                "回滚批次": row.get("回滚批次", ""),
            }
            for row in rows
            if relation is None or relation.casefold() in str(row.get("影响关系", "")).casefold()
        ]
        return result

    # ===== 跨模块数据流实现 =====
    def _push_to_quality_audit(self, entry: dict[str, Any]) -> str:
        rows = store.rows("quality_audit")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        qa_entry: dict[str, Any] = {
            "id": next_id,
            "status": "待审计",
            "pending": True,
            "abnormal": False,
            "报告编号": f"QUAL-EVOL-{next_id:04d}",
            "审计范围": f"graph_evolution_{entry.get('版本标签','')}",
            "实体准确率": "needs-verification",
            "关系完整率": "needs-verification",
            "冗余度": "post-evolve-check",
            "更新时效": str(entry.get("演化时间", "2026-11-18")),
            "合规结论": "待审计",
            "审计时间": "2026-11-18",
        }
        rows.append(qa_entry)
        return f"已触质量审计 {qa_entry['报告编号']}"

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["cross_module_consumers"] = ["quality_audit", "graph_compare", SNAPSHOT_MODULE]
        base["rollback_snapshots"] = len(store.rows(SNAPSHOT_MODULE))
        return base
