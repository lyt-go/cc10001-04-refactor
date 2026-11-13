<template>
  <section class="page" data-module="scheduler">
    <header class="page-head">
      <div>
        <h2>调度计划管理</h2>
        <p class="page-desc">管理采集任务的周期性调度安排，配置执行时间、优先级和任务间依赖关系。审批状态决定任务能否进入待调度。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记调度计划</button>
        <button class="btn" type="button" @click="exportRows">导出调度计划清单</button>
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
        <span>审批状态</span>
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
          <th>审批状态（最终）</th>
          <th>关联任务运行情况</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :class="{ 'row-conflict': row.conflict }">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span class="status-badge" :class="statusClass(row.effective_status)">{{ row.effective_status }}</span>
            <span v-if="row.conflict" class="conflict-tag" title="计划已暂停但仍有关联任务在执行，两者结论冲突">状态冲突</span>
            <p v-if="row.blocked_reason" class="reason-text" :title="row.blocked_reason">{{ row.blocked_reason }}</p>
          </td>
          <td>
            <span v-if="!row.linked_task_count" class="muted-text">无关联任务</span>
            <span v-else>
              关联 {{ row.linked_task_count }} 条
              <template v-if="row.running_task_codes?.length">
                ，执行中：{{ row.running_task_codes.join('、') }}
              </template>
            </span>
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
          <td :colspan="columns.length + 3" class="empty-state">暂无调度计划数据，可先登记调度计划</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条调度计划记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | string[] | null | undefined> & {
  effective_status?: string
  blocked_reason?: string
  linked_task_count?: number
  running_task_codes?: string[]
  conflict?: boolean
}

const ENDPOINT = '/api/scheduler'
const columns = ["计划编号", "关联任务", "cron表达式", "生效日期", "失效日期", "优先级", "超时分钟", "依赖计划"]
const statuses = ["草稿", "计划审批中", "已排期", "已暂停"]

// 动作按当前审批状态收敛：只给当前状态下真正可执行的入口，减少无效/重复提交。
const ACTIONS_BY_STATUS: Record<string, string[]> = {
  '草稿': ['提交审批'],
  '计划审批中': ['确认排期'],
  '已排期': ['暂停计划', '确认排期'],
  '已暂停': ['确认排期', '暂停计划'],
}

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const statusFilter = ref('')
const filterFields = columns.slice(0, 3)

const stats = computed(() => [
  { label: '调度计划总数', value: total.value },
  { label: '已排期计划', value: rows.value.filter((r) => r.effective_status === '已排期').length },
  { label: '已暂停计划', value: rows.value.filter((r) => r.effective_status === '已暂停').length },
])

function availableActions(row: Row): string[] {
  return ACTIONS_BY_STATUS[row.effective_status ?? ''] ?? []
}

function statusClass(status?: string): string {
  if (status === '已排期') return 'badge-ok'
  if (status === '已暂停') return 'badge-warn'
  if (status === '计划审批中') return 'badge-review'
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
  errorMessage.value = '调度计划登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json().catch(() => null)
    // 后端业务拦截返回 200 + ok=false，消息里带可操作原因，需要原样展示。
    if (!response.ok || !payload?.ok) {
      throw new Error(payload?.message ?? '调度计划动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '调度计划操作失败'
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
      throw new Error('调度计划列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '调度计划列表读取失败'
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
.badge-warn { background: #fdece8; color: #b42318; }
.badge-review { background: #e8f0fe; color: #1f6feb; }
.badge-muted { background: #eef1f5; color: #64748b; }
.reason-text {
  margin: 4px 0 0;
  font-size: 12px;
  color: #b42318;
  max-width: 260px;
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
