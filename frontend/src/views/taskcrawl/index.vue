<template>
  <section class="page" data-module="taskcrawl">
    <header class="page-head">
      <div>
        <h2>采集任务管理</h2>
        <p class="page-desc">定义单次或周期性采集任务，关联采集源与解析规则，记录增量水位与批次边界。执行状态记录真实运行结果，能否调度以计划审批结论为准。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记采集任务</button>
        <button class="btn" type="button" @click="exportRows">导出采集任务清单</button>
      </div>
    </header>

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
      <label class="filter-item">
        <span>执行状态</span>
        <select v-model="statusFilter">
          <option value="">全部</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>执行状态（最终）</th>
          <th>计划审批结论 / 阻塞原因</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :class="{ 'row-conflict': row.conflict }">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span class="status-badge" :class="statusClass(row.effective_status)">{{ row.effective_status }}</span>
            <span v-if="row.conflict" class="conflict-tag" title="计划已暂停但任务仍在执行，两者结论冲突">状态冲突</span>
          </td>
          <td>
            <span v-if="row.plan_status" class="plan-tag">计划 {{ row.plan_code }}：{{ row.plan_status }}</span>
            <span v-else class="muted-text">无关联计划</span>
            <p v-if="row.blocked_reason" class="reason-text" :title="row.blocked_reason">{{ row.blocked_reason }}</p>
          </td>
          <td class="row-actions">
            <button
              v-for="action in availableActions(row)"
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
          <td :colspan="columns.length + 3" class="empty-state">暂无采集任务数据，可先登记采集任务</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集任务记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null | undefined> & {
  effective_status?: string
  plan_status?: string | null
  plan_code?: string | null
  blocked_reason?: string
  conflict?: boolean
}

const ENDPOINT = '/api/taskcrawl'
const columns = ["任务编号", "采集源名称", "规则编号", "调度周期", "增量字段", "批次大小", "起始水位", "截止水位"]
const statuses = ["草稿", "待调度", "执行中", "已完成", "已终止"]

// 动作按当前执行状态收敛；终态（已完成/已终止）没有任何可执行动作，终态不被改写。
const ACTIONS_BY_STATUS: Record<string, string[]> = {
  '草稿': ['提交调度'],
  '待调度': ['启动任务', '终止任务', '提交调度'],
  '执行中': ['终止任务'],
  '已完成': [],
  '已终止': [],
}

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const statusFilter = ref('')
const filterFields = columns.slice(0, 3)

const stats = computed(() => [
  { label: '采集任务总数', value: total.value },
  { label: '执行中任务', value: rows.value.filter((r) => r.effective_status === '执行中').length },
  { label: '待调度任务', value: rows.value.filter((r) => r.effective_status === '待调度').length },
])

function availableActions(row: Row): string[] {
  return ACTIONS_BY_STATUS[row.effective_status ?? ''] ?? []
}

function statusClass(status?: string): string {
  if (status === '已完成') return 'badge-ok'
  if (status === '执行中') return 'badge-running'
  if (status === '已终止') return 'badge-warn'
  if (status === '待调度') return 'badge-review'
  return 'badge-muted'
}

function resetFilters() {
  filters.value = {}
  statusFilter.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '采集任务登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json().catch(() => null)
    // 后端业务拦截（如计划未排期、任务已终止）返回 200 + ok=false，原因原样提示给用户。
    if (!response.ok || !payload?.ok) {
      throw new Error(payload?.message ?? '采集任务动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集任务操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams({
    ...(filters.value as Record<string, string>),
    ...(statusFilter.value ? { status: statusFilter.value } : {}),
  }).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('采集任务列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集任务列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.status-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 12px;
  white-space: nowrap;
}
.badge-ok { background: #e7f6ec; color: #1a7f37; }
.badge-running { background: #e8f0fe; color: #1f6feb; }
.badge-warn { background: #fdece8; color: #b42318; }
.badge-review { background: #fff4e0; color: #b25e09; }
.badge-muted { background: #eef1f5; color: #64748b; }
.plan-tag {
  display: inline-block;
  font-size: 12px;
  color: #475569;
  background: #f1f5f9;
  border-radius: 6px;
  padding: 1px 6px;
}
.reason-text {
  margin: 4px 0 0;
  font-size: 12px;
  color: #b42318;
  max-width: 280px;
  line-height: 1.4;
}
.conflict-tag {
  margin-left: 6px;
  font-size: 11px;
  color: #b42318;
  background: #fdece8;
  border: 1px solid #f3b4a8;
  border-radius: 8px;
  padding: 1px 6px;
}
.row-conflict {
  background: #fff8f6;
}
.muted-text { color: var(--muted); font-size: 12px; }
</style>
