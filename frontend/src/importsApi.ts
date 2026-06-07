import { ApiError } from './api'

const API_BASE = '/api'

async function requestImport<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, init)
  const contentType = res.headers.get('content-type') || ''
  const isJson = contentType.includes('application/json')
  const body = isJson ? await res.json().catch(() => null) : await res.text().catch(() => '')

  if (!res.ok) {
    const message =
      typeof body === 'object' && body && 'detail' in body && typeof body.detail === 'string'
        ? body.detail
        : typeof body === 'string' && body
          ? body
          : `API error: ${res.status}`
    throw new ApiError(message, res.status, body)
  }

  return body as T
}

export interface ImportDuplicateReason {
  type: 'source_id' | 'heuristic' | 'file_hash' | 'legacy_statement' | 'payslip_signature' | 'retirement_source_id'
  existing_id: string
  existing_origin: string
  existing_date?: string
  existing_amount?: number
  existing_merchant_raw?: string
}

export type ImportKind = 'csv' | 'statement_pdf' | 'payslip_pdf' | 'retirement_csv' | 'retirement_statement_pdf' | 'investment_csv'
export type ImportRecordType = 'transactions' | 'payslip' | 'retirement' | 'investment'

export interface ImportPreviewItem {
  source: string
  source_id: string
  date: string
  amount: number
  merchant_raw: string
  account_last4: string | null
  merchant_clean?: string | null
  origin: string
  duplicate_reason: ImportDuplicateReason | null
}

export interface ImportDuplicateSummary {
  total_transactions: number
  duplicate_count: number
  importable_count: number
  items: ImportPreviewItem[]
}

export interface ImportPreview {
  import_id: string
  filename: string
  file_hash: string
  source: string
  source_key: string
  kind: ImportKind
  record_type: ImportRecordType
  currency?: string
  path: string
  created_at: string
  already_imported: boolean
  transactions: Array<{
    source: string
    source_id: string
    date: string
    amount: number
    merchant_raw: string
    account_last4: string | null
      merchant_clean?: string | null
      origin: string
  }>
  payload: any
  duplicate_summary: ImportDuplicateSummary
}

export interface ImportCommitResult {
  status: 'success' | 'already_imported'
  import_id: string
  imported: number
  skipped: number
  duplicate_count: number
  archived_path?: string
}

export const importsApi = {
  preview(file: File, source: string, kind?: ImportKind) {
    const form = new FormData()
    form.append('file', file)
    form.append('source', source)
    if (kind) form.append('kind', kind)
    return requestImport<ImportPreview>('/imports/preview', { method: 'POST', body: form })
  },
  getPreview(importId: string) {
    return requestImport<ImportPreview>(`/imports/${importId}`)
  },
  commit(importId: string) {
    return requestImport<ImportCommitResult>(`/imports/${importId}/commit`, { method: 'POST' })
  },
}
