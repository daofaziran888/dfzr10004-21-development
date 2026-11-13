"""告警聚合与恢复领域服务。

设计要点（与需求逐条对应）：

* 按「监控指标 + 告警级别 + 采集计划」合并相邻的同类失败事件，记录首次触发、
  最近一次触发、累计次数与静默截止时间；同一条事件（同一事件编号 / 同一时间戳）
  重复上报只会并入同一个聚合组，不会新建。
* 静默期内继续累计但不再发通知、不允许重复升级；静默结束后的下一次失败重新开始
  一个静默批次并再次通知，但聚合组保持不变（相邻事件合并）。
* 累计次数达到告警阈值（默认 3 次）后，在采集计划处置清单里生成一条待处置待办；
  待办内容始终从最新聚合结论实时刷新，恢复或人工确认后退出，之后同一组再次活跃
  会重新进入清单。
* 升级名额是全局互斥资源：同一时间只允许一个静默批次占用，两个批次并发申请时
  只接受一次占用，另一次被拒绝。
* 恢复事件关闭聚合组，并生成一条带恢复结论的恢复通知，同步关闭处置待办。
* 所有写操作在同一把锁内完成，FastAPI 同步路由下保证「检查—占用」原子性。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any, Callable

from app.store import store

# ---- 静默时长与升级优先级的取舍 ---------------------------------------------
# 告警级别越高，静默越短（高优问题需要更快再次暴露），升级优先级越高。
LEVEL_RULES: dict[str, dict[str, Any]] = {
    "紧急": {"rank": 3, "silence_minutes": 5, "priority": "P1"},
    "重要": {"rank": 2, "silence_minutes": 15, "priority": "P2"},
    "一般": {"rank": 1, "silence_minutes": 30, "priority": "P3"},
}
LEVEL_ALIASES = {
    "p1": "紧急", "紧急": "紧急", "critical": "紧急",
    "p2": "重要", "重要": "重要", "major": "重要",
    "p3": "一般", "一般": "一般", "minor": "一般", "warning": "一般",
}
DEFAULT_THRESHOLD = 3  # 累计失败达到该次数后生成处置待办

TABLES = ("alert_event", "alert_group", "alert_notification", "alert_todo")


def _now() -> datetime:
    return datetime.now()


def _fmt(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def normalize_level(raw: Any) -> str:
    key = str(raw or "").strip().lower()
    return LEVEL_ALIASES.get(key, "一般")


def parse_time(raw: Any, fallback: datetime) -> datetime:
    """事件时间允许客户端显式传入（回归测试需要确定时间），否则取当前时间。"""
    text = str(raw or "").strip()
    if not text:
        return fallback
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return fallback


class IncidentService:
    """聚合组、通知、待办、升级名额的唯一写入口，alert 与 taskplan 共用同一实例。"""

    def __init__(self, clock: Callable[[], datetime] = _now) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        # 升级名额状态：None=空闲；否则记录当前申请批次（聚合组 id + 批次号）。
        # 先登记申请再判定占用，保证并发时第一个申请者必定成为占用者，
        # 其余批次只接受一次拒绝，不会出现「全部被拒却无人占用」。
        self._slot_claim: tuple[int, int] | None = None
        self._escalation_holder: int | None = None  # 当前占用升级名额的聚合组 id

    # ---- 基础工具 -----------------------------------------------------------
    def _table(self, name: str) -> list[dict[str, Any]]:
        return store.rows(name)

    def _next_id(self, table: str) -> int:
        return max((int(row.get("id", 0)) for row in self._table(table)), default=0) + 1

    def _find_group(self, group_id: int) -> dict[str, Any] | None:
        for row in self._table("alert_group"):
            if int(row.get("id", 0)) == group_id:
                return row
        return None

    def _find_todo(self, group_id: int) -> dict[str, Any] | None:
        for row in self._table("alert_todo"):
            if int(row.get("聚合组ID", 0)) == group_id:
                return row
        return None

    @staticmethod
    def _group_key(metric: str, level: str, plan: str) -> str:
        return f"{metric}|{level}|{plan}"

    # ---- 查询 ---------------------------------------------------------------
    def list_groups(
        self,
        *,
        status: str | None = None,
        keyword: str | None = None,
        plan: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = self._table("alert_group")
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        if plan:
            rows = [row for row in rows if row.get("采集计划") == plan]
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("监控指标", ""))
                or keyword in str(row.get("聚合编号", ""))
                or keyword in str(row.get("采集计划", ""))
            ]
        return sorted(rows, key=lambda row: int(row.get("id", 0)), reverse=True)

    def list_notifications(self, group_id: int | None = None) -> list[dict[str, Any]]:
        rows = self._table("alert_notification")
        if group_id is not None:
            rows = [row for row in rows if int(row.get("聚合组ID", 0)) == group_id]
        return sorted(rows, key=lambda row: int(row.get("id", 0)), reverse=True)

    def list_events(self, group_id: int | None = None) -> list[dict[str, Any]]:
        rows = self._table("alert_event")
        if group_id is not None:
            rows = [row for row in rows if int(row.get("聚合组ID", 0)) == group_id]
        return sorted(rows, key=lambda row: int(row.get("id", 0)), reverse=True)

    def list_todos(self, *, include_closed: bool = False, plan: str | None = None) -> list[dict[str, Any]]:
        """处置清单：待办结论直接取最新聚合组，保证两个清单使用同一聚合结论。"""
        result: list[dict[str, Any]] = []
        for todo in self._table("alert_todo"):
            if not include_closed and todo.get("状态") != "待处置":
                continue
            group = self._find_group(int(todo.get("聚合组ID", 0)))
            if group is None:
                continue
            if plan and group.get("采集计划") != plan:
                continue
            result.append(self._todo_view(todo, group))
        return sorted(result, key=lambda view: int(view["聚合组ID"]), reverse=True)

    def stats(self) -> dict[str, int]:
        groups = self._table("alert_group")
        active = [g for g in groups if g.get("状态") == "聚合中"]
        silenced = [g for g in active if str(g.get("静默截止时间", "")) > _fmt(self._clock())]
        todos = [t for t in self._table("alert_todo") if t.get("状态") == "待处置"]
        return {
            "聚合组总数": len(groups),
            "活跃聚合组": len(active),
            "静默中": len(silenced),
            "待处置待办": len(todos),
            "通知总数": len(self._table("alert_notification")),
            "升级名额占用": 1 if self._escalation_holder is not None else 0,
        }

    def _todo_view(self, todo: dict[str, Any], group: dict[str, Any]) -> dict[str, Any]:
        """待办视图与聚合组保持同一结论：次数、级别、静默、升级都从聚合组取。"""
        view = dict(todo)
        view.update({
            "聚合编号": group.get("聚合编号"),
            "监控指标": group.get("监控指标"),
            "告警级别": group.get("告警级别"),
            "采集计划": group.get("采集计划"),
            "累计次数": group.get("累计次数"),
            "首次触发": group.get("首次触发"),
            "最近触发": group.get("最近触发"),
            "静默截止时间": group.get("静默截止时间"),
            "升级状态": group.get("升级状态"),
            "升级优先级": group.get("升级优先级"),
            "聚合状态": group.get("状态"),
        })
        return view

    # ---- 通知 ---------------------------------------------------------------
    def _add_notification(
        self,
        group: dict[str, Any],
        kind: str,
        title: str,
        conclusion: str,
    ) -> dict[str, Any]:
        note = {
            "id": self._next_id("alert_notification"),
            "聚合组ID": group["id"],
            "聚合编号": group["聚合编号"],
            "通知类型": kind,
            "通知标题": title,
            "通知内容": conclusion,
            "通知时间": _fmt(self._clock()),
            "通知渠道": "站内消息",
            "监控指标": group["监控指标"],
            "告警级别": group["告警级别"],
            "采集计划": group["采集计划"],
        }
        self._table("alert_notification").append(note)
        return note

    # ---- 待办 ---------------------------------------------------------------
    def _ensure_todo(self, group: dict[str, Any]) -> dict[str, Any]:
        todo = self._find_todo(int(group["id"]))
        if todo is None:
            todo = {
                "id": self._next_id("alert_todo"),
                "待办编号": f"TODO-{int(group['id']):04d}",
                "聚合组ID": group["id"],
                "计划编号": group["采集计划"],
                "状态": "待处置",
                "进入时间": _fmt(self._clock()),
                "关闭时间": None,
                "退出原因": None,
            }
            self._table("alert_todo").append(todo)
        elif todo.get("状态") != "待处置":
            # 人工确认后同组再次活跃：待办重新进入清单
            todo["状态"] = "待处置"
            todo["关闭时间"] = None
            todo["退出原因"] = None
        return todo

    def _close_todo(self, group_id: int, reason: str) -> None:
        todo = self._find_todo(group_id)
        if todo is not None and todo.get("状态") == "待处置":
            todo["状态"] = "已关闭"
            todo["关闭时间"] = _fmt(self._clock())
            todo["退出原因"] = reason

    def _new_event(
        self,
        group_id: int | None,
        metric: str,
        level: str,
        plan: str,
        kind: str,
        stamp: str,
        summary: str,
        event_code: str,
    ) -> dict[str, Any]:
        eid = self._next_id("alert_event")
        return {
            "id": eid,
            "事件编号": event_code or f"EVT-{eid:04d}",
            "聚合组ID": group_id,
            "监控指标": metric,
            "告警级别": level,
            "采集计划": plan,
            "事件类型": kind,
            "发生时间": stamp,
            "事件摘要": summary,
        }

    # ---- 事件上报与聚合 -----------------------------------------------------
    def ingest_event(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        metric = str(values.get("监控指标") or "").strip()
        plan = str(values.get("采集计划") or "").strip()
        event_kind = str(values.get("事件类型") or "失败").strip()
        if not metric or not plan:
            return None, "缺少必填字段：监控指标、采集计划"
        level = normalize_level(values.get("告警级别"))
        occurred = parse_time(values.get("发生时间"), self._clock())
        event_code = str(values.get("事件编号") or "").strip()

        with self._lock:
            # 同一事件编号重复上报：幂等并入，直接返回原事件，不重复计数
            if event_code:
                for old in self._table("alert_event"):
                    if old.get("事件编号") == event_code:
                        group = self._find_group(int(old["聚合组ID"]))
                        return old, f"事件 {event_code} 已并入聚合组 {group['聚合编号'] if group else old['聚合组ID']}，未重复计数"

            if event_kind in ("恢复", "恢复正常", "recovered"):
                return self._handle_recovery(metric, level, plan, occurred, event_code, values)
            return self._handle_failure(metric, level, plan, occurred, event_code, values)

    def _handle_failure(
        self,
        metric: str,
        level: str,
        plan: str,
        occurred: datetime,
        event_code: str,
        values: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        key = self._group_key(metric, level, plan)
        group = next(
            (
                row
                for row in self._table("alert_group")
                if row.get("聚合键") == key and row.get("状态") == "聚合中"
            ),
            None,
        )
        rule = LEVEL_RULES[level]
        now = self._clock()
        created = group is None

        if group is None:
            gid = self._next_id("alert_group")
            group = {
                "id": gid,
                "聚合编号": f"AGR-{gid:04d}",
                "聚合键": key,
                "监控指标": metric,
                "告警级别": level,
                "采集计划": plan,
                "状态": "聚合中",
                "首次触发": _fmt(occurred),
                "最近触发": _fmt(occurred),
                "累计次数": 0,
                "静默截止时间": None,
                "静默批次": 0,
                "告警阈值": int(values.get("告警阈值") or DEFAULT_THRESHOLD),
                "升级状态": "未升级",
                "升级优先级": rule["priority"],
                "升级占用时间": None,
                "升级拒绝批次": None,
                "已确认": False,
                "确认时间": None,
                "恢复时间": None,
                "恢复结论": None,
            }
            self._table("alert_group").append(group)

        # 相同事件（同一事件编号）重复上报只并入同一组；未带编号的事件按独立事件累计
        stamp = _fmt(occurred)
        duplicate = bool(event_code) and any(
            int(ev.get("聚合组ID", 0)) == int(group["id"]) and ev.get("事件编号") == event_code
            for ev in self._table("alert_event")
        )

        event = self._new_event(
            group["id"], metric, level, plan, "失败", stamp,
            str(values.get("事件摘要") or f"{plan} 采集失败，触发 {metric} 告警"),
            event_code,
        )
        self._table("alert_event").append(event)

        messages: list[str] = []
        if duplicate:
            messages.append("相同事件重复上报，已并入同一聚合组且不重复计数")
        else:
            group["累计次数"] = int(group["累计次数"]) + 1
        group["最近触发"] = stamp

        silenced = bool(group["静默截止时间"]) and group["静默截止时间"] > _fmt(now)
        if silenced:
            # 静默期内：继续累计，不重复通知、不重复升级
            messages.append(f"处于静默期（截止 {group['静默截止时间']}），本次仅累计不重复通知/升级")
        else:
            # 开启一个新的静默批次：静默期内的重复失败被吸收
            group["静默批次"] = int(group["静默批次"]) + 1
            group["静默截止时间"] = _fmt(now + timedelta(minutes=int(rule["silence_minutes"])))
            kind = "首次告警" if created else "再次告警"
            self._add_notification(
                group,
                kind,
                f"【{level}】{metric} 告警（{plan}）",
                f"{plan} 的 {metric} 第 {group['累计次数']} 次失败，已进入静默至 {group['静默截止时间']}，静默期内同类事件合并提醒。",
            )
            messages.append(f"已开启第 {group['静默批次']} 个静默批次并发送{kind}通知")

        # 达到阈值：生成/刷新处置待办，并尝试占用升级名额（每个静默批次只尝试一次）
        threshold = int(group.get("告警阈值") or DEFAULT_THRESHOLD)
        if int(group["累计次数"]) >= threshold:
            todo = self._ensure_todo(group)
            messages.append(f"累计 {group['累计次数']} 次达到阈值 {threshold}，处置清单待办 {todo['待办编号']} 已刷新")
            current_batch = int(group["静默批次"])
            if group.get("升级状态") != "已升级" and group.get("升级拒绝批次") != current_batch:
                self._try_escalate(group, current_batch, messages)

        return event, "；".join(messages)

    def _try_escalate(self, group: dict[str, Any], batch: int, messages: list[str]) -> None:
        """两个静默批次并发申请同一升级名额时只接受一次占用。"""
        claim = (int(group["id"]), batch)
        if self._slot_claim is None:
            # 第一个到达的批次登记申请并占用名额（锁内原子完成）
            self._slot_claim = claim
            self._escalation_holder = int(group["id"])
            group["升级状态"] = "已升级"
            group["升级占用时间"] = _fmt(self._clock())
            self._add_notification(
                group,
                "升级通知",
                f"【升级】{group['监控指标']} 已占用升级名额",
                f"聚合组 {group['聚合编号']} 第 {batch} 批次升级成功，优先级 {group['升级优先级']}，请优先处置 {group['采集计划']}。",
            )
            messages.append("升级名额申请成功，已发送升级通知")
        elif self._escalation_holder == int(group["id"]):
            messages.append("升级名额已由本聚合组占用，不重复触发升级")
        else:
            holder = self._find_group(self._escalation_holder)
            group["升级状态"] = "升级被拒"
            group["升级拒绝批次"] = batch
            holder_label = holder["聚合编号"] if holder else self._escalation_holder
            messages.append(f"升级名额已被 {holder_label} 占用，本批次申请仅接受一次占用，本次拒绝")

    def _handle_recovery(
        self,
        metric: str,
        level: str,
        plan: str,
        occurred: datetime,
        event_code: str,
        values: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        key = self._group_key(metric, level, plan)
        group = next(
            (row for row in self._table("alert_group")
             if row.get("聚合键") == key and row.get("状态") == "聚合中"),
            None,
        )
        event = self._new_event(
            group["id"] if group else None, metric, level, plan, "恢复", _fmt(occurred),
            str(values.get("事件摘要") or f"{plan} 的 {metric} 恢复正常"),
            event_code,
        )
        self._table("alert_event").append(event)

        if group is None:
            return event, "未找到进行中的聚合组，恢复事件已记录，无需生成恢复通知"

        conclusion = str(values.get("恢复结论") or "").strip() or (
            f"{plan} 的 {metric} 连续监控恢复正常；"
            f"本次告警共累计失败 {group['累计次数']} 次，"
            f"首次触发 {group['首次触发']}，最近触发 {group['最近触发']}，静默 {group['静默批次']} 批次。"
        )
        group["状态"] = "已恢复"
        group["恢复时间"] = _fmt(occurred)
        group["恢复结论"] = conclusion
        group["静默截止时间"] = None
        if self._escalation_holder == int(group["id"]):
            self._escalation_holder = None
            self._slot_claim = None
        self._close_todo(int(group["id"]), "告警恢复自动退出")
        self._add_notification(
            group,
            "恢复通知",
            f"【恢复】{metric} 告警恢复（{plan}）",
            conclusion,
        )
        return event, f"聚合组 {group['聚合编号']} 已恢复并生成恢复通知，处置待办同步退出"

    # ---- 人工确认 / 手动恢复 ------------------------------------------------
    def acknowledge(self, group_id: int, remark: str | None = None) -> tuple[dict[str, Any] | None, str]:
        with self._lock:
            group = self._find_group(group_id)
            if group is None:
                return None, f"聚合组 {group_id} 不存在"
            if group.get("状态") != "聚合中":
                return group, f"聚合组 {group['聚合编号']} 已结束（{group.get('状态')}），无需确认"
            group["已确认"] = True
            group["确认时间"] = _fmt(self._clock())
            self._close_todo(group_id, f"人工确认退出：{remark or '值班人员已确认处置'}")
            self._add_notification(
                group,
                "确认通知",
                f"【确认】{group['监控指标']} 告警已人工确认（{group['采集计划']}）",
                f"值班人员已人工确认聚合组 {group['聚合编号']}，处置待办退出；若同类事件再次发生将重新进入清单。备注：{remark or '无'}",
            )
            return group, f"聚合组 {group['聚合编号']} 已人工确认，处置待办退出"

    def recover_group(self, group_id: int, remark: str | None = None) -> tuple[dict[str, Any] | None, str]:
        with self._lock:
            group = self._find_group(group_id)
            if group is None:
                return None, f"聚合组 {group_id} 不存在"
            if group.get("状态") != "聚合中":
                return group, f"聚合组 {group['聚合编号']} 已是{group.get('状态')}状态，无需重复恢复"
            values = {
                "监控指标": group["监控指标"],
                "告警级别": group["告警级别"],
                "采集计划": group["采集计划"],
                "事件类型": "恢复",
                "恢复结论": remark,
            }
            _, message = self._handle_recovery(
                group["监控指标"], group["告警级别"], group["采集计划"],
                self._clock(), "", values,
            )
            return self._find_group(group_id), message

    # ---- 多条合并 -----------------------------------------------------------
    def merge_groups(self, group_ids: list[int]) -> tuple[dict[str, Any] | None, list[str]]:
        unique_ids = list(dict.fromkeys(int(gid) for gid in group_ids))
        if len(unique_ids) < 2:
            return None, ["至少选择两个聚合组才能合并"]
        with self._lock:
            groups = [self._find_group(gid) for gid in unique_ids]
            if any(group is None for group in groups):
                return None, ["存在已删除或不存在的聚合组，合并已中止"]
            actives = [group for group in groups if group.get("状态") == "聚合中"]
            if len(actives) < 2:
                return None, ["只有处于「聚合中」的聚合组才能合并，未做任何变更"]
            # 冲突时以最近一次有效聚合为准：最近触发时间最新的组作为目标组
            target = max(actives, key=lambda g: str(g.get("最近触发", "")))
            sources = [g for g in actives if int(g["id"]) != int(target["id"])]

            total = int(target["累计次数"])
            for source in sources:
                total += int(source["累计次数"])
                for event in self._table("alert_event"):
                    if int(event.get("聚合组ID", 0)) == int(source["id"]):
                        event["聚合组ID"] = target["id"]
                for note in self._table("alert_notification"):
                    if int(note.get("聚合组ID", 0)) == int(source["id"]):
                        note["聚合组ID"] = target["id"]
                source["状态"] = "已合并"
                source["合并目标"] = target["聚合编号"]
                # 源组若持有升级名额，统一交给目标组，避免名额悬空
                if self._escalation_holder == int(source["id"]):
                    self._escalation_holder = int(target["id"])
                    self._slot_claim = (int(target["id"]), self._slot_claim[1] if self._slot_claim else 0)
                    target["升级状态"] = "已升级"
                    target["升级占用时间"] = target["升级占用时间"] or _fmt(self._clock())
                self._close_todo(int(source["id"]), f"合并至 {target['聚合编号']} 后退出")
            target["累计次数"] = total
            target["静默截止时间"] = max(
                str(target.get("静默截止时间") or ""),
                *(str(g.get("静默截止时间") or "") for g in sources),
            ) or None
            if int(target["累计次数"]) >= int(target.get("告警阈值") or DEFAULT_THRESHOLD):
                self._ensure_todo(target)
            self._add_notification(
                target,
                "合并通知",
                f"【合并】{len(sources) + 1} 个聚合组已合并为 {target['聚合编号']}",
                f"合并后以最近一次有效聚合为准，累计失败 {target['累计次数']} 次，最近触发 {target['最近触发']}。",
            )
            return target, [f"{g['聚合编号']} 已并入 {target['聚合编号']}" for g in sources]

    # ---- 批量处置：一次提交分批展示结果，可跳过单条 --------------------------
    def batch_process(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        summary = {"确认": 0, "恢复": 0, "合并": 0, "跳过": 0, "失败": 0}
        merge_ids: list[int] = []
        for item in items:
            action = str(item.get("action") or "").strip()
            group_id = int(item.get("group_id") or 0)
            remark = item.get("remark")
            if action == "跳过":
                summary["跳过"] += 1
                results.append({"group_id": group_id, "action": action, "ok": True, "message": "已跳过该条"})
                continue
            if action == "合并":
                merge_ids.append(group_id)
                continue
            try:
                with self._lock:
                    if action == "确认":
                        group, message = self.acknowledge(group_id, remark)
                    elif action == "恢复":
                        group, message = self.recover_group(group_id, remark)
                    else:
                        group, message = None, f"不支持的批量动作「{action}」"
                ok = group is not None
                summary[action if ok else "失败"] = summary.get(action if ok else "失败", 0) + 1
                results.append({"group_id": group_id, "action": action, "ok": ok, "message": message})
            except Exception as exc:  # 单条失败不影响其余条目
                summary["失败"] += 1
                results.append({"group_id": group_id, "action": action, "ok": False, "message": str(exc)})

        if merge_ids:
            target, messages = self.merge_groups(merge_ids)
            ok = target is not None
            summary["合并" if ok else "失败"] = summary.get("合并" if ok else "失败", 0) + 1
            results.append({
                "group_id": merge_ids,
                "action": "合并",
                "ok": ok,
                "message": "；".join(messages) if ok else "；".join(messages),
            })
        return {"ok": summary["失败"] == 0, "summary": summary, "results": results}


# 单例：alert 路由与 taskplan 路由注入同一个服务，共享同一份聚合结论
incident_service = IncidentService()


def reset_incident_tables() -> None:
    """清空聚合相关四张表并释放升级名额，回归测试用。"""
    incident_service._escalation_holder = None
    incident_service._slot_claim = None
    for table in TABLES:
        incident_service._table(table).clear()


def bootstrap_demo() -> None:
    """首次启动时灌入一条活跃聚合与一条已恢复聚合，方便面板直接看到效果。"""
    if incident_service._table("alert_group"):
        return
    base = incident_service._clock()
    # 活跃聚合：ALER-0002（页面抓取成功率 / 重要）× TASK-0002，累计 4 次、已达阈值
    for idx in range(4):
        incident_service.ingest_event({
            "监控指标": "页面抓取成功率",
            "告警级别": "重要",
            "采集计划": "TASK-0002",
            "事件编号": f"DEMO-EVT-{idx + 1}",
            "发生时间": _fmt(base - timedelta(minutes=20 - idx * 5)),
            "事件摘要": "目标站点连续超时，页面抓取成功率低于 80%",
        })
    # 已恢复聚合：保留恢复通知与历史轨迹，证明历史告警可追溯
    for idx in range(3):
        incident_service.ingest_event({
            "监控指标": "接口响应延迟",
            "告警级别": "一般",
            "采集计划": "TASK-0002",
            "事件编号": f"DEMO-REC-{idx + 1}",
            "发生时间": _fmt(base - timedelta(hours=2, minutes=idx * 10)),
        })
    incident_service.ingest_event({
        "监控指标": "接口响应延迟",
        "告警级别": "一般",
        "采集计划": "TASK-0002",
        "事件类型": "恢复",
        "事件编号": "DEMO-REC-OK",
        "发生时间": _fmt(base - timedelta(minutes=30)),
        "恢复结论": "代理节点切换后接口响应延迟回落至 300ms 以内，连续 3 次巡检正常，告警恢复。",
    })
