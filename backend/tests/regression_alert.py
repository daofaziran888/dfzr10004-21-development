"""告警聚合与恢复回归脚本（ALER-0002 × TASK-0002）。

运行方式（在 backend/ 目录下）：
    .venv/bin/python tests/regression_alert.py

覆盖需求：
1. 按监控指标 + 告警级别 + 采集计划合并相邻事件，记录首次/最近触发、累计次数、静默截止；
2. 相同事件重复上报只并入同一聚合组，不重复计数；静默期不重复通知、不重复升级；
3. 达到阈值后采集计划处置清单生成待办，待办与告警规则清单使用同一聚合结论；
4. 升级名额全局互斥：两个静默批次并发申请只接受一次占用（含多线程压测）；
5. 恢复生成带结论的恢复通知，待办同步退出，历史事件/通知仍可追溯；
6. 人工确认后待办退出，同组再次活跃重新进入清单；
7. 批量处置一次提交、分批返回结果，支持多选、合并、逐条确认、整组恢复与跳过单条。
"""
from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services.incident import (  # noqa: E402
    LEVEL_RULES,
    incident_service,
    reset_incident_tables,
)

client = TestClient(app)

PASSED = 0


def check(title: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if not condition:
        raise AssertionError(f"✗ {title} {detail}")
    PASSED += 1
    print(f"  ✓ {title}{(' — ' + detail) if detail else ''}")


class FakeClock:
    """可控时钟：静默截止判断依赖服务时钟，回归需要人工推进时间。"""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, minutes: int) -> None:
        self.now += timedelta(minutes=minutes)


def post_event(values: dict) -> dict:
    response = client.post("/api/alert/incidents/events", json={"values": values})
    assert response.status_code == 200, response.text
    return response.json()


