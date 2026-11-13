/** 告警聚合域的接口类型与请求封装，告警面板与采集计划处置清单共用。 */
import { request } from './client'

export interface AlertGroup {
  id: number
  聚合编号: string
  聚合键: string
  监控指标: string
  告警级别: string
  采集计划: string
  状态: '聚合中' | '已恢复' | '已合并'
  首次触发: string
  最近触发: string
  累计次数: number
  静默截止时间: string | null
  静默批次: number
  告警阈值: number
  升级状态: string
  升级优先级: string
  升级占用时间: string | null
  升级拒绝批次: number | null
  已确认: boolean
  确认时间: string | null
  恢复时间: string | null
  恢复结论: string | null
  合并目标?: string
}

export interface AlertNotification {
  id: number
  聚合组ID: number
  聚合编号: string
  通知类型: string
  通知标题: string
  通知内容: string
  通知时间: string
  通知渠道: string
}

export interface AlertEvent {
  id: number
  事件编号: string
  聚合组ID: number | null
  监控指标: string
  告警级别: string
  采集计划: string
  事件类型: string
  发生时间: string
  事件摘要: string
}

export interface DisposalTodo {
  id: number
  待办编号: string
  聚合组ID: number
  计划编号: string
  状态: '待处置' | '已关闭'
  进入时间: string
  关闭时间: string | null
  退出原因: string | null
  聚合编号: string
  监控指标: string
  告警级别: string
  采集计划: string
  累计次数: number
  首次触发: string
  最近触发: string
  静默截止时间: string | null
  升级状态: string
  升级优先级: string
  聚合状态: string
}

export interface BatchItem {
  group_id: number
  action: '确认' | '恢复' | '合并' | '跳过'
  remark?: string
}

export interface BatchResultRow {
  group_id: number | number[]
  action: string
  ok: boolean
  message: string
}

export interface BatchResponse {
  ok: boolean
  summary: Record<string, number>
  results: BatchResultRow[]
}

async function getJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) {
    throw new Error(`接口返回 ${response.status}，数据未更新`)
  }
  return (await response.json()) as T
}

export const incidentApi = {
  stats: () => getJson<Record<string, number>>('/api/alert/incidents/stats'),
  groups: (params: Record<string, string> = {}) => {
    const query = new URLSearchParams(params).toString()
    return getJson<{ items: AlertGroup[]; total: number }>(
      `/api/alert/incidents/groups${query ? `?${query}` : ''}`,
    )
  },
  notifications: (groupId?: number) =>
    getJson<{ items: AlertNotification[]; total: number }>(
      `/api/alert/incidents/notifications${groupId ? `?group_id=${groupId}` : ''}`,
    ),
  events: (groupId?: number) =>
    getJson<{ items: AlertEvent[]; total: number }>(
      `/api/alert/incidents/events${groupId ? `?group_id=${groupId}` : ''}`,
    ),
  todos: (includeClosed = false, plan?: string) => {
    const params = new URLSearchParams()
    if (includeClosed) params.set('include_closed', 'true')
    if (plan) params.set('plan', plan)
    const query = params.toString()
    return getJson<{ items: DisposalTodo[]; total: number }>(
      `/api/taskplan/incidents/todos${query ? `?${query}` : ''}`,
    )
  },
  ingest: (values: Record<string, unknown>) =>
    request('/api/alert/incidents/events', {
      method: 'POST',
      body: JSON.stringify({ values }),
    }).then((res) => res.json()),
  acknowledge: (groupId: number, remark?: string) =>
    request(`/api/alert/incidents/groups/${groupId}/acknowledge`, {
      method: 'POST',
      body: JSON.stringify({ values: {}, remark }),
    }).then((res) => res.json()),
  recover: (groupId: number, remark?: string) =>
    request(`/api/alert/incidents/groups/${groupId}/recover`, {
      method: 'POST',
      body: JSON.stringify({ values: {}, remark }),
    }).then((res) => res.json()),
  merge: (groupIds: number[]) =>
    request('/api/alert/incidents/groups/merge', {
      method: 'POST',
      body: JSON.stringify({ values: { group_ids: groupIds } }),
    }).then((res) => res.json()),
  batch: (items: BatchItem[]) =>
    request('/api/alert/incidents/batch', {
      method: 'POST',
      body: JSON.stringify({ values: { items } }),
    }).then((res) => res.json() as Promise<BatchResponse>),
}
