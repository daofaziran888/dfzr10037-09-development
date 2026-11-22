"""回滚差异快照端到端场景验证（服务层直跑，不依赖外部 HTTP 客户端）。

场景：
1. 复核者甲在回滚前领取正向 A→B 比对，证据保留起止版本与方向；
2. 图谱演化执行回滚，计算回滚差异快照，同步反向 B→A 待复核；
3. 同一次回滚重试不重复发出整改项；
4. 复核者乙在回滚后领取反向待办，读取快照与当前图版本；
5. 复核者甲重复领取被阻止；乙提交结论并回写演化记录，指针指向两个原版本；
6. 被撤销版本不再作为当前修订图，两份方向相反的证据均可回查。
"""
from __future__ import annotations

from app.services.graph_compare import GraphCompareService
from app.services.graph_evolve import SNAPSHOT_MODULE, GraphEvolveService
from app.store import store

evolve_svc = GraphEvolveService()
compare_svc = GraphCompareService()

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {label}" + (f" -> {detail}" if detail else ""))
    if not condition:
        failures.append(label)


# ---------- 0. 登记一条新演化记录：A=graph-v2.0 → B=graph-v2.1 ----------
entry, missing = evolve_svc.create_entry({
    "记录编号": "GRAP-E2E-01",
    "变更类型": "关系新增",
    "影响实体": "订单.订单号",
    "影响关系": "表-字段归属关系",
    "版本标签": "graph-v2.1",
    "变更描述": "新增订单号归属边",
    "回滚版本": "graph-v2.0",
    "演化时间": "2026-11-22",
})
assert entry is not None and not missing
eid = int(entry["id"])
print(f"\n== 登记演化记录 id={eid} {entry['记录编号']} ==")

# ---------- 1. 提交评审：复核者甲回滚前领取正向 A→B ----------
entry, msg = evolve_svc.run_action(eid, "提交评审")
print(msg)
forward_no = entry["正向比对编号"]
forward = next(r for r in store.rows("graph_compare") if r["任务编号"] == forward_no)
check("正向比对方向为 A→B", forward["比对方向"] == "正向 A→B")
check("正向证据起止版本", forward["基准图"] == "graph-v2.0" and forward["对比图"] == "graph-v2.1",
      f"{forward['基准图']}→{forward['对比图']}")
check("正向比对无回滚快照", forward["回滚快照编号"] == "")
fid = int(forward["id"])
_, msg = compare_svc.run_action(fid, "启动比对", reviewer="复核者甲")
print(msg)
forward = store.find("graph_compare", fid)
check("复核者甲可持有回滚前正向证据", forward["status"] == "比对中" and forward["复核人"] == "复核者甲")

# 提交评审重试：不重复制造正向比对
_, msg = evolve_svc.run_action(eid, "提交评审")
forward_count = sum(1 for r in store.rows("graph_compare") if r["任务编号"] == forward_no)
check("评审重试不重复发正向比对", forward_count == 1, msg)

# ---------- 2. 执行回滚：快照 + 版本切换 + 反向待复核 ----------
entry, msg = evolve_svc.run_action(eid, "执行回滚", remark="归属边误挂，人工撤销")
print(msg)
check("演化状态=已回滚", entry["status"] == "已回滚")
check("被撤销版本 B 状态=已撤销", entry["版本状态"] == "已撤销")
check("当前修订图切回 A", entry["当前修订图"] == "graph-v2.0", entry["当前修订图"])
snapshot_no = entry["回滚快照编号"]
snapshot = next(r for r in store.rows(SNAPSHOT_MODULE) if r["快照编号"] == snapshot_no)
check("快照固定两个可读原版本",
      snapshot["被撤销版本"] == "graph-v2.1" and snapshot["恢复版本"] == "graph-v2.0",
      snapshot["可读原版本"])
check("快照记录反向 B→A", snapshot["回滚方向"] == "反向 B→A" and snapshot["原比对方向"] == "正向 A→B")
reverse_no = entry["反向比对编号"]
reverse = next(r for r in store.rows("graph_compare") if r["任务编号"] == reverse_no)
check("反向比对进入待复核清单", reverse["status"] == "待复核" and reverse["比对维度"] == "rollback-diff")
check("反向比对方向 B→A", reverse["基准图"] == "graph-v2.1" and reverse["对比图"] == "graph-v2.0")
check("反向待办关联快照编号", reverse["回滚快照编号"] == snapshot_no)

# ---------- 3. 回滚重试幂等：不重复发整改项 ----------
compare_before = len(store.rows("graph_compare"))
snapshot_before = len(store.rows(SNAPSHOT_MODULE))
entry, msg = evolve_svc.run_action(eid, "执行回滚")
print(msg)
compare_after = len(store.rows("graph_compare"))
snapshot_after = len(store.rows(SNAPSHOT_MODULE))
check("回滚重试不新增比对整改项", compare_before == compare_after, f"{compare_before} -> {compare_after}")
check("回滚重试不重复计算快照", snapshot_before == snapshot_after)
check("反向比对编号保持不变", entry["反向比对编号"] == reverse_no)

