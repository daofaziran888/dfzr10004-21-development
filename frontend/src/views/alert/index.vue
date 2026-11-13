<template>
  <section class="page" data-module="alert">
    <header class="page-head">
      <div>
        <h2>采集告警中心 · 告警聚合与恢复</h2>
        <p class="page-desc">
          按监控指标、告警级别与采集计划合并相邻失败事件，静默期内不重复提醒；
          达到阈值进入处置清单，恢复时下发带结论的恢复通知，历史告警全程可追溯。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="reloadAll">刷新聚合结论</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in statCards" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <nav class="tab-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="tab-item"
        :class="{ active: activeTab === tab.key }"
        type="button"
        @click="activeTab = tab.key"
      >
        {{ tab.label }}
      </button>
    </nav>

    <!-- 聚合组面板：批量选择、多条合并、逐条确认、整组恢复 -->
    <div v-if="activeTab === 'groups'">
      <form class="filter-bar" @submit.prevent="reloadGroups">
        <label class="filter-item">
          <span>聚合状态</span>
          <select v-model="groupFilter.status">
            <option value="">全部</option>
            <option value="聚合中">聚合中</option>
            <option value="已恢复">已恢复</option>
            <option value="已合并">已合并</option>
          </select>
        </label>
        <label class="filter-item">
          <span>检索</span>
          <input v-model="groupFilter.keyword" placeholder="监控指标 / 聚合编号 / 采集计划" />
        </label>
        <button class="btn" type="submit">查询</button>
      </form>

      <div class="batch-bar">
        <label class="select-all">
          <input type="checkbox" :checked="allActiveChecked" @change="toggleAll" />
          全选当前活跃组（已选 {{ selectedIds.length }} 条）
        </label>
        <button class="btn primary" type="button" :disabled="selectedIds.length < 2" @click="submitMerge">
          多条合并（{{ selectedIds.length }}）
        </button>
        <button class="btn" type="button" :disabled="!selectedIds.length" @click="submitBatch('确认')">
          批量确认
        </button>
        <button class="btn" type="button" :disabled="!selectedIds.length" @click="submitBatch('恢复')">
          整组恢复
        </button>
        <button class="btn ghost" type="button" :disabled="!selectedIds.length" @click="submitSkip">
          跳过选中
        </button>
      </div>

      <!-- 一次提交后分批展示结果：每条一行，成功/失败/跳过分别着色，可继续多选处理 -->
      <div v-if="batchResults.length" class="result-panel">
        <div class="result-head">
          <strong>本次提交分批结果</strong>
          <span class="result-summary">
            <template v-for="(count, key) in lastSummary" :key="key">
              <em class="result-chip">{{ key }} {{ count }}</em>
            </template>
          </span>
          <button class="link" type="button" @click="batchResults = []">关闭结果</button>
        </div>
        <ul class="result-list">
          <li v-for="(row, index) in batchResults" :key="index" :class="row.ok ? 'ok' : 'fail'">
            <span class="result-action">{{ row.action }}</span>
            <span class="result-target">{{ formatTarget(row.group_id) }}</span>
            <span class="result-message">{{ row.message }}</span>
          </li>
        </ul>
      </div>

      <table class="data-table">
        <thead>
          <tr>
            <th style="width: 42px">选择</th>
            <th v-for="column in groupColumns" :key="column">{{ column }}</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in groups" :key="row.id" :class="{ muted: row.状态 !== '聚合中' }">
            <td>
              <input
                v-if="row.状态 === '聚合中'"
                type="checkbox"
                :checked="selectedIds.includes(row.id)"
                @change="toggleOne(row.id)"
              />
            </td>
            <td>{{ row.聚合编号 }}</td>
            <td>{{ row.监控指标 }}</td>
            <td><span class="badge" :class="levelClass(row.告警级别)">{{ row.告警级别 }}</span></td>
            <td>{{ row.采集计划 }}</td>
            <td><span class="badge" :class="statusClass(row.状态)">{{ row.状态 }}</span></td>
            <td>{{ row.累计次数 }} / 阈值 {{ row.告警阈值 }}</td>
            <td>{{ row.首次触发 }}<br /><span class="muted-text">最近 {{ row.最近触发 }}</span></td>
            <td>
              <span v-if="isSilenced(row)" class="badge silence">静默中 · {{ silenceLeft(row) }}</span>
              <span v-else class="muted-text">{{ row.静默截止时间 ?? '—' }}</span>
            </td>
            <td>
              <span class="badge" :class="escalationClass(row.升级状态)">{{ row.升级状态 }}</span>
              <div class="muted-text">{{ row.升级优先级 }} · 第 {{ row.静默批次 }} 批次</div>
            </td>
            <td class="row-actions">
              <button class="link" type="button" @click="openDetail(row)">明细</button>
              <button
                v-if="row.状态 === '聚合中'"
                class="link"
                type="button"
                @click="ackOne(row)"
              >逐条确认</button>
              <button
                v-if="row.状态 === '聚合中'"
                class="link danger"
                type="button"
                @click="recoverOne(row)"
              >整组恢复</button>
              <button
                v-if="row.状态 === '聚合中'"
                class="link"
                type="button"
                @click="skipOne(row)"
              >跳过</button>
            </td>
          </tr>
          <tr v-if="!groups.length">
            <td :colspan="groupColumns.length + 2" class="empty-state">暂无聚合组，可在「事件模拟上报」里制造采集失败</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 通知面板：首次/再次告警、升级、恢复、确认、合并通知 -->
    <div v-else-if="activeTab === 'notifications'">
      <table class="data-table">
        <thead>
          <tr><th v-for="column in noteColumns" :key="column">{{ column }}</th></tr>
        </thead>
        <tbody>
          <tr v-for="note in notifications" :key="note.id">
            <td>{{ note.通知时间 }}</td>
            <td><span class="badge" :class="noteClass(note.通知类型)">{{ note.通知类型 }}</span></td>
            <td>{{ note.聚合编号 }}</td>
            <td>{{ note.通知标题 }}</td>
            <td class="conclusion-cell">{{ note.通知内容 }}</td>
            <td>{{ note.通知渠道 }}</td>
          </tr>
          <tr v-if="!notifications.length">
            <td :colspan="noteColumns.length" class="empty-state">暂无通知记录</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 历史事件：恢复/合并后仍可追溯 -->
    <div v-else-if="activeTab === 'events'">
      <form class="filter-bar" @submit.prevent="reloadEvents">
        <label class="filter-item">
          <span>聚合组ID（留空查全部）</span>
          <input v-model.number="eventFilter.groupId" type="number" placeholder="例如 1" />
        </label>
        <button class="btn" type="submit">查询事件轨迹</button>
      </form>
      <table class="data-table">
        <thead>
          <tr><th v-for="column in eventColumns" :key="column">{{ column }}</th></tr>
        </thead>
        <tbody>
          <tr v-for="event in events" :key="event.id">
            <td>{{ event.事件编号 }}</td>
            <td>{{ event.监控指标 }}</td>
            <td><span class="badge" :class="levelClass(event.告警级别)">{{ event.告警级别 }}</span></td>
            <td>{{ event.采集计划 }}</td>
            <td><span class="badge" :class="event.事件类型 === '恢复' ? 'recovered' : 'firing'">{{ event.事件类型 }}</span></td>
            <td>{{ event.发生时间 }}</td>
            <td>{{ event.事件摘要 }}</td>
          </tr>
          <tr v-if="!events.length">
            <td :colspan="eventColumns.length" class="empty-state">暂无事件记录</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 事件模拟上报：手工制造失败/恢复事件，验证聚合与恢复链路 -->
    <div v-else-if="activeTab === 'simulate'" class="simulate-panel">
      <form class="simulate-form" @submit.prevent="submitIngest">
        <label class="filter-item">
          <span>监控指标（对应 ALER-0002）</span>
          <input v-model="simForm.监控指标" required placeholder="页面抓取成功率" />
        </label>
        <label class="filter-item">
          <span>告警级别</span>
          <select v-model="simForm.告警级别">
            <option>紧急</option>
            <option>重要</option>
            <option>一般</option>
          </select>
        </label>
        <label class="filter-item">
          <span>采集计划（对应 TASK-0002）</span>
          <input v-model="simForm.采集计划" required placeholder="TASK-0002" />
        </label>
        <label class="filter-item">
          <span>事件类型</span>
          <select v-model="simForm.事件类型">
            <option>失败</option>
            <option>恢复</option>
          </select>
        </label>
        <label class="filter-item">
          <span>事件编号（重复编号只并入同组）</span>
          <input v-model="simForm.事件编号" placeholder="留空由系统生成" />
        </label>
        <label class="filter-item wide">
          <span>事件摘要 / 恢复结论</span>
          <input v-model="simForm.事件摘要" placeholder="恢复事件时会作为恢复结论写入恢复通知" />
        </label>
        <button class="btn primary" type="submit">提交事件</button>
      </form>
      <p v-if="ingestMessage" class="ingest-message">{{ ingestMessage }}</p>
      <ul class="rule-hint">
        <li>静默时长：紧急 5 分钟、重要 15 分钟、一般 30 分钟；级别越高静默越短、升级优先级越高（P1/P2/P3）。</li>
        <li>默认阈值 3 次：累计失败达到阈值后采集计划处置清单生成待办，并尝试占用全局唯一升级名额。</li>
      </ul>
    </div>

    <!-- 原有告警规则清单 -->
    <div v-else>
      <form class="filter-bar" @submit.prevent="reloadRules">
        <label class="filter-item">
          <span>规则编号</span>
          <input v-model="ruleKeyword" placeholder="按规则编号检索" />
        </label>
        <button class="btn" type="submit">查询</button>
      </form>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in ruleColumns" :key="column">{{ column }}</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in ruleRows" :key="String(row.id)">
            <td v-for="column in ruleColumns" :key="column">{{ row[column] ?? '—' }}</td>
            <td class="row-actions">
              <button v-for="action in ruleActions" :key="action" class="link" type="button" @click="runRuleAction(action, row)">
                {{ action }}
              </button>
            </td>
          </tr>
          <tr v-if="!ruleRows.length">
            <td :colspan="ruleColumns.length + 1" class="empty-state">暂无告警规则数据</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 聚合组明细抽屉：通知 + 事件轨迹 -->
    <div v-if="detailGroup" class="drawer-mask" @click.self="detailGroup = null">
      <div class="drawer">
        <header class="drawer-head">
          <strong>{{ detailGroup.聚合编号 }} · 聚合明细</strong>
          <button class="link" type="button" @click="detailGroup = null">关闭</button>
        </header>
        <div class="drawer-body">
          <p v-if="detailGroup.恢复结论" class="conclusion-box">
            <strong>恢复结论：</strong>{{ detailGroup.恢复结论 }}
          </p>
          <h4>通知记录</h4>
          <ul class="drawer-list">
            <li v-for="note in detailNotifications" :key="note.id">
              <span class="badge" :class="noteClass(note.通知类型)">{{ note.通知类型 }}</span>
              {{ note.通知时间 }} — {{ note.通知内容 }}
            </li>
            <li v-if="!detailNotifications.length" class="muted-text">暂无通知</li>
          </ul>
          <h4>事件轨迹（历史可追溯）</h4>
          <ul class="drawer-list">
            <li v-for="event in detailEvents" :key="event.id">
              <span class="badge" :class="event.事件类型 === '恢复' ? 'recovered' : 'firing'">{{ event.事件类型 }}</span>
              {{ event.发生时间 }} — {{ event.事件摘要 }}（{{ event.事件编号 }}）
            </li>
          </ul>
        </div>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ groups.length }} 个聚合组 · {{ notifications.length }} 条通知 · {{ events.length }} 条事件</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'

