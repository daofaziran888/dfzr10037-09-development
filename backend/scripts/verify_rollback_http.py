"""HTTP 冒烟测试：经真实路由验证回滚差异快照全流程（标准库 urllib，无新依赖）。"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8011/api"


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    url_path, _, query = path.partition("?")
    if query:
        pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
        query = "?" + urllib.parse.urlencode(pairs)
    req = urllib.request.Request(
        BASE + url_path + query, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" -> {detail}" if detail else ""))
    if not condition:
        failures.append(label)


# 1. 待复核清单（种子里已有 1 条反向待办）
status, body = call("GET", "/rollback/review-queue")
check("GET 待复核清单", status == 200 and body["total"] >= 1, f"{body.get('total')} 条")
seed_task = next(i for i in body["items"] if i["任务编号"] == "GRAP-ROLLBACK-CMP-SEED")

# 2. 直接读快照
status, body = call("GET", f"/rollback/snapshots/SNAP-RB-SEED-0001")
check("GET 回滚差异快照", status == 200 and body["被撤销版本"] == "graph-v1.3",
      body.get("可读原版本", ""))
status, body = call("GET", "/rollback/snapshots/NOPE")
check("快照不存在返回 404", status == 404)

# 3. 待办读取自己的快照
status, body = call("GET", f"/rollback/compare/{seed_task['id']}/snapshot")
check("待办关联快照可读", status == 200 and body["快照编号"] == "SNAP-RB-SEED-0001")

# 4. 当前图版本
status, body = call("GET", "/rollback/current-revision")
rolled = next(i for i in body["items"] if i["记录编号"] == "GRAP-0003")
check("当前修订图接口", rolled["当前修订图"] == "graph-v1.2" and rolled["版本状态"] == "已撤销")

# 5. 登记新演化 → 评审 → 回滚（HTTP 动作接口带 remark/reviewer）
status, body = call("POST", "/graph_evolve", {"values": {
    "记录编号": "GRAP-HTTP-01", "变更类型": "关系新增", "影响实体": "订单.订单号",
    "影响关系": "表-字段归属关系", "版本标签": "graph-v3.1", "变更描述": "HTTP 流程",
    "回滚版本": "graph-v3.0", "演化时间": "2026-11-22"}})
eid = body["entry"]["id"]
status, body = call("POST", f"/graph_evolve/{eid}/actions", {"action": "提交评审"})
check("HTTP 提交评审", body["ok"] and body["entry"]["正向比对编号"])
forward_no = body["entry"]["正向比对编号"]
status, body = call("POST", "/graph_evolve", {})  # 缺必填字段
check("缺必填字段被拒绝", body["ok"] is False)

status, body = call("POST", f"/graph_evolve/{eid}/actions",
                    {"action": "执行回滚", "remark": "HTTP 撤销原因"})
check("HTTP 执行回滚", body["ok"] and "回滚差异快照" in body["message"], body["message"])
ev = body["entry"]
check("回滚后当前修订图=A", ev["当前修订图"] == "graph-v3.0" and ev["版本状态"] == "已撤销")

# 回滚重试
status, body2 = call("POST", f"/graph_evolve/{eid}/actions", {"action": "执行回滚"})
check("HTTP 回滚重试幂等", body2["ok"] and "不重复发出整改项" in body2["message"])

# 6. 列表/筛选仍工作，且新字段出现在清单
status, body = call("GET", "/graph_evolve?影响关系=表-字段归属关系")
check("演化清单筛选", body["total"] >= 1 and all("回滚快照编号" in r for r in body["items"]))
status, body = call("GET", "/graph_compare?比对维度=rollback-diff")
check("比对清单按维度筛选", body["total"] >= 2)

# 7. 两位复核者场景
status, queue = call("GET", "/rollback/review-queue")
task = next(i for i in queue["items"] if i["任务编号"] == ev["反向比对编号"])
tid = task["id"]
# 甲先在正向任务上操作（回滚前证据）
status, fwd = call("GET", "/graph_compare")
fwd_task = next(i for i in fwd["items"] if i["任务编号"] == forward_no)
status, body = call("POST", f"/graph_compare/{fwd_task['id']}/actions",
                    {"action": "启动比对", "reviewer": "复核者甲"})
check("HTTP 复核者甲持有正向 A→B",
      body["entry"]["复核人"] == "复核者甲" and body["entry"]["比对方向"] == "正向 A→B")
# 乙领取反向
status, body = call("POST", f"/graph_compare/{tid}/actions",
                    {"action": "领取复核", "reviewer": "复核者乙"})
check("HTTP 复核者乙领取反向 B→A", body["ok"] and "已加载回滚差异快照" in body["message"], body["message"])
# 甲重复领取被拒
status, body = call("POST", f"/graph_compare/{tid}/actions",
                    {"action": "领取复核", "reviewer": "复核者甲"})
check("HTTP 重复领取被拒", body["ok"] is False, body["message"])
# 乙提交结论
status, body = call("POST", f"/graph_compare/{tid}/actions",
                    {"action": "提交复核结论", "reviewer": "复核者乙", "remark": "HTTP 复核通过"})
check("HTTP 提交结论并回写", body["ok"] and "回写演化记录" in body["message"], body["message"])
# 回写后查演化记录
status, body = call("GET", f"/graph_evolve/{eid}")
check("HTTP 回写指针含两个原版本",
      body["回写基准版本"] == "graph-v3.1" and body["回写对比版本"] == "graph-v3.0"
      and body["复核结论编号"] == ev["反向比对编号"])

# 8. 未知动作仍被拒
status, body = call("POST", f"/graph_evolve/{eid}/actions", {"action": "乱搞"})
check("非法动作被拒", body["ok"] is False)

print("\n" + ("HTTP 冒烟全部通过 ✅" if not failures else f"失败：{failures} ❌"))
raise SystemExit(1 if failures else 0)
