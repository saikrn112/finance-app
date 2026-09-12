import { useState, useMemo, useRef, useCallback, useEffect } from 'react'
import { AgGridReact } from 'ag-grid-react'
import { AllCommunityModule, ModuleRegistry, type ColDef, type ICellRendererParams, type SortDirection } from 'ag-grid-community'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { createPortal } from 'react-dom'
import { getCategoryColor } from './colors'
import { CATEGORY_ORDER } from './colors'
import { useFilterStore } from './store'
import { useProjects } from './hooks'
import { sortByRecentActivity } from './projectSort'
import { getGridTheme } from './gridTheme'
import { api } from './api'
import type { ProjectDetail, Transaction, TransactionCategoryOption } from './api'
import { formatCurrency } from './privacy'

ModuleRegistry.registerModules([AllCommunityModule])

const DEFAULT_SORTING_ORDER: SortDirection[] = ['asc', 'desc']

// Keep in sync with COLORS in ProjectsPage.tsx.
const PROJECT_COLORS = [
  '#22c55e', '#3b82f6', '#eab308', '#f97316', '#ef4444', '#8b5cf6', '#ec4899', '#06b6d4',
  '#14b8a6', '#a855f7', '#84cc16', '#0ea5e9', '#f43f5e', '#d946ef', '#10b981', '#6366f1',
]

function splitCategory(category: string | null | undefined) {
  const normalized = category || 'Uncategorized'
  const [topCategory, subcategory = ''] = normalized.split('/', 2)
  return {
    topCategory: topCategory || 'Uncategorized',
    subcategory,
  }
}

function joinCategory(topCategory: string, subcategory: string) {
  if (!topCategory || topCategory === 'Uncategorized') return 'Uncategorized'
  return subcategory ? `${topCategory}/${subcategory}` : topCategory
}

function buildProjectCategories(transactions: Transaction[]) {
  const categories = new Map<string, { category: string; total: number; subcategories: Map<string, number> }>()
  for (const txn of transactions) {
    if (txn.amount >= 0) continue
    const normalized = txn.category || 'Uncategorized'
    const [topCategory, subcategory = ''] = normalized.split('/', 2)
    const bucket = categories.get(topCategory) || {
      category: topCategory,
      total: 0,
      subcategories: new Map<string, number>(),
    }
    const magnitude = Math.abs(txn.amount)
    bucket.total += magnitude
    if (subcategory) {
      bucket.subcategories.set(subcategory, (bucket.subcategories.get(subcategory) || 0) + magnitude)
    }
    categories.set(topCategory, bucket)
  }
  return Array.from(categories.values())
    .sort((left, right) => right.total - left.total)
    .map((bucket) => ({
      category: bucket.category,
      total: bucket.total,
      subcategories: Array.from(bucket.subcategories.entries())
        .sort((left, right) => right[1] - left[1])
        .map(([name, total]) => ({ name, total })),
    }))
}

function SplitFilterHeader(props: any) {
  const { members, onFilterChange } = props
  const [open, setOpen] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const btnRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!open) return
    const handler = (e: PointerEvent) => {
      const target = e.target as HTMLElement
      if (target.closest('[data-split-filter-popup]') || target === btnRef.current) return
      setOpen(false)
    }
    document.addEventListener('pointerdown', handler)
    return () => document.removeEventListener('pointerdown', handler)
  }, [open])

  const toggle = (id: string) => {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      onFilterChange(Array.from(next))
      return next
    })
  }

  const clearAll = () => {
    setSelected(new Set())
    onFilterChange([])
  }

  const hasFilter = selected.size > 0
  const rect = btnRef.current?.getBoundingClientRect()

  return (
    <div className="ag-header-cell-label" style={{ display: 'flex', alignItems: 'center', gap: '4px', width: '100%' }}>
      <span className="ag-header-cell-text">Split</span>
      <button
        ref={btnRef}
        onPointerDown={e => { e.stopPropagation(); e.preventDefault(); setOpen(!open) }}
        style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', justifyContent: 'center', width: 20, height: 20, borderRadius: 4, color: hasFilter ? '#3b82f6' : 'var(--text-muted)' }}
        title="Filter by people"
      >
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M22 3H2l8 9.46V19l4 2v-8.54L22 3z"/></svg>
      </button>
      {open && rect && createPortal(
        <div
          data-split-filter-popup="true"
          className="fixed z-[1300] min-w-[160px] rounded-lg p-2 shadow-lg app-elevated"
          style={{ top: rect.bottom + 4, left: rect.left - 120 }}
        >
          <div className="space-y-0.5 max-h-48 overflow-y-auto overscroll-contain">
            {(members || []).map((m: any) => (
              <button
                key={m.id}
                className={`flex w-full items-center gap-2 rounded px-2 py-1.5 text-xs ${selected.has(m.id) ? 'app-selected' : 'app-hover'}`}
                onClick={() => toggle(m.id)}
              >
                <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: m.color }} />
                <span className="truncate flex-1 text-left">{m.name}</span>
                {selected.has(m.id) ? <span className="text-blue-500 text-[10px]">✓</span> : null}
              </button>
            ))}
          </div>
          {hasFilter && (
            <button onClick={clearAll} className="mt-1 w-full text-center text-[10px] text-slate-400 hover:text-blue-400">Clear filter</button>
          )}
        </div>,
        document.body,
      )}
    </div>
  )
}

function AmountCell(props: ICellRendererParams) {
  const v = props.value as number
  const privacyMode = useFilterStore((state) => state.privacyMode)
  const displayCurrency = useFilterStore((state) => state.displayCurrency)
  return <span className={v > 0 ? 'text-green-500' : ''}>{formatCurrency(v, privacyMode, { currency: displayCurrency })}</span>
}


