<template>
  <section class="page" data-module="taskplan">
    <header class="page-head">
      <div>
        <h2>采集任务编排管理</h2>
        <p class="page-desc">维护采集计划的站点配置、频率设置与资源分配；处置清单与告警面板共用同一聚合结论，告警恢复或人工确认后待办退出。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记采集计划</button>
        <button class="btn" type="button" @click="exportRows">导出采集任务编排清单</button>
      </div>
    </header>

    <section class="disposal-panel">
      <header class="disposal-head">
        <h3>采集计划处置清单（来源：告警聚合结论）</h3>
        <div class="page-actions">
          <label class="history-check">
            <input v-model="includeClosedTodos" type="checkbox" @change="reloadTodos" />
            显示已退出的历史待办
          </label>
          <button class="btn" type="button" @click="reloadTodos">刷新待办</button>
        </div>
      </header>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in todoColumns" :key="column">{{ column }}</th>
            <th>处置动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="todo in todos" :key="todo.id" :class="{ muted: todo.状态 !== '待处置' }">
            <td>{{ todo.待办编号 }}</td>
            <td>{{ todo.聚合编号 }}</td>
            <td>{{ todo.计划编号 }}</td>
            <td>{{ todo.监控指标 }}</td>
            <td><span class="badge" :class="levelClass(todo.告警级别)">{{ todo.告警级别 }}</span></td>
            <td>{{ todo.累计次数 }}</td>
            <td>{{ todo.最近触发 }}</td>
            <td>{{ todo.静默截止时间 ?? '—' }}</td>
            <td><span class="badge" :class="escalationClass(todo.升级状态)">{{ todo.升级状态 }}（{{ todo.升级优先级 }}）</span></td>
            <td><span class="badge" :class="todo.状态 === '待处置' ? 'firing' : 'recovered'">{{ todo.状态 }}</span></td>
            <td>{{ todo.退出原因 ?? '—' }}</td>
            <td class="row-actions">
              <template v-if="todo.状态 === '待处置'">
                <button class="link" type="button" @click="ackTodo(todo)">人工确认</button>
                <button class="link danger" type="button" @click="recoverTodo(todo)">整组恢复</button>
              </template>
              <span v-else class="muted-text">已退出</span>
            </td>
          </tr>
          <tr v-if="!todos.length">
            <td :colspan="todoColumns.length + 1" class="empty-state">暂无待处置待办：聚合告警达到阈值后会自动进入此清单</td>
          </tr>
        </tbody>
      </table>
      <p v-if="todoMessage" class="ingest-message">{{ todoMessage }}</p>
    </section>


    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无采集任务编排数据，可先登记采集计划</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集任务编排记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { incidentApi, type DisposalTodo } from '@/api/incident'
import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/taskplan'
const columns = ["计划编号", "目标站点", "采集频率", "数据范围", "优先级", "资源配额", "预计用时", "计划状态"]
const actions = ["启动计划", "暂停计划", "归档计划"]
const statuses = ["草稿", "运行中", "已暂停", "已完成"]
const stats = [{"label": "采集计划总数", "value": 0}, {"label": "运行中计划", "value": 0}, {"label": "已暂停计划", "value": 0}]

const todoColumns = ['待办编号', '聚合编号', '计划编号', '监控指标', '告警级别', '累计次数', '最近触发', '静默截止时间', '升级状态', '待办状态', '退出原因']
const todos = ref<DisposalTodo[]>([])
const includeClosedTodos = ref(false)
const todoMessage = ref('')

function levelClass(level: string): string {
  return { 紧急: 'critical', 重要: 'major', 一般: 'minor' }[level] ?? 'minor'
}
function escalationClass(status: string): string {
  return { 已升级: 'critical', 升级被拒: 'merged', 未升级: 'minor' }[status] ?? 'minor'
}

async function reloadTodos() {
  todoMessage.value = ''
  try {
    todos.value = (await incidentApi.todos(includeClosedTodos.value)).items
  } catch (error) {
    todoMessage.value = error instanceof Error ? error.message : '处置清单读取失败'
  }
}

async function ackTodo(todo: DisposalTodo) {
  const remark = window.prompt(`确认待办 ${todo.待办编号} 的处置备注（可留空）`) ?? ''
  const result = await incidentApi.acknowledge(todo.聚合组ID, remark)
  todoMessage.value = result.message
  await reloadTodos()
}

async function recoverTodo(todo: DisposalTodo) {
  const remark = window.prompt(`整组恢复 ${todo.聚合编号} 的恢复结论（可留空自动生成）`) ?? ''
  const result = await incidentApi.recover(todo.聚合组ID, remark)
  todoMessage.value = result.message
  await reloadTodos()
}

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '采集计划登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('采集任务编排动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集任务编排操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('采集计划列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集任务编排列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void reloadTodos()
})
</script>