import {
  incidentApi,
  type AlertEvent,
  type AlertGroup,
  type AlertNotification,
  type BatchResultRow,
} from '@/api/incident'
import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const tabs = [
  { key: 'groups', label: '聚合告警' },
  { key: 'notifications', label: '通知记录' },
  { key: 'events', label: '历史事件' },
  { key: 'simulate', label: '事件模拟上报' },
  { key: 'rules', label: '告警规则清单' },
] as const

const activeTab = ref<(typeof tabs)[number]['key']>('groups')

const groups = ref<AlertGroup[]>([])
const notifications = ref<AlertNotification[]>([])
const events = ref<AlertEvent[]>([])
const stats = ref<Record<string, number>>({})
const selectedIds = ref<number[]>([])
const batchResults = ref<BatchResultRow[]>([])
const lastSummary = ref<Record<string, number>>({})
const errorMessage = ref('')
const ingestMessage = ref('')
const detailGroup = ref<AlertGroup | null>(null)
const detailNotifications = ref<AlertNotification[]>([])
const detailEvents = ref<AlertEvent[]>([])
const now = ref(Date.now())
const timer = window.setInterval(() => { now.value = Date.now() }, 1000)
onUnmounted(() => window.clearInterval(timer))

const groupFilter = reactive({ status: '聚合中', keyword: '' })
const eventFilter = reactive({ groupId: undefined as number | undefined })

