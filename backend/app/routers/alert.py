"""采集告警中心接口。

上半部分保留原有告警规则的登记与状态流转；
下半部分接入告警聚合与恢复域（聚合组、事件、通知、批量处置）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.alert import AlertService
from app.services.incident import incident_service

router = APIRouter(prefix="/api/alert", tags=["采集告警中心"])

service = AlertService()

LIST_FIELDS = ["规则编号", "监控指标", "告警级别", "触发条件", "通知渠道", "静默时间", "升级策略", "是否启用"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


# ---------------------------------------------------------------------------
# 告警规则清单（原有能力，保持不变）
# ---------------------------------------------------------------------------
@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤采集告警中心列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集告警中心清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "alert", "total": total, "items": items}


@router.get("/incidents/stats")
def incident_stats() -> dict[str, Any]:
    """告警面板顶部统计：活跃组、静默中、待处置待办与升级名额占用情况。"""
    return incident_service.stats()


@router.get("/incidents/groups")
def list_groups(
    status: str | None = Query(default=None, description="聚合中、已恢复、已合并"),
    keyword: str | None = Query(default=None, description="按监控指标/聚合编号/采集计划检索"),
) -> dict[str, Any]:
    """聚合组清单就是告警规则清单使用的同一份聚合结论。"""
    items = incident_service.list_groups(status=status, keyword=keyword)
    return {"items": items, "total": len(items)}


@router.get("/incidents/notifications")
def list_notifications(group_id: int | None = Query(default=None)) -> dict[str, Any]:
    """通知记录：首次/再次告警、升级、恢复、确认、合并通知都可追溯。"""
    items = incident_service.list_notifications(group_id)
    return {"items": items, "total": len(items)}


@router.get("/incidents/events")
def list_events(group_id: int | None = Query(default=None)) -> dict[str, Any]:
    """原始事件历史：即使聚合组恢复/合并，事件轨迹仍完整保留。"""
    items = incident_service.list_events(group_id)
    return {"items": items, "total": len(items)}


@router.post("/incidents/events", response_model=ActionResult)
def ingest_event(payload: EntryPayload) -> ActionResult:
    """上报一次采集失败或恢复事件，由聚合服务决定合并、静默、升级或恢复。"""
    event, message = incident_service.ingest_event(payload.values)
    if event is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=event)


@router.post("/incidents/groups/merge", response_model=ActionResult)
def merge_groups(payload: EntryPayload) -> ActionResult:
    """把多个聚合组合并为一条；冲突时以最近一次有效聚合为准。"""
    raw_ids = payload.values.get("group_ids") or []
    group_ids = [int(value) for value in raw_ids]
    target, messages = incident_service.merge_groups(group_ids)
    if target is None:
        return ActionResult(ok=False, message="；".join(messages))
    return ActionResult(ok=True, message="；".join(messages), entry=target)


@router.post("/incidents/batch")
def batch_process(payload: EntryPayload) -> dict[str, Any]:
    """批量处置：一次提交多条确认/恢复/合并/跳过，按条返回结果，单条失败不阻断其余。"""
    items = payload.values.get("items") or []
    return incident_service.batch_process(items)


@router.post("/incidents/groups/{group_id}/acknowledge", response_model=ActionResult)
def acknowledge_group(group_id: int, payload: EntryPayload) -> ActionResult:
    """逐条确认：人工确认后处置待办退出，同组再次活跃会重新进入清单。"""
    group, message = incident_service.acknowledge(group_id, payload.remark)
    if group is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=group)


@router.post("/incidents/groups/{group_id}/recover", response_model=ActionResult)
def recover_group(group_id: int, payload: EntryPayload) -> ActionResult:
    """整组恢复：生成带恢复结论的恢复通知，并同步退出处置待办。"""
    group, message = incident_service.recover_group(group_id, payload.remark)
    if group is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=group)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条告警规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"告警规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条告警规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="告警规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条告警规则执行提交校验、启用规则、停用规则；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