export function Ledger({
  transactions,
  title = 'Transactions',
  useGlobalFilters = true,
  helperText,
  projectId,
  members,
}: {
  transactions: Transaction[]
  title?: string
  useGlobalFilters?: boolean
  helperText?: string
  projectId?: string
  members?: { id: string; name: string; color: string }[]
}) {
  const gridRef = useRef<AgGridReact>(null)
  const storeSearch = useFilterStore(s => s.search)
  const setStoreSearch = useFilterStore(s => s.setSearch)
  const storeCategory = useFilterStore(s => s.category) || ''
  const setStoreCategory = (v: string) => useFilterStore.getState().setCategory(v || null)
  const displayCurrency = useFilterStore(s => s.displayCurrency)
  const dark = useFilterStore(s => s.dark)
  const gridTheme = useMemo(() => getGridTheme(), [dark])
  const [localSearch, setLocalSearch] = useState('')
  const [localCategory, setLocalCategory] = useState('')
  const [acctFilter, setAcctFilter] = useState('')
  const { data: allProjects } = useProjects()
  const qc = useQueryClient()
  const [projectMenu, setProjectMenu] = useState<{ txnId: string; anchor: DOMRect; mode: 'edit' } | null>(null)

  // Split popup lives here, not in the cell renderer: it holds amount inputs, and form
  // state in a cell renderer's deps remounts the input on every keystroke.
  const [splitMenu, setSplitMenu] = useState<{ txnId: string; anchor: DOMRect } | null>(null)
  const [splitMode, setSplitMode] = useState<'equal' | 'unequal'>('equal')
  const [splitDraft, setSplitDraft] = useState<Record<string, string>>({})
  const [splitSaving, setSplitSaving] = useState(false)
  const [splitError, setSplitError] = useState<string | null>(null)

  useEffect(() => {
    if (!splitMenu) return
    const handler = (event: PointerEvent) => {
      const target = event.target as HTMLElement | null
      if (target?.closest('[data-split-menu]') || target?.closest('[data-split-trigger]')) return
      setSplitMenu(null)
    }
    document.addEventListener('pointerdown', handler)
    return () => document.removeEventListener('pointerdown', handler)
  }, [splitMenu])

  const splitTxn = splitMenu ? transactions.find(t => t.id === splitMenu.txnId) ?? null : null

  // Seed the draft from whatever the transaction currently has whenever the popup opens.
  useEffect(() => {
    if (!splitTxn) return
    const mode = splitTxn.split_mode === 'unequal' ? 'unequal' : 'equal'
    setSplitMode(mode)
    setSplitError(null)
    const seeded: Record<string, string> = {}
    for (const sp of splitTxn.splits || []) {
      if (sp.share_amount != null) seeded[sp.id] = sp.share_amount.toFixed(2)
    }
    setSplitDraft(seeded)
  }, [splitMenu?.txnId])

  const [categoryEditor, setCategoryEditor] = useState<{
    txnId: string
    anchor: DOMRect
    topCategory: string
    subcategory: string
    saving: boolean
    error: string | null
  } | null>(null)

  const fallbackCategoryOptions = useMemo<TransactionCategoryOption[]>(() => {
    const byCategory = new Map<string, Set<string>>()
    CATEGORY_ORDER.forEach(category => byCategory.set(category, new Set()))
    transactions.forEach((txn) => {
      const { topCategory, subcategory } = splitCategory(txn.category)
      if (!byCategory.has(topCategory)) byCategory.set(topCategory, new Set())
      if (subcategory) byCategory.get(topCategory)?.add(subcategory)
    })
    if (!byCategory.has('Uncategorized')) byCategory.set('Uncategorized', new Set())
    return Array.from(byCategory.entries()).map(([category, subcategories]) => ({
      category,
      subcategories: Array.from(subcategories).sort((left, right) => left.localeCompare(right)),
    }))
  }, [transactions])

  const { data: categoryOptionsData } = useQuery({
    queryKey: ['transaction-category-options'],
    queryFn: () => api.getTransactionCategoryOptions(),
    staleTime: 0,
    refetchOnMount: 'always',
  })

  const categoryOptions = categoryOptionsData?.categories?.length ? categoryOptionsData.categories : fallbackCategoryOptions
  const subcategoriesByCategory = useMemo(
    () => new Map(categoryOptions.map((option) => [option.category, option.subcategories])),
    [categoryOptions],
  )

  const toggleProject = useCallback(async (txnId: string, projectId: string, assigned: boolean) => {
    if (assigned) {
      await api.removeFromProject(projectId, txnId)
    } else {
      await api.addToProject(projectId, [txnId])
    }
    qc.invalidateQueries({ queryKey: ['transactions'] })
    qc.invalidateQueries({ queryKey: ['projects'] })
    qc.invalidateQueries({ queryKey: ['project'] })
  }, [qc])

  const [creatingProject, setCreatingProject] = useState(false)
  const [newProjectName, setNewProjectName] = useState('')
  const [newProjectColor, setNewProjectColor] = useState(PROJECT_COLORS[0])
  const [newProjectBudget, setNewProjectBudget] = useState('')
  const [newProjectMemberIds, setNewProjectMemberIds] = useState<string[]>([])
  const [contacts, setContacts] = useState<{ id: string; name: string; color: string }[]>([])
  const [savingProject, setSavingProject] = useState(false)
  const [projectError, setProjectError] = useState<string | null>(null)

  const resetProjectForm = useCallback(() => {
    setCreatingProject(false)
    setNewProjectName('')
    setNewProjectBudget('')
    setNewProjectMemberIds([])
    setProjectError(null)
  }, [])

  const openProjectForm = useCallback(() => {
    // Pre-pick an unused colour so the user usually doesn't have to choose.
    const used = (allProjects || []).map((p) => p.color)
    const available = PROJECT_COLORS.filter((c) => !used.includes(c))
    const pool = available.length > 0 ? available : PROJECT_COLORS
    setNewProjectColor(pool[Math.floor(Math.random() * pool.length)])
    setNewProjectName('')
    setNewProjectBudget('')
    setNewProjectMemberIds([])
    setProjectError(null)
    setCreatingProject(true)
    api.getContacts().then(setContacts).catch(() => setContacts([]))
  }, [allProjects])

  // Creates the project only. The transaction is *not* attached — the user picks it from
  // the "Add to project" list afterwards, so assignment stays a deliberate action.
  const submitNewProject = useCallback(async () => {
    const name = newProjectName.trim()
    if (!name) return
    setSavingProject(true)
    setProjectError(null)
    try {
      const budget = newProjectBudget.trim() ? Number(newProjectBudget) : undefined
      const created = await api.createProject({
        name,
        color: newProjectColor,
        ...(budget !== undefined && !Number.isNaN(budget) ? { budget } : {}),
      })
      if (newProjectMemberIds.length > 0) {
        await api.addProjectMembers(created.id, newProjectMemberIds)
      }
      // Refresh so the new project appears in "Add to project" immediately, with the
      // popup left open for the user to click it.
      await qc.invalidateQueries({ queryKey: ['projects'] })
      resetProjectForm()
    } catch (err) {
      setProjectError(err instanceof Error ? err.message : 'Could not create project')
    } finally {
      setSavingProject(false)
    }
  }, [qc, newProjectName, newProjectColor, newProjectBudget, newProjectMemberIds, resetProjectForm])

  const updateTransactionCaches = useCallback((updated: Transaction) => {
    qc.setQueriesData({ queryKey: ['transactions'] }, (current: unknown) => {
      if (!current || typeof current !== 'object' || !('transactions' in current)) return current
      const payload = current as { transactions?: Transaction[]; total?: number; offset?: number; limit?: number }
      if (!Array.isArray(payload.transactions)) return current
      return {
        ...payload,
        transactions: payload.transactions.map((txn) => (txn.id === updated.id ? { ...txn, ...updated } : txn)),
      }
    })
    qc.setQueriesData({ queryKey: ['project'] }, (current: unknown) => {
      if (!current || typeof current !== 'object' || !('transactions' in current)) return current
      const payload = current as ProjectDetail
      if (!Array.isArray(payload.transactions)) return current
      const nextTransactions = payload.transactions.map((txn) => (txn.id === updated.id ? { ...txn, ...updated } : txn))
      return {
        ...payload,
        transactions: nextTransactions,
        categories: buildProjectCategories(nextTransactions),
      }
    })
  }, [qc])

  const invalidateAnalytics = useCallback(() => {
    qc.invalidateQueries({ queryKey: ['transactions'] })
    qc.invalidateQueries({ queryKey: ['summary'] })
    qc.invalidateQueries({ queryKey: ['by-category'] })
    qc.invalidateQueries({ queryKey: ['by-merchant'] })
    qc.invalidateQueries({ queryKey: ['all-trends'] })
    qc.invalidateQueries({ queryKey: ['project'] })
    qc.invalidateQueries({ queryKey: ['projects'] })
  }, [qc])

  const openCategoryEditor = useCallback((txn: Transaction, anchor: DOMRect) => {
    const { topCategory, subcategory } = splitCategory(txn.category)
    setCategoryEditor({
      txnId: txn.id,
      anchor,
      topCategory,
      subcategory,
      saving: false,
      error: null,
    })
  }, [])

  const saveCategoryEditor = useCallback(async () => {
    if (!categoryEditor) return
    const nextCategory = joinCategory(categoryEditor.topCategory, categoryEditor.subcategory)
    setCategoryEditor((current) => (current ? { ...current, saving: true, error: null } : current))
    try {
      const updated = await api.updateTransaction(categoryEditor.txnId, { category: nextCategory }, displayCurrency)
      updateTransactionCaches(updated)
      setCategoryEditor(null)
      invalidateAnalytics()
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Failed to update category'
      setCategoryEditor((current) => (current ? { ...current, saving: false, error: message } : current))
    }
  }, [categoryEditor, invalidateAnalytics, updateTransactionCaches, displayCurrency])

  useEffect(() => {
    if (!projectMenu) return

    // Only dismiss for interactions *outside* the popup. This listener used to close
    // unconditionally, and the popup survived by calling stopPropagation on its own
    // pointerdown — which broke the moment the popup grew a scrollable list and a form,
    // since `scroll` is registered in the capture phase and fires for the popup's own
    // scrolling too.
    const isInsidePopup = (target: EventTarget | null) => {
      const el = target as HTMLElement | null
      return Boolean(el?.closest?.('[data-project-menu]') || el?.closest?.('[data-project-trigger]'))
    }

    const handlePointerDown = (event: PointerEvent) => {
      if (isInsidePopup(event.target)) return
      setProjectMenu(null)
    }
    const handleScroll = (event: Event) => {
      if (isInsidePopup(event.target)) return
      setProjectMenu(null)
    }
    const handleResize = () => setProjectMenu(null)
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setProjectMenu(null)
    }

    document.addEventListener('pointerdown', handlePointerDown)
    document.addEventListener('keydown', handleEscape)
    window.addEventListener('resize', handleResize)
    window.addEventListener('scroll', handleScroll, true)

    return () => {
      document.removeEventListener('pointerdown', handlePointerDown)
      document.removeEventListener('keydown', handleEscape)
      window.removeEventListener('resize', handleResize)
      window.removeEventListener('scroll', handleScroll, true)
    }
  }, [projectMenu])

  useEffect(() => {
    if (!categoryEditor) return

    const handlePointerDown = () => setCategoryEditor(null)
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setCategoryEditor(null)
    }

    document.addEventListener('pointerdown', handlePointerDown)
    document.addEventListener('keydown', handleEscape)
    window.addEventListener('resize', handlePointerDown)
    window.addEventListener('scroll', handlePointerDown, true)

    return () => {
      document.removeEventListener('pointerdown', handlePointerDown)
      document.removeEventListener('keydown', handleEscape)
      window.removeEventListener('resize', handlePointerDown)
      window.removeEventListener('scroll', handlePointerDown, true)
    }
  }, [categoryEditor])

  const ProjectCell = useCallback((props: ICellRendererParams) => {
    const txn = props.data as Transaction
    const assigned = txn.projects || []
    const activeMenu = projectMenu?.txnId === txn.id ? projectMenu.mode : null
    const projectTitle = assigned.map((project) => project.name).join(', ')
    // Both the dot cluster and the + open the same editable menu — a separate read-only
    // "summary" popup was just a dead end for anyone trying to reassign.
    const openMenu = (element: HTMLElement) => {
      const anchor = element.getBoundingClientRect()
      setProjectMenu(activeMenu === 'edit' ? null : { txnId: txn.id, anchor, mode: 'edit' })
    }

    return (
      <div className="flex h-full w-full items-center gap-1 overflow-hidden">
        {assigned.length > 0 ? (
          <button
            type="button"
            data-project-trigger="true"
            title={projectTitle}
            className="min-w-0 self-center flex-1 inline-flex items-center justify-start gap-1 rounded-md border px-2 py-1"
            style={{ borderColor: 'var(--border-default)', background: 'var(--surface-secondary)' }}
            onClick={(event) => {
              event.stopPropagation()
              openMenu(event.currentTarget as HTMLElement)
            }}
          >
            <span className="flex shrink-0 items-center gap-1">
              {assigned.slice(0, 3).map((project) => (
                <span
                  key={project.id}
                  className="h-3 w-3 rounded-full"
                  style={{ backgroundColor: project.color }}
                />
              ))}
            </span>
            {assigned.length > 3 ? (
              <span className="ml-1 text-[10px] font-semibold" style={{ color: 'var(--text-secondary)' }}>+{assigned.length - 3}</span>
            ) : null}
          </button>
        ) : null}
        <span
          data-project-trigger="true"
          className="h-5 w-5 shrink-0 self-center flex items-center justify-center rounded-full border border-dashed cursor-pointer hover:border-blue-500 hover:text-blue-500 text-sm leading-none"
          style={{ borderColor: 'var(--border-default)', color: 'var(--text-muted)' }}
          onClick={(event) => {
            event.stopPropagation()
            openMenu(event.currentTarget as HTMLElement)
          }}>+</span>
      </div>
    )
  }, [projectMenu, allProjects, toggleProject])

  const EditableCategoryCell = useCallback((props: ICellRendererParams) => {
    const txn = props.data as Transaction
    const { topCategory } = splitCategory(txn.category)
    const color = getCategoryColor(topCategory)
    const isEditing = categoryEditor?.txnId === txn.id

    return (
      <button
        type="button"
        className={`flex h-full w-full items-center gap-1.5 rounded px-1 text-left ${isEditing ? 'app-selected' : 'app-hover'}`}
        onClick={(event) => {
          event.stopPropagation()
          const anchor = (event.currentTarget as HTMLElement).getBoundingClientRect()
          openCategoryEditor(txn, anchor)
        }}
      >
        <span className="inline-block h-2.5 w-2.5 flex-shrink-0 rounded-full" style={{ backgroundColor: color }} />
        <span className="truncate">{topCategory}</span>
      </button>
    )
  }, [categoryEditor?.txnId, openCategoryEditor])

  const EditableSubcategoryCell = useCallback((props: ICellRendererParams) => {
    const txn = props.data as Transaction
    const { subcategory } = splitCategory(txn.category)
    const isEditing = categoryEditor?.txnId === txn.id

    return (
      <button
        type="button"
        className={`flex h-full w-full items-center rounded px-1 text-left ${isEditing ? 'app-selected' : 'app-hover'}`}
        style={{ color: 'var(--text-muted)' }}
        onClick={(event) => {
          event.stopPropagation()
          const anchor = (event.currentTarget as HTMLElement).getBoundingClientRect()
          openCategoryEditor(txn, anchor)
        }}
      >
        <span className="truncate">{subcategory || '—'}</span>
      </button>
    )
  }, [categoryEditor?.txnId, openCategoryEditor])

  // The project popup is rendered from the Ledger body, not from inside the cell. Keeping
  // it in the cell renderer meant every keystroke in the "new project" field changed
  // ProjectCell's identity -> colDefs changed -> ag-grid rebuilt columns -> the input
  // remounted and lost focus. It also died whenever the row scrolled out of view.
  // --- Split popup behaviour -------------------------------------------------------
  const splitAssigned = splitTxn?.splits ?? []
  const splitTotal = Math.abs(splitTxn?.amount ?? 0)
  const splitAssignedSum = splitAssigned.reduce(
    (sum, sp) => sum + (parseFloat(splitDraft[sp.id] ?? '') || 0), 0,
  )
  const splitRemainder = Math.round((splitTotal - splitAssignedSum) * 100) / 100
  const splitBalanced = Math.abs(splitRemainder) < 0.005

  const refreshSplitRow = useCallback((txnId: string) => {
    const node = gridRef.current?.api?.getRowNode?.(txnId)
    if (node) gridRef.current?.api?.refreshCells({ rowNodes: [node], force: true })
  }, [])

  // Equal mode: toggling a person saves immediately, exactly as before.
  const toggleSplitMember = useCallback(async (contactId: string) => {
    if (!projectId || !splitTxn) return
    const current = splitTxn.splits || []
    const has = current.some(sp => sp.id === contactId)
    const member = (members || []).find(m => m.id === contactId)
    if (!has && !member) return
    const next = has
      ? current.filter(sp => sp.id !== contactId)
      : [...current, { ...member!, share_amount: null }]
    try {
      await api.updateTransactionSplits(projectId, splitTxn.id, next.map(sp => sp.id))
      splitTxn.splits = next
      splitTxn.split_mode = 'equal'
      setSplitDraft(prev => {
        const copy = { ...prev }
        delete copy[contactId]
        return copy
      })
      refreshSplitRow(splitTxn.id)
    } catch (err) {
      setSplitError(err instanceof Error ? err.message : 'Could not update split')
    }
  }, [projectId, splitTxn, members, refreshSplitRow])

  const saveUnequalSplit = useCallback(async () => {
    if (!projectId || !splitTxn || !splitBalanced) return
    setSplitSaving(true)
    setSplitError(null)
    try {
      const ids = splitAssigned.map(sp => sp.id)
      const amounts: Record<string, number> = {}
      for (const id of ids) amounts[id] = parseFloat(splitDraft[id] ?? '') || 0
      await api.updateTransactionSplits(projectId, splitTxn.id, ids, amounts)
      splitTxn.splits = splitAssigned.map(sp => ({ ...sp, share_amount: amounts[sp.id] }))
      splitTxn.split_mode = 'unequal'
      refreshSplitRow(splitTxn.id)
      setSplitMenu(null)
      qc.invalidateQueries({ queryKey: ['project'] })
    } catch (err) {
      setSplitError(err instanceof Error ? err.message : 'Could not save split')
    } finally {
      setSplitSaving(false)
    }
  }, [projectId, splitTxn, splitAssigned, splitDraft, splitBalanced, refreshSplitRow, qc])

  const revertToEqualSplit = useCallback(async () => {
    if (!projectId || !splitTxn) return
    setSplitSaving(true)
    setSplitError(null)
    try {
      const ids = splitAssigned.map(sp => sp.id)
      await api.updateTransactionSplits(projectId, splitTxn.id, ids)
      splitTxn.splits = splitAssigned.map(sp => ({ ...sp, share_amount: null }))
      splitTxn.split_mode = 'equal'
      refreshSplitRow(splitTxn.id)
      setSplitMode('equal')
      setSplitDraft({})
      qc.invalidateQueries({ queryKey: ['project'] })
    } catch (err) {
      setSplitError(err instanceof Error ? err.message : 'Could not reset split')
    } finally {
      setSplitSaving(false)
    }
  }, [projectId, splitTxn, splitAssigned, refreshSplitRow, qc])

  const fillSplitEvenly = useCallback(() => {
    if (!splitAssigned.length) return
    const each = splitTotal / splitAssigned.length
    const seeded: Record<string, string> = {}
    splitAssigned.forEach((sp, index) => {
      // Put any rounding remainder on the first person so the total lands exactly.
      const value = index === 0
        ? splitTotal - Number((each).toFixed(2)) * (splitAssigned.length - 1)
        : each
      seeded[sp.id] = value.toFixed(2)
    })
    setSplitDraft(seeded)
  }, [splitAssigned, splitTotal])

  const projectMenuTxn = projectMenu ? transactions.find((t) => t.id === projectMenu.txnId) ?? null : null
  const projectMenuAssigned = projectMenuTxn?.projects ?? []
  // Same order as the Projects sidebar: most recent activity first.
  const projectMenuUnassigned = sortByRecentActivity(
    (allProjects || []).filter(
      (project) => !projectMenuAssigned.some((current) => current.id === project.id),
    ),
  )
  const menuLeft = projectMenu ? Math.max(12, Math.min(projectMenu.anchor.left, window.innerWidth - 236)) : 0
  const menuTop = projectMenu ? Math.min(projectMenu.anchor.bottom + 8, Math.max(16, window.innerHeight - 320)) : 0

  const categoryMenuLeft = categoryEditor ? Math.max(12, Math.min(categoryEditor.anchor.left, window.innerWidth - 320)) : 0
  const categoryMenuTop = categoryEditor ? Math.min(categoryEditor.anchor.bottom + 8, window.innerHeight - 220) : 0
  const availableSubcategories = categoryEditor ? (subcategoriesByCategory.get(categoryEditor.topCategory) || []) : []
  const showSubcategorySelect = !!categoryEditor && categoryEditor.topCategory !== 'Uncategorized' && availableSubcategories.length > 0

  const categories = CATEGORY_ORDER
  const globalSearch = useGlobalFilters ? storeSearch : localSearch
  const setGlobalSearch = useGlobalFilters ? setStoreSearch : setLocalSearch
  const catFilter = useGlobalFilters ? storeCategory : localCategory
  const setCatFilter = useGlobalFilters ? setStoreCategory : setLocalCategory

  const accounts = useMemo(() => {
    const s = new Set(transactions.map(t => t.source))
    return Array.from(s).sort()
  }, [transactions])

  const [editingDesc, setEditingDesc] = useState<string | null>(null)
  const [splitFilterIds, setSplitFilterIds] = useState<string[]>([])
  const [descDraft, setDescDraft] = useState('')

  // Merchant cell with an attached note. The note lives on the transaction in both views:
  // a project is only a grouping, so a note must survive the project being deleted. Empty
  // notes stay hidden until row hover so the table isn't noisy.
  const NoteMerchantCell = useCallback((props: ICellRendererParams) => {
    const txn = props.data as Transaction | undefined
    if (!txn) return null
    const label = props.value || txn.merchant_clean || txn.merchant_raw || 'Unknown merchant'
    // `description` is the legacy per-project note; fall back to it so older rows still
    // render until they are migrated.
    const note = (txn.notes ?? txn.description) || ''
    const isEditing = editingDesc === txn.id

    const save = async (value: string) => {
      const trimmed = value.trim()
      try {
        await api.updateTransaction(txn.id, { notes: trimmed || null }, displayCurrency)
        txn.notes = trimmed || null
        txn.description = undefined
      } catch (err) {
        console.error('Failed to save note:', err)
      }
      setEditingDesc(null)
      props.api.refreshCells({ rowNodes: [props.node], force: true })
    }

    const startEdit = (event: React.MouseEvent) => {
      event.stopPropagation()
      setEditingDesc(txn.id)
      setDescDraft(note)
    }

    const noteInput = (
      <input
        autoFocus
        className="text-[11px] bg-transparent border-b border-blue-400 outline-none w-full"
        style={{ color: 'var(--text-muted)' }}
        value={descDraft}
        onChange={(e) => setDescDraft(e.target.value)}
        onClick={(e) => e.stopPropagation()}
        onBlur={() => void save(descDraft)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') void save(descDraft)
          if (e.key === 'Escape') setEditingDesc(null)
        }}
      />
    )

    const addAffordance = (
      <span
        className="shrink-0 text-[11px] cursor-pointer opacity-0 group-hover:opacity-100 transition-opacity hover:text-blue-400"
        style={{ color: 'var(--text-muted)' }}
        onClick={startEdit}
      >
        + note
      </span>
    )

    // Single line in both views: merchant, then the note inline. Keeps row height
    // uniform and the table quiet when most rows have no note.
    return (
      <div className="group flex h-full min-w-0 items-center gap-2">
        <span className="truncate">{label}</span>
        {txn.pending ? (
          <span className="shrink-0 text-[10px] font-medium" style={{ color: 'var(--text-muted)' }}>(pending)</span>
        ) : null}
        {isEditing ? (
          <span className="min-w-0 flex-1">{noteInput}</span>
        ) : note ? (
          <span
            title={note}
            className="min-w-0 flex-1 truncate text-[11px] cursor-pointer hover:text-blue-400"
            style={{ color: 'var(--text-muted)' }}
            onClick={startEdit}
          >
            — {note}
          </span>
        ) : addAffordance}
      </div>
    )
  }, [projectId, editingDesc, descDraft, displayCurrency])

  // Triggers only. The popup itself is rendered from the Ledger body: it carries amount
  // inputs, and form state in a cell renderer's dependency array remounts the input on
  // every keystroke (and row virtualization unmounts the popup outright).
  const SplitCell = useCallback((props: ICellRendererParams) => {
    const txn = props.data as Transaction | undefined
    if (!txn) return null
    const splits = txn.splits || []
    const unequal = txn.split_mode === 'unequal'

    const open = (element: HTMLElement) => {
      const anchor = element.getBoundingClientRect()
      setSplitMenu(splitMenu?.txnId === txn.id ? null : { txnId: txn.id, anchor })
    }

    return (
      <div className="flex h-full w-full items-center gap-1 overflow-hidden">
        {splits.length > 0 ? (
          <button
            type="button"
            data-split-trigger="true"
            title={`${splits.map(s => s.name).join(', ')}${unequal ? ' (unequal)' : ''}`}
            className="min-w-0 self-center flex-1 inline-flex items-center justify-start gap-1 rounded-md border px-2 py-1"
            style={{ borderColor: 'var(--border-default)', background: 'var(--surface-secondary)' }}
            onClick={(e) => { e.stopPropagation(); open(e.currentTarget as HTMLElement) }}
          >
            <span className="flex shrink-0 items-center gap-1">
              {splits.slice(0, 4).map(s => (
                <span key={s.id} className="h-3 w-3 rounded-full" style={{ backgroundColor: s.color }} />
              ))}
            </span>
            {splits.length > 4 ? <span className="ml-1 text-[10px] font-semibold" style={{ color: 'var(--text-secondary)' }}>+{splits.length - 4}</span> : null}
            {/* Marker for an uneven split. Sized and line-height-pinned to match the dots:
                a bare glyph carries a full line box, which grew the button past the row
                height and painted outside the cell (it has overflow: visible). */}
            {unequal ? (
              <span
                aria-label="Unequal split"
                title="Unequal split"
                className="ml-0.5 flex h-3 w-3 shrink-0 items-center justify-center rounded-sm text-[9px] font-bold leading-none text-blue-400"
                style={{ lineHeight: 1, background: 'color-mix(in srgb, var(--surface-secondary) 60%, transparent)' }}
              >
                ≠
              </span>
            ) : null}
          </button>
        ) : null}
        {!splits.length && members && members.length > 0 ? (
          <span
            data-split-trigger="true"
            className="h-5 w-5 shrink-0 self-center flex items-center justify-center rounded-full border border-dashed cursor-pointer hover:border-blue-500 hover:text-blue-500 text-sm leading-none"
            style={{ borderColor: 'var(--border-default)', color: 'var(--text-muted)' }}
            onClick={(e) => { e.stopPropagation(); open(e.currentTarget as HTMLElement) }}
          >+</span>
        ) : null}
      </div>
    )
  }, [members, splitMenu])

  const colDefs = useMemo(() => ([
    {
      field: 'effective_date',
      headerName: 'Date',
      width: 120,
      sort: 'desc',
      filter: false,
      valueGetter: (p: any) => p.data.effective_date || p.data.authorized_date || p.data.date,
      tooltipValueGetter: (p: any) => {
        const effective = p.data.effective_date || p.data.authorized_date || p.data.date
        const posted = p.data.date
        return effective !== posted ? `Authorized ${effective} • Posted ${posted}` : effective
      },
    },
    { field: 'merchant_clean', headerName: 'Merchant', flex: 2,
      tooltipValueGetter: (p: any) => p.data.merchant_clean || p.data.merchant_raw,
      valueGetter: (p: any) => p.data.merchant_clean || p.data.merchant_raw,
      cellRenderer: NoteMerchantCell },
    { field: 'category', headerName: 'Category', flex: 1,
      cellRenderer: EditableCategoryCell,
      valueGetter: (p: any) => (p.data.category || 'Uncategorized').split('/')[0] },
    { headerName: 'Sub', flex: 1,
      cellRenderer: EditableSubcategoryCell,
      valueGetter: (p: any) => (p.data.category || '').split('/')[1] || '—' },
    { field: 'source', headerName: 'Account', width: projectId ? 110 : 150 },
    {
      field: 'projects',
      headerName: 'Projects',
      width: projectId ? 90 : 140,
      maxWidth: projectId ? 100 : 160,
      cellRenderer: ProjectCell,
      filter: false,
      sortable: false,
      cellStyle: {
        overflow: 'visible',
        display: 'flex',
        alignItems: 'center',
        paddingTop: '0',
        paddingBottom: '0',
      },
      valueFormatter: () => '',
    },
    ...(projectId && members && members.length > 0 ? [{
      field: 'splits',
      headerName: 'Split',
      width: 110,
      maxWidth: 130,
      cellRenderer: SplitCell,
      filter: false,
      sortable: false,
      headerComponent: SplitFilterHeader,
      headerComponentParams: { members, onFilterChange: (ids: string[]) => setSplitFilterIds(ids) },
      cellStyle: { overflow: 'visible', display: 'flex', alignItems: 'center', paddingTop: '0', paddingBottom: '0' },
      valueFormatter: () => '',
    }] as ColDef[] : []),
    { field: 'amount', headerName: 'Amount', width: 130, type: 'rightAligned',
      cellRenderer: AmountCell, filter: false, sortingOrder: ['asc', 'desc', null] },
  ] as ColDef[]), [EditableCategoryCell, EditableSubcategoryCell, ProjectCell, NoteMerchantCell, SplitCell, projectId, members])

  const defaultColDef = useMemo(() => ({
    sortable: true,
    resizable: false,
    filter: true,
    sortingOrder: DEFAULT_SORTING_ORDER,
  }), [])

  const filtered = useMemo(() => {
    let result = transactions
    if (globalSearch) {
      const q = globalSearch.toLowerCase()
      result = result.filter(t =>
        (t.merchant_clean || t.merchant_raw || '').toLowerCase().includes(q))
    }
    if (catFilter) result = result.filter(t => (t.category || 'Uncategorized').split('/')[0] === catFilter)
    if (acctFilter) result = result.filter(t => t.source === acctFilter)
    if (splitFilterIds.length > 0) {
      result = result.filter(t => {
        const txnSplitIds = new Set((t.splits || []).map(s => s.id))
        return splitFilterIds.every(id => txnSplitIds.has(id)) && txnSplitIds.size === splitFilterIds.length
      })
    }
    return result
  }, [transactions, globalSearch, catFilter, acctFilter, splitFilterIds])

  const hasFilters = globalSearch || catFilter || acctFilter || splitFilterIds.length > 0

  // Size the grid to its content (capped at 15 rows) so short lists don't leave a
  // dead gap and long lists still scroll internally. Project rows are taller
  // because the merchant cell carries a second description line.
  // Project view: let ag-grid size itself to its rows (short lists, variable row height
  // from the description line) so there's never dead space under the table.
  // Main view: keep a fixed viewport with internal scrolling — it can hold thousands of rows.
  const gridHeight = useMemo(() => {
    if (projectId) return undefined
    const visibleRows = Math.min(Math.max(filtered.length, 1), 15)
    return visibleRows * 42 + 90 // rows + header + pagination bar
  }, [filtered.length, projectId])

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-3">
          <h2 className="font-semibold">{title} ({filtered.length})</h2>
          {helperText ? <span className="text-xs" style={{ color: 'var(--text-muted)' }}>{helperText}</span> : null}
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <select value={catFilter} onChange={e => setCatFilter(e.target.value)}
            className="app-input px-2 py-1.5 text-xs rounded">
            <option value="">All Categories</option>
            {categories.map(c => <option key={c} value={c}>{c}</option>)}
          </select>
          <select value={acctFilter} onChange={e => setAcctFilter(e.target.value)}
            className="app-input px-2 py-1.5 text-xs rounded">
            <option value="">All Accounts</option>
            {accounts.map(a => <option key={a} value={a}>{a}</option>)}
          </select>
          <input type="text" placeholder="Search merchant..." value={globalSearch}
            onChange={e => setGlobalSearch(e.target.value)}
            className="app-input px-2 py-1.5 text-xs rounded w-48" />
          {hasFilters && (
            <button onClick={() => { setGlobalSearch(''); setCatFilter(''); setAcctFilter(''); setSplitFilterIds([]) }}
              className="text-xs text-blue-500 hover:underline">Clear</button>
          )}
        </div>
      </div>
      <div style={{ width: '100%', height: gridHeight }}>
        <AgGridReact
          ref={gridRef}
          theme={gridTheme}
          rowData={filtered}
          columnDefs={colDefs}
          defaultColDef={defaultColDef}
          {...(projectId ? { domLayout: 'autoHeight' as const } : {})}
          pagination={true}
          paginationPageSize={200}
          paginationPageSizeSelector={[50, 100, 200, 500]}
          animateRows={true}
          getRowId={(p) => p.data.id}
          suppressHorizontalScroll={true}
          enableCellTextSelection={true}
          ensureDomOrder={true}
        />
      </div>
      {splitMenu && splitTxn && members && members.length > 0 && createPortal(
        (() => {
          const top = Math.min(splitMenu.anchor.bottom + 8, Math.max(16, window.innerHeight - 300))
          return (
            <div
              data-split-menu="true"
              className="fixed z-[1200] flex min-w-[240px] flex-col rounded-lg p-2 shadow-lg app-elevated"
              style={{
                left: Math.min(splitMenu.anchor.left, window.innerWidth - 260),
                top,
                maxHeight: `calc(100vh - ${top + 16}px)`,
              }}
            >
              <div className="mb-1 flex shrink-0 items-center justify-between gap-2 px-1">
                <span className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">Split with</span>
                <div className="flex overflow-hidden rounded border" style={{ borderColor: 'var(--border-default)' }}>
                  <button
                    type="button"
                    className={`px-1.5 py-0.5 text-[10px] ${splitMode === 'equal' ? 'bg-blue-500/20 text-blue-400' : 'text-slate-400'}`}
                    onClick={() => { if (splitMode !== 'equal') void revertToEqualSplit() }}
                  >
                    Equal
                  </button>
                  <button
                    type="button"
                    className={`px-1.5 py-0.5 text-[10px] ${splitMode === 'unequal' ? 'bg-blue-500/20 text-blue-400' : 'text-slate-400'}`}
                    onClick={() => {
                      setSplitMode('unequal')
                      // Start from the current even division so the boxes are never blank.
                      if (Object.keys(splitDraft).length === 0) fillSplitEvenly()
                    }}
                  >
                    Unequal
                  </button>
                </div>
              </div>

              <div className="min-h-0 flex-1 space-y-0.5 overflow-y-auto overscroll-contain">
                {members.map(m => {
                  const active = splitAssigned.some(sp => sp.id === m.id)
                  return (
                    <div key={m.id} className={`flex items-center gap-2 rounded px-2 py-1.5 text-xs ${active ? 'app-selected' : ''}`}>
                      <button
                        type="button"
                        className="flex min-w-0 flex-1 items-center gap-2 text-left"
                        onClick={() => void toggleSplitMember(m.id)}
                        title={active ? 'Remove from split' : 'Add to split'}
                      >
                        <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: m.color }} />
                        <span className="truncate flex-1">{m.name}</span>
                        {active && splitMode === 'equal' ? <span className="text-blue-500">✓</span> : null}
                      </button>
                      {splitMode === 'unequal' && active ? (
                        <input
                          type="number"
                          step="0.01"
                          min="0"
                          value={splitDraft[m.id] ?? ''}
                          onChange={(e) => setSplitDraft(prev => ({ ...prev, [m.id]: e.target.value }))}
                          className="app-input w-20 shrink-0 rounded px-1.5 py-0.5 text-right text-[11px]"
                          placeholder="0.00"
                        />
                      ) : null}
                    </div>
                  )
                })}
              </div>

              {splitMode === 'unequal' ? (
                <div className="mt-1 shrink-0 border-t pt-1" style={{ borderColor: 'var(--border-default)' }}>
                  <div className="flex items-center justify-between px-1 text-[10px]" style={{ color: 'var(--text-muted)' }}>
                    <span>assigned {formatCurrency(splitAssignedSum, false, { currency: displayCurrency })}</span>
                    <span style={{ color: splitBalanced ? 'var(--color-positive)' : 'var(--color-negative)' }}>
                      {splitBalanced ? 'balanced' : `left ${formatCurrency(splitRemainder, false, { currency: displayCurrency })}`}
                    </span>
                  </div>
                  <div className="mt-1 flex items-center justify-between gap-2 px-1">
                    <button type="button" onClick={fillSplitEvenly} className="text-[10px] text-slate-400 hover:text-blue-400">
                      Split evenly
                    </button>
                    <button
                      type="button"
                      disabled={!splitBalanced || splitSaving || splitAssigned.length === 0}
                      onClick={() => void saveUnequalSplit()}
                      className="text-[11px] text-blue-500 hover:underline disabled:opacity-50"
                      title={splitBalanced ? 'Save shares' : 'Shares must add up to the transaction amount'}
                    >
                      {splitSaving ? 'Saving…' : 'Save'}
                    </button>
                  </div>
                  {splitError ? (
                    <div className="px-1 pt-1 text-[10px]" style={{ color: 'var(--color-negative)' }}>{splitError}</div>
                  ) : null}
                </div>
              ) : splitError ? (
                <div className="mt-1 px-1 text-[10px]" style={{ color: 'var(--color-negative)' }}>{splitError}</div>
              ) : null}
            </div>
          )
        })(),
        document.body,
      )}
      {projectMenu && allProjects && projectMenuTxn && createPortal(
        <div
          data-project-menu="true"
          className="fixed z-[1200] flex min-w-[220px] flex-col rounded-lg p-2 shadow-lg app-elevated"
          style={{ left: menuLeft, top: menuTop, maxHeight: `calc(100vh - ${menuTop + 16}px)` }}
        >
          {/* Lists scroll; the New-project footer stays pinned so it can't drift
              off-screen once there are a lot of projects. */}
          <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
          {creatingProject ? (
            <div className="space-y-2 px-1 py-0.5">
              <div className="px-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">New project</div>
              <input
                autoFocus
                value={newProjectName}
                placeholder="Project name"
                className="app-input w-full rounded px-2 py-1 text-xs"
                onChange={(event) => setNewProjectName(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') void submitNewProject()
                  if (event.key === 'Escape') resetProjectForm()
                }}
              />
              <div className="flex items-center gap-2">
                <span className="text-[10px] text-slate-400">Colour</span>
                <div className="flex flex-wrap gap-1">
                  {PROJECT_COLORS.slice(0, 8).map((c) => (
                    <button
                      key={c}
                      type="button"
                      aria-label={`Use colour ${c}`}
                      onClick={() => setNewProjectColor(c)}
                      className={`h-4 w-4 rounded-full ${newProjectColor === c ? 'ring-2 ring-blue-500 ring-offset-1' : ''}`}
                      style={{ backgroundColor: c }}
                    />
                  ))}
                </div>
              </div>
              <input
                value={newProjectBudget}
                placeholder="Budget (optional)"
                type="number"
                className="app-input w-full rounded px-2 py-1 text-xs"
                onChange={(event) => setNewProjectBudget(event.target.value)}
              />
              <div>
                <div className="mb-1 px-1 text-[10px] text-slate-400">Split with</div>
                {contacts.length === 0 ? (
                  <div className="px-1 text-[10px] text-slate-500">No people yet — add them in Settings.</div>
                ) : (
                  <div className="flex flex-wrap gap-1">
                    {contacts.map((c) => {
                      const on = newProjectMemberIds.includes(c.id)
                      return (
                        <button
                          key={c.id}
                          type="button"
                          onClick={() => setNewProjectMemberIds((prev) => (
                            prev.includes(c.id) ? prev.filter((id) => id !== c.id) : [...prev, c.id]
                          ))}
                          className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] ${on ? 'border-blue-500 bg-blue-500/10' : 'border-slate-500/30 opacity-60'}`}
                          style={{ color: c.color }}
                        >
                          <span className="h-2 w-2 rounded-full" style={{ backgroundColor: c.color }} />
                          {c.name}
                        </button>
                      )
                    })}
                  </div>
                )}
              </div>
            </div>
          ) : (
          <>
          {projectMenuAssigned.length > 0 ? (
            <div className="mb-2">
              <div className="mb-1 px-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">Assigned</div>
              <div className="space-y-0.5">
                {projectMenuAssigned.map((project) => (
                  <button
                    key={project.id}
                    type="button"
                    className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs app-hover"
                    style={{ color: 'var(--text-primary)' }}
                    onClick={() => {
                      toggleProject(projectMenuTxn.id, project.id, true)
                      setProjectMenu(null)
                    }}
                  >
                    <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: project.color }} />
                    <span className="flex-1 truncate">{project.name}</span>
                    <span className="text-[10px] leading-none" style={{ color: 'var(--text-muted)' }}>×</span>
                  </button>
                ))}
              </div>
            </div>
          ) : null}
          {projectMenuUnassigned.length > 0 ? (
            <div>
              <div className="mb-1 px-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">Add to project</div>
              <div className="space-y-0.5">
                {projectMenuUnassigned.map((project) => (
                  <button
                    key={project.id}
                    type="button"
                    className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs app-hover"
                    onClick={() => {
                      toggleProject(projectMenuTxn.id, project.id, false)
                      setProjectMenu(null)
                    }}
                  >
                    <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: project.color }} />
                    <span className="flex-1 truncate" style={{ color: 'var(--text-primary)' }}>{project.name}</span>
                    <span className="text-slate-400">+</span>
                  </button>
                ))}
              </div>
            </div>
          ) : null}
          </>
          )}
          </div>
          <div className="mt-1 shrink-0 border-t pt-1" style={{ borderColor: 'var(--border-default)' }}>
            {creatingProject ? (
              <div className="flex items-center justify-end gap-2 px-1 py-0.5">
                <button
                  type="button"
                  className="text-[11px] text-slate-400 hover:underline"
                  onClick={resetProjectForm}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  disabled={!newProjectName.trim() || savingProject}
                  className="text-[11px] text-blue-500 hover:underline disabled:opacity-50"
                  onClick={() => void submitNewProject()}
                >
                  {savingProject ? 'Creating…' : 'Create'}
                </button>
              </div>
            ) : (
              <button
                type="button"
                className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs app-hover text-blue-500"
                onClick={() => openProjectForm()}
              >
                <span className="text-sm leading-none">+</span>
                <span>New project</span>
              </button>
            )}
            {projectError ? (
              <div className="px-2 pt-1 text-[10px]" style={{ color: 'var(--color-negative)' }}>{projectError}</div>
            ) : null}
          </div>
        </div>,
        document.body,
      )}
      {categoryEditor && createPortal(
        <div
          className="fixed z-[1200] min-w-[280px] rounded-xl p-3 shadow-xl app-elevated"
          style={{ left: categoryMenuLeft, top: categoryMenuTop }}
          onPointerDown={(event) => event.stopPropagation()}
        >
          <div className="mb-3">
            <div className="text-xs uppercase tracking-[0.18em]" style={{ color: 'var(--text-muted)' }}>Edit category</div>
            <div className="mt-1 text-sm font-medium" style={{ color: 'var(--text-primary)' }}>
              {transactions.find((txn) => txn.id === categoryEditor.txnId)?.merchant_clean || 'Transaction'}
            </div>
          </div>

          <div className="space-y-3">
            <label className="block text-xs" style={{ color: 'var(--text-muted)' }}>
              Category
              <select
                value={categoryEditor.topCategory}
                onChange={(event) => {
                  const nextTopCategory = event.target.value
                  const nextSubcategories = subcategoriesByCategory.get(nextTopCategory) || []
                  setCategoryEditor((current) => current ? {
                    ...current,
                    topCategory: nextTopCategory,
                    subcategory: nextSubcategories.includes(current.subcategory) ? current.subcategory : '',
                  } : current)
                }}
                className="app-input mt-1 w-full rounded-lg px-3 py-2 text-sm"
              >
                {categoryOptions.map((option) => (
                  <option key={option.category} value={option.category}>{option.category}</option>
                ))}
              </select>
            </label>

            {showSubcategorySelect ? (
              <label className="block text-xs" style={{ color: 'var(--text-muted)' }}>
                Subcategory
                <select
                  value={categoryEditor.subcategory}
                  onChange={(event) => setCategoryEditor((current) => current ? { ...current, subcategory: event.target.value } : current)}
                  className="app-input mt-1 w-full rounded-lg px-3 py-2 text-sm"
                >
                  <option value="">None</option>
                  {availableSubcategories.map((subcategory) => (
                    <option key={subcategory} value={subcategory}>{subcategory}</option>
                  ))}
                </select>
              </label>
            ) : null}

            {categoryEditor.error ? (
              <div className="rounded-lg border border-rose-500/20 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
                {categoryEditor.error}
              </div>
            ) : null}

            <div className="flex items-center justify-end gap-2">
              <button
                type="button"
                onClick={() => setCategoryEditor(null)}
                disabled={categoryEditor.saving}
                className="app-btn-secondary rounded-lg px-3 py-1.5 text-sm disabled:cursor-not-allowed disabled:opacity-60"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => void saveCategoryEditor()}
                disabled={categoryEditor.saving}
                className="app-btn-primary rounded-lg px-3 py-1.5 text-sm disabled:cursor-not-allowed disabled:opacity-60"
              >
                {categoryEditor.saving ? 'Saving...' : 'Save'}
              </button>
            </div>
          </div>
        </div>,
        document.body,
      )}
    </div>
  )
}
