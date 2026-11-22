"""治理看板领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class GovernDashService(BaseModuleService):
    MODULE = "govern_dash"
    ENTITY = "治理指标"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["指标编号", "指标名称", "评估对象", "目标值", "当前值", "趋势方向", "更新频率", "最后刷新"]
    REQUIRED_FIELDS = ["指标编号", "指标名称", "评估对象"]
    STATUS_ORDER = ["草稿", "监控中", "偏离基线", "已达标"]
    ACTION_RULES = {"启动监控": "监控中", "确认达标": "已达标", "标记偏离": "偏离基线"}
    NEGATIVE_ACTIONS = []

    # ===== 跨模块数据流实现 =====
    def run_action(
        self, entry_id: int, action: str, remark: str | None = None, **kwargs: Any
    ) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action, remark=remark)
        if entry is None:
            return entry, message
        # 链路 L22: govern_dash → quality_audit（治理偏离基线时触发质量审计）
        if entry.get("status") == "偏离基线":
            audit_msg = self._push_to_quality_audit(entry)
            message += "；" + audit_msg
        # 链路 L23: govern_dash → schedule_job（监控指标触发定时任务）
        if entry.get("status") == "监控中":
            job_msg = self._push_to_schedule_job(entry)
            message += "；" + job_msg
        return entry, message

    def _push_to_quality_audit(self, entry: dict[str, Any]) -> str:
        rows = store.rows("quality_audit")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        qa_entry: dict[str, Any] = {
            "id": next_id,
            "status": "待审计",
            "pending": True,
            "abnormal": False,
            "报告编号": f"QUAL-GOV-{next_id:04d}",
            "审计范围": f"指标{entry.get('指标名称','')}偏离基线",
            "实体准确率": "re-verify",
            "关系完整率": "re-verify",
            "冗余度": "unknown",
            "更新时效": "立即",
            "合规结论": "待审计",
            "审计时间": str(entry.get("最后刷新", "2026-11-18")),
        }
        rows.append(qa_entry)
        return f"偏离基线已触发质量审计 {qa_entry['报告编号']}"

    def _push_to_schedule_job(self, entry: dict[str, Any]) -> str:
        rows = store.rows("schedule_job")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        sj_entry: dict[str, Any] = {
            "id": next_id,
            "status": "待激活",
            "pending": True,
            "abnormal": False,
            "作业编号": f"SCHE-GOV-{next_id:04d}",
            "任务类型": f"监控刷新-{entry.get('指标名称','')}",
            "关联图谱": str(entry.get("评估对象", "")),
            "Cron表达式": "0 */6 * * *",
            "时区设置": "Asia/Shanghai",
            "依赖检查": "none",
            "失败通知": "slack",
            "下次执行": "2026-11-19 00:00:00",
        }
        rows.append(sj_entry)
        return f"已创建定时监控任务 {sj_entry['作业编号']}"

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["cross_module_consumers"] = ["quality_audit", "schedule_job"]
        return base