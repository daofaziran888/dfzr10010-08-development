<template>
  <section class="page" data-module="migration-workbench">
    <header class="page-head">
      <div>
        <h2>分段迁移工作台</h2>
        <p class="page-desc">
          读取迁移任务与数据范围，按逻辑迁移批次提交数据分段；每段数据迁移、源目标一致性校验与血缘写入同批完成才形成完整提交边界，重复提交幂等不追加记录。
        </p>
      </div>
    </header>

    <div class="workbench">
      <!-- 左：任务列表与进度 -->
      <aside class="task-pane">
        <h3 class="pane-title">迁移任务（{{ tasks.length }}）</h3>
        <button class="btn block" type="button" @click="reloadTasks">刷新任务进度</button>
        <ul class="task-list">
          <li
            v-for="task in tasks"
            :key="task.task_id"
            class="task-card"
            :class="{ active: task.task_id === activeTaskId }"
            @click="selectTask(task.task_id)"
          >
            <div class="task-card-head">
              <strong>{{ task.task_code }}</strong>
              <span class="badge" :class="statusClass(task.status)">{{ task.status }}</span>
            </div>
            <div class="task-card-line">{{ task.source_table }} → {{ task.target_table }}</div>
            <div class="task-card-line muted">
              {{ task.segment_count }} 段 · 每段 {{ formatNumber(task.rows_per_segment) }} 行
            </div>
            <div class="progress-track">
              <div class="progress-fill" :style="{ width: `${task.progress.percent}%` }"></div>
            </div>
            <div class="task-card-line muted">
              已发布 {{ task.progress.published_count }}/{{ task.segment_count }}
              （{{ task.progress.percent }}%）· 续做位置 第 {{ task.progress.next_resume_seq }} 段
            </div>
            <div v-if="task.progress.completed" class="complete-flag">数据与血缘同版，已完成</div>
          </li>
        </ul>
      </aside>

      <!-- 右：任务明细与分段操作 -->
      <div class="detail-pane" v-if="activeTask">
        <div class="detail-head">
          <div>
            <h3>{{ activeTask.task_code }} · 数据范围与校验口径</h3>
            <p class="muted detail-sub">
              {{ activeTask.source_table }} → {{ activeTask.target_table }} ｜
              数据范围：{{ activeTask.data_scope }} ｜
              目标分区 {{ activeTask.partition_start }} 至 {{ activeTask.partition_end }}
            </p>
          </div>
          <button class="btn" type="button" @click="reloadDetail">刷新明细</button>
        </div>

        <div class="info-grid">
          <div class="info-item"><span>去重键</span><strong>{{ activeTask.dedup_key }}</strong></div>
          <div class="info-item"><span>金额字段</span><strong>{{ activeTask.amount_field }}</strong></div>
          <div class="info-item"><span>冲突策略</span><strong>{{ activeTask.conflict_policy }}</strong></div>
          <div class="info-item">
            <span>目标表映射</span>
            <strong>
              <template v-for="(target, source, index) in activeTask.target_table_mapping" :key="source">
                {{ source }}→{{ target }}<template v-if="index < Object.keys(activeTask.target_table_mapping).length - 1">；</template>
              </template>
            </strong>
          </div>
          <div class="info-item info-wide">
            <span>校验规则</span>
            <strong>{{ activeTask.check_rules.join('；') }}</strong>
          </div>
          <div class="info-item info-wide">
            <span>故障注入（演示用）</span>
            <strong>
              血缘失败分段：{{ activeTask.lineage_fail_seqs.length ? activeTask.lineage_fail_seqs.join('、') : '无' }}
              ｜ 校验失败分段：{{ activeTask.check_fail_seqs.length ? activeTask.check_fail_seqs.join('、') : '无' }}
            </strong>
          </div>
        </div>

        <div class="stat-row">
          <article class="stat-card">
            <span class="stat-label">分段总数</span>
            <strong class="stat-value">{{ activeTask.progress.segment_count }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">已发布（完整边界）</span>
            <strong class="stat-value">{{ activeTask.progress.published_count }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">已校验待补血缘</span>
            <strong class="stat-value warn">{{ activeTask.progress.waiting_lineage_count }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">已撤回</span>
            <strong class="stat-value muted-text">{{ activeTask.progress.rolled_back_count }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">校验失败</span>
            <strong class="stat-value danger">{{ activeTask.progress.check_failed_count }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">下一次续做位置</span>
            <strong class="stat-value">
              {{ activeTask.progress.next_resume_seq > activeTask.segment_count
                ? '无（全部完成）'
                : `第 ${activeTask.progress.next_resume_seq} 段` }}
            </strong>
          </article>
        </div>

        <div class="action-bar">
          <label class="mode-select">
            <span>执行模式</span>
            <select v-model="batchMode">
              <option v-for="mode in modes" :key="mode" :value="mode">{{ mode }}</option>
            </select>
          </label>
          <button class="btn primary" type="button" :disabled="!selectedSeqs.length || busy" @click="submitSelected">
            提交选中 {{ selectedSeqs.length }} 个分段
          </button>
          <button class="btn" type="button" :disabled="busy" @click="resumeFromBoundary">
            从续做边界继续（第 {{ activeTask.progress.next_resume_seq }} 段）
          </button>
          <button class="btn ghost" type="button" :disabled="busy" @click="recoverLineageFaults">解除血缘故障</button>
          <button class="btn ghost" type="button" :disabled="busy" @click="recoverAllFaults">解除全部故障</button>
          <button class="btn ghost" type="button" @click="toggleSelectAll">
            {{ allSelected ? '清空选择' : '全选分段' }}
          </button>
        </div>

        <div v-if="message" class="notice" :class="messageKind">{{ message }}</div>

        <div class="table-wrap">
          <table class="data-table segment-table">
            <thead>
              <tr>
                <th class="col-check">选择</th>
                <th>分段</th>
                <th>数据范围（行序号）</th>
                <th>目标分区</th>
                <th>源行数 / 目标行数</th>
                <th>去重数（源/目标）</th>
                <th>金额汇总（源/目标）</th>
                <th>状态</th>
                <th>逻辑批次</th>
                <th>校验结果</th>
                <th>血缘记录</th>
                <th>尝试</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="seg in activeTask.segments" :key="seg.seq" :class="{ selected: selectedSeqs.includes(seg.seq) }">
                <td class="col-check">
                  <input v-model="selectedSeqs" type="checkbox" :value="seg.seq" />
                </td>
                <td>
                  <strong>{{ seg.name }}</strong>
                  <div class="muted small">#{{ seg.seq }}</div>
                </td>
                <td>{{ formatNumber(seg.range_start) }} ~ {{ formatNumber(seg.range_end) }}</td>
                <td>{{ seg.partition_start }}<br />{{ seg.partition_end }}</td>
                <td>
                  {{ formatNumber(seg.source_row_count) }}
                  <span class="muted">/</span>
                  <span :class="{ mismatch: seg.target_row_count !== null && seg.target_row_count !== seg.source_row_count }">
                    {{ seg.target_row_count === null ? '—' : formatNumber(seg.target_row_count) }}
                  </span>
                </td>
                <td>
                  {{ formatNumber(seg.source_distinct_key) }}
                  <span class="muted">/</span>
                  <span :class="{ mismatch: seg.target_distinct_key !== null && seg.target_distinct_key !== seg.source_distinct_key }">
                    {{ seg.target_distinct_key === null ? '—' : formatNumber(seg.target_distinct_key) }}
                  </span>
                </td>
                <td>
                  {{ formatAmount(seg.source_amount_sum) }}
                  <span class="muted">/</span>
                  <span :class="{ mismatch: seg.target_amount_sum !== null && seg.target_amount_sum !== seg.source_amount_sum }">
                    {{ seg.target_amount_sum === null ? '—' : formatAmount(seg.target_amount_sum) }}
                  </span>
                </td>
                <td><span class="badge" :class="statusClass(seg.status)">{{ seg.status }}</span></td>
                <td class="small">{{ seg.batch_id ?? '—' }}</td>
                <td>
                  <button v-if="seg.check_result_id" class="link" type="button" @click="openCheck(seg.check_result_id!)">
                    {{ seg.check_result_id }}
                  </button>
                  <span v-else class="muted">—</span>
                </td>
                <td>
                  <span v-if="seg.lineage_record_id" class="ok-text">{{ seg.lineage_record_id }}</span>
                  <span v-else class="muted">未写入</span>
                </td>
                <td>{{ seg.attempts }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- 最近批次：多选分段分别保留结果 -->
        <div v-if="lastBatch" class="batch-panel">
          <h4>最近批次 {{ lastBatch.batch_id }}（{{ lastBatch.mode }} · {{ lastBatch.created_at }}）</h4>
          <p class="muted small">
            提交分段 {{ lastBatch.segment_seqs.join('、') }} ｜ 冲突策略：{{ lastBatch.conflict_policy }}
          </p>
          <ul class="outcome-list">
            <li v-for="outcome in lastOutcomes" :key="outcome.seq" class="outcome-item">
              <span class="badge" :class="statusClass(outcome.status)">{{ outcome.status }}</span>
              <strong>第 {{ outcome.seq }} 段</strong>
              <span class="muted">{{ outcome.message }}</span>
              <em v-if="outcome.idempotent" class="idempotent-tag">幂等未追加</em>
            </li>
          </ul>
        </div>
      </div>
    </div>

    <!-- 校验结果弹层：审计详情同源数据 -->
    <div v-if="checkDetail" class="modal-mask" @click.self="checkDetail = null">
      <div class="modal-card">
        <div class="modal-head">
          <h3>源目标一致性校验结果 {{ checkDetail.check_result.result_id }}</h3>
          <button class="btn" type="button" @click="checkDetail = null">关闭</button>
        </div>
        <dl class="check-grid">
          <div><dt>分段</dt><dd>{{ checkDetail.check_result.segment_name }}（批次 {{ checkDetail.check_result.batch_id }}）</dd></div>
          <div><dt>校验规则</dt><dd>{{ checkDetail.check_result.rule_desc }}</dd></div>
          <div><dt>源/目标行数</dt>
            <dd :class="{ mismatch: !checkDetail.check_result.row_count_match }">
              {{ checkDetail.check_result.source_row_count }} / {{ checkDetail.check_result.target_row_count }}
              {{ checkDetail.check_result.row_count_match ? '✓' : '✗' }}
            </dd>
          </div>
          <div><dt>去重数</dt>
            <dd :class="{ mismatch: !checkDetail.check_result.distinct_match }">
              {{ checkDetail.check_result.source_distinct_key }} / {{ checkDetail.check_result.target_distinct_key }}
              {{ checkDetail.check_result.distinct_match ? '✓' : '✗' }}
            </dd>
          </div>
          <div><dt>金额汇总</dt>
            <dd :class="{ mismatch: !checkDetail.check_result.amount_match }">
              {{ formatAmount(checkDetail.check_result.source_amount_sum) }} /
              {{ formatAmount(checkDetail.check_result.target_amount_sum) }}
              {{ checkDetail.check_result.amount_match ? '✓' : '✗' }}
            </dd>
          </div>
          <div><dt>校验结论</dt><dd>{{ checkDetail.check_result.conclusion }}</dd></div>
          <div v-if="checkDetail.audit_item" class="full-row">
            <dt>迁移审计清单</dt>
            <dd>
              <span class="badge" :class="statusClass(checkDetail.audit_item.status)">{{ checkDetail.audit_item.status }}</span>
              <span class="muted">来源入口：{{ checkDetail.audit_item.source_entry }}</span>
              <p v-if="checkDetail.audit_item.conclusion_remark" class="small">{{ checkDetail.audit_item.conclusion_remark }}</p>
            </dd>
          </div>
          <div class="full-row">
            <dt>回写痕迹</dt>
            <dd>
              <ol v-if="checkDetail.check_result.conclusions.length" class="conclusion-list">
                <li v-for="(item, index) in checkDetail.check_result.conclusions" :key="index">
                  {{ item.at }} · {{ item.entry }} · {{ item.action }}<span v-if="item.remark">（{{ item.remark }}）</span>
                </li>
              </ol>
              <span v-else class="muted">暂无处理结论，可在迁移审计清单页回写</span>
            </dd>
          </div>
        </dl>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  clearFaults,
  getCheckResult,
  getTask,
  listTasks,
  resumeTask,
  submitBatch,
  type BatchMode,
  type CheckResult,
  type AuditItem,
  type MigrationBatch,
  type MigrationTask,
  type SegmentOutcome,
} from '@/api/migration'

const modes: BatchMode[] = ['批量提交', '整组校验', '逐条执行']

const tasks = ref<MigrationTask[]>([])
const activeTaskId = ref<string>('')
const activeTask = ref<MigrationTask | null>(null)
const selectedSeqs = ref<number[]>([])
const batchMode = ref<BatchMode>('批量提交')
const busy = ref(false)
const message = ref('')
const messageKind = ref<'ok' | 'err'>('ok')
const lastBatch = ref<MigrationBatch | null>(null)
const checkDetail = ref<{ check_result: CheckResult; audit_item: AuditItem | null } | null>(null)

const allSelected = computed(
  () => activeTask.value !== null && selectedSeqs.value.length === activeTask.value.segment_count,
)
const lastOutcomes = computed<SegmentOutcome[]>(() => {
  if (!lastBatch.value) return []
  return Object.values(lastBatch.value.segment_outcomes).sort((a, b) => a.seq - b.seq)
})

function notify(text: string, kind: 'ok' | 'err' = 'ok') {
  message.value = text
  messageKind.value = kind
}

function statusClass(status: string): string {
  if (status === '已发布' || status === '已完成' || status === '审计通过') return 'badge-green'
  if (status === '已校验待补血缘' || status === '待补血缘') return 'badge-orange'
  if (status === '已撤回' || status === '审计驳回') return 'badge-yellow'
  if (status === '校验失败') return 'badge-red'
  if (status === '迁移中' || status === '待审计') return 'badge-blue'
  return 'badge-gray'
}

function formatNumber(value: number | null): string {
  return value === null ? '—' : value.toLocaleString('zh-CN')
}

function formatAmount(value: number | null): string {
  return value === null ? '—' : value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

async function reloadTasks() {
  try {
    const payload = await listTasks()
    tasks.value = payload.items
    if (!activeTaskId.value && tasks.value.length) {
      await selectTask(tasks.value[0].task_id)
    }
  } catch (error) {
    notify(error instanceof Error ? error.message : '迁移任务读取失败', 'err')
  }
}

async function selectTask(taskId: string) {
  activeTaskId.value = taskId
  selectedSeqs.value = []
  lastBatch.value = null
  await reloadDetail()
}

async function reloadDetail() {
  if (!activeTaskId.value) return
  try {
    activeTask.value = await getTask(activeTaskId.value)
  } catch (error) {
    notify(error instanceof Error ? error.message : '迁移明细读取失败', 'err')
  }
}

function toggleSelectAll() {
  if (!activeTask.value) return
  selectedSeqs.value = allSelected.value ? [] : (activeTask.value.segments ?? []).map((s) => s.seq)
}

async function submitSelected() {
  if (!activeTask.value || !selectedSeqs.value.length) return
  busy.value = true
  try {
    const result = await submitBatch(activeTask.value.task_id, [...selectedSeqs.value].sort((a, b) => a - b), batchMode.value)
    lastBatch.value = result.batch ?? null
    notify(`${result.message}：${result.batch?.batch_id ?? ''}，续做位置 第 ${result.next_resume_seq} 段`)
    await reloadTasks()
    await reloadDetail()
  } catch (error) {
    notify(error instanceof Error ? error.message : '批次提交失败', 'err')
  } finally {
    busy.value = false
  }
}

async function resumeFromBoundary() {
  if (!activeTask.value) return
  busy.value = true
  try {
    const result = await resumeTask(activeTask.value.task_id, batchMode.value)
    if (!result.ok) {
      notify(result.message, 'err')
      return
    }
    lastBatch.value = result.batch ?? null
    notify(`${result.message}，续做位置推进到 第 ${result.next_resume_seq} 段`)
    await reloadTasks()
    await reloadDetail()
  } catch (error) {
    notify(error instanceof Error ? error.message : '续做失败', 'err')
  } finally {
    busy.value = false
  }
}

async function recoverLineageFaults() {
  if (!activeTask.value) return
  busy.value = true
  try {
    const result = await clearFaults(activeTask.value.task_id, { lineage_seqs: activeTask.value.lineage_fail_seqs })
    notify(result.message)
    await reloadTasks()
    await reloadDetail()
  } catch (error) {
    notify(error instanceof Error ? error.message : '故障解除失败', 'err')
  } finally {
    busy.value = false
  }
}

async function recoverAllFaults() {
  if (!activeTask.value) return
  busy.value = true
  try {
    const result = await clearFaults(activeTask.value.task_id, { clear_all: true })
    notify(result.message)
    await reloadTasks()
    await reloadDetail()
  } catch (error) {
    notify(error instanceof Error ? error.message : '故障解除失败', 'err')
  } finally {
    busy.value = false
  }
}

async function openCheck(resultId: string) {
  try {
    checkDetail.value = await getCheckResult(resultId)
  } catch (error) {
    notify(error instanceof Error ? error.message : '校验结果读取失败', 'err')
  }
}

onMounted(reloadTasks)
</script>

<style scoped>
.workbench { display: flex; gap: 14px; align-items: flex-start; }
.task-pane { width: 300px; flex-shrink: 0; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
.pane-title { margin: 0 0 10px; font-size: 14px; }
.block { width: 100%; margin-bottom: 10px; }
.task-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; max-height: 70vh; overflow: auto; }
.task-card { border: 1px solid var(--border); border-radius: 8px; padding: 10px; cursor: pointer; }
.task-card:hover { border-color: var(--brand); }
.task-card.active { border-color: var(--brand); box-shadow: 0 0 0 1px var(--brand); }
.task-card-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; font-size: 13px; }
.task-card-line { font-size: 12px; margin: 2px 0; word-break: break-all; }
.progress-track { height: 6px; background: #eef2f7; border-radius: 3px; margin: 6px 0; overflow: hidden; }
.progress-fill { height: 100%; background: var(--brand); }
.complete-flag { color: #067647; font-size: 12px; margin-top: 4px; }
.detail-pane { flex: 1; min-width: 0; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px; }
.detail-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; }
.detail-sub { margin: 6px 0 0; font-size: 12px; }
.info-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin: 12px 0; }
.info-item { background: #f8fafc; border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; }
.info-item span { display: block; font-size: 12px; color: var(--muted); margin-bottom: 2px; }
.info-wide { grid-column: span 2; }
.action-bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 10px 0; }
.mode-select span { font-size: 12px; color: var(--muted); margin-right: 6px; }
.mode-select select { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
.notice { padding: 8px 12px; border-radius: 6px; font-size: 13px; margin-bottom: 10px; }
.notice.ok { background: #ecfdf3; color: #067647; border: 1px solid #abefc6; }
.notice.err { background: #fef3f2; color: #b42318; border: 1px solid #fda29b; }
.table-wrap { overflow-x: auto; }
.segment-table { font-size: 12px; }
.segment-table tr.selected { background: #eff6ff; }
.col-check { width: 40px; text-align: center; }
.small { font-size: 12px; }
.muted { color: var(--muted); }
.muted-text { color: var(--muted); }
.warn { color: #b54708; }
.danger { color: #b42318; }
.ok-text { color: #067647; font-size: 12px; }
.mismatch { color: #b42318; font-weight: 600; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; white-space: nowrap; }
.badge-green { background: #ecfdf3; color: #067647; }
.badge-orange { background: #fffaeb; color: #b54708; }
.badge-yellow { background: #fef7e0; color: #9a6700; }
.badge-red { background: #fef3f2; color: #b42318; }
.badge-blue { background: #eff8ff; color: #175cd3; }
.badge-gray { background: #f2f4f7; color: #475467; }
.batch-panel { margin-top: 14px; border: 1px solid var(--border); border-radius: 8px; padding: 12px; background: #fcfcfd; }
.batch-panel h4 { margin: 0 0 6px; font-size: 13px; }
.outcome-list { list-style: none; margin: 8px 0 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.outcome-item { display: flex; gap: 8px; align-items: center; font-size: 12px; flex-wrap: wrap; }
.idempotent-tag { font-style: normal; background: #f2f4f7; border-radius: 4px; padding: 1px 6px; color: #475467; }
.modal-mask { position: fixed; inset: 0; background: rgba(16, 24, 40, 0.45); display: flex; align-items: center; justify-content: center; z-index: 20; }
.modal-card { background: #fff; border-radius: 10px; width: 720px; max-width: 92vw; max-height: 86vh; overflow: auto; padding: 18px; }
.modal-head { display: flex; justify-content: space-between; align-items: center; }
.check-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 12px; }
.check-grid dt { font-size: 12px; color: var(--muted); }
.check-grid dd { margin: 2px 0 0; font-size: 13px; }
.check-grid .full-row { grid-column: span 2; }
.conclusion-list { margin: 4px 0 0; padding-left: 18px; font-size: 12px; }
</style>