const groupColumns = [
  '聚合编号', '监控指标', '告警级别', '采集计划', '状态', '累计次数', '触发时间', '静默截止时间', '升级状态',
]
const noteColumns = ['通知时间', '通知类型', '聚合编号', '通知标题', '通知内容', '通知渠道']
const eventColumns = ['事件编号', '监控指标', '告警级别', '采集计划', '事件类型', '发生时间', '事件摘要']

const ruleColumns = ['规则编号', '监控指标', '告警级别', '触发条件', '通知渠道', '静默时间', '升级策略', '是否启用']
const ruleActions = ['提交校验', '启用规则', '停用规则']
const ruleRows = ref<Row[]>([])
const ruleKeyword = ref('')

const simForm = reactive({
  监控指标: '页面抓取成功率',
  告警级别: '重要',
  采集计划: 'TASK-0002',
  事件类型: '失败',
  事件编号: '',
  事件摘要: '',
})

const statCards = computed(() => [
  { label: '活跃聚合组', value: stats.value['活跃聚合组'] ?? 0 },
  { label: '静默中', value: stats.value['静默中'] ?? 0 },
  { label: '待处置待办', value: stats.value['待处置待办'] ?? 0 },
  { label: '通知总数', value: stats.value['通知总数'] ?? 0 },
  { label: '升级名额占用', value: stats.value['升级名额占用'] ?? 0 },
])

