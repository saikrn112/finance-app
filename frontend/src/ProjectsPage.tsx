import { useEffect, useMemo, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useProjects, useProject } from './hooks'
import { api, ApiError, type Contact, type MemberTotal, type SplitwiseCommitSummary, type Transaction, type ProjectDetail as ProjectDetailType, type ProjectSummary } from './api'
import { useFilterStore } from './store'
import { formatCount, formatCurrency } from './privacy'
import { Ledger } from './Ledger'
import { getCategoryColor, getCategoryColorFaded } from './colors'
import { sortByRecentActivity } from './projectSort'

const COLORS = [
  '#22c55e', '#3b82f6', '#eab308', '#f97316', '#ef4444', '#8b5cf6', '#ec4899', '#06b6d4',
  '#14b8a6', '#a855f7', '#84cc16', '#0ea5e9', '#f43f5e', '#d946ef', '#10b981', '#6366f1',
]
const MEMBER_COLORS = ['#22c55e', '#3b82f6', '#f97316', '#ef4444', '#8b5cf6', '#ec4899', '#06b6d4', '#14b8a6', '#f43f5e', '#a855f7', '#84cc16', '#0ea5e9']

function pickRandomColor(usedColors: string[], palette: string[] = MEMBER_COLORS): string {
  const available = palette.filter(c => !usedColors.includes(c))
  const pool = available.length > 0 ? available : palette
  return pool[Math.floor(Math.random() * pool.length)]
}

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

  const active = useMemo(
    () => sortByRecentActivity(projects?.filter(p => p.status === 'active') || []),
    [projects],
  )
  const completed = useMemo(
    () => sortByRecentActivity(projects?.filter(p => p.status !== 'active') || []),
    [projects],
  )

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

  const handleCreate = async (data: Partial<ProjectSummary>, memberIds: string[] = []) => {
    setBusy('create')
    setError(null)
    try {
      const created = await api.createProject(data)
      if (memberIds.length > 0) {
        await api.addProjectMembers(created.id, memberIds)
      }
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

      {/* Page scrolls naturally; the sidebar sticks. A fixed-height row with its own
          scrollbar used to clip the footer buttons and leave dead space below. */}
      <div className="flex gap-4 items-start">
        {/* Left: project list */}
        <div className="w-64 shrink-0 space-y-4 sticky top-4 overflow-y-auto" style={{ maxHeight: 'calc(100vh - 2rem)' }}>
          <Section label="Active" items={active} selectedId={selectedId} onSelect={setSelectedId} fmt={fmt} privacyMode={privacyMode} />
          {completed.length > 0 && <Section label="Completed" items={completed} selectedId={selectedId} onSelect={setSelectedId} fmt={fmt} privacyMode={privacyMode} />}
          {error && <div className="app-badge-negative text-xs rounded-lg px-3 py-2">{error}</div>}
          {creating ? (
            <CreateForm
              onSubmit={handleCreate}
              onCancel={() => { setCreating(false); setError(null) }}
              saving={busy === 'create'}
              usedColors={(projects || []).map(p => p.color)}
            />
          ) : (
            <button onClick={() => setCreating(true)} className="text-sm text-blue-500 hover:underline">+ New Project</button>
          )}
        </div>

        {/* Right: detail */}
        <div className="flex-1 min-w-0">
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
        <span className="w-4 h-4 rounded-full shrink-0" style={{ backgroundColor: p.color }} />
        <h2 className="text-xl font-bold">{p.name}</h2>
        <span className={`text-xs px-2 py-0.5 rounded-full ${p.status === 'active' ? 'app-badge-positive' : 'app-badge-neutral'}`}>
          {p.status}
        </span>
        {/* Delete lives in Project Settings, deliberately far from this button. */}
        <button
          onClick={onToggleStatus}
          disabled={busy !== null}
          className="app-btn-secondary ml-auto shrink-0 text-xs disabled:opacity-60"
        >
          {busy === 'toggle' ? 'Updating…' : p.status === 'active' ? 'Mark Complete' : 'Reactivate'}
        </button>
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
        <div className="mt-4 border-t border-slate-200/30 pt-3">
          <MembersSection projectId={p.id} members={p.members || []} />
        </div>
        <div className="mt-4 border-t border-slate-200/30 pt-3">
          <DeleteProjectSection name={p.name} busy={busy === 'delete'} onDelete={onDelete} />
        </div>
      </div>

      {/* Member-wise split summary */}
      <SplitwiseCommitPanel projectId={p.id} />

      <MemberSplitSummary transactions={p.transactions} members={p.members || []} memberTotals={p.member_totals} fmt={fmt} fmtSigned={fmtSigned} />

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
                          <div className="h-full rounded" style={{ width: `${(sub.total / totalCategorySpend) * 100}%`, backgroundColor: getCategoryColorFaded(c.category) }} />
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
          projectId={p.id}
          members={p.members || []}
        />
      </div>


    </div>
  )
}