def main() -> None:
    reset_incident_tables()
    clock = FakeClock(datetime(2026, 11, 13, 10, 0, 0))
    incident_service._clock = clock

    metric = "页面抓取成功率"  # ALER-0002 的监控指标
    level = "重要"             # ALER-0002 的告警级别，静默 15 分钟、P2
    plan = "TASK-0002"

    print("【0】基础数据与规则就位")
    rule = next(r for r in client.get("/api/alert").json()["items"] if r["规则编号"] == "ALER-0002")
    check("ALER-0002 已启用", rule["status"] == "已启用")
    task = next(r for r in client.get("/api/taskplan").json()["items"] if r["计划编号"] == "TASK-0002")
    check("TASK-0002 运行中", task["status"] == "运行中")

    print("【1】首次失败建组并开启静默批次")
    r1 = post_event({"监控指标": metric, "告警级别": level, "采集计划": plan,
                     "事件编号": "REG-001", "发生时间": "2026-11-13 10:00:00"})
    check("首次上报成功", r1["ok"], r1["message"])
    groups = client.get("/api/alert/incidents/groups").json()["items"]
    group_a = next(g for g in groups if g["采集计划"] == plan and g["监控指标"] == metric)
    gid_a = group_a["id"]
    check("聚合键合并维度正确", group_a["聚合键"] == f"{metric}|{level}|{plan}")
    check("记录首次/最近触发", group_a["首次触发"] == "2026-11-13 10:00:00"
          and group_a["最近触发"] == "2026-11-13 10:00:00")
    check("累计次数=1", group_a["累计次数"] == 1)
    silence_end = "2026-11-13 10:15:00"
    check("重要级别静默 15 分钟", group_a["静默截止时间"] == silence_end,
          f"规则={LEVEL_RULES['重要']['silence_minutes']} 分钟")
    notes = client.get("/api/alert/incidents/notifications", params={"group_id": gid_a}).json()["items"]
    check("首报发送首次告警通知", any(n["通知类型"] == "首次告警" for n in notes))

    print("【2】相同事件重复上报只能并入同一聚合组")
    r_dup = post_event({"监控指标": metric, "告警级别": level, "采集计划": plan,
                        "事件编号": "REG-001", "发生时间": "2026-11-13 10:01:00"})
    check("重复事件幂等提示并入", r_dup["ok"] and "未重复计数" in r_dup["message"], r_dup["message"])
    group_a = client.get("/api/alert/incidents/groups").json()["items"][0]
    check("重复事件不累计", group_a["累计次数"] == 1)

    print("【3】静默期内连续失败被吸收，达到阈值生成处置待办并占用升级名额")
    for idx, stamp in enumerate(("10:03:00", "10:06:00"), start=2):
        post_event({"监控指标": metric, "告警级别": level, "采集计划": plan,
                    "事件编号": f"REG-00{idx}", "发生时间": f"2026-11-13 {stamp}"})
    # 同一事件编号再报一次（时间戳不同）：按身份识别为重复，并入但不计数
    r_same_code = post_event({"监控指标": metric, "告警级别": level, "采集计划": plan,
                              "事件编号": "REG-002", "发生时间": "2026-11-13 10:07:00"})
    check("同编号重复上报并入同组不计数", "未重复计数" in r_same_code["message"], r_same_code["message"])
    group_a = next(g for g in client.get("/api/alert/incidents/groups").json()["items"]
                   if g["id"] == gid_a)
    check("相邻事件合并为一组", group_a["累计次数"] == 3)
    check("静默期不重复通知", len(client.get(
        "/api/alert/incidents/notifications", params={"group_id": gid_a}).json()["items"]) == 2)  # 首次+升级
    check("升级只触发一次", group_a["升级状态"] == "已升级" and group_a["升级优先级"] == "P2")
    todos = client.get("/api/taskplan/incidents/todos").json()["items"]
    todo_a = next(t for t in todos if t["聚合组ID"] == gid_a)
    check("阈值后处置清单生成待办", todo_a["状态"] == "待处置" and todo_a["计划编号"] == plan)
    check("待办与告警清单同一聚合结论",
          todo_a["累计次数"] == group_a["累计次数"] and todo_a["告警级别"] == group_a["告警级别"]
          and todo_a["静默截止时间"] == group_a["静默截止时间"])
    check("升级名额被唯一占用", client.get("/api/alert/incidents/stats").json()["升级名额占用"] == 1)

    print("【4】第二个静默批次申请同一升级名额只能被拒绝一次")
    metric_b = "数据落库延迟"
    for idx in range(3):
        post_event({"监控指标": metric_b, "告警级别": "紧急", "采集计划": plan,
                    "事件编号": f"REGB-00{idx}", "发生时间": f"2026-11-13 10:0{idx}:00"})
    group_b = next(g for g in client.get("/api/alert/incidents/groups").json()["items"]
                   if g["监控指标"] == metric_b)
    check("名额占用中再次申请被拒", group_b["升级状态"] == "升级被拒")
    check("全局仍只有一次占用", client.get("/api/alert/incidents/stats").json()["升级名额占用"] == 1)
    # 静默期内继续失败：拒绝批次不重复尝试、不刷通知
    post_event({"监控指标": metric_b, "告警级别": "紧急", "采集计划": plan,
                "事件编号": "REGB-003", "发生时间": "2026-11-13 10:04:00"})
    notes_b = client.get("/api/alert/incidents/notifications",
                         params={"group_id": group_b["id"]}).json()["items"]
    check("被拒组静默期无重复升级/通知", all(n["通知类型"] != "升级通知" for n in notes_b))

    print("【5】静默结束后开启新批次，再次告警；名额未释放前仍被拒")
    clock.advance(16)  # 10:16，A/B 的静默均已结束
    post_event({"监控指标": metric, "告警级别": level, "采集计划": plan,
                "事件编号": "REG-004", "发生时间": "2026-11-13 10:16:00"})
    post_event({"监控指标": metric_b, "告警级别": "紧急", "采集计划": plan,
                "事件编号": "REGB-004", "发生时间": "2026-11-13 10:16:00"})
    group_b = next(g for g in client.get("/api/alert/incidents/groups").json()["items"]
                   if g["id"] == group_b["id"])
    check("新批次申请仍被拒且只记一次", group_b["升级状态"] == "升级被拒"
          and group_b["升级拒绝批次"] == 2)
    notes_a = client.get("/api/alert/incidents/notifications", params={"group_id": gid_a}).json()["items"]
    check("静默结束发送再次告警通知", any(n["通知类型"] == "再次告警" for n in notes_a))

    print("【6】恢复：生成恢复结论通知，待办退出，名额释放，历史可追溯")
    r_rec = post_event({
        "监控指标": metric, "告警级别": level, "采集计划": plan, "事件类型": "恢复",
        "事件编号": "REG-OK", "发生时间": "2026-11-13 10:20:00",
        "恢复结论": "TASK-0002 代理切换后页面抓取成功率恢复至 98%，连续 3 轮巡检正常，告警恢复。",
    })
    check("恢复事件受理", r_rec["ok"], r_rec["message"])
    group_a = next(g for g in client.get("/api/alert/incidents/groups").json()["items"]
                   if g["id"] == gid_a)
    check("聚合组状态已恢复", group_a["状态"] == "已恢复" and group_a["静默截止时间"] is None)
    rec_note = next(n for n in client.get(
        "/api/alert/incidents/notifications", params={"group_id": gid_a}).json()["items"]
        if n["通知类型"] == "恢复通知")
    check("恢复通知带恢复结论", "成功率恢复至 98%" in rec_note["通知内容"])
    open_todos = client.get("/api/taskplan/incidents/todos").json()["items"]
    check("恢复后待办退出待处置清单", all(t["聚合组ID"] != gid_a for t in open_todos))
    closed = client.get("/api/taskplan/incidents/todos",
                        params={"include_closed": "true"}).json()["items"]
    closed_a = next(t for t in closed if t["聚合组ID"] == gid_a)
    check("历史待办保留退出原因", closed_a["状态"] == "已关闭"
          and closed_a["退出原因"] == "告警恢复自动退出")
    events_a = client.get("/api/alert/incidents/events", params={"group_id": gid_a}).json()["items"]
    check("历史事件仍可追溯", len(events_a) >= 5 and all(e["聚合组ID"] == gid_a for e in events_a))
    check("名额随恢复释放", client.get("/api/alert/incidents/stats").json()["升级名额占用"] == 0)

    print("【7】名额释放后，B 组新静默批次可以占用（互斥而非永久封锁）")
    clock.advance(20)  # 10:36，超过紧急级别的 5 分钟静默
    post_event({"监控指标": metric_b, "告警级别": "紧急", "采集计划": plan,
                "事件编号": "REGB-005", "发生时间": "2026-11-13 10:36:00"})
    group_b = next(g for g in client.get("/api/alert/incidents/groups").json()["items"]
                   if g["id"] == group_b["id"])
    check("释放后申请成功", group_b["升级状态"] == "已升级" and group_b["升级优先级"] == "P1")

    print("【8】恢复后同维度再次活跃：新建聚合组，待办重新进入清单后人工确认退出")
    for idx in range(3):
        post_event({"监控指标": metric, "告警级别": level, "采集计划": plan,
                    "事件编号": f"REG2-00{idx}", "发生时间": f"2026-11-13 11:0{idx}:00"})
    new_group = next(g for g in client.get("/api/alert/incidents/groups",
                                           params={"status": "聚合中"}).json()["items"]
                     if g["监控指标"] == metric)
    check("恢复后再次活跃生成新聚合组", new_group["id"] != gid_a and new_group["累计次数"] == 3)
    new_todo = next(t for t in client.get("/api/taskplan/incidents/todos").json()["items"]
                    if t["聚合组ID"] == new_group["id"])
    check("待办重新进入处置清单", new_todo["状态"] == "待处置")
    ack = client.post(f"/api/taskplan/incidents/todos/{new_group['id']}/acknowledge",
                      json={"values": {}, "remark": "值班已人工排查并修复"})
    check("人工确认成功", ack.json()["ok"], ack.json()["message"])
    check("确认后待办退出", all(
        t["聚合组ID"] != new_group["id"]
        for t in client.get("/api/taskplan/incidents/todos").json()["items"]))
    ack_note = next(n for n in client.get(
        "/api/alert/incidents/notifications", params={"group_id": new_group["id"]}).json()["items"]
        if n["通知类型"] == "确认通知")
    check("确认动作留痕通知", "人工确认" in ack_note["通知内容"])

    print("【9】批量处置：一次提交分批展示，支持确认/恢复/跳过")
    batch_groups = []
    for name in ("批量指标甲", "批量指标乙", "批量指标丙"):
        post_event({"监控指标": name, "告警级别": "一般", "采集计划": plan,
                    "事件编号": f"BULK-{name}-1"})
        batch_groups.append(next(g for g in client.get("/api/alert/incidents/groups").json()["items"]
                                 if g["监控指标"] == name)["id"])
    payload = {"values": {"items": [
        {"group_id": batch_groups[0], "action": "确认", "remark": "批量确认甲"},
        {"group_id": batch_groups[1], "action": "跳过"},
        {"group_id": batch_groups[2], "action": "恢复", "remark": "批量恢复丙"},
    ]}}
    br = client.post("/api/alert/incidents/batch", json=payload).json()
    check("批量接口逐条返回结果", len(br["results"]) == 3)
    check("汇总计数正确", br["summary"] == {"确认": 1, "恢复": 1, "合并": 0, "跳过": 1, "失败": 0},
          str(br["summary"]))
    skipped = next(g for g in client.get("/api/alert/incidents/groups").json()["items"]
                   if g["id"] == batch_groups[1])
    check("跳过单条保持原状", skipped["状态"] == "聚合中" and skipped["已确认"] is False)
    check("批量确认生效", next(g for g in client.get("/api/alert/incidents/groups").json()["items"]
                               if g["id"] == batch_groups[0])["已确认"] is True)

    print("【10】多条合并：冲突时以最近一次有效聚合为准")
    m1, m2 = "合并指标X", "合并指标Y"
    post_event({"监控指标": m1, "告警级别": "一般", "采集计划": plan, "事件编号": "MRG-X1",
                "发生时间": "2026-11-13 12:00:00"})
    post_event({"监控指标": m2, "告警级别": "一般", "采集计划": plan, "事件编号": "MRG-Y1",
                "发生时间": "2026-11-13 12:09:00"})
    gx = next(g for g in client.get("/api/alert/incidents/groups").json()["items"]
              if g["监控指标"] == m1)
    gy = next(g for g in client.get("/api/alert/incidents/groups").json()["items"]
              if g["监控指标"] == m2)
    mr = client.post("/api/alert/incidents/groups/merge",
                     json={"values": {"group_ids": [gx["id"], gy["id"]]}}).json()
    check("合并成功", mr["ok"], mr["message"])
    check("以最近触发组为目标", mr["entry"]["id"] == gy["id"])
    gx_after = next(g for g in client.get("/api/alert/incidents/groups",
                                          params={"status": "已合并"}).json()["items"]
                    if g["id"] == gx["id"])
    check("源组标记已合并", gx_after["合并目标"] == gy["聚合编号"])
    moved = client.get("/api/alert/incidents/events", params={"group_id": gy["id"]}).json()["items"]
    check("事件并入目标组可追溯", len(moved) == 2)
    check("合并通知已生成", any(n["通知类型"] == "合并通知" for n in client.get(
        "/api/alert/incidents/notifications", params={"group_id": gy["id"]}).json()["items"]))

    print("【11】升级名额并发互斥压测：20 个静默批次同时申请只接受一次占用")
    reset_incident_tables()
    errors: list[Exception] = []

    def drive(index: int) -> None:
        try:
            for step in range(3):
                incident_service.ingest_event({
                    "监控指标": f"并发指标{index}",
                    "告警级别": "重要",
                    "采集计划": f"TASK-{index:04d}",
                    "事件编号": f"PAR-{index}-{step}",
                })
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=drive, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("并发上报无异常", not errors, str(errors[:1]))
    all_groups = client.get("/api/alert/incidents/groups").json()["items"]
    upgraded = [g for g in all_groups if g["升级状态"] == "已升级"]
    rejected = [g for g in all_groups if g["升级状态"] == "升级被拒"]
    check("仅一个批次占用名额", len(upgraded) == 1, f"已升级组数={len(upgraded)}")
    check("其余批次全部被拒", len(rejected) == 19, f"被拒组数={len(rejected)}")
    check("名额占用统计一致", client.get("/api/alert/incidents/stats").json()["升级名额占用"] == 1)

    print(f"\n全部回归通过：{PASSED} 项断言（ALER-0002 × TASK-0002）")


if __name__ == "__main__":
    main()
