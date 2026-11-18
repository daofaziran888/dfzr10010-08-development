<template>
  <section class="page" data-module="migration-audit">
    <header class="page-head">
      <div>
        <h2>迁移审计清单</h2>
        <p class="page-desc">
          审计清单与其它业务入口基于同一份源目标一致性校验结果，保存目标表映射；任一入口回写处理结论都会同步到迁移审计清单，刷新后两边批次与结论一致。
        </p>
      </div>
      <button class="btn" type="button" @click="reload">刷新清单</button>
    </header>

    <!-- 两个业务入口切换：底层都是同一份校验结果 -->
    <nav class="entry-tabs">
      <button
        v-for="entry in entries"
        :key="entry.key"
        class="tab-btn"
        :class="{ active: activeEntry === entry.key }"
        type="button"
        @click="switchEntry(entry.key)"
      >
        {{ entry.label }}
      </button>
    </nav>

    <div class="filter-bar">
      <label class="filter-item">
        <span>迁移任务</span>
        <select v-model="taskFilter">
          <option value="">全部任务</option>
          <option v-for="task in tasks" :key="task.task_id" :value="task.task_id">
            {{ task.task_code }}
          </option>
        </select>
      </label>
      <label class="filter-item">
        <span>审计状态</span>
        <select v-model="statusFilter">
          <option value="">全部状态</option>
          <option v-for="status in auditStatuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <span class="entry-hint">当前入口：{{ activeEntryLabel }} —— 结论将通过该入口回写</span>
    </div>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">审计条目</span>
        <strong class="stat-value">{{ audits.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待审计</span>
        <strong class="stat-value warn">{{ countByStatus('待审计') }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">审计通过</span>
        <strong class="stat-value ok">{{ countByStatus('审计通过') }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">审计驳回</span>
        <strong class="stat-value danger">{{ countByStatus('审计驳回') }}</strong>
      </article>
    </div>

    <table class="data-table">
      <thead>
        <tr>
          <th>审计编号</th>
          <th>迁移任务</th>
          <th>分段</th>
          <th>逻辑批次</th>
          <th>校验结果</th>
          <th>校验是否通过</th>
          <th>目标表映射</th>
          <th>审计状态</th>
          <th>最近回写入口</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in filteredAudits" :key="item.audit_id">
          <td>{{ item.audit_id }}</td>
          <td>{{ item.task_code }}<div class="muted small">{{ item.task_id }}</div></td>
          <td>{{ item.segment_name }}</td>
          <td class="small">{{ item.batch_id }}</td>
          <td>
            <button class="link" type="button" @click="openDetail(item.check_result_id)">
              {{ item.check_result_id }}
            </button>
          </td>
          <td>
            <span class="badge" :class="item.check_passed ? 'badge-green' : 'badge-red'">
              {{ item.check_passed ? '校验通过' : '校验未通过' }}
            </span>
          </td>
          <td class="mapping-cell">
            <span v-for="(target, source) in item.target_table_mapping" :key="source" class="mapping-chip">
              {{ source }}→{{ target }}
            </span>
          </td>
          <td><span class="badge" :class="auditStatusClass(item.status)">{{ item.status }}</span></td>
          <td class="small">{{ item.source_entry }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="conclude(item, '审计通过')">通过</button>
            <button class="link danger-link" type="button" @click="conclude(item, '审计驳回')">驳回</button>
          </td>
        </tr>
        <tr v-if="!filteredAudits.length">
          <td colspan="10" class="empty-state">暂无审计条目，先在分段迁移工作台提交迁移批次生成校验结果</td>
        </tr>
      </tbody>
    </table>

    <p v-if="message" class="notice" :class="messageKind">{{ message }}</p>

    <!-- 审计详情：查看源目标一致性校验结果，可返回迁移列表 -->
    <div v-if="detail" class="modal-mask" @click.self="closeDetail">
      <div class="modal-card">
        <div class="modal-head">
          <h3>审计详情 · {{ detail.check_result.result_id }}</h3>
          <button class="btn" type="button" @click="closeDetail">返回迁移列表</button>
        </div>
        <p class="muted small">
          所属任务 {{ detail.check_result.task_code }} ｜ 分段 {{ detail.check_result.segment_name }} ｜
          逻辑批次 {{ detail.check_result.batch_id }}
        </p>

        <div class="result-block">
          <h4>源目标一致性校验结果（全平台唯一一份）</h4>
          <p class="small">{{ detail.check_result.rule_desc }}</p>
          <table class="data-table check-table">
            <thead>
              <tr><th>校验项</th><th>源端</th><th>目标端</th><th>结果</th></tr>
            </thead>
            <tbody>
              <tr>
                <td>行数</td>
                <td>{{ formatNumber(detail.check_result.source_row_count) }}</td>
                <td>{{ formatNumber(detail.check_result.target_row_count) }}</td>
                <td><span class="badge" :class="detail.check_result.row_count_match ? 'badge-green' : 'badge-red'">
                  {{ detail.check_result.row_count_match ? '一致' : '不一致' }}
                </span></td>
              </tr>
              <tr>
                <td>去重数</td>
                <td>{{ formatNumber(detail.check_result.source_distinct_key) }}</td>
                <td>{{ formatNumber(detail.check_result.target_distinct_key) }}</td>
                <td><span class="badge" :class="detail.check_result.distinct_match ? 'badge-green' : 'badge-red'">
                  {{ detail.check_result.distinct_match ? '一致' : '不一致' }}
                </span></td>
              </tr>
              <tr>
                <td>金额汇总</td>
                <td>{{ formatAmount(detail.check_result.source_amount_sum) }}</td>
                <td>{{ formatAmount(detail.check_result.target_amount_sum) }}</td>
                <td><span class="badge" :class="detail.check_result.amount_match ? 'badge-green' : 'badge-red'">
                  {{ detail.check_result.amount_match ? '一致' : '不一致' }}
                </span></td>
              </tr>
            </tbody>
          </table>
          <p class="conclusion-text">{{ detail.check_result.conclusion }}</p>
        </div>

        <div v-if="detail.audit_item" class="result-block">
          <h4>迁移审计清单回写</h4>
          <p class="small muted">
            审计编号 {{ detail.audit_item.audit_id }} ｜ 当前状态
            <span class="badge" :class="auditStatusClass(detail.audit_item.status)">{{ detail.audit_item.status }}</span>
            ｜ 最近入口 {{ detail.audit_item.source_entry }}
          </p>
          <p v-if="detail.audit_item.conclusion_remark" class="small">备注：{{ detail.audit_item.conclusion_remark }}</p>
          <div class="result-block-actions">
            <button class="btn primary" type="button" @click="conclude(detail.audit_item!, '审计通过')">审计通过</button>
            <button class="btn" type="button" @click="conclude(detail.audit_item!, '审计驳回')">审计驳回</button>
          </div>
        </div>

        <div class="result-block">
          <h4>回写痕迹（两个入口共用同一结果）</h4>
          <ol v-if="detail.check_result.conclusions.length" class="conclusion-list">
            <li v-for="(item, index) in detail.check_result.conclusions" :key="index">
              {{ item.at }} · {{ item.entry }} · {{ item.action }}
              <span v-if="item.remark" class="muted">（{{ item.remark }}）</span>
            </li>
          </ol>
          <p v-else class="muted small">暂无处理结论。</p>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  getCheckResult,
  listAudits,
  listTasks,
  writeAuditConclusion,
  writeExternalConclusion,
  type AuditItem,
  type AuditStatus,
  type CheckResult,
  type MigrationTask,
} from '@/api/migration'

type EntryKey = 'audit' | 'external'
const entries: { key: EntryKey; label: string }[] = [
  { key: 'audit', label: '迁移审计清单入口' },
  { key: 'external', label: '其它业务入口' },
]
const auditStatuses: AuditStatus[] = ['待审计', '审计通过', '审计驳回']

const activeEntry = ref<EntryKey>('audit')
const tasks = ref<MigrationTask[]>([])
const audits = ref<AuditItem[]>([])
const taskFilter = ref('')
const statusFilter = ref('')
const message = ref('')
const messageKind = ref<'ok' | 'err'>('ok')
const detail = ref<{ check_result: CheckResult; audit_item: AuditItem | null } | null>(null)
const detailResultId = ref('')

const activeEntryLabel = computed(() => entries.find((e) => e.key === activeEntry.value)?.label ?? '')
const filteredAudits = computed(() =>
  audits.value.filter(
    (item) =>
      (!taskFilter.value || item.task_id === taskFilter.value) &&
      (!statusFilter.value || item.status === statusFilter.value),
  ),
)

function notify(text: string, kind: 'ok' | 'err' = 'ok') {
  message.value = text
  messageKind.value = kind
}

function countByStatus(status: AuditStatus): number {
  return audits.value.filter((item) => item.status === status).length
}

function auditStatusClass(status: string): string {
  if (status === '审计通过') return 'badge-green'
  if (status === '审计驳回') return 'badge-yellow'
  return 'badge-blue'
}

function formatNumber(value: number): string {
  return value.toLocaleString('zh-CN')
}

function formatAmount(value: number): string {
  return value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

async function reload() {
  try {
    const [taskPayload, auditPayload] = await Promise.all([listTasks(), listAudits()])
    tasks.value = taskPayload.items
    audits.value = auditPayload.items
  } catch (error) {
    notify(error instanceof Error ? error.message : '审计清单读取失败', 'err')
  }
}

function switchEntry(key: EntryKey) {
  activeEntry.value = key
  // 切换入口后刷新：验证两边看到的批次与结论完全相同
  void reload()
}

async function openDetail(resultId: string) {
  detailResultId.value = resultId
  try {
    detail.value = await getCheckResult(resultId)
  } catch (error) {
    notify(error instanceof Error ? error.message : '校验结果读取失败', 'err')
  }
}

async function closeDetail() {
  detail.value = null
  detailResultId.value = ''
  // 从审计详情返回迁移列表时刷新，保证清单与另一入口同批次同结论
  await reload()
}

async function conclude(item: AuditItem, conclusion: AuditStatus) {
  const remark = window.prompt(
    `请输入「${conclusion}」处理说明（入口：${activeEntryLabel.value}）`,
    activeEntry.value === 'audit' ? '审计清单复核' : '其它业务入口同步校验结论',
  )
  if (remark === null) return
  try {
    const payload = activeEntry.value === 'audit'
      ? await writeAuditConclusion(item.check_result_id, conclusion, remark)
      : await writeExternalConclusion(item.check_result_id, conclusion, remark)
    if (!payload.ok) {
      notify(payload.message, 'err')
      return
    }
    notify(`处理结论已通过${activeEntryLabel.value}回写迁移审计清单：${conclusion}`)
    await reload()
    if (detail.value && detailResultId.value === item.check_result_id) {
      detail.value = await getCheckResult(item.check_result_id)
    }
  } catch (error) {
    notify(error instanceof Error ? error.message : '结论回写失败', 'err')
  }
}

onMounted(reload)
</script>

<style scoped>
.entry-tabs { display: flex; gap: 8px; margin-bottom: 12px; }
.tab-btn { border: 1px solid var(--border); background: #fff; border-radius: 6px; padding: 8px 16px; cursor: pointer; font-size: 13px; }
.tab-btn.active { background: var(--brand); border-color: var(--brand); color: #fff; }
.entry-hint { font-size: 12px; color: var(--muted); align-self: center; }
.mapping-cell { max-width: 260px; }
.mapping-chip { display: inline-block; background: #f2f4f7; border-radius: 4px; padding: 1px 6px; margin: 1px 2px; font-size: 12px; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; white-space: nowrap; }
.badge-green { background: #ecfdf3; color: #067647; }
.badge-yellow { background: #fef7e0; color: #9a6700; }
.badge-red { background: #fef3f2; color: #b42318; }
.badge-blue { background: #eff8ff; color: #175cd3; }
.warn { color: #b54708; }
.ok { color: #067647; }
.danger { color: #b42318; }
.danger-link { color: #b42318; }
.small { font-size: 12px; }
.muted { color: var(--muted); }
.notice { padding: 8px 12px; border-radius: 6px; font-size: 13px; margin-top: 10px; }
.notice.ok { background: #ecfdf3; color: #067647; border: 1px solid #abefc6; }
.notice.err { background: #fef3f2; color: #b42318; border: 1px solid #fda29b; }
.modal-mask { position: fixed; inset: 0; background: rgba(16, 24, 40, 0.45); display: flex; align-items: center; justify-content: center; z-index: 20; }
.modal-card { background: #fff; border-radius: 10px; width: 760px; max-width: 92vw; max-height: 86vh; overflow: auto; padding: 18px; }
.modal-head { display: flex; justify-content: space-between; align-items: center; }
.result-block { border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-top: 12px; }
.result-block h4 { margin: 0 0 8px; font-size: 13px; }
.result-block-actions { display: flex; gap: 8px; margin-top: 8px; }
.check-table { margin-top: 8px; }
.conclusion-text { font-size: 13px; margin: 10px 0 0; background: #f8fafc; padding: 8px 10px; border-radius: 6px; }
.conclusion-list { margin: 4px 0 0; padding-left: 18px; font-size: 12px; }
.filter-item select { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; min-width: 160px; }
</style>