function DeleteProjectSection({ name, busy, onDelete }: { name: string; busy: boolean; onDelete: () => void }) {
  const [open, setOpen] = useState(false)
  const [typed, setTyped] = useState('')
  // Deleting a project also drops its transaction links, and there is no undo. Requiring the
  // name to be typed makes it impossible to do by accident.
  const confirmed = typed.trim() === name

  if (!open) {
    return (
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-xs font-semibold" style={{ color: 'var(--color-negative)' }}>Delete project</div>
          <div className="text-xs text-slate-500">
            Removes the project and its transaction assignments. The transactions themselves stay
            in the ledger. This cannot be undone.
          </div>
        </div>
        <button
          type="button"
          onClick={() => { setOpen(true); setTyped('') }}
          className="app-btn-secondary shrink-0 text-xs"
        >
          Delete…
        </button>
      </div>
    )
  }

  return (
    <div>
      <div className="text-xs font-semibold mb-2" style={{ color: 'var(--color-negative)' }}>
        Type <span className="font-mono">{name}</span> to confirm
      </div>
      <div className="flex items-center gap-2">
        <input
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          placeholder={name}
          autoFocus
          className="app-input min-w-0 flex-1 rounded px-3 py-2 text-sm"
        />
        <button
          type="button"
          onClick={() => {
            // Last gate before an irreversible delete, on top of the typed name.
            if (!window.confirm(`Delete "${name}"? Its transaction assignments and splits go with it. This cannot be undone.`)) return
            onDelete()
          }}
          disabled={!confirmed || busy}
          className="app-badge-negative shrink-0 rounded px-3 py-1.5 text-xs disabled:opacity-40"
        >
          {busy ? 'Deleting…' : 'Delete project'}
        </button>
        <button
          type="button"
          onClick={() => { setOpen(false); setTyped('') }}
          disabled={busy}
          className="app-btn-secondary shrink-0 text-xs"
        >
          Cancel
        </button>
      </div>
    </div>
  )
}

