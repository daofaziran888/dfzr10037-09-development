"""图谱比对领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。

支持两组方向相反的图版本比对共存：

- 回滚前已领取的正向 A→B 比对（evolution-diff）：证据保留起止版本与方向；
- 回滚后同步进来的反向 B→A 比对（rollback-diff）：进入待复核清单，
  每个待办通过「回滚快照编号」读取回滚差异快照，复核确认后把结论
  回写图谱演化，且回写指针指向快照中两个仍可读取的原版本。

状态生命周期：待执行 → 比对中 → 已出报告（普通比对）；
回滚比对另有：待复核 → 复核中 → 已复核。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.graph_evolve import SNAPSHOT_MODULE
from app.store import store


class GraphCompareService(BaseModuleService):
    MODULE = "graph_compare"
    ENTITY = "比对任务"
    OPERATION_TERM = "校验"
    LIST_FIELDS = [
        "任务编号", "基准图", "对比图", "比对方向", "比对维度", "新增实体",
        "删除实体", "变更关系", "影响关系", "来源演化记录", "回滚快照编号",
        "回滚批次", "复核人", "复核结论", "回写演化记录", "比对时间",
    ]
    REQUIRED_FIELDS = ["任务编号", "基准图", "对比图"]
    STATUS_ORDER = ["待执行", "比对中", "已出报告", "比对失败", "待复核", "复核中", "已复核"]
    ACTION_RULES = {
        "启动比对": "比对中",
        "确认报告": "已出报告",
        "标记失败": "比对失败",
        "领取复核": "复核中",
        "提交复核结论": "已复核",
    }
    NEGATIVE_ACTIONS = []

    # ===== 跨模块数据流实现 =====
    def run_action(
        self,
        entry_id: int,
        action: str,
        remark: str | None = None,
        reviewer: str | None = None,
        **kwargs: Any,
    ) -> tuple[dict[str, Any] | None, str]:
        if action == "领取复核":
            return self._claim_review(entry_id, reviewer or remark)
        if action == "提交复核结论":
            return self._submit_conclusion(entry_id, reviewer, remark)

        entry, message = super().run_action(entry_id, action, remark)
        if entry is None:
            return entry, message
        # 普通比对也记录领取/操作人，保证回滚前正向证据可追溯到复核者
        if reviewer and reviewer.strip():
            entry["复核人"] = reviewer.strip()
            message += f"；操作人 {reviewer.strip()} 已登记"
        # 链路 L14/L15：普通比对出报告后触发报表与质量审计（回滚复核不走该链路）
        if entry.get("status") == "已出报告" and entry.get("比对维度") != "rollback-diff":
            message += "；" + self._push_to_report_gen(entry)
            message += "；" + self._push_to_quality_audit(entry)
        return entry, message

    # ===== 待复核清单：回滚同步进来的反向 B→A 待办 =====
    def pending_review_queue(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in store.rows(self.MODULE)
            if row.get("比对维度") == "rollback-diff" and row.get("status") in ("待复核", "复核中")
        ]

    def read_rollback_snapshot(self, entry_id: int) -> dict[str, Any] | None:
        """待办读取回滚差异快照；非回滚比对没有快照。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or not entry.get("回滚快照编号"):
            return None
        return next(
            (
                dict(row)
                for row in store.rows(SNAPSHOT_MODULE)
                if row.get("快照编号") == entry["回滚快照编号"]
            ),
            None,
        )

    def _claim_review(
        self, entry_id: int, reviewer: str | None
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if entry.get("比对维度") != "rollback-diff":
            return None, "仅回滚差异比对（rollback-diff）可领取复核"
        if entry.get("status") not in ("待复核", "复核中"):
            return None, f"比对任务当前状态为「{entry.get('status')}」，不在待复核清单中"
        reviewer_name = (reviewer or "").strip() or "未署名复核人"
        if entry.get("status") == "复核中":
            # 同一待办可被原复核人重复打开查看，他人不可重复领取
            if entry.get("复核人") and entry["复核人"] != reviewer_name:
                return None, f"该待办已由 {entry['复核人']} 领取，不能重复领取"
        else:
            entry["status"] = "复核中"
        entry["复核人"] = reviewer_name
        snapshot = self.read_rollback_snapshot(entry_id)
        if snapshot is None:
            return None, "回滚差异快照缺失或已不可读，无法领取复核"
        message = (
            f"复核人 {reviewer_name} 已领取 {entry['任务编号']}，"
            f"已加载回滚差异快照 {snapshot['快照编号']}："
            f"{snapshot['被撤销版本']}→{snapshot['恢复版本']}（{snapshot['回滚方向']}），"
            f"当前修订图={snapshot['当前修订图']}"
        )
        return entry, message

    def _submit_conclusion(
        self,
        entry_id: int,
        reviewer: str | None,
        conclusion: str | None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if entry.get("比对维度") != "rollback-diff":
            return None, "仅回滚差异比对可提交复核结论"
        if entry.get("status") != "复核中":
            return None, "请先领取复核再提交结论"
        text = (conclusion or "").strip()
        if not text:
            return None, "复核结论不能为空"
        reviewer_name = (reviewer or entry.get("复核人") or "").strip() or "未署名复核人"
        entry["status"] = "已复核"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["复核人"] = reviewer_name
        entry["复核结论"] = text

        # 确认结果回写图谱演化，指向快照里两个仍可读取的原版本
        snapshot = self.read_rollback_snapshot(entry_id)
        record_no = str(entry.get("来源演化记录", ""))
        from app.services.graph_evolve import GraphEvolveService

        target = GraphEvolveService().writeback_compare_result(
            record_no=record_no,
            compare_no=str(entry.get("任务编号", "")),
            reviewer=reviewer_name,
            conclusion=text,
            snapshot=snapshot,
        )
        entry["回写演化记录"] = record_no
        if target is None:
            return entry, (
                f"复核结论已保存，但演化记录 {record_no} 不存在，回写未命中；"
                f"两个原版本仍可在快照 {entry.get('回滚快照编号','')} 中读取"
            )
        versions = (
            f"{snapshot['被撤销版本']} / {snapshot['恢复版本']}"
            if snapshot else "快照缺失"
        )
        return entry, (
            f"复核结论已提交并回写演化记录 {record_no}，原版本指针（被撤销/恢复）：{versions}"
        )

    # ===== 普通比对的跨模块链路 =====
    def _push_to_report_gen(self, entry: dict[str, Any]) -> str:
        rows = store.rows("report_gen")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rp_entry: dict[str, Any] = {
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "报告编号": f"REPO-CMP-{next_id:04d}",
            "报告类型": "graph-compare-report",
            "数据范围": f"{entry.get('基准图','')}-vs-{entry.get('对比图','')}",
            "图表配置": "diff-table,chord-diagram",
            "输出格式": "html",
            "生成周期": "on-compare-done",
            "分发列表": "",
            "最近生成": str(entry.get("比对时间", "2026-11-18")),
        }
        rows.append(rp_entry)
        return f"已生成比对报表 {rp_entry['报告编号']}"

    def _push_to_quality_audit(self, entry: dict[str, Any]) -> str:
        rows = store.rows("quality_audit")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        qa_entry: dict[str, Any] = {
            "id": next_id,
            "status": "待审计",
            "pending": True,
            "abnormal": False,
            "报告编号": f"QUAL-CMP-{next_id:04d}",
            "审计范围": f"graph-compare-{entry.get('任务编号','')}",
            "实体准确率": "from-compare-delta",
            "关系完整率": "from-compare-delta",
            "冗余度": "estimated-low",
            "更新时效": "实时",
            "合规结论": "待审计",
            "审计时间": str(entry.get("比对时间", "2026-11-18")),
        }
        rows.append(qa_entry)
        return f"已触发质量审计 {qa_entry['报告编号']}"

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["cross_module_consumers"] = ["report_gen", "quality_audit"]
        base["pending_review"] = len(self.pending_review_queue())
        return base
