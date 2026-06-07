import { useEffect, useMemo, useState, type ReactNode } from 'react'
import {
  AlertTriangle,
  ArrowRightCircle,
  BadgeDollarSign,
  CalendarRange,
  CheckCircle2,
  Landmark,
  Receipt,
  Trash2,
  Upload,
  Wallet,
} from 'lucide-react'
import {
  importsApi,
  type ImportCommitResult,
  type ImportKind,
  type ImportPreview,
  type ImportPreviewItem,
} from './importsApi'
import { useFilterStore } from './store'
import { formatCurrency } from './privacy'

interface SourceOption {
  value: string
  label: string
  group: string
  hint: string
  allowedKinds: ImportKind[]
}

interface ImportCard {
  id: string
  filename: string
  source: string
  preview: ImportPreview | null
  result: ImportCommitResult | null
  status: string | null
  error: string | null
  previewing: boolean
  committing: boolean
}

function useImportSources() {
  const [sources, setSources] = useState<SourceOption[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetch('/api/imports/sources')
      .then((r) => r.json())
      .then((data: any[]) => {
        setSources(data.map((s) => ({
          value: s.value,
          label: s.label,
          group: s.group,
          hint: s.hint,
          allowedKinds: s.allowed_kinds || s.allowedKinds || [],
        })))
        setLoading(false)
      })
      .catch(() => setLoading(false))
  }, [])

  const sourceMap = useMemo(
    () => Object.fromEntries(sources.map((option) => [option.value, option])) as Record<string, SourceOption>,
    [sources],
  )

  const sourceGroups = useMemo(
    () => Array.from(new Set(sources.map((option) => option.group))),
    [sources],
  )

  return { sources, sourceMap, sourceGroups, loading }
}

function fmt(n: number, privacyMode: boolean, currency?: string) {
  return formatCurrency(n, privacyMode, { currency })
}

function shortDate(value: string | undefined) {
  if (!value) return 'Unknown'
  const parsed = new Date(value.includes('T') ? value : `${value}T12:00:00`)
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
}

function makeId() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `import-${Math.random().toString(36).slice(2, 10)}`
}

function inferStatementSource(_filename: string): string | null {
  return null
}

function resolveUpload(file: File, source: string, sourceMap: Record<string, SourceOption>) {
  const option = sourceMap[source]
  if (!option) return { error: 'Unknown source selected.' }
  const name = file.name.toLowerCase()
  const extension = name.slice(name.lastIndexOf('.'))

  if (!extension) {
    return { error: 'Choose a CSV or PDF file.' }
  }

  if (option.allowedKinds.includes('payslip_pdf')) {
    if (extension !== '.pdf') return { error: `${option.label} imports expect a PDF payslip.` }
    return { source, kind: 'payslip_pdf' as ImportKind, statusPrefix: `Previewing ${option.label} payslip...` }
  }

  if (option.allowedKinds.includes('retirement_csv')) {
    if (extension === '.pdf' && option.allowedKinds.includes('retirement_statement_pdf')) {
      return { source, kind: 'retirement_statement_pdf' as ImportKind, statusPrefix: `Previewing ${option.label} statement...` }
    }
    if (extension !== '.csv') return { error: `${option.label} imports expect a CSV activity export.` }
    return { source, kind: 'retirement_csv' as ImportKind, statusPrefix: `Previewing ${option.label} activity...` }
  }

  if (option.allowedKinds.includes('investment_csv')) {
    if (extension !== '.csv') return { error: `${option.label} imports expect a CSV statement export.` }
    return { source, kind: 'investment_csv' as ImportKind, statusPrefix: `Previewing ${option.label} holdings...` }
  }

  if (extension === '.csv' && option.allowedKinds.includes('csv')) {
    return { source, kind: 'csv' as ImportKind, statusPrefix: `Previewing ${option.label} CSV...` }
  }

  if (extension === '.pdf' && option.allowedKinds.includes('statement_pdf')) {
    const inferred = inferStatementSource(file.name)
    if (inferred && inferred !== source && sourceMap[inferred]?.allowedKinds.includes('statement_pdf')) {
      return {
        source: inferred,
        kind: 'statement_pdf' as ImportKind,
        adjustedMessage: `Detected ${sourceMap[inferred].label} from the filename and previewed it with that account.`,
        statusPrefix: `Previewing ${sourceMap[inferred].label} statement...`,
      }
    }
    return { source, kind: 'statement_pdf' as ImportKind, statusPrefix: `Previewing ${option.label} statement...` }
  }

  const expected = option.allowedKinds.includes('statement_pdf') || option.allowedKinds.includes('payslip_pdf') ? 'PDF' : 'CSV'
  return { error: `${option.label} imports expect a ${expected} file.` }
}