function CreateForm({
  onSubmit,
  onCancel,
  saving,
  usedColors = [],
}: {
  onSubmit: (data: Partial<ProjectSummary>, memberIds: string[]) => void
  onCancel: () => void
  saving: boolean
  usedColors?: string[]
}) {
  const [name, setName] = useState('')
  const [color, setColor] = useState(() => pickRandomColor(usedColors, COLORS))
  const [budget, setBudget] = useState('')
  const [contacts, setContacts] = useState<Contact[]>([])
  const [selectedMembers, setSelectedMembers] = useState<Set<string>>(new Set())
  const [newMemberName, setNewMemberName] = useState('')
  const [newMemberColor, setNewMemberColor] = useState(() => pickRandomColor([]))
  const [showColorPicker, setShowColorPicker] = useState(false)

  useEffect(() => {
    api.getContacts().then(list => {
      setContacts(list)
      const ore = list.find(c => c.name === 'ore')
      if (ore) setSelectedMembers(new Set([ore.id]))
      setNewMemberColor(pickRandomColor(list.map(c => c.color)))
    }).catch(() => {})
  }, [])

  const toggleMember = (id: string) => {
    setSelectedMembers(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const addNewMember = async () => {
    if (!newMemberName.trim()) return
    try {
      const c = await api.createContact(newMemberName.trim(), newMemberColor)
      setContacts(prev => [...prev, c])
      setSelectedMembers(prev => new Set([...prev, c.id]))
      setNewMemberName('')
      setNewMemberColor(pickRandomColor([...contacts.map(ct => ct.color), c.color]))
      setShowColorPicker(false)
    } catch { /* dupe */ }
  }

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
      <div>
        <div className="text-[10px] text-slate-500 uppercase tracking-wide mb-1">Members</div>
        <div className="flex flex-wrap gap-1.5 mb-1.5">
          {contacts.map(c => (
            <button key={c.id} type="button" onClick={() => toggleMember(c.id)}
              className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] border ${selectedMembers.has(c.id) ? 'border-blue-500 bg-blue-500/10' : 'border-slate-500/30 opacity-50'}`}
              style={{ color: c.color }}
            >
              <span className="w-2 h-2 rounded-full" style={{ backgroundColor: c.color }} />
              {c.name}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-1">
          <input value={newMemberName} onChange={e => setNewMemberName(e.target.value)} placeholder="+ New person"
            className="app-input px-2 py-1 text-[11px] rounded w-24"
            onKeyDown={e => { if (e.key === 'Enter') void addNewMember() }} />
          {newMemberName && (
            <>
              <span className="w-4 h-4 rounded-full cursor-pointer border border-slate-500/30" style={{ backgroundColor: newMemberColor }} onClick={() => setShowColorPicker(!showColorPicker)} title="Click to change color" />
              {showColorPicker && <input type="color" value={newMemberColor} onChange={e => setNewMemberColor(e.target.value)} className="w-5 h-5 rounded cursor-pointer" />}
              <button onClick={() => void addNewMember()} className="text-[11px] text-blue-500">Add</button>
            </>
          )}
        </div>
      </div>
      <div className="flex gap-2">
        <button onClick={() => name.trim() && onSubmit({ name: name.trim(), color, budget: budget ? parseFloat(budget) : undefined }, Array.from(selectedMembers))}
          className="app-btn-primary text-xs disabled:opacity-60" disabled={!name.trim() || saving}>{saving ? 'Creating…' : 'Create'}</button>
        <button onClick={onCancel} disabled={saving} className="app-btn-secondary text-xs disabled:opacity-60">Cancel</button>
      </div>
    </div>
  )
}


function MembersSection({ projectId, members }: { projectId: string; members: Contact[] }) {
  const qc = useQueryClient()
  const [contacts, setContacts] = useState<Contact[]>([])
  const [adding, setAdding] = useState(false)
  const [newName, setNewName] = useState('')
  const [newColor, setNewColor] = useState(() => pickRandomColor([]))
  const [showColorPicker, setShowColorPicker] = useState(false)

  useEffect(() => {
    api.getContacts().then(list => {
      setContacts(list)
      setNewColor(pickRandomColor(list.map(c => c.color)))
    }).catch(() => {})
  }, [])

  const unassigned = contacts.filter(c => !members.some(m => m.id === c.id))

  const addMember = async (contactId: string) => {
    await api.addProjectMembers(projectId, [contactId])
    await qc.invalidateQueries({ queryKey: ['project', projectId] })
  }

  const removeMember = async (contactId: string) => {
    try {
      await api.removeProjectMember(projectId, contactId)
      await qc.invalidateQueries({ queryKey: ['project', projectId] })
    } catch (e) {
      console.error('Failed to remove member:', e)
    }
  }

  const createAndAdd = async () => {
    if (!newName.trim()) return
    try {
      const contact = await api.createContact(newName.trim(), newColor)
      setContacts(prev => [...prev, contact])
      await api.addProjectMembers(projectId, [contact.id])
      await qc.invalidateQueries({ queryKey: ['project', projectId] })
      setNewName('')
      setNewColor(pickRandomColor([...contacts.map(c => c.color), contact.color]))
      setShowColorPicker(false)
      setAdding(false)
    } catch { /* ignore dupe */ }
  }

  return (
    <div className="app-surface rounded-lg p-4 mb-5">
      <h3 className="text-sm font-semibold mb-3">Members (Split)</h3>
      <div className="flex flex-wrap gap-2 mb-3">
        {members.map(m => (
          <span key={m.id} className="inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs" style={{ backgroundColor: `${m.color}22`, color: m.color, border: `1px solid ${m.color}44` }}>
            <span className="w-2 h-2 rounded-full" style={{ backgroundColor: m.color }} />
            {m.name}
            <button onClick={() => void removeMember(m.id)} className="ml-0.5 hover:opacity-70">×</button>
          </span>
        ))}
      </div>
      <div className="flex items-center gap-2">
        {unassigned.length > 0 && (
          <select
            className="app-input px-2 py-1.5 text-xs rounded"
            value=""
            onChange={e => { if (e.target.value) void addMember(e.target.value) }}
          >
            <option value="">Add existing...</option>
            {unassigned.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        )}
        {adding ? (
          <div className="flex items-center gap-1">
            <input
              autoFocus
              className="app-input px-2 py-1.5 text-xs rounded w-28"
              placeholder="Name"
              value={newName}
              onChange={e => setNewName(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') void createAndAdd() }}
            />
            <span className="w-5 h-5 rounded-full cursor-pointer border border-slate-500/30" style={{ backgroundColor: newColor }} onClick={() => setShowColorPicker(!showColorPicker)} title="Click to change color" />
            {showColorPicker && <input type="color" value={newColor} onChange={e => setNewColor(e.target.value)} className="w-5 h-5 rounded cursor-pointer" />}
            <button onClick={() => void createAndAdd()} className="text-xs text-blue-500 hover:underline">Add</button>
            <button onClick={() => { setAdding(false); setShowColorPicker(false) }} className="text-xs text-slate-400 hover:underline">Cancel</button>
          </div>
        ) : (
          <button onClick={() => setAdding(true)} className="text-xs text-blue-500 hover:underline">+ New person</button>
        )}
      </div>
    </div>
  )
}


function MemberSplitSummary({
  transactions,
  members,
  memberTotals,
  fmt,
  fmtSigned,
}: {
  transactions: Transaction[]
  members: Contact[]
  memberTotals?: MemberTotal[]
  fmt: (n: number) => string
  fmtSigned: (n: number) => string
}) {
  if (!members || members.length === 0) return null

  const totals = useMemo(() => {
    // Prefer the server's figures: they are the same numbers a Splitwise commit will use,
    // so the UI can't drift from what actually gets pushed. The local pass below is a
    // fallback for a response that predates member_totals.
    if (memberTotals && memberTotals.length > 0) {
      return memberTotals.filter(m => m.expenditure > 0 || m.income > 0)
    }
    const map = new Map<string, { expenditure: number; income: number }>()
    for (const m of members) map.set(m.id, { expenditure: 0, income: 0 })
    for (const txn of transactions) {
      const splits = txn.splits || []
      if (splits.length === 0) continue
      const equalShare = Math.abs(txn.amount) / splits.length
      for (const s of splits) {
        const total = map.get(s.id)
        if (!total) continue
        // An explicit share wins; otherwise the transaction divides equally.
        const share = s.share_amount == null ? equalShare : Math.abs(s.share_amount)
        if (txn.amount < 0) total.expenditure += share
        else if (txn.amount > 0) total.income += share
      }
    }
    return members
      .map(m => {
        const total = map.get(m.id) || { expenditure: 0, income: 0 }
        return { ...m, ...total, net: total.income - total.expenditure }
      })
      .filter(m => m.expenditure > 0 || m.income > 0)
  }, [transactions, members, memberTotals])

  if (totals.length === 0) return null

  return (
    <div className="app-surface rounded-lg p-4 mb-5">
      <h3 className="text-sm font-semibold mb-3">Split Summary</h3>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[520px] text-sm">
          <thead>
            <tr className="border-b border-slate-200/20 text-xs text-slate-500">
              <th className="py-2 text-left font-medium">Person</th>
              <th className="py-2 text-right font-medium">Expenditure</th>
              <th className="py-2 text-right font-medium">Income</th>
              <th className="py-2 text-right font-medium">Net</th>
            </tr>
          </thead>
          <tbody>
            {totals.map(m => (
              <tr key={m.id} className="border-b border-slate-200/10 last:border-0">
                <td className="py-2.5 text-left">
                  <span className="inline-flex items-center gap-2">
                    <span className="w-3 h-3 rounded-full shrink-0" style={{ backgroundColor: m.color }} />
                    <span className="truncate">{m.name}</span>
                  </span>
                </td>
                <td className="py-2.5 text-right">{fmt(m.expenditure)}</td>
                <td className="py-2.5 text-right" style={m.income > 0 ? { color: 'var(--color-positive)' } : undefined}>{fmt(m.income)}</td>
                <td className="py-2.5 text-right font-semibold" style={{ color: m.net > 0 ? 'var(--color-positive)' : m.net < 0 ? 'var(--color-negative)' : undefined }}>{fmtSigned(m.net)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function SplitwiseCommitPanel({ projectId }: { projectId: string }) {
  const [summary, setSummary] = useState<SplitwiseCommitSummary | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastRun, setLastRun] = useState<string | null>(null)

  const load = () => {
    api.previewSplitwiseCommit(projectId).then(setSummary).catch(() => setSummary(null))
  }

  useEffect(load, [projectId])

  const commit = async (amend: boolean) => {
    setBusy(true)
    setError(null)
    try {
      const result = await api.commitSplitwiseProject(projectId, amend)
      setSummary(result)
      const pushed = (result.created ?? 0) + (result.updated ?? 0)
      setLastRun(
        result.done
          ? `All ${result.committed} committed.`
          : `Pushed ${pushed}; ${result.pending} still queued — press again.`,
      )
    } catch (err: any) {
      setError(err?.message || 'Commit failed')
      load()
    } finally {
      setBusy(false)
    }
  }

  // Hidden entirely unless Splitwise is connected, so the integration stays optional.
  if (!summary || summary.connected === false) return null

  const blocked = summary.unmapped_members.length > 0
  const nothingToDo = summary.pending === 0 && summary.failed.length === 0

  return (
    <div className="app-surface rounded-lg p-4 mb-5">
      <div className="mb-2 flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold">Splitwise</h3>
        <span className="text-xs text-slate-500">
          {summary.committed} of {summary.transactions} committed
        </span>
      </div>

      {blocked ? (
        <div className="mb-2 rounded border border-amber-400/50 bg-amber-500/10 px-3 py-2 text-xs">
          Link these people in Settings first: <strong>{summary.unmapped_members.join(', ')}</strong>
        </div>
      ) : null}

      {summary.failed.length > 0 ? (
        <div className="mb-2 rounded border border-rose-400/50 bg-rose-500/10 px-3 py-2 text-xs">
          {summary.failed.length} failed. {summary.failed[0].error}
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={busy || blocked || nothingToDo}
          onClick={() => void commit(false)}
          className="app-btn-primary text-xs disabled:opacity-50"
          title={
            blocked ? 'Link everyone to Splitwise first'
              : nothingToDo ? 'Nothing pending'
              : `Commits up to ${summary.batch_size} at a time`
          }
        >
          {busy ? 'Committing…' : nothingToDo ? 'Up to date' : `Commit ${Math.min(summary.pending, summary.batch_size)} of ${summary.pending}`}
        </button>
        {summary.committed > 0 ? (
          <button
            type="button"
            disabled={busy || blocked}
            onClick={() => void commit(true)}
            className="app-btn-secondary text-xs disabled:opacity-50"
            title="Re-push transactions whose shares changed since they were committed"
          >
            Amend changed
          </button>
        ) : null}
        {lastRun ? <span className="text-[11px] text-slate-500">{lastRun}</span> : null}
      </div>

      {error ? <div className="mt-2 text-xs" style={{ color: 'var(--color-negative)' }}>{error}</div> : null}
    </div>
  )
}
