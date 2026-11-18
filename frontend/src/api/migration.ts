/** 分段迁移引擎接口封装与类型定义。 */
import { request } from '@/api/client'

export type SegmentStatus =
  | '待迁移'
  | '迁移中'
  | '已校验待补血缘'
  | '已撤回'
  | '已发布'
  | '校验失败'

export type TaskStatus = '待执行' | '迁移中' | '待补血缘' | '已完成' | '校验失败'
export type BatchMode = '批量提交' | '整组校验' | '逐条执行'
export type ConflictPolicy = '保留待补血缘' | '撤回分段只发布前一段'
export type AuditStatus = '待审计' | '审计通过' | '审计驳回'

export interface Progress {
  segment_count: number
  published_count: number
  waiting_lineage_count: number
  rolled_back_count: number
  check_failed_count: number
  percent: number
  next_resume_seq: number
  completed: boolean
}

export interface MigrationTask {
  task_id: string
  task_code: string
  source_table: string
  target_table: string
  target_table_mapping: Record<string, string>
  data_scope: string
  partition_start: string
  partition_end: string
  dedup_key: string
  amount_field: string
  check_rules: string[]
  segment_count: number
  rows_per_segment: number
  conflict_policy: ConflictPolicy
  lineage_fail_seqs: number[]
  check_fail_seqs: number[]
  status: TaskStatus
  created_at: string
  updated_at: string
  progress: Progress
  segments?: Segment[]
  batches?: MigrationBatch[]
}

export interface Segment {
  seq: number
  name: string
  range_start: number
  range_end: number
  rows_per_segment: number
  partition_start: string
  partition_end: string
  source_row_count: number
  source_distinct_key: number
  source_amount_sum: number
  target_row_count: number | null
  target_distinct_key: number | null
  target_amount_sum: number | null
  status: SegmentStatus
  batch_id: string | null
  check_result_id: string | null
  lineage_record_id: string | null
  updated_at: string | null
  attempts: number
}

export interface SegmentOutcome {
  seq: number
  status: SegmentStatus
  idempotent: boolean
  conflict?: boolean
  message: string
  check_result_id?: string
  lineage_record_id?: string
  batch_id?: string
}

export interface MigrationBatch {
  batch_id: string
  task_id: string
  task_code: string
  mode: BatchMode
  segment_seqs: number[]
  submitted_segment_seqs: number[]
  conflict_policy: ConflictPolicy
  created_at: string
  segment_outcomes: Record<string, SegmentOutcome>
}

export interface CheckResult {
  result_id: string
  task_id: string
  task_code: string
  segment_seq: number
  segment_name: string
  batch_id: string
  rule_desc: string
  source_row_count: number
  target_row_count: number
  source_distinct_key: number
  target_distinct_key: number
  source_amount_sum: number
  target_amount_sum: number
  row_count_match: boolean
  distinct_match: boolean
  amount_match: boolean
  passed: boolean
  conclusion: string
  created_at: string
  conclusions: { entry: string; action: string; remark: string; at: string }[]
}

export interface AuditItem {
  audit_id: string
  task_id: string
  task_code: string
  segment_seq: number
  segment_name: string
  batch_id: string
  check_result_id: string
  target_table_mapping: Record<string, string>
  check_conclusion: string
  check_passed: boolean
  status: AuditStatus
  source_entry: string
  conclusion_remark: string | null
  created_at: string
  updated_at: string
}

export interface LineageRecord {
  lineage_id: string
  task_id: string
  batch_id: string
  segment_seq: number
  source_table: string
  target_table: string
  transform_type: string
  field_mapping: Record<string, string>
  created_at: string
}

interface BatchResponse {
  ok: boolean
  message: string
  batch?: MigrationBatch
  progress?: Progress
  next_resume_seq?: number
  task_status?: TaskStatus
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  if (!response.ok) {
    const detail = await response.json().catch(() => ({ detail: '请求失败' }))
    throw new Error(detail.detail ?? '请求失败')
  }
  return (await response.json()) as T
}

export function listTasks(): Promise<{ items: MigrationTask[]; total: number }> {
  return request('/api/migration/tasks').then((r) => r.json())
}

export function getTask(taskId: string): Promise<MigrationTask> {
  return request(`/api/migration/tasks/${taskId}`).then((r) => r.json())
}

export function submitBatch(
  taskId: string,
  segmentSeqs: number[],
  mode: BatchMode,
  batchId?: string,
): Promise<BatchResponse> {
  return postJson(`/api/migration/tasks/${taskId}/batches`, {
    segment_seqs: segmentSeqs,
    mode,
    batch_id: batchId ?? null,
  })
}

export function resumeTask(
  taskId: string,
  mode: BatchMode,
  limit?: number,
): Promise<BatchResponse> {
  return postJson(`/api/migration/tasks/${taskId}/resume`, { mode, limit: limit ?? null })
}

export function clearFaults(
  taskId: string,
  payload: { lineage_seqs?: number[]; check_seqs?: number[]; clear_all?: boolean },
): Promise<{ ok: boolean; message: string; next_resume_seq: number }> {
  return postJson(`/api/migration/tasks/${taskId}/faults/clear`, payload)
}

export function listCheckResults(taskId?: string): Promise<{ items: CheckResult[]; total: number }> {
  const query = taskId ? `?task_id=${encodeURIComponent(taskId)}` : ''
  return request(`/api/migration/check-results${query}`).then((r) => r.json())
}

export function getCheckResult(resultId: string): Promise<{ check_result: CheckResult; audit_item: AuditItem | null }> {
  return request(`/api/migration/check-results/${resultId}`).then((r) => r.json())
}

export function listAudits(params?: { taskId?: string; status?: AuditStatus }): Promise<{ items: AuditItem[]; total: number }> {
  const query = new URLSearchParams()
  if (params?.taskId) query.set('task_id', params.taskId)
  if (params?.status) query.set('status', params.status)
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return request(`/api/migration/audits${suffix}`).then((r) => r.json())
}

export function writeAuditConclusion(
  checkResultId: string,
  conclusion: AuditStatus,
  remark: string,
): Promise<{ ok: boolean; message: string; audit_item?: AuditItem }> {
  return postJson('/api/migration/audits/conclusion', {
    check_result_id: checkResultId,
    conclusion,
    remark,
    source_entry: '迁移审计清单',
  })
}

export function writeExternalConclusion(
  checkResultId: string,
  conclusion: AuditStatus,
  remark: string,
): Promise<{ ok: boolean; message: string; audit_item?: AuditItem }> {
  return postJson('/api/migration/entries/conclusion', {
    check_result_id: checkResultId,
    conclusion,
    remark,
    source_entry: '其它业务入口',
  })
}

export function listLineage(taskId: string): Promise<{ items: LineageRecord[]; total: number }> {
  return request(`/api/migration/tasks/${taskId}/lineage`).then((r) => r.json())
}
