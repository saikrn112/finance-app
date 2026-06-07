import { useEffect, useMemo, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useProjects, useProject } from './hooks'
import { api, ApiError, type ProjectDetail as ProjectDetailType, type ProjectSummary } from './api'
import { useFilterStore } from './store'
import { formatCount, formatCurrency } from './privacy'
import { Ledger } from './Ledger'
import { getCategoryColor } from './colors'

const COLORS = ['#22c55e', '#3b82f6', '#eab308', '#f97316', '#ef4444', '#8b5cf6', '#ec4899', '#06b6d4']

function formatProjectDate(value: string | null | undefined) {
  if (!value) return null
  const d = new Date(`${value}T00:00:00`)
  if (Number.isNaN(d.getTime())) return value
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
}

export function ProjectsPage({ onBack }: { onBack: () => void }) {
  const privacyMode = useFilterStore((state) => state.privacyMode)
  const displayCurrency = useFilterStore((state) => state.displayCurrency)
  const fmt = (n: number) => formatCurrency(n, privacyMode, { currency: displayCurrency })
  const fmtSigned = (n: number) => formatCurrency(n, privacyMode, { signed: true, absolute: true, currency: displayCurrency })
  const { data: projects } = useProjects()
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const { data: detail } = useProject(selectedId)
  const [creating, setCreating] = useState(false)
  const [busy, setBusy] = useState<'create' | 'toggle' | 'delete' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const qc = useQueryClient()

  const active = useMemo(() => projects?.filter(p => p.status === 'active') || [], [projects])
  const completed = useMemo(() => projects?.filter(p => p.status !== 'active') || [], [projects])

  useEffect(() => {
    if (!projects || projects.length === 0) {
      if (selectedId) setSelectedId(null)
      return
    }
    if (!selectedId || !projects.some(project => project.id === selectedId)) {
      setSelectedId(projects[0].id)
    }
  }, [projects, selectedId])

  const messageFromError = (err: unknown) => {
    if (err instanceof ApiError) return err.message
    if (err instanceof Error) return err.message
    return 'Something went wrong. Try again.'
  }

  const handleCreate = async (data: Partial<ProjectSummary>) => {
    setBusy('create')
    setError(null)
    try {
      const created = await api.createProject(data)
      setSelectedId(created.id)
      setCreating(false)
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['projects'] }),
        qc.invalidateQueries({ queryKey: ['project', created.id] }),
      ])
    } catch (err) {
      setError(messageFromError(err))
    } finally {
      setBusy(null)
    }
  }

  const handleStatusToggle = async (p: ProjectSummary) => {
    const next = p.status === 'active' ? 'completed' : 'active'
    setBusy('toggle')
    setError(null)
    try {
      await api.updateProject(p.id, { status: next })
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['projects'] }),
        qc.invalidateQueries({ queryKey: ['project', p.id] }),
      ])
    } catch (err) {
      setError(messageFromError(err))
    } finally {
      setBusy(null)
    }
  }

  const handleDelete = async (id: string) => {
    setBusy('delete')
    setError(null)
    try {
      await api.deleteProject(id)
      if (selectedId === id) setSelectedId(null)
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['projects'] }),
        qc.invalidateQueries({ queryKey: ['project'] }),
      ])
    } catch (err) {
      setError(messageFromError(err))
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="app-shell min-h-screen p-4" style={{ color: 'var(--text-primary)' }}>
      <div className="flex items-center gap-3 mb-5">
        <button onClick={onBack} className="text-sm text-blue-500 hover:underline">← Back</button>
        <h1 className="text-lg font-bold">Projects</h1>
      </div>

      <div className="flex gap-4" style={{ height: 'calc(100vh - 100px)' }}>
        {/* Left: project list */}
        <div className="w-64 shrink-0 space-y-4 overflow-y-auto">
          <Section label="Active" items={active} selectedId={selectedId} onSelect={setSelectedId} fmt={fmt} privacyMode={privacyMode} />
          {completed.length > 0 && <Section label="Completed" items={completed} selectedId={selectedId} onSelect={setSelectedId} fmt={fmt} privacyMode={privacyMode} />}
          {error && <div className="app-badge-negative text-xs rounded-lg px-3 py-2">{error}</div>}
          {creating ? (
            <CreateForm onSubmit={handleCreate} onCancel={() => { setCreating(false); setError(null) }} saving={busy === 'create'} />
          ) : (
            <button onClick={() => setCreating(true)} className="text-sm text-blue-500 hover:underline">+ New Project</button>
          )}
        </div>

        {/* Right: detail */}
        <div className="flex-1 overflow-y-auto">
          {detail ? (
            <ProjectDetail
              project={detail}
              fmt={fmt}
              fmtSigned={fmtSigned}
              onToggleStatus={() => handleStatusToggle(detail)}
              onDelete={() => handleDelete(detail.id)}
              busy={busy}
            />
          ) : projects?.length ? (
            <div className="flex items-center justify-center h-full text-slate-500 text-sm">Loading project…</div>
          ) : (
            <div className="flex items-center justify-center h-full">
              <div className="text-center max-w-sm">
                <h2 className="text-lg font-semibold mb-2">No projects yet</h2>
                <p className="text-sm text-slate-500 mb-3">Create a project to track a trip, move, or any cross-category spend.</p>
                {!creating && <button onClick={() => setCreating(true)} className="text-sm text-blue-500 hover:underline">Create your first project</button>}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function Section({
  label,
  items,
  selectedId,
  onSelect,
  fmt,
  privacyMode,
}: {
  label: string
  items: ProjectSummary[]
  selectedId: string | null
  onSelect: (id: string) => void
  fmt: (n: number) => string
  privacyMode: boolean
}) {
  return (
    <div>
      <div className="text-xs text-slate-500 uppercase tracking-wide mb-1">{label} ({formatCount(items.length, privacyMode)})</div>
      {items.map(p => (
        <div key={p.id} onClick={() => onSelect(p.id)}
          className={`p-2 rounded cursor-pointer mb-1 ${selectedId === p.id ? 'app-selected' : 'app-hover'}`}>
          <div className="flex items-center gap-2">
            <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: p.color }} />
            <span className="text-sm font-medium truncate">{p.name}</span>
          </div>
          <div className="text-xs text-slate-500 ml-4.5 mt-0.5">
            {fmt(p.spent)}{p.budget ? ` / ${fmt(p.budget)}` : ''} · {formatCount(p.txn_count, privacyMode)} txns
          </div>
          <div className="text-[11px] text-slate-400 ml-4.5 mt-0.5">
            {label === 'Completed'
              ? `${formatProjectDate(p.earliest_transaction_date || p.start_date || p.created_at) || 'Started unknown'} → ${formatProjectDate(p.end_date || p.latest_transaction_date) || 'Ended unknown'}`
              : `Started ${formatProjectDate(p.earliest_transaction_date || p.start_date || p.created_at) || 'unknown'}`}
          </div>
          {p.budget && p.budget > 0 && (
            <div className="ml-4.5 mt-1 h-1.5 rounded-full overflow-hidden" style={{ background: 'var(--chart-bar-track)' }}>
              <div className="h-full rounded-full" style={{ width: `${Math.min(100, (p.spent / p.budget) * 100)}%`, backgroundColor: p.color }} />
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

function ProjectDetail({
  project: p,
  fmt,
  fmtSigned,
  onToggleStatus,
  onDelete,
  busy,
}: {
  project: ProjectDetailType
  fmt: (n: number) => string
  fmtSigned: (n: number) => string
  onToggleStatus: () => void
  onDelete: () => void
  busy: 'create' | 'toggle' | 'delete' | null
}) {
  const qc = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [draftName, setDraftName] = useState(p.name)
  const [draftColor, setDraftColor] = useState(p.color)
  const [draftBudget, setDraftBudget] = useState(p.budget?.toString() || '')
  const spending = p.transactions?.reduce((sum, t) => sum + (t.amount < 0 ? Math.abs(t.amount) : 0), 0) || 0
  const income = p.transactions?.reduce((sum, t) => sum + (t.amount > 0 ? t.amount : 0), 0) || 0
  const net = income - spending
  const totalCategorySpend = Math.max(p.categories?.reduce((sum, c) => sum + Math.abs(c.total), 0) || 0, 1)

  useEffect(() => {
    setDraftName(p.name)
    setDraftColor(p.color)
    setDraftBudget(p.budget?.toString() || '')
    setEditing(false)
    setSaveError(null)
  }, [p.id, p.name, p.color, p.budget])

  const handleSave = async () => {
    setSaving(true)
    setSaveError(null)
    try {
      await api.updateProject(p.id, {
        name: draftName.trim(),
        color: draftColor,
        budget: draftBudget.trim() ? parseFloat(draftBudget) : null,
      })
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['projects'] }),
        qc.invalidateQueries({ queryKey: ['project', p.id] }),
      ])
      setEditing(false)
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : 'Failed to update project')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div>
      <div className="flex items-center gap-3 mb-4">
        <span className="w-4 h-4 rounded-full" style={{ backgroundColor: p.color }} />
        <h2 className="text-xl font-bold">{p.name}</h2>
        <span className={`text-xs px-2 py-0.5 rounded-full ${p.status === 'active' ? 'app-badge-positive' : 'app-badge-neutral'}`}>
          {p.status}
        </span>
      </div>

      <div className="app-surface rounded-lg p-4 mb-5">
        <div className="flex items-center justify-between gap-3 mb-3">
          <h3 className="text-sm font-semibold">Project Settings</h3>
          {editing ? (
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => { setEditing(false); setSaveError(null); setDraftName(p.name); setDraftColor(p.color); setDraftBudget(p.budget?.toString() || '') }}
                disabled={saving}
                className="app-btn-secondary text-xs disabled:opacity-60"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => void handleSave()}
                disabled={saving || !draftName.trim()}
                className="text-xs px-3 py-1.5 rounded bg-blue-500 text-white hover:bg-blue-600 disabled:opacity-60"
              >
                {saving ? 'Saving…' : 'Save'}
              </button>
            </div>
          ) : (
            <button type="button" onClick={() => setEditing(true)} className="text-sm text-blue-500 hover:underline">Edit</button>
          )}
        </div>

        {editing ? (
          <div className="grid grid-cols-[minmax(0,1.5fr)_auto_minmax(0,1fr)] gap-3 items-end">
            <label className="block text-xs text-slate-500">
              Name
              <input
                value={draftName}
                onChange={(e) => setDraftName(e.target.value)}
                className="app-input mt-1 w-full text-sm px-3 py-2 rounded"
              />
            </label>
            <label className="block text-xs text-slate-500">
              Color
              <div className="mt-1 flex gap-1">
                {COLORS.map(c => (
                  <button key={c} type="button" onClick={() => setDraftColor(c)}
                    className={`w-6 h-6 rounded-full ${draftColor === c ? 'ring-2 ring-offset-1 ring-blue-500' : ''}`}
                    style={{ backgroundColor: c }} />
                ))}
              </div>
            </label>
            <label className="block text-xs text-slate-500">
              Budget (optional)
              <input
                value={draftBudget}
                onChange={(e) => setDraftBudget(e.target.value)}
                type="number"
                placeholder="None"
                className="app-input mt-1 w-full text-sm px-3 py-2 rounded"
              />
            </label>
          </div>
        ) : (
          <div className="flex items-center gap-6 text-sm">
            <div>
              <div className="text-xs text-slate-500">Name</div>
              <div className="font-medium">{p.name}</div>
            </div>
            <div>
              <div className="text-xs text-slate-500">Color</div>
              <div className="flex items-center gap-2"><span className="w-3 h-3 rounded-full" style={{ backgroundColor: p.color }} />{p.color}</div>
            </div>
            <div>
              <div className="text-xs text-slate-500">Budget</div>
              <div className="font-medium">{p.budget ? fmt(p.budget) : 'None'}</div>
            </div>
          </div>
        )}
        {saveError ? <div className="mt-3 text-xs" style={{ color: 'var(--color-negative)' }}>{saveError}</div> : null}
      </div>

      {/* Stats */}
      <div className={`grid gap-3 mb-5 ${p.budget ? 'grid-cols-5' : 'grid-cols-3'}`}>
        <div className="app-surface rounded-lg p-3">
          <div className="text-xs text-slate-500">Outflow</div>
          <div className="text-lg font-bold">{fmt(spending)}</div>
        </div>
        <div className="app-surface rounded-lg p-3">
          <div className="text-xs text-slate-500">Income</div>
          <div className="text-lg font-bold" style={income > 0 ? { color: 'var(--color-positive)' } : undefined}>{fmt(income)}</div>
        </div>
        <div className="app-surface rounded-lg p-3">
          <div className="text-xs text-slate-500">Net</div>
          <div className="text-lg font-bold" style={{ color: net >= 0 ? 'var(--color-positive)' : 'var(--color-negative)' }}>{fmtSigned(net)}</div>
        </div>
        {p.budget && (
          <>
            <div className="app-surface rounded-lg p-3">
              <div className="text-xs text-slate-500">Budget</div>
              <div className="text-lg font-bold">{fmt(p.budget)}</div>
            </div>
            <div className="app-surface rounded-lg p-3">
              <div className="text-xs text-slate-500">Remaining</div>
              <div className="text-lg font-bold" style={{ color: p.budget - p.spent >= 0 ? 'var(--color-positive)' : 'var(--color-negative)' }}>
                {fmt(p.budget - p.spent)}
              </div>
            </div>
          </>
        )}
      </div>

      {p.budget && p.budget > 0 && (
        <div className="h-3 rounded-full overflow-hidden mb-5" style={{ background: 'var(--chart-bar-track)' }}>
          <div className="h-full rounded-full transition-all" style={{ width: `${Math.min(100, (p.spent / p.budget) * 100)}%`, backgroundColor: p.color }} />
        </div>
      )}

      {/* Category breakdown */}
      {p.categories?.length > 0 && (
        <div className="app-surface rounded-lg p-4 mb-5">
          <h3 className="text-sm font-semibold mb-3">By Category</h3>
          <div className="space-y-4">
            {p.categories.map((c) => (
              <div key={c.category}>
                <div className="flex items-center gap-3 text-sm font-medium">
                  <span className="w-40 truncate">{c.category}</span>
                  <div className="flex-1 h-4 rounded overflow-hidden" style={{ background: 'var(--chart-bar-track)' }}>
                    <div className="h-full rounded" style={{ width: `${(Math.abs(c.total) / totalCategorySpend) * 100}%`, backgroundColor: getCategoryColor(c.category) }} />
                  </div>
                  <span className="w-24 text-right">{fmt(c.total)}</span>
                </div>
                {c.subcategories.length > 0 ? (
                  <div className="mt-2 ml-5 space-y-1.5">
                    {c.subcategories.map((sub) => (
                      <div key={`${c.category}-${sub.name}`} className="flex items-center gap-3 text-xs" style={{ color: 'var(--text-muted)' }}>
                        <span className="w-40 truncate">• {sub.name}</span>
                        <div className="flex-1 h-2.5 rounded overflow-hidden" style={{ background: 'var(--chart-bar-track)' }}>
                          <div className="h-full rounded" style={{ width: `${(sub.total / totalCategorySpend) * 100}%`, backgroundColor: `${getCategoryColor(c.category)}bb` }} />
                        </div>
                        <span className="w-24 text-right">{fmt(sub.total)}</span>
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="app-surface rounded-lg p-4 mb-5 overflow-hidden">
        <Ledger
          transactions={p.transactions}
          title="Transactions"
          useGlobalFilters={false}
          helperText="Click Category/Sub to edit. Use + in Projects to reassign."
        />
      </div>

      {/* Actions */}
      <div className="flex gap-2">
        <button onClick={onToggleStatus} disabled={busy !== null} className="app-btn-secondary text-xs disabled:opacity-60">
          {busy === 'toggle' ? 'Updating…' : p.status === 'active' ? 'Mark Complete' : 'Reactivate'}
        </button>
        <button onClick={onDelete} disabled={busy !== null} className="app-badge-negative text-xs px-3 py-1.5 rounded disabled:opacity-60">
          {busy === 'delete' ? 'Deleting…' : 'Delete'}
        </button>
      </div>
    </div>
  )
}

function CreateForm({
  onSubmit,
  onCancel,
  saving,
}: {
  onSubmit: (data: Partial<ProjectSummary>) => void
  onCancel: () => void
  saving: boolean
}) {
  const [name, setName] = useState('')
  const [color, setColor] = useState(COLORS[0])
  const [budget, setBudget] = useState('')

  return (
    <div className="app-surface rounded-lg p-3 space-y-2">
      <input value={name} onChange={e => setName(e.target.value)} placeholder="Project name"
        className="app-input w-full text-sm px-2 py-1.5 rounded" autoFocus />
      <div className="flex gap-1">
        {COLORS.map(c => (
          <button key={c} type="button" onClick={() => setColor(c)}
            className={`w-5 h-5 rounded-full ${color === c ? 'ring-2 ring-offset-1 ring-blue-500' : ''}`}
            style={{ backgroundColor: c }} />
        ))}
      </div>
      <input value={budget} onChange={e => setBudget(e.target.value)} placeholder="Budget (optional)" type="number"
        className="app-input w-full text-sm px-2 py-1.5 rounded" />
      <div className="flex gap-2">
        <button onClick={() => name.trim() && onSubmit({ name: name.trim(), color, budget: budget ? parseFloat(budget) : undefined })}
          className="app-btn-primary text-xs disabled:opacity-60" disabled={!name.trim() || saving}>{saving ? 'Creating…' : 'Create'}</button>
        <button onClick={onCancel} disabled={saving} className="app-btn-secondary text-xs disabled:opacity-60">Cancel</button>
      </div>
    </div>
  )
}