function canCommit(preview: ImportPreview | null, result: ImportCommitResult | null) {
  if (!preview || result || preview.already_imported) return false
  if (preview.record_type === 'transactions') return preview.duplicate_summary.importable_count > 0
  return preview.duplicate_summary.importable_count > 0
}

function itemState(item: ImportPreviewItem) {
  return item.duplicate_reason ? 'duplicate' : 'importable'
}

export function ImportWorkflow({ onSuccess }: { onSuccess?: () => void }) {
  const privacyMode = useFilterStore((state) => state.privacyMode)
  const { sources: SOURCE_OPTIONS, sourceMap: SOURCE_MAP, sourceGroups: SOURCE_GROUPS, loading: sourcesLoading } = useImportSources()
  const [source, setSource] = useState<string>('')
  const [cards, setCards] = useState<ImportCard[]>([])
  const [dragging, setDragging] = useState(false)
  const [globalError, setGlobalError] = useState<string | null>(null)
  const [committingAll, setCommittingAll] = useState(false)

  const selectedSource = source ? SOURCE_MAP[source] : null
  const readyCards = useMemo(() => cards.filter((card) => canCommit(card.preview, card.result)), [cards])

  const summary = useMemo(() => {
    return {
      total: cards.length,
      importable: cards.reduce((sum, card) => sum + (card.preview?.duplicate_summary.importable_count || 0), 0),
      duplicates: cards.reduce((sum, card) => sum + (card.preview?.duplicate_summary.duplicate_count || 0), 0),
      errors: cards.filter((card) => Boolean(card.error)).length,
    }
  }, [cards])

  function updateCard(id: string, updater: (card: ImportCard) => ImportCard) {
    setCards((current) => current.map((card) => (card.id === id ? updater(card) : card)))
  }

  async function previewFiles(fileList: FileList | File[]) {
    if (!source) {
      setGlobalError('Choose the account you are uploading for before selecting files.')
      return
    }
    setGlobalError(null)

    const files = Array.from(fileList)
    const placeholders = files.map<ImportCard>((file) => ({
      id: makeId(),
      filename: file.name,
      source,
      preview: null,
      result: null,
      status: null,
      error: null,
      previewing: true,
      committing: false,
    }))

    setCards((current) => [...placeholders, ...current])

    for (let index = 0; index < files.length; index += 1) {
      const file = files[index]
      const placeholder = placeholders[index]
      const resolved = resolveUpload(file, source, SOURCE_MAP)
      if ('error' in resolved) {
        updateCard(placeholder.id, (card) => ({
          ...card,
          previewing: false,
          error: resolved.error ?? 'Preview failed',
          status: null,
        }))
        continue
      }

      updateCard(placeholder.id, (card) => ({
        ...card,
        source: resolved.source,
        status: resolved.statusPrefix,
        error: null,
      }))

      try {
        const preview = await importsApi.preview(file, resolved.source, resolved.kind)
        const adjustedPrefix = resolved.adjustedMessage ? `${resolved.adjustedMessage} ` : ''
        let status = `${adjustedPrefix}Preview ready.`
        if (preview.already_imported) {
          status = `${adjustedPrefix}This file was already imported before.`
        } else if (preview.record_type === 'transactions' && preview.duplicate_summary.total_transactions === 0) {
          status = `${adjustedPrefix}No transactions were parsed from this file. This is often a zero-activity statement, but it can also mean this layout needs parser support.`
        } else if (preview.record_type === 'transactions' && preview.duplicate_summary.importable_count === 0) {
          status = `${adjustedPrefix}All parsed rows already exist in the ledger.`
        } else if (preview.record_type === 'payslip' && preview.duplicate_summary.importable_count === 0) {
          status = `${adjustedPrefix}This paycheck already exists in payroll history.`
        } else if (preview.record_type === 'payslip') {
          status = `${adjustedPrefix}Payroll preview ready. Review the pay-cycle summary before committing.`
        } else if (preview.record_type === 'retirement' && preview.duplicate_summary.importable_count === 0) {
          status = `${adjustedPrefix}All parsed retirement activity already exists.`
        } else if (preview.record_type === 'retirement' && preview.duplicate_summary.duplicate_count > 0) {
          status = `${adjustedPrefix}Retirement activity preview ready. ${preview.duplicate_summary.importable_count} new row(s), ${preview.duplicate_summary.duplicate_count} duplicate row(s).`
        } else if (preview.record_type === 'retirement') {
          status = `${adjustedPrefix}Retirement activity preview ready. Review the contribution summary before committing.`
        } else if (preview.record_type === 'investment' && preview.duplicate_summary.importable_count === 0) {
          status = `${adjustedPrefix}This HSA statement already exists in investment history.`
        } else if (preview.record_type === 'investment') {
          status = `${adjustedPrefix}HSA holdings preview ready. Review the statement snapshot before committing.`
        }
        updateCard(placeholder.id, (card) => ({
          ...card,
          source: preview.source_key,
          preview,
          previewing: false,
          status,
          error: null,
        }))
      } catch (err) {
        updateCard(placeholder.id, (card) => ({
          ...card,
          previewing: false,
          preview: null,
          status: null,
          error: err instanceof Error ? err.message : 'Preview failed',
        }))
      }
    }
  }

  async function handleCommit(cardId: string) {
    const target = cards.find((card) => card.id === cardId)
    if (!target?.preview) return
    updateCard(cardId, (card) => ({ ...card, committing: true, error: null, status: 'Committing import...' }))
    try {
      const result = await importsApi.commit(target.preview.import_id)
      updateCard(cardId, (card) => ({
        ...card,
        committing: false,
        result,
        status:
          result.status === 'already_imported'
            ? 'This file was already committed earlier.'
            : `Import committed: imported ${result.imported}, skipped ${result.skipped}.`,
      }))
      onSuccess?.()
    } catch (err) {
      updateCard(cardId, (card) => ({
        ...card,
        committing: false,
        error: err instanceof Error ? err.message : 'Commit failed',
      }))
    }
  }

  async function handleCommitAll() {
    if (!readyCards.length) return
    setCommittingAll(true)
    try {
      for (const card of readyCards) {
        await handleCommit(card.id)
      }
    } finally {
      setCommittingAll(false)
    }
  }

  return (
    <div className="space-y-4">
      <div className="grid gap-3 xl:grid-cols-[1.1fr_0.9fr]">
        <label className="app-surface space-y-1 rounded-2xl p-4 dark:border-slate-700 dark:bg-slate-900/70">
          <span className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">Account</span>
          <select
            value={source}
            onChange={(event) => {
              setSource(event.target.value)
              setGlobalError(null)
            }}
            disabled={sourcesLoading}
            className="app-input mt-1 w-full rounded-xl px-3 py-2.5 text-sm dark:border-slate-600 dark:bg-slate-900"
          >
            <option value="">{sourcesLoading ? 'Loading sources...' : 'Choose an account...'}</option>
            {SOURCE_GROUPS.map((group) => (
              <optgroup key={group} label={group}>
                {SOURCE_OPTIONS.filter((option) => option.group === group).map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </optgroup>
            ))}
          </select>
          <div className="text-xs text-slate-500 dark:text-slate-400">
            {selectedSource ? `${selectedSource.label}: ${selectedSource.hint}.` : 'Pick the source first. File type is inferred from the selected account and filename.'}
          </div>
        </label>

        <div className="grid gap-3 sm:grid-cols-4">
          <StatCard label="Files" value={String(summary.total)} tone="neutral" />
          <StatCard label="Importable" value={String(summary.importable)} tone="good" />
          <StatCard label="Duplicates" value={String(summary.duplicates)} tone={summary.duplicates ? 'warn' : 'neutral'} />
          <StatCard label="Errors" value={String(summary.errors)} tone={summary.errors ? 'warn' : 'neutral'} />
        </div>
      </div>

      <div
        data-tour="tour-import-upload"
        onDragOver={(event) => {
          event.preventDefault()
          if (source) setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault()
          setDragging(false)
          if (event.dataTransfer.files?.length) void previewFiles(event.dataTransfer.files)
        }}
        className={`rounded-2xl border-2 border-dashed p-6 text-center transition-colors ${
          !source
            ? 'border-[var(--app-border-soft)] bg-[var(--app-surface-2)]/70 opacity-70 dark:border-slate-800 dark:bg-slate-900/30'
            : dragging
              ? 'border-blue-500 bg-blue-50 dark:bg-blue-950/30'
              : 'border-[var(--app-border)] bg-[var(--app-surface-2)] dark:border-slate-600 dark:bg-slate-900/40'
        }`}
      >
        <input
          id="import-file-input"
          type="file"
          multiple
          accept=".pdf,.csv,application/pdf,text/csv"
          onChange={(event) => {
            if (event.target.files?.length) {
              void previewFiles(event.target.files)
            }
            event.target.value = ''
          }}
          disabled={!source}
          className="hidden"
        />
        <label htmlFor="import-file-input" className={source ? 'cursor-pointer' : 'cursor-not-allowed'}>
          <Upload className="mx-auto mb-3 text-slate-400" size={28} />
          <div className="text-sm font-medium text-slate-700 dark:text-slate-200">
            {source ? 'Drop one or more files here or click to browse' : 'Choose an account before uploading'}
          </div>
          <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">
            Batch uploads keep each file separate, preview duplicates first, and only write to the DB when you commit.
          </div>
        </label>
      </div>

      {(globalError || readyCards.length > 0) && (
        <div className="app-surface flex flex-wrap items-center justify-between gap-3 rounded-2xl px-4 py-3 dark:border-slate-700 dark:bg-slate-900/70">
          <div className={`text-sm ${globalError ? 'text-rose-600 dark:text-rose-300' : 'text-slate-600 dark:text-slate-300'}`}>
            {globalError || `${readyCards.length} file(s) are ready to commit.`}
          </div>
          <button
            type="button"
            onClick={() => void handleCommitAll()}
            disabled={!readyCards.length || committingAll}
            className="inline-flex items-center gap-2 rounded-lg bg-blue-600 px-3 py-2 text-sm font-medium text-white transition-colors hover:bg-blue-700 disabled:cursor-not-allowed disabled:bg-slate-400"
          >
            <ArrowRightCircle size={16} />
            <span>{committingAll ? 'Committing...' : `Commit Ready (${readyCards.length})`}</span>
          </button>
        </div>
      )}

      {cards.length > 0 && (
        <div className="space-y-3">
          {cards.map((card) => (
            <ImportPreviewCard
              key={card.id}
              card={card}
              privacyMode={privacyMode}
              sourceMap={SOURCE_MAP}
              onCommit={() => void handleCommit(card.id)}
              onRemove={() => setCards((current) => current.filter((item) => item.id !== card.id))}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function ImportPreviewCard({
  card,
  privacyMode,
  sourceMap,
  onCommit,
  onRemove,
}: {
  card: ImportCard
  privacyMode: boolean
  sourceMap: Record<string, SourceOption>
  onCommit: () => void
  onRemove: () => void
}) {
  const preview = card.preview
  const source = sourceMap[card.source]
  const statusTone = card.error
    ? 'border-rose-300 bg-rose-50 text-rose-700 dark:border-rose-800 dark:bg-rose-950/40 dark:text-rose-200'
    : 'border-[var(--app-border-soft)] bg-[var(--app-surface-2)] text-slate-600 dark:border-slate-700 dark:bg-slate-900/60 dark:text-slate-300'

  return (
    <div className="app-surface space-y-4 rounded-2xl p-4 dark:border-slate-700 dark:bg-slate-900/70">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-sm font-semibold text-slate-900 dark:text-slate-100">{card.filename}</div>
          <div className="text-xs text-slate-500 dark:text-slate-400">
            {preview?.source || source?.label || card.source} · {labelForKind(preview?.kind)} {preview ? `· hash ${preview.file_hash.slice(0, 12)}` : ''}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onRemove}
            className="inline-flex items-center gap-1 rounded-lg border border-slate-300 px-2.5 py-2 text-xs text-slate-600 transition-colors hover:border-slate-400 hover:text-slate-900 dark:border-slate-600 dark:text-slate-300 dark:hover:border-slate-500"
          >
            <Trash2 size={14} />
            Remove
          </button>
          <button
            type="button"
            onClick={onCommit}
            disabled={!canCommit(preview, card.result) || card.previewing || card.committing}
            className="inline-flex items-center gap-2 rounded-lg bg-blue-600 px-3 py-2 text-sm font-medium text-white transition-colors hover:bg-blue-700 disabled:cursor-not-allowed disabled:bg-slate-400"
          >
            <ArrowRightCircle size={16} />
            <span>{card.committing ? 'Committing...' : 'Commit Import'}</span>
          </button>
        </div>
      </div>

      {(card.status || card.error) && (
        <div className={`rounded-lg border px-3 py-2 text-sm ${statusTone}`}>
          {card.error || card.status}
        </div>
      )}

      {card.previewing && (
        <div className="rounded-xl border border-[var(--app-border-soft)] bg-[var(--app-surface-2)] px-4 py-4 dark:border-slate-700 dark:bg-slate-950/40">
          <div className="flex items-center gap-3 text-sm text-slate-500 dark:text-slate-400">
            <div className="h-4 w-4 animate-spin rounded-full border-2 border-slate-300 border-t-blue-500" />
            <span>Parsing file — this may take a few seconds for large PDFs...</span>
          </div>
          <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-800">
            <div className="h-full w-1/3 animate-pulse rounded-full bg-blue-500/60" />
          </div>
        </div>
      )}

      {preview && (
        <>
          {preview.record_type === 'transactions' ? <TransactionPreview preview={preview} privacyMode={privacyMode} currency={preview.currency} /> : null}
          {preview.record_type === 'payslip' ? <PayslipPreview preview={preview} privacyMode={privacyMode} currency={preview.currency} /> : null}
          {preview.record_type === 'retirement' ? <RetirementPreview preview={preview} privacyMode={privacyMode} currency={preview.currency} /> : null}
          {preview.record_type === 'investment' ? <InvestmentPreview preview={preview} privacyMode={privacyMode} currency={preview.currency} /> : null}
          {card.result && (
            <div className="rounded-xl border border-emerald-300 bg-emerald-50 px-3 py-2 text-sm text-emerald-700 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-200">
              Commit complete: imported {card.result.imported}, skipped {card.result.skipped}.
            </div>
          )}
        </>
      )}
    </div>
  )
}

function TransactionPreview({ preview, privacyMode, currency }: { preview: ImportPreview; privacyMode: boolean; currency?: string }) {
  const payload = preview.payload || {}
  const hasBalances = payload.beginning_balance != null || payload.ending_balance != null
  return (
    <div className="space-y-3">
      <div className={`grid gap-3 ${hasBalances ? 'sm:grid-cols-3 lg:grid-cols-6' : 'sm:grid-cols-4'}`}>
        <StatCard label="Rows" value={String(preview.duplicate_summary.total_transactions)} tone="neutral" />
        <StatCard label="Importable" value={String(preview.duplicate_summary.importable_count)} tone="good" />
        <StatCard label="Duplicates" value={String(preview.duplicate_summary.duplicate_count)} tone={preview.duplicate_summary.duplicate_count ? 'warn' : 'neutral'} />
        <StatCard label="Status" value={preview.already_imported ? 'Already imported' : 'Ready'} tone={preview.already_imported ? 'warn' : 'good'} />
        {payload.beginning_balance != null && (
          <StatCard label="Opening Balance" value={fmt(Number(payload.beginning_balance), privacyMode, currency)} tone="neutral" />
        )}
        {payload.ending_balance != null && (
          <StatCard label="Closing Balance" value={fmt(Number(payload.ending_balance), privacyMode, currency)} tone="good" />
        )}
      </div>

      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <h4 className="text-sm font-semibold text-slate-800 dark:text-slate-100">Preview Rows</h4>
          <span className="text-xs text-slate-500 dark:text-slate-400">
            Showing {Math.min(preview.duplicate_summary.items.length, 200)} of {preview.duplicate_summary.items.length}
          </span>
        </div>
        {preview.duplicate_summary.items.length ? (
          <div className="max-h-[600px] space-y-2 overflow-y-auto pr-1">
            {[...preview.duplicate_summary.items].sort((a, b) => (a.duplicate_reason ? 1 : 0) - (b.duplicate_reason ? 1 : 0)).slice(0, 200).map((item) => (
              <PreviewRow key={item.source_id} item={item} privacyMode={privacyMode} currency={currency} />
            ))}
          </div>
        ) : (
          <div className="rounded-xl border border-dashed border-[var(--app-border)] bg-[var(--app-surface-2)] px-4 py-6 text-sm text-slate-500 dark:border-slate-700 dark:bg-slate-950/40 dark:text-slate-400">
            No transactions were parsed from this file. If this statement should contain activity, the parser likely needs another layout variant.
          </div>
        )}
      </div>
    </div>
  )
}

function PayslipPreview({ preview, privacyMode }: { preview: ImportPreview; privacyMode: boolean; currency?: string }) {
  const payload = preview.payload || {}
  const taxes = Object.entries(payload.taxes || {}).filter(([key]) => !key.endsWith('_ytd')).slice(0, 4)
  const deductions = Object.entries(payload.deductions || {}).filter(([key]) => !key.endsWith('_ytd')).slice(0, 4)

  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-4">
        <StatCard label="Employer" value={payload.employer || preview.source} tone="neutral" />
        <StatCard label="Pay Date" value={shortDate(payload.pay_date)} tone="good" />
        <StatCard label="Gross" value={fmt(Number(payload.gross || 0), privacyMode, currency)} tone="neutral" />
        <StatCard label="Net" value={fmt(Number(payload.net || 0), privacyMode, currency)} tone="good" />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="rounded-xl border border-[var(--app-border-soft)] bg-[var(--app-surface-2)] p-4 dark:border-slate-800 dark:bg-slate-950/40">
          <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-slate-800 dark:text-slate-100">
            <Receipt size={16} />
            Pay-Cycle Snapshot
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <MiniMetric icon={<BadgeDollarSign size={14} />} label="Gross" value={fmt(Number(payload.gross || 0), privacyMode, currency)} />
            <MiniMetric icon={<Wallet size={14} />} label="Net" value={fmt(Number(payload.net || 0), privacyMode, currency)} />
            <MiniMetric icon={<Landmark size={14} />} label="Taxes" value={fmt(Number(payload.total_taxes || 0), privacyMode, currency)} />
            <MiniMetric icon={<Landmark size={14} />} label="Deductions" value={fmt(Number(payload.total_deductions || 0), privacyMode, currency)} />
          </div>
        </div>

        <div className="rounded-xl border border-[var(--app-border-soft)] bg-[var(--app-surface-2)] p-4 dark:border-slate-800 dark:bg-slate-950/40">
          <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-slate-800 dark:text-slate-100">
            <CalendarRange size={16} />
            Parsed Lines
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <MiniList title="Taxes" items={taxes} privacyMode={privacyMode} />
            <MiniList title="Deductions" items={deductions} privacyMode={privacyMode} />
          </div>
        </div>
      </div>
    </div>
  )
}

function RetirementPreview({ preview, privacyMode }: { preview: ImportPreview; privacyMode: boolean; currency?: string }) {
  const payload = preview.payload || {}
  const rows = (payload.transactions || []) as Array<any>
  const summary = payload.summary || {}
  const statements = (payload.statements || []) as Array<any>
  const activity = (payload.activity || []) as Array<any>

  if (statements.length > 0 || activity.length > 0) {
    return (
      <div className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-4">
          <StatCard label="Statements" value={String(statements.length)} tone="neutral" />
          <StatCard label="Ending Balance" value={fmt(Number(summary.balance || 0), privacyMode, currency)} tone="good" />
          <StatCard label="Contributions" value={fmt(Number(summary.total_contributed || 0), privacyMode, currency)} tone="neutral" />
          <StatCard label="Latest" value={summary.latest_statement_end ? shortDate(summary.latest_statement_end) : 'Unknown'} tone="neutral" />
        </div>

        {statements.length ? (
          <div className="rounded-xl border border-[var(--app-border-soft)] bg-[var(--app-surface-2)] p-4 dark:border-slate-800 dark:bg-slate-950/40">
            <div className="mb-3 flex items-center justify-between gap-3">
              <div className="flex items-center gap-2 text-sm font-semibold text-slate-800 dark:text-slate-100">
                <Wallet size={16} />
                Statement Preview
              </div>
              <span className="text-xs text-slate-500 dark:text-slate-400">{statements.length} statement rows parsed</span>
            </div>
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-slate-500 dark:text-slate-400">
                  <th className="pb-2">Month</th>
                  <th className="pb-2 text-right">Employee</th>
                  <th className="pb-2 text-right">Employer</th>
                  <th className="pb-2 text-right">Market</th>
                  <th className="pb-2 text-right">End</th>
                </tr>
              </thead>
              <tbody>
                {statements.map((row) => (
                  <tr key={row.period_end} className="border-t border-[var(--app-border-soft)] dark:border-slate-800">
                    <td className="py-2">{shortDate(row.period_end)}</td>
                    <td className="py-2 text-right">{fmt(Number(row.employee_contributions || 0), privacyMode, currency)}</td>
                    <td className="py-2 text-right">{fmt(Number(row.employer_contributions || 0), privacyMode, currency)}</td>
                    <td className="py-2 text-right">{fmt(Number(row.market_change || 0), privacyMode, currency)}</td>
                    <td className="py-2 text-right font-medium">{fmt(Number(row.ending_balance || 0), privacyMode, currency)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}

        {activity.length ? (
          <div className="rounded-xl border border-[var(--app-border-soft)] bg-[var(--app-surface-2)] p-4 dark:border-slate-800 dark:bg-slate-950/40">
            <div className="mb-3 flex items-center justify-between gap-3">
              <div className="flex items-center gap-2 text-sm font-semibold text-slate-800 dark:text-slate-100">
                <Wallet size={16} />
                Activity Preview
              </div>
              <span className="text-xs text-slate-500 dark:text-slate-400">{activity.length} activity rows parsed</span>
            </div>
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-slate-500 dark:text-slate-400">
                  <th className="pb-2">Date</th>
                  <th className="pb-2">Investment</th>
                  <th className="pb-2 text-right">Amount</th>
                </tr>
              </thead>
              <tbody>
                {activity.slice(0, 200).map((row) => (
                  <tr key={`${row.date}|${row.investment}|${row.amount}`} className="border-t border-[var(--app-border-soft)] dark:border-slate-800">
                    <td className="py-2">{row.date}</td>
                    <td className="py-2">{row.investment}</td>
                    <td className="py-2 text-right font-medium text-emerald-500">{fmt(Number(row.amount || 0), privacyMode, currency)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </div>
    )
  }

  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-4">
        <StatCard label="Rows" value={String(preview.duplicate_summary.total_transactions)} tone="neutral" />
        <StatCard label="Balance" value={fmt(Number(summary.balance || 0), privacyMode, currency)} tone="good" />
        <StatCard label="Contributed" value={fmt(Number(summary.total_contributed || 0), privacyMode, currency)} tone="neutral" />
        <StatCard label="Range" value={summary.date_range?.length ? `${shortDate(summary.date_range[0])} → ${shortDate(summary.date_range[1])}` : 'Unknown'} tone="neutral" />
      </div>

      <div className="rounded-xl border border-[var(--app-border-soft)] bg-[var(--app-surface-2)] p-4 dark:border-slate-800 dark:bg-slate-950/40">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 text-sm font-semibold text-slate-800 dark:text-slate-100">
            <Wallet size={16} />
            Retirement Activity Preview
          </div>
          <span className="text-xs text-slate-500 dark:text-slate-400">{rows.length} rows parsed</span>
        </div>
        <div className="max-h-72 overflow-y-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-slate-500 dark:text-slate-400">
                <th className="pb-2">Date</th>
                <th className="pb-2">Type</th>
                <th className="pb-2 text-right">Amount</th>
              </tr>
            </thead>
            <tbody>
              {rows.slice(0, 200).map((row) => (
                <tr key={row.source_id} className="border-t border-[var(--app-border-soft)] dark:border-slate-800">
                  <td className="py-2">{row.date}</td>
                  <td className="py-2">{row.type}</td>
                  <td className={`py-2 text-right font-medium ${Number(row.amount) >= 0 ? 'text-emerald-500' : 'text-rose-400'}`}>
                    {fmt(Number(row.amount), privacyMode, currency)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

function InvestmentPreview({ preview, privacyMode }: { preview: ImportPreview; privacyMode: boolean; currency?: string }) {
  const payload = preview.payload || {}
  const account = payload.account || {}
  const summary = payload.summary || {}
  const holdings = (payload.holdings || []) as Array<any>

  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-4">
        <StatCard label="Account" value={account.account_type || 'HSA'} tone="neutral" />
        <StatCard label="Statement" value={shortDate(account.statement_date)} tone="good" />
        <StatCard label="Ending Value" value={fmt(Number(summary.ending_value || 0), privacyMode, currency)} tone="good" />
        <StatCard label="Holdings" value={String(holdings.length)} tone="neutral" />
      </div>

      <div className="grid gap-4 lg:grid-cols-[0.95fr_1.05fr]">
        <div className="rounded-xl border border-[var(--app-border-soft)] bg-[var(--app-surface-2)] p-4 dark:border-slate-800 dark:bg-slate-950/40">
          <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-slate-800 dark:text-slate-100">
            <Wallet size={16} />
            Statement Snapshot
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <MiniMetric icon={<BadgeDollarSign size={14} />} label="Beginning" value={fmt(Number(summary.beginning_value || 0), privacyMode, currency)} />
            <MiniMetric icon={<BadgeDollarSign size={14} />} label="Ending" value={fmt(Number(summary.ending_value || 0), privacyMode, currency)} />
            <MiniMetric icon={<Landmark size={14} />} label="Change" value={fmt(Number(summary.change_in_investment || 0), privacyMode, currency)} />
            <MiniMetric icon={<Landmark size={14} />} label="This Period" value={fmt(Number(summary.total_this_period || 0), privacyMode, currency)} />
          </div>
        </div>

        <div className="rounded-xl border border-[var(--app-border-soft)] bg-[var(--app-surface-2)] p-4 dark:border-slate-800 dark:bg-slate-950/40">
          <div className="mb-3 flex items-center justify-between gap-3">
            <div className="flex items-center gap-2 text-sm font-semibold text-slate-800 dark:text-slate-100">
              <Landmark size={16} />
              Holdings Preview
            </div>
            <span className="text-xs text-slate-500 dark:text-slate-400">{holdings.length} rows parsed</span>
          </div>
          <div className="max-h-72 overflow-y-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-slate-500 dark:text-slate-400">
                  <th className="pb-2">Ticker</th>
                  <th className="pb-2">Name</th>
                  <th className="pb-2 text-right">Value</th>
                </tr>
              </thead>
              <tbody>
                {holdings.slice(0, 200).map((row) => (
                  <tr key={`${row.security_id}-${row.ticker}`} className="border-t border-[var(--app-border-soft)] dark:border-slate-800">
                    <td className="py-2 font-medium text-slate-900 dark:text-slate-100">{row.ticker || 'Cash'}</td>
                    <td className="py-2 text-slate-600 dark:text-slate-300">{row.name}</td>
                    <td className="py-2 text-right font-medium text-slate-900 dark:text-slate-100">
                      {fmt(Number(row.value || 0), privacyMode, currency)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  )
}

function MiniMetric({ icon, label, value }: { icon: ReactNode; label: string; value: string }) {
  return (
    <div className="app-surface rounded-lg px-3 py-2 dark:border-slate-700 dark:bg-slate-900">
      <div className="flex items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
        {icon}
        {label}
      </div>
      <div className="mt-1 text-sm font-semibold text-slate-900 dark:text-slate-100">{value}</div>
    </div>
  )
}

function MiniList({ title, items, privacyMode }: { title: string; items: Array<[string, unknown]>; privacyMode: boolean }) {
  return (
    <div>
      <div className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">{title}</div>
      <div className="space-y-1.5">
        {items.length ? (
          items.map(([label, value]) => (
            <div key={label} className="flex items-center justify-between gap-2 text-sm">
              <span className="truncate text-slate-600 dark:text-slate-300">{label.replaceAll('_', ' ')}</span>
              <span className="font-medium text-slate-900 dark:text-slate-100">{fmt(Number(value || 0), privacyMode, currency)}</span>
            </div>
          ))
        ) : (
          <div className="text-sm text-slate-500 dark:text-slate-400">No parsed lines</div>
        )}
      </div>
    </div>
  )
}

function labelForKind(kind: ImportKind | undefined) {
  if (!kind) return 'Import file'
  return {
    csv: 'CSV import',
    statement_pdf: 'Statement PDF',
    payslip_pdf: 'Payslip PDF',
    retirement_csv: 'Retirement CSV',
    retirement_statement_pdf: 'Retirement Statement PDF',
    investment_csv: 'Investment CSV',
  }[kind]
}

function StatCard({
  label,
  value,
  tone,
}: {
  label: string
  value: string
  tone: 'good' | 'warn' | 'neutral'
}) {
  const tones = {
    good: 'border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950/30 dark:text-emerald-200',
    warn: 'border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200',
    neutral: 'border-[var(--app-border-soft)] bg-[var(--app-surface-2)] text-slate-800 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100',
  }
  return (
    <div className={`rounded-xl border px-4 py-3 ${tones[tone]}`}>
      <div className="text-xs uppercase tracking-wide opacity-70">{label}</div>
      <div className="mt-1 text-lg font-semibold">{value}</div>
    </div>
  )
}

function PreviewRow({ item, privacyMode, currency }: { item: ImportPreviewItem; privacyMode: boolean; currency?: string }) {
  const state = itemState(item)
  const badgeClass =
    state === 'duplicate'
      ? 'bg-amber-500/10 text-amber-600 dark:text-amber-300'
      : 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-300'

  return (
    <div className="rounded-xl border border-[var(--app-border-soft)] bg-[var(--app-surface-2)] p-3 text-sm dark:border-slate-700 dark:bg-slate-950/50">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate font-medium text-slate-900 dark:text-slate-100">{item.merchant_raw}</div>
          <div className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">
            {item.date} · {item.source} · {item.account_last4 ? `••${item.account_last4}` : 'manual'}
          </div>
          {item.duplicate_reason?.type === 'file_hash' ? (
            <div className="mt-1 text-xs text-amber-600 dark:text-amber-300">Previously imported file hash matches this upload.</div>
          ) : item.duplicate_reason ? (
            <div className="mt-1 text-xs text-amber-600 dark:text-amber-300">
              Likely duplicate via {item.duplicate_reason.type} against {item.duplicate_reason.existing_origin}.
            </div>
          ) : null}
        </div>
        <div className="text-right">
          <div className={`font-semibold ${item.amount >= 0 ? 'text-emerald-500' : 'text-rose-400'}`}>{fmt(item.amount, privacyMode, currency)}</div>
          <span className={`mt-1 inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ${badgeClass}`}>
            {state === 'duplicate' ? <AlertTriangle size={12} /> : <CheckCircle2 size={12} />}
            {state === 'duplicate' ? 'Duplicate' : 'Importable'}
          </span>
        </div>
      </div>
    </div>
  )
}