# ---------- 4. 待复核清单 + 复核者乙回滚后领取 ----------
queue = compare_svc.pending_review_queue()
queue_nos = {item["任务编号"] for item in queue}
check("待复核清单包含回滚待办", reverse_no in queue_nos and "GRAP-ROLLBACK-CMP-SEED" in queue_nos,
      f"共 {len(queue)} 条")
rid = int(reverse["id"])
read_snapshot = compare_svc.read_rollback_snapshot(rid)
check("待办可读取回滚差异快照", read_snapshot is not None and read_snapshot["快照编号"] == snapshot_no)
_, msg = compare_svc.run_action(rid, "领取复核", reviewer="复核者乙")
print(msg)
reverse = store.find("graph_compare", rid)
check("复核者乙领取成功", reverse["status"] == "复核中" and reverse["复核人"] == "复核者乙")
check("领取时看到当前修订图为 A", "当前修订图=graph-v2.0" in msg)

# 非回滚比对不能领取复核
_, claim_msg = compare_svc.run_action(fid, "领取复核", reviewer="复核者乙")
check("普通比对禁止领取复核", claim_msg.startswith("仅回滚差异比对"), claim_msg)

# ---------- 5. 复核者甲不能重复领取；乙提交结论并回写 ----------
_, claim_msg = compare_svc.run_action(rid, "领取复核", reviewer="复核者甲")
check("他人不可重复领取", claim_msg.startswith("该待办已由"), claim_msg)
# 原复核人可重复打开
_, claim_msg = compare_svc.run_action(rid, "领取复核", reviewer="复核者乙")
check("原复核人可重新打开待办", "已加载回滚差异快照" in claim_msg)

# 结论为空被拒绝
_, bad_msg = compare_svc.run_action(rid, "提交复核结论", reviewer="复核者乙", remark="   ")
check("空结论被拒绝", bad_msg == "复核结论不能为空")
# 未领取不能提交
fresh_reverse = next(r for r in store.rows("graph_compare") if r["任务编号"] == "GRAP-ROLLBACK-CMP-SEED")
_, bad_msg = compare_svc.run_action(int(fresh_reverse["id"]), "提交复核结论", reviewer="复核者乙", remark="x")
check("未领取不能提交结论", bad_msg == "请先领取复核再提交结论")

_, msg = compare_svc.run_action(
    rid, "提交复核结论", reviewer="复核者乙",
    remark="撤销原因成立：归属边误挂字段；新整改项按 B→A 差异撤回该边并补挂正确字段",
)
print(msg)
reverse = store.find("graph_compare", rid)
check("反向比对已复核", reverse["status"] == "已复核" and reverse["pending"] is False)
evolve_back = store.find("graph_evolve", eid)
check("结论回写演化记录",
      evolve_back.get("复核结论编号") == reverse_no
      and reverse.get("回写演化记录") == "GRAP-E2E-01")
check("回写指针指向两个原版本",
      evolve_back.get("回写基准版本") == "graph-v2.1"
      and evolve_back.get("回写对比版本") == "graph-v2.0"
      and evolve_back.get("回写快照编号") == snapshot_no,
      f"{evolve_back.get('回写基准版本')} / {evolve_back.get('回写对比版本')}")

# ---------- 6. 版本生命周期 + 双向证据可回查 ----------
revisions = evolve_svc.current_revision("表-字段归属关系")
mine = next(r for r in revisions if r["记录编号"] == "GRAP-E2E-01")
check("被撤销版本不是当前修订图", mine["当前修订图"] == "graph-v2.0" and mine["版本状态"] == "已撤销")
forward = store.find("graph_compare", fid)
check("正向证据方向与起止版本保持可回查",
      forward["比对方向"] == "正向 A→B" and forward["基准图"] == "graph-v2.0"
      and forward["对比图"] == "graph-v2.1")
check("反向证据方向与起止版本保持可回查",
      reverse["比对方向"] == "反向 B→A" and reverse["基准图"] == "graph-v2.1"
      and reverse["对比图"] == "graph-v2.0")
check("快照在复核完成后仍可读取", compare_svc.read_rollback_snapshot(rid)["快照编号"] == snapshot_no)

# ---------- 7. 种子数据场景：另一组已存在的回滚可直接走复核 ----------
seed_rev = next(r for r in store.rows("graph_compare") if r["任务编号"] == "GRAP-ROLLBACK-CMP-SEED")
sid = int(seed_rev["id"])
seed_snap = compare_svc.read_rollback_snapshot(sid)
check("种子回滚待办快照可读", seed_snap["快照编号"] == "SNAP-RB-SEED-0001"
      and seed_snap["可读原版本"] == "graph-v1.2 | graph-v1.3")
_, msg = compare_svc.run_action(sid, "领取复核", reviewer="复核者丙")
print(msg)
check("种子待办可被第三位复核者领取", "graph-v1.3→graph-v1.2" in msg and "当前修订图=graph-v1.2" in msg)

print("\n" + ("全部断言通过 ✅" if not failures else f"存在失败项：{failures} ❌"))
raise SystemExit(1 if failures else 0)
