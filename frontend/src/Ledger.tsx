import { useState, useMemo, useRef, useCallback, useEffect } from 'react'
import { AgGridReact } from 'ag-grid-react'
import { AllCommunityModule, ModuleRegistry, type ColDef, type ICellRendererParams, type SortDirection } from 'ag-grid-community'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { createPortal } from 'react-dom'
import { getCategoryColor } from './colors'
import { CATEGORY_ORDER } from './colors'
import { useFilterStore } from './store'
import { useProjects } from './hooks'
import { getGridTheme } from './gridTheme'
import { api } from './api'
import type { Contact, ProjectDetail, Transaction, TransactionCategoryOption } from './api'
import { formatCurrency } from './privacy'

ModuleRegistry.registerModules([AllCommunityModule])

const DEFAULT_SORTING_ORDER: SortDirection[] = ['asc', 'desc']

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
          data-split-filter-popup
          className="fixed z-[1300] min-w-[160px] rounded-lg p-2 shadow-lg app-elevated"
          style={{ top: rect.bottom + 4, left: rect.left - 120 }}
        >
          <div className="space-y-0.5 max-h-48 overflow-y-auto">
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

function MerchantCell(props: ICellRendererParams) {
  const txn = props.data as Transaction
  const label = txn.merchant_clean || txn.merchant_raw
  return (
    <div className="flex min-w-0 items-center gap-2">
      <span className="truncate">{label}</span>
      {txn.pending ? (
        <span className="shrink-0 text-[10px] font-medium" style={{ color: 'var(--text-muted)' }}>(pending)</span>
      ) : null}
    </div>
  )
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
  const [projectMenu, setProjectMenu] = useState<{ txnId: string; anchor: DOMRect; mode: 'summary' | 'edit' } | null>(null)
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

    const handlePointerDown = () => setProjectMenu(null)
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setProjectMenu(null)
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
    const menuLeft = projectMenu ? Math.max(12, Math.min(projectMenu.anchor.left, window.innerWidth - 236)) : 0
    const menuTop = projectMenu ? Math.min(projectMenu.anchor.bottom + 8, window.innerHeight - 120) : 0
    const projectTitle = assigned.map((project) => project.name).join(', ')
    const unassignedProjects = (allProjects || []).filter((project) => !assigned.some((current) => current.id === project.id))
    const openMenu = (element: HTMLElement, mode: 'summary' | 'edit') => {
      const anchor = element.getBoundingClientRect()
      setProjectMenu(activeMenu === mode ? null : { txnId: txn.id, anchor, mode })
    }

    return (
      <div className="flex h-full w-full items-center gap-1 overflow-hidden">
        {assigned.length > 0 ? (
          <button
            type="button"
            title={projectTitle}
            className="min-w-0 self-center flex-1 inline-flex items-center justify-start gap-1 rounded-md border px-2 py-1"
            style={{ borderColor: 'var(--border-default)', background: 'var(--surface-secondary)' }}
            onClick={(event) => {
              event.stopPropagation()
              openMenu(event.currentTarget as HTMLElement, 'summary')
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
        <span className="h-5 w-5 shrink-0 self-center flex items-center justify-center rounded-full border border-dashed cursor-pointer hover:border-blue-500 hover:text-blue-500 text-sm leading-none"
          style={{ borderColor: 'var(--border-default)', color: 'var(--text-muted)' }}
          onClick={(event) => {
            event.stopPropagation()
            openMenu(event.currentTarget as HTMLElement, 'edit')
          }}>+</span>
        {activeMenu === 'summary' && createPortal(
          <div
            className="fixed z-[1200] min-w-[220px] rounded-lg p-2 shadow-lg app-elevated"
            style={{ left: menuLeft, top: menuTop }}
            onPointerDown={(event) => event.stopPropagation()}
          >
            <div className="mb-1 px-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">Projects</div>
            <div className="space-y-0.5">
              {assigned.map((project) => (
                <div
                  key={project.id}
                  className="flex items-center gap-2 rounded px-2 py-1.5 text-xs"
                  style={{ color: 'var(--text-primary)' }}
                >
                  <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: project.color }} />
                  <span className="truncate">{project.name}</span>
                </div>
              ))}
            </div>
          </div>,
          document.body,
        )}
        {activeMenu === 'edit' && allProjects && createPortal(
          <div
            className="fixed z-[1200] min-w-[220px] rounded-lg p-2 shadow-lg app-elevated"
            style={{ left: menuLeft, top: menuTop }}
            onPointerDown={(event) => event.stopPropagation()}
          >
            {assigned.length > 0 ? (
              <div className="mb-2">
                <div className="mb-1 px-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">Assigned</div>
                <div className="space-y-0.5">
                  {assigned.map((project) => (
                    <button
                      key={project.id}
                      type="button"
                      className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs app-hover"
                      style={{ color: 'var(--text-primary)' }}
                      onClick={() => {
                        toggleProject(txn.id, project.id, true)
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
            {unassignedProjects.length > 0 ? (
              <div>
                <div className="mb-1 px-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">Add to project</div>
                <div className="space-y-0.5">
                  {unassignedProjects.map((project) => (
                    <button
                      key={project.id}
                      type="button"
                      className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs app-hover"
                      onClick={() => {
                        toggleProject(txn.id, project.id, false)
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
            {allProjects.length === 0 && <div className="px-2 py-1 text-xs text-slate-500">No projects yet. Create one in Projects.</div>}
          </div>,
          document.body,
        )}
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

  const ProjectMerchantCell = useCallback((props: ICellRendererParams) => {
    const txn = props.data as Transaction
    const label = txn.merchant_clean || txn.merchant_raw
    const isEditing = editingDesc === txn.id
    const saveDescription = async (value: string) => {
      if (!projectId) return
      await api.updateTransactionProject(projectId, txn.id, { description: value || '' })
      txn.description = value || undefined
      setEditingDesc(null)
      props.api.refreshCells({ rowNodes: [props.node], force: true })
    }
    return (
      <div className="flex flex-col justify-center min-w-0 py-1">
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate">{label}</span>
          {txn.pending ? (
            <span className="shrink-0 text-[10px] font-medium" style={{ color: 'var(--text-muted)' }}>(pending)</span>
          ) : null}
        </div>
        {isEditing ? (
          <input
            autoFocus
            className="mt-0.5 text-[11px] bg-transparent border-b border-blue-400 outline-none w-full"
            style={{ color: 'var(--text-muted)' }}
            value={descDraft}
            onChange={(e) => setDescDraft(e.target.value)}
            onBlur={() => void saveDescription(descDraft)}
            onKeyDown={(e) => { if (e.key === 'Enter') void saveDescription(descDraft); if (e.key === 'Escape') setEditingDesc(null) }}
          />
        ) : (
          <span
            className="mt-0.5 text-[11px] truncate cursor-pointer hover:text-blue-400"
            style={{ color: 'var(--text-muted)', fontStyle: txn.description ? 'normal' : 'italic' }}
            onClick={() => { setEditingDesc(txn.id); setDescDraft(txn.description || '') }}
          >
            {txn.description || 'Add note...'}
          </span>
        )}
      </div>
    )
  }, [projectId, editingDesc, descDraft])

  const SplitCell = useCallback((props: ICellRendererParams) => {
    const txn = props.data as Transaction
    const splits = txn.splits || []
    const [menuOpen, setMenuOpen] = useState(false)
    const [menuPos, setMenuPos] = useState({ left: 0, top: 0 })

    const toggleMember = async (contactId: string) => {
      if (!projectId) return
      const current = txn.splits || []
      const has = current.some(s => s.id === contactId)
      const next = has ? current.filter(s => s.id !== contactId) : [...current, (members || []).find(m => m.id === contactId)!]
      await api.updateTransactionSplits(projectId, txn.id, next.map(s => s.id))
      txn.splits = next
      props.api.refreshCells({ rowNodes: [props.node], force: true })
    }

    return (
      <div className="flex h-full w-full items-center gap-1 overflow-hidden">
        {splits.length > 0 ? (
          <button
            type="button"
            title={splits.map(s => s.name).join(', ')}
            className="min-w-0 self-center flex-1 inline-flex items-center justify-start gap-1 rounded-md border px-2 py-1"
            style={{ borderColor: 'var(--border-default)', background: 'var(--surface-secondary)' }}
            onClick={(e) => { e.stopPropagation(); const r = e.currentTarget.getBoundingClientRect(); setMenuPos({ left: r.left, top: r.bottom + 8 }); setMenuOpen(!menuOpen) }}
          >
            <span className="flex shrink-0 items-center gap-1">
              {splits.slice(0, 4).map(s => (
                <span key={s.id} className="h-3 w-3 rounded-full" style={{ backgroundColor: s.color }} />
              ))}
            </span>
            {splits.length > 4 ? <span className="ml-1 text-[10px] font-semibold" style={{ color: 'var(--text-secondary)' }}>+{splits.length - 4}</span> : null}
          </button>
        ) : null}
        {!splits.length && members && members.length > 0 ? (
          <span
            className="h-5 w-5 shrink-0 self-center flex items-center justify-center rounded-full border border-dashed cursor-pointer hover:border-blue-500 hover:text-blue-500 text-sm leading-none"
            style={{ borderColor: 'var(--border-default)', color: 'var(--text-muted)' }}
            onClick={(e) => { e.stopPropagation(); const r = e.currentTarget.getBoundingClientRect(); setMenuPos({ left: r.left, top: r.bottom + 8 }); setMenuOpen(true) }}
          >+</span>
        ) : null}
        {menuOpen && members && createPortal(
          <div
            className="fixed z-[1200] min-w-[200px] rounded-lg p-2 shadow-lg app-elevated"
            style={{ left: Math.min(menuPos.left, window.innerWidth - 220), top: Math.min(menuPos.top, window.innerHeight - 150) }}
            onPointerDown={(e) => e.stopPropagation()}
          >
            <div className="mb-1 px-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">Split with</div>
            <div className="space-y-0.5">
              {members.map(m => {
                const active = splits.some(s => s.id === m.id)
                return (
                  <button
                    key={m.id}
                    className={`flex w-full items-center gap-2 rounded px-2 py-1.5 text-xs ${active ? 'app-selected' : 'app-hover'}`}
                    onClick={() => void toggleMember(m.id)}
                  >
                    <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: m.color }} />
                    <span className="truncate flex-1 text-left">{m.name}</span>
                    {active ? <span className="text-blue-500">✓</span> : null}
                  </button>
                )
              })}
            </div>
            <button className="mt-1 w-full text-center text-[10px] text-slate-400 hover:text-slate-200" onClick={() => setMenuOpen(false)}>Close</button>
          </div>,
          document.body,
        )}
      </div>
    )
  }, [projectId, members])

  const colDefs = useMemo<ColDef[]>(() => [
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
      cellRenderer: projectId ? ProjectMerchantCell : MerchantCell,
      ...(projectId ? { cellStyle: { lineHeight: '1.2', paddingTop: '2px', paddingBottom: '2px' }, autoHeight: true } : {}) },
    { field: 'category', headerName: 'Category', flex: 1,
      cellRenderer: EditableCategoryCell,
      valueGetter: (p: any) => (p.data.category || 'Uncategorized').split('/')[0] },
    { headerName: 'Sub', flex: 1,
      cellRenderer: EditableSubcategoryCell,
      valueGetter: (p: any) => (p.data.category || '').split('/')[1] || '—' },
    { field: 'source', headerName: 'Account', width: 150 },
    {
      field: 'projects',
      headerName: 'Projects',
      width: 140,
      maxWidth: 160,
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
      width: 150,
      maxWidth: 180,
      cellRenderer: SplitCell,
      filter: false,
      sortable: false,
      headerComponent: SplitFilterHeader,
      headerComponentParams: { members, onFilterChange: (ids: string[]) => setSplitFilterIds(ids) },
      cellStyle: { overflow: 'visible', display: 'flex', alignItems: 'center', paddingTop: '0', paddingBottom: '0' },
      valueFormatter: () => '',
    }] : []),
    { field: 'amount', headerName: 'Amount', width: 130, type: 'rightAligned',
      cellRenderer: AmountCell, filter: false, sortingOrder: ['asc', 'desc', null] },
  ], [EditableCategoryCell, EditableSubcategoryCell, ProjectCell, ProjectMerchantCell, SplitCell, SplitFilterHeader, projectId, members, setSplitFilterIds])

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
      <div style={{ width: '100%', height: 15 * 42 + 90 }}>
        <AgGridReact
          ref={gridRef}
          theme={gridTheme}
          rowData={filtered}
          columnDefs={colDefs}
          defaultColDef={defaultColDef}
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