const activeGroups = computed(() => groups.value.filter((g) => g.状态 === '聚合中'))
const allActiveChecked = computed(
  () => activeGroups.value.length > 0 && activeGroups.value.every((g) => selectedIds.value.includes(g.id)),
)

function isSilenced(group: AlertGroup): boolean {
  return Boolean(group.静默截止时间) && group.状态 === '聚合中'
    && new Date(group.静默截止时间 as string).getTime() > now.value
}

function silenceLeft(group: AlertGroup): string {
  const ms = new Date(group.静默截止时间 as string).getTime() - now.value
  if (ms <= 0) return '已到期'
  const minutes = Math.floor(ms / 60000)
  const seconds = Math.floor((ms % 60000) / 1000)
  return `剩 ${minutes}分${seconds}秒`
}

function levelClass(level: string): string {
  return { 紧急: 'critical', 重要: 'major', 一般: 'minor' }[level] ?? 'minor'
}
function statusClass(status: string): string {
  return { 聚合中: 'firing', 已恢复: 'recovered', 已合并: 'merged' }[status] ?? ''
}
function escalationClass(status: string): string {
  return { 已升级: 'critical', 升级被拒: 'merged', 未升级: 'minor' }[status] ?? 'minor'
}
function noteClass(kind: string): string {
  if (kind.includes('恢复')) return 'recovered'
  if (kind.includes('升级')) return 'critical'
  if (kind.includes('确认') || kind.includes('合并')) return 'merged'
  return 'firing'
}
function formatTarget(target: number | number[]): string {
  return Array.isArray(target) ? `聚合组 ${target.join('、')}` : `聚合组 ${target}`
}

function toggleOne(id: number) {
  const index = selectedIds.value.indexOf(id)
  if (index >= 0) selectedIds.value.splice(index, 1)
  else selectedIds.value.push(id)
}
function toggleAll() {
  selectedIds.value = allActiveChecked.value ? [] : activeGroups.value.map((g) => g.id)
}

