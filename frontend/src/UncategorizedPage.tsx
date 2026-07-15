import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type TransactionCategoryOption, type UncategorizedApplyResult, type UncategorizedReviewGroup } from './api'
import { useFilterStore } from './store'
import { formatCount, formatCurrency } from './privacy'

function splitCategory(category: string | null | undefined) {
  const normalized = category || 'Uncategorized'
  const [topCategory, subcategory = ''] = normalized.split('/', 2)
  return { topCategory: topCategory || 'Uncategorized', subcategory }
}

function joinCategory(topCategory: string, subcategory: string) {
  if (!topCategory || topCategory === 'Uncategorized') return 'Uncategorized'
  return subcategory ? `${topCategory}/${subcategory}` : topCategory
}

function formatDate(value: string | null) {
  if (!value) return 'Unknown'
  return new Date(`${value}T00:00:00`).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

type StagedChange = {
  merchant_key: string
  display_name: string
  category: string
  merchant_clean: string
  rule_pattern_source: string
  transaction_count: number
  learnRule: boolean
  enabled: boolean
}

export function UncategorizedPage({ onBack }: { onBack: () => void }) {
  const qc = useQueryClient()
  const privacyMode = useFilterStore((state) => state.privacyMode)
  const displayCurrency = useFilterStore((state) => state.displayCurrency)
  const fmt = (n: number) => formatCurrency(n, privacyMode, { currency: displayCurrency })
  const [searchDraft, setSearchDraft] = useState('')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [topCategory, setTopCategory] = useState('Uncategorized')
  const [subcategory, setSubcategory] = useState('')
  const [merchantClean, setMerchantClean] = useState('')
  const [learnRule, setLearnRule] = useState(true)
  const [stagedChanges, setStagedChanges] = useState<StagedChange[]>([])
  const [lastUndoPayload, setLastUndoPayload] = useState<UncategorizedApplyResult['undo_payload'] | null>(null)

  const { data: reviewData, isLoading } = useQuery({
    queryKey: ['uncategorized-review', search, page, displayCurrency],
    queryFn: () => api.getUncategorizedReview(search, page, 100, displayCurrency),
    staleTime: 30_000,
  })
  const { data: categoryOptionsData } = useQuery({
    queryKey: ['transaction-category-options'],
    queryFn: () => api.getTransactionCategoryOptions(),
    staleTime: 0,
    refetchOnMount: 'always',
  })

  const groups = reviewData?.groups || []
  const groupsByKey = useMemo(() => new Map(groups.map((group) => [group.merchant_key, group])), [groups])
  const selectedGroup = groups.find((group) => group.merchant_key === selectedId) || groups[0] || null

  useEffect(() => {
    if (!selectedGroup) {
      setSelectedId(null)
      return
    }
    setSelectedId(selectedGroup.merchant_key)
  }, [selectedGroup?.merchant_key])

  useEffect(() => {
    if (!selectedGroup || groups.some((group) => group.merchant_key === selectedId)) return
    setSelectedId(selectedGroup.merchant_key)
  }, [groups, selectedGroup, selectedId])

  useEffect(() => {
    if (!selectedGroup) return
    const existingStage = stagedChanges.find((change) => change.merchant_key === selectedGroup.merchant_key)
    if (existingStage) {
      const { topCategory: stagedTop, subcategory: stagedSub } = splitCategory(existingStage.category)
      setTopCategory(stagedTop)
      setSubcategory(stagedSub)
      setMerchantClean(existingStage.merchant_clean)
      setLearnRule(existingStage.learnRule)
      return
    }
    const current = splitCategory(selectedGroup.transactions[0]?.category)
    setTopCategory(current.topCategory)
    setSubcategory(current.subcategory)
    setMerchantClean(selectedGroup.merchant_clean_suggestion || selectedGroup.display_name)
    setLearnRule(true)
  }, [selectedGroup?.merchant_key, stagedChanges])

  const categoryOptions = categoryOptionsData?.categories || []
  const availableSubcategories = useMemo(() => {
    const match = categoryOptions.find((option: TransactionCategoryOption) => option.category === topCategory)
    return match?.subcategories || []
  }, [categoryOptions, topCategory])

  useEffect(() => {
    if (topCategory === 'Uncategorized') {
      setSubcategory('')
      return
    }
    if (subcategory && !availableSubcategories.includes(subcategory)) {
      setSubcategory('')
    }
  }, [availableSubcategories, subcategory, topCategory])

  const invalidateFinanceViews = () => {
    qc.invalidateQueries({ queryKey: ['uncategorized-review'] })
    qc.invalidateQueries({ queryKey: ['transactions'] })
    qc.invalidateQueries({ queryKey: ['summary'] })
    qc.invalidateQueries({ queryKey: ['by-category'] })
    qc.invalidateQueries({ queryKey: ['by-merchant'] })
    qc.invalidateQueries({ queryKey: ['all-trends'] })
    qc.invalidateQueries({ queryKey: ['getting-started', 'uncategorized'] })
  }

  const batchApplyMutation = useMutation({
    mutationFn: async () => {
      const queued = stagedChanges.filter((change) => change.enabled)
      if (!queued.length) throw new Error('No staged changes selected')
      const results: UncategorizedApplyResult[] = []
      for (const change of queued) {
        const group = groupsByKey.get(change.merchant_key)
        if (!group) continue
        const result = await api.applyUncategorizedReview({
          merchant_key: change.merchant_key,
          transaction_ids: group.transactions.map((txn) => txn.id),
          category: change.category,
          merchant_clean: change.merchant_clean,
          rule_pattern_source: change.rule_pattern_source,
          apply_to_similar: true,
          save_rule: change.learnRule,
        })
        results.push(result)
      }
      return results
    },
    onSuccess: (results) => {
      setLastUndoPayload(results.flatMap((result) => result.undo_payload))
      const appliedKeys = new Set(stagedChanges.filter((change) => change.enabled).map((change) => change.merchant_key))
      setStagedChanges((current) => current.filter((change) => !appliedKeys.has(change.merchant_key)))
      invalidateFinanceViews()
    },
  })

  const undoMutation = useMutation({
    mutationFn: async () => {
      if (!lastUndoPayload?.length) throw new Error('Nothing to undo')
      return api.undoUncategorizedReview(lastUndoPayload)
    },
    onSuccess: () => {
      setLastUndoPayload(null)
      invalidateFinanceViews()
    },
  })

  const queueCount = stagedChanges.filter((change) => change.enabled).length
  const selectedStage = selectedGroup ? stagedChanges.find((change) => change.merchant_key === selectedGroup.merchant_key) : null

  const handleStageChange = () => {
    if (!selectedGroup) return
    const nextStage: StagedChange = {
      merchant_key: selectedGroup.merchant_key,
      display_name: selectedGroup.display_name,
      category: joinCategory(topCategory, subcategory),
      merchant_clean: merchantClean.trim() || selectedGroup.merchant_clean_suggestion,
      rule_pattern_source: selectedGroup.rule_pattern_source,
      transaction_count: selectedGroup.transaction_count,
      learnRule,
      enabled: true,
    }
    setStagedChanges((current) => {
      const rest = current.filter((change) => change.merchant_key !== nextStage.merchant_key)
      return [nextStage, ...rest]
    })
  }

  return (
    <div className="app-shell min-h-screen p-4" style={{ color: 'var(--text-primary)' }}>
      <div className="mb-5 flex items-center gap-3">
        <button onClick={onBack} className="text-sm text-blue-500 hover:underline">← Back</button>
        <h1 className="text-lg font-bold">Uncategorized</h1>
        <div className="ml-auto text-xs text-slate-500">
          {reviewData ? `${formatCount(reviewData.total_groups, privacyMode)} groups · ${formatCount(reviewData.total_transactions, privacyMode)} txns` : null}
        </div>
      </div>

      <div className="flex gap-4" style={{ height: 'calc(100vh - 100px)' }}>
        <div className="w-80 shrink-0 overflow-y-auto">
          <div className="sticky top-0 z-10 pb-3" style={{ background: 'var(--surface-page)' }}>
          <div className="mb-3">
            <input
              type="text"
              value={searchDraft}
              onChange={(event) => setSearchDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  setPage(1)
                  setSearch(searchDraft.trim())
                }
              }}
              placeholder="Search merchant..."
              className="app-input w-full rounded-lg px-3 py-2 text-sm"
            />
          </div>
          <div className="mb-2 text-xs uppercase tracking-wide text-slate-500">
            Merchant Groups {reviewData ? `(${formatCount(reviewData.total_groups, privacyMode)} total)` : ''}
          </div>
          {reviewData && reviewData.total_pages > 1 ? (
            <div className="app-surface mb-2 flex items-center justify-between rounded-lg px-3 py-2 text-xs dark:border-slate-700 dark:bg-slate-800">
              <button
                type="button"
                onClick={() => setPage((current) => Math.max(1, current - 1))}
                disabled={page <= 1}
                className="rounded px-2 py-1 hover:bg-[var(--app-surface-2)] disabled:opacity-40 dark:hover:bg-slate-700"
              >
                Prev
              </button>
              <span className="text-slate-500">
                Page {reviewData.page} / {reviewData.total_pages}
              </span>
              <button
                type="button"
                onClick={() => setPage((current) => Math.min(reviewData.total_pages, current + 1))}
                disabled={page >= reviewData.total_pages}
                className="rounded px-2 py-1 hover:bg-[var(--app-surface-2)] disabled:opacity-40 dark:hover:bg-slate-700"
              >
                Next
              </button>
            </div>
          ) : null}
          </div>
          {isLoading ? <div className="text-sm text-slate-500">Loading…</div> : null}
          {!isLoading && groups.length === 0 ? <div className="text-sm text-slate-500">No uncategorized groups.</div> : null}
          {groups.map((group) => (
            <GroupCard
              key={group.merchant_key}
              group={group}
              active={selectedGroup?.merchant_key === group.merchant_key}
              staged={stagedChanges.some((change) => change.merchant_key === group.merchant_key)}
              onSelect={() => setSelectedId(group.merchant_key)}
              fmt={fmt}
            />
          ))}
        </div>

        <div className="flex min-w-0 flex-1 flex-col gap-4">
          <div className="min-h-0 flex-1 overflow-y-auto">
            {selectedGroup ? (
              <div className="space-y-4">
                <div className="app-surface rounded-lg p-4">
                  <div className="flex items-start justify-between gap-4">
                    <div>
                      <h2 className="text-xl font-bold">{selectedGroup.display_name}</h2>
                      <div className="mt-1 text-sm text-slate-500">
                        {formatCount(selectedGroup.transaction_count, privacyMode)} uncategorized matches · latest {formatDate(selectedGroup.latest_date)}
                      </div>
                    </div>
                    <div className={`text-sm font-semibold ${selectedGroup.total_amount >= 0 ? 'text-emerald-500' : 'text-rose-500'}`}>
                      {fmt(selectedGroup.total_amount)}
                    </div>
                  </div>
                </div>

                <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_340px]">
                  <div className="app-surface rounded-lg p-4">
                    <div className="flex items-center justify-between mb-3">
                      <h3 className="text-sm font-semibold">Sample Transactions</h3>
                      <span className="text-xs text-slate-500">latest first</span>
                    </div>
                    <div className="space-y-2">
                      {selectedGroup.transactions.map((txn) => (
                        <div key={txn.id} className="grid grid-cols-[110px_minmax(0,1fr)_120px] gap-3 rounded-lg border border-[var(--app-border-soft)] px-3 py-2 text-sm dark:border-slate-700">
                          <div className="text-slate-500">{formatDate(txn.date)}</div>
                          <div className="min-w-0">
                            <div className="truncate font-medium">{txn.merchant_clean || txn.merchant_raw}</div>
                            <div className="truncate text-xs text-slate-500">{txn.merchant_raw}</div>
                          </div>
                          <div className={`text-right font-medium ${txn.amount >= 0 ? 'text-emerald-500' : 'text-rose-500'}`}>{fmt(txn.amount)}</div>
                        </div>
                      ))}
                    </div>
                  </div>

                  <div className="app-surface rounded-lg p-4">
                    <h3 className="mb-3 text-sm font-semibold">Stage Mapping</h3>
                    <div className="space-y-3">
                      <div>
                        <label className="mb-1 block text-xs text-slate-500">Category</label>
                        <select
                          value={topCategory}
                          onChange={(event) => setTopCategory(event.target.value)}
                          className="app-input w-full rounded-lg px-3 py-2 text-sm"
                        >
                          {categoryOptions.map((option) => (
                            <option key={option.category} value={option.category}>{option.category}</option>
                          ))}
                        </select>
                      </div>

                      {topCategory !== 'Uncategorized' && availableSubcategories.length > 0 ? (
                        <div>
                          <label className="mb-1 block text-xs text-slate-500">Subcategory</label>
                          <select
                            value={subcategory}
                            onChange={(event) => setSubcategory(event.target.value)}
                            className="app-input w-full rounded-lg px-3 py-2 text-sm"
                          >
                            <option value="">None</option>
                            {availableSubcategories.map((option) => (
                              <option key={option} value={option}>{option}</option>
                            ))}
                          </select>
                        </div>
                      ) : null}

                      <div>
                        <label className="mb-1 block text-xs text-slate-500">Merchant Clean</label>
                        <input
                          type="text"
                          value={merchantClean}
                          onChange={(event) => setMerchantClean(event.target.value)}
                          className="app-input w-full rounded-lg px-3 py-2 text-sm"
                        />
                      </div>

                      <label className="flex items-center gap-2 rounded-lg border border-[var(--app-border-soft)] bg-[var(--app-surface-2)] px-3 py-2 text-sm dark:border-slate-700">
                        <input
                          type="checkbox"
                          checked={learnRule}
                          onChange={(event) => setLearnRule(event.target.checked)}
                        />
                        <span>Learn this merchant for future imports</span>
                      </label>

                      <div className="text-xs text-slate-500">
                        Rule seed: <span className="font-medium text-slate-700 dark:text-slate-200">{selectedGroup.rule_pattern_source || selectedGroup.display_name}</span>
                      </div>

                      <button
                        type="button"
                        onClick={handleStageChange}
                        className="app-btn-primary w-full rounded-lg px-3 py-2 text-sm font-medium"
                      >
                        {selectedStage ? 'Update staged change' : 'Add to staged queue'}
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            ) : (
              <div className="flex h-full items-center justify-center text-sm text-slate-500">
                Select a merchant group to review it.
              </div>
            )}
          </div>

          <div className="app-surface flex min-h-0 flex-col rounded-lg p-4">
            <div className="mb-3 flex items-center justify-between gap-3">
              <div>
                <h3 className="text-sm font-semibold">Staged Changes</h3>
                <div className="text-xs text-slate-500">
                  Review queued mappings before writing them to the DB.
                </div>
              </div>
              <div className="text-xs text-slate-500">
                {formatCount(queueCount, privacyMode)} selected
              </div>
            </div>

            {stagedChanges.length === 0 ? (
              <div className="rounded-lg border border-dashed border-slate-300 px-4 py-6 text-sm text-slate-500 dark:border-slate-700">
                No staged mappings yet. Add merchant decisions here first, then apply the batch.
              </div>
            ) : (
              <div className="min-h-0 flex-1 overflow-y-auto pr-1">
                <div className="space-y-2">
                {stagedChanges.map((change) => (
                  <div key={change.merchant_key} className="grid grid-cols-[28px_minmax(0,1fr)_150px_90px_32px] items-center gap-3 rounded-lg border border-[var(--app-border-soft)] px-3 py-2 text-sm dark:border-slate-700">
                    <input
                      type="checkbox"
                      checked={change.enabled}
                      onChange={(event) => {
                        const checked = event.target.checked
                        setStagedChanges((current) => current.map((item) => item.merchant_key === change.merchant_key ? { ...item, enabled: checked } : item))
                      }}
                    />
                    <button
                      type="button"
                      onClick={() => setSelectedId(change.merchant_key)}
                      className="min-w-0 text-left hover:underline"
                    >
                      <div className="truncate font-medium">{change.display_name}</div>
                      <div className="truncate text-xs text-slate-500">
                        {change.category}{change.learnRule ? ' · future rule' : ''}
                      </div>
                    </button>
                    <div className="text-xs text-slate-500">{formatCount(change.transaction_count, privacyMode)} rows</div>
                    <div className="text-xs text-slate-500">{change.enabled ? 'Ready' : 'Skipped'}</div>
                    <button
                      type="button"
                      onClick={() => setStagedChanges((current) => current.filter((item) => item.merchant_key !== change.merchant_key))}
                      className="text-slate-400 hover:text-rose-500"
                      title="Remove staged change"
                    >
                      ×
                    </button>
                  </div>
                ))}
                </div>
              </div>
            )}

            <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-[var(--app-border-soft)] pt-4 dark:border-slate-700">
              <button
                type="button"
                onClick={() => batchApplyMutation.mutate()}
                disabled={!queueCount || batchApplyMutation.isPending || undoMutation.isPending}
                className="app-btn-primary rounded-lg px-3 py-2 text-sm font-medium disabled:opacity-60"
              >
                {batchApplyMutation.isPending ? 'Applying…' : `Apply ${formatCount(queueCount, privacyMode)} staged change${queueCount === 1 ? '' : 's'}`}
              </button>
              <button
                type="button"
                onClick={() => setStagedChanges([])}
                disabled={!stagedChanges.length || batchApplyMutation.isPending || undoMutation.isPending}
                className="app-btn-secondary rounded-lg px-3 py-2 text-sm font-medium disabled:opacity-60"
              >
                Clear queue
              </button>
              {lastUndoPayload?.length ? (
                <button
                  type="button"
                  onClick={() => undoMutation.mutate()}
                  disabled={batchApplyMutation.isPending || undoMutation.isPending}
                  className="app-btn-secondary rounded-lg px-3 py-2 text-sm font-medium disabled:opacity-60"
                >
                  {undoMutation.isPending ? 'Undoing…' : 'Undo last batch'}
                </button>
              ) : null}
            </div>

            {batchApplyMutation.isError ? (
              <div className="mt-2 text-xs text-rose-500">{batchApplyMutation.error instanceof Error ? batchApplyMutation.error.message : 'Failed to apply staged changes'}</div>
            ) : null}
            {undoMutation.isError ? (
              <div className="mt-2 text-xs text-rose-500">{undoMutation.error instanceof Error ? undoMutation.error.message : 'Failed to undo last batch'}</div>
            ) : null}
            {batchApplyMutation.isSuccess ? (
              <div className="mt-2 text-xs text-emerald-600 dark:text-emerald-300">
                Applied {formatCount(batchApplyMutation.data.reduce((sum, item) => sum + item.updated_count, 0), privacyMode)} rows across {formatCount(batchApplyMutation.data.length, privacyMode)} staged mappings.
              </div>
            ) : null}
            {undoMutation.isSuccess ? (
              <div className="mt-2 text-xs text-emerald-600 dark:text-emerald-300">
                Restored {formatCount(undoMutation.data.updated_count, privacyMode)} rows from the last batch.
              </div>
            ) : null}
          </div>
        </div>
      </div>
    </div>
  )
}

function GroupCard({
  group,
  active,
  staged,
  onSelect,
  fmt,
}: {
  group: UncategorizedReviewGroup
  active: boolean
  staged: boolean
  onSelect: () => void
  fmt: (n: number) => string
}) {
  return (
    <div
      onClick={onSelect}
      className={`mb-1 rounded cursor-pointer p-2 ${active ? 'bg-[var(--app-surface-2)] dark:bg-slate-700' : 'hover:bg-[var(--app-surface-2)] dark:hover:bg-slate-800'}`}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium">{group.display_name}</div>
          <div className="mt-0.5 text-xs text-slate-500">
            {group.transaction_count} txns · {formatDate(group.latest_date)}{staged ? ' · queued' : ''}
          </div>
        </div>
        <div className={`shrink-0 text-xs font-medium ${group.total_amount >= 0 ? 'text-emerald-500' : 'text-rose-500'}`}>
          {fmt(group.total_amount)}
        </div>
      </div>
    </div>
  )
}