async function reloadGroups() {
  const params: Record<string, string> = {}
  if (groupFilter.status) params.status = groupFilter.status
  if (groupFilter.keyword) params.keyword = groupFilter.keyword
  const payload = await incidentApi.groups(params)
  groups.value = payload.items
  selectedIds.value = selectedIds.value.filter((id) =>
    payload.items.some((g) => g.id === id && g.状态 === '聚合中'))
}
async function reloadNotifications() {
  notifications.value = (await incidentApi.notifications()).items
}
async function reloadEvents() {
  events.value = (await incidentApi.events(eventFilter.groupId)).items
}
async function reloadStats() {
  stats.value = await incidentApi.stats()
}
async function reloadAll() {
  errorMessage.value = ''
  try {
    await Promise.all([reloadGroups(), reloadNotifications(), reloadStats()])
    if (activeTab.value === 'events') await reloadEvents()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '聚合数据读取失败'
  }
}

async function applyBatchResponse(promise: Promise<{ ok: boolean; summary: Record<string, number>; results: BatchResultRow[] }>) {
  const payload = await promise
  lastSummary.value = payload.summary
  batchResults.value = payload.results
  selectedIds.value = []
  await reloadAll()
}

async function submitBatch(action: '确认' | '恢复') {
  const remark = window.prompt(`批量${action}备注（可留空）`) ?? ''
  await applyBatchResponse(incidentApi.batch(
    selectedIds.value.map((group_id) => ({ group_id, action, remark })),
  ))
}
async function submitSkip() {
  // 用户可以只跳过单条：这里把当前多选统一标记为跳过，逐条返回结果
  await applyBatchResponse(incidentApi.batch(
    selectedIds.value.map((group_id) => ({ group_id, action: '跳过' })),
  ))
}
async function skipOne(group: AlertGroup) {
  // 只跳过单条：走同一批量接口，结果面板只展示这一条
  await applyBatchResponse(incidentApi.batch([{ group_id: group.id, action: '跳过' }]))
}
async function submitMerge() {
  if (selectedIds.value.length < 2) return
  const result = await incidentApi.merge(selectedIds.value)
  lastSummary.value = { 合并: result.ok ? 1 : 0 }
  batchResults.value = [{ group_id: selectedIds.value, action: '合并', ok: result.ok, message: result.message }]
  selectedIds.value = []
  await reloadAll()
}
async function ackOne(group: AlertGroup) {
  const remark = window.prompt(`确认聚合组 ${group.聚合编号} 的处置备注（可留空）`) ?? ''
  const result = await incidentApi.acknowledge(group.id, remark)
  lastSummary.value = { 确认: result.ok ? 1 : 0, 失败: result.ok ? 0 : 1 }
  batchResults.value = [{ group_id: group.id, action: '确认', ok: result.ok, message: result.message }]
  await reloadAll()
}
async function recoverOne(group: AlertGroup) {
  const remark = window.prompt(`整组恢复 ${group.聚合编号} 的恢复结论（可留空自动生成）`) ?? ''
  const result = await incidentApi.recover(group.id, remark)
  lastSummary.value = { 恢复: result.ok ? 1 : 0, 失败: result.ok ? 0 : 1 }
  batchResults.value = [{ group_id: group.id, action: '恢复', ok: result.ok, message: result.message }]
  await reloadAll()
}

async function openDetail(group: AlertGroup) {
  detailGroup.value = group
  const [notes, evts] = await Promise.all([
    incidentApi.notifications(group.id),
    incidentApi.events(group.id),
  ])
  detailNotifications.value = notes.items
  detailEvents.value = evts.items
}

async function submitIngest() {
  errorMessage.value = ''
  const values: Record<string, string> = { ...simForm }
  if (!values.事件编号) delete values.事件编号
  if (!values.事件摘要) delete values.事件摘要
  try {
    const result = await incidentApi.ingest(values)
    ingestMessage.value = result.message
    await reloadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '事件上报失败'
  }
}

async function reloadRules() {
  const query = ruleKeyword.value ? `?keyword=${encodeURIComponent(ruleKeyword.value)}` : ''
  const response = await request(`/api/alert${query}`)
  if (!response.ok) throw new Error('告警规则列表读取失败')
  ruleRows.value = (await response.json()).items
}

async function runRuleAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`/api/alert/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const result = await response.json()
    if (!result.ok) throw new Error(result.message)
    await reloadRules()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '告警规则操作失败'
  }
}

onMounted(async () => {
  await Promise.all([reloadAll(), reloadRules()])
})
</script>
