import { useDeferredValue, useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, CalendarClock, CreditCard, History, LineChart, Receipt, Search } from 'lucide-react'
import { Area, AreaChart, CartesianGrid, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, type RecurringCatalogItem, type RecurringDetail, type RecurringTrendsResponse } from './api'
import { PeriodNav } from './PeriodNav'
import { useFilterStore } from './store'
import { formatAxisCurrency, formatCount, formatCurrency } from './privacy'
const PALETTE = ['#0f766e', '#2563eb', '#f97316', '#8b5cf6', '#ef4444', '#14b8a6', '#64748b']

const METRICS = [
  { value: 'monthly_equivalent_total', label: 'Monthly Eq' },
  { value: 'charge_total', label: 'Actual Charges' },
  { value: 'active_count', label: 'Active Count' },
]

const GROUP_BY = [
  { value: 'service', label: 'Service' },
  { value: 'category', label: 'Category' },
  { value: 'source', label: 'Card / Source' },
  { value: 'currency', label: 'Currency' },
]

function formatShortDate(value: string) {
  return new Date(`${value}T12:00:00`).toLocaleDateString('en-US', { month: 'short', year: '2-digit' })
}

function formatDay(value: string) {
  return new Date(`${value}T12:00:00`).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
}

function statusClass(status: string) {
  if (status === 'active') return 'bg-emerald-500/10 text-emerald-400'
  if (status === 'ended') return 'bg-slate-500/10 text-slate-400'
  return 'bg-amber-500/10 text-amber-400'
}

function flagLabel(flag: string) {
  if (flag === 'price_changed') return 'price'
  if (flag === 'card_changed') return 'card'
  return flag.replace(/_/g, ' ')
}

function frequencyLabel(value: string) {
  return value.replace(/_/g, ' ')
}

function eventLabel(value: string) {
  return value.replace(/_/g, ' ')
}

function buildTrendData(trends: RecurringTrendsResponse | undefined) {
  if (!trends) return { data: [], series: [] as Array<{ key: string; label: string; dataKey: string; color: string }> }
  const topGroups = trends.groups.slice(0, 6)
  const topKeys = new Set(topGroups.map((group) => group.key))
  const series = topGroups.map((group, index) => ({
    key: group.key,
    label: group.label,
    dataKey: `series_${index}`,
    color: PALETTE[index % PALETTE.length],
  }))

  const data = trends.points.map((point) => {
    const row: Record<string, string | number> = {
      date: point.date,
      label: point.label,
      total: point.total,
    }
    topGroups.forEach((group, index) => {
      row[`series_${index}`] = point.breakdown[group.key] || 0
    })
    let other = 0
    for (const [key, value] of Object.entries(point.breakdown)) {
      if (!topKeys.has(key)) other += value
    }
    row.other = Number(other.toFixed(2))
    return row
  })

  if (data.some((row) => Number(row.other))) {
    series.push({ key: 'other', label: 'Other', dataKey: 'other', color: PALETTE[PALETTE.length - 1] })
  }

  return { data, series }
}

function filterItems(
  items: RecurringCatalogItem[],
  deferredSearch: string,
  statusFilter: string,
  kindFilter: string,
  sourceFilter: string,
  categoryFilter: string,
  currencyFilter: string,
) {
  return items.filter((item) => {
    if (statusFilter !== 'all' && item.status !== statusFilter) return false
    if (kindFilter !== 'all' && item.kind !== kindFilter) return false
    if (sourceFilter !== 'all' && item.current_source !== sourceFilter) return false
    if (categoryFilter !== 'all' && item.top_category !== categoryFilter) return false
    if (currencyFilter !== 'all' && item.currency !== currencyFilter) return false
    if (!deferredSearch) return true
    const search = deferredSearch.toLowerCase()
    return (
      item.display_name.toLowerCase().includes(search) ||
      item.current_source.toLowerCase().includes(search) ||
      item.category.toLowerCase().includes(search)
    )
  })
}

function ChartTooltip({
  active,
  label,
  payload,
  metric,
  privacyMode,
  displayCurrency,
}: {
  active?: boolean
  label?: string
  payload?: Array<{ name: string; value: number; color: string }>
  metric: string
  privacyMode: boolean
  displayCurrency: string
}) {
  if (!active || !payload?.length) return null
  const total = payload.find((entry) => entry.name === 'Total')?.value ?? 0
  const detailRows = payload.filter((entry) => entry.name !== 'Total' && entry.value)
  return (
    <div className="rounded-xl border border-[var(--app-border-soft)] bg-[var(--app-surface)]/95 p-3 text-xs shadow-xl dark:border-slate-700 dark:bg-slate-950/95">
      <div className="mb-2 font-medium text-slate-900 dark:text-slate-100">{formatShortDate(label || '')}</div>
      <div className="mb-2 text-slate-600 dark:text-slate-300">
        Total: {metric === 'active_count' ? formatCount(total, privacyMode) : formatCurrency(total, privacyMode, { currency: displayCurrency })}
      </div>
      <div className="space-y-1">
        {detailRows.slice(0, 5).map((entry) => (
          <div key={entry.name} className="flex items-center justify-between gap-3">
            <span className="inline-flex items-center gap-2 text-slate-600 dark:text-slate-300">
              <span className="h-2 w-2 rounded-full" style={{ backgroundColor: entry.color }} />
              {entry.name}
            </span>
            <span className="font-medium text-slate-900 dark:text-slate-100">
              {metric === 'active_count' ? formatCount(entry.value, privacyMode) : formatCurrency(entry.value, privacyMode, { currency: displayCurrency })}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

function SummaryCard({
  label,
  value,
  meta,
  accent,
}: {
  label: string
  value: string
  meta: string
  accent: string
}) {
  return (
    <div className="app-surface rounded-2xl p-4 dark:border-slate-800 dark:bg-slate-950">
      <div className="mb-1 text-xs uppercase tracking-[0.18em] text-slate-500 dark:text-slate-400">{label}</div>
      <div className={`text-2xl font-semibold ${accent}`}>{value}</div>
      <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">{meta}</div>
    </div>
  )
}

function EmptyState({ title, body }: { title: string; body: string }) {
  return (
    <div className="rounded-2xl border border-dashed border-[var(--app-border)] bg-[var(--app-surface)]/70 px-4 py-8 text-center text-sm text-slate-500 dark:border-slate-700 dark:bg-slate-950/70 dark:text-slate-400">
      <div className="font-medium text-slate-700 dark:text-slate-200">{title}</div>
      <div className="mt-1">{body}</div>
    </div>
  )
}

function DetailPanel({ detail, privacyMode, displayCurrency }: { detail: RecurringDetail | undefined; privacyMode: boolean; displayCurrency: string }) {
  if (!detail) {
    return <EmptyState title="Select a recurring item" body="Pick a row from the catalog to inspect lifecycle events and linked charges." />
  }

  return (
    <div className="app-surface rounded-2xl p-5 dark:border-slate-800 dark:bg-slate-950">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-xl font-semibold">{detail.display_name}</h2>
            <span className={`rounded-full px-2.5 py-1 text-xs uppercase tracking-wide ${statusClass(detail.status)}`}>{detail.status}</span>
            <span className="rounded-full bg-slate-200 px-2.5 py-1 text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-300">{detail.kind}</span>
          </div>
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">{detail.category}</p>
        </div>
        <div className="text-right">
          <div className="text-2xl font-semibold">{formatCurrency(detail.current_amount, privacyMode, { currency: displayCurrency })}</div>
          <div className="text-sm text-slate-500 dark:text-slate-400">{frequencyLabel(detail.frequency)} · {formatCurrency(detail.monthly_equivalent, privacyMode, { currency: displayCurrency })}/mo</div>
        </div>
      </div>

      <div className="mb-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <InfoPill label="Current Source" value={detail.current_account_last4 ? `${detail.current_source} ••${detail.current_account_last4}` : detail.current_source} />
        <InfoPill label="First Seen" value={formatDay(detail.first_seen)} />
        <InfoPill label="Last Seen" value={formatDay(detail.last_seen)} />
        <InfoPill label={detail.status === 'ended' ? 'Ended' : 'Next Expected'} value={formatDay(detail.status === 'ended' ? (detail.ended_at || detail.last_seen) : detail.next_expected)} />
      </div>

      {detail.flags.length ? (
        <div className="mb-5 flex flex-wrap gap-2">
          {detail.flags.map((flag) => (
            <span key={flag} className="rounded-full bg-amber-500/10 px-2.5 py-1 text-xs text-amber-400">
              {flagLabel(flag)}
            </span>
          ))}
        </div>
      ) : null}

      <div className="grid gap-4 xl:grid-cols-2">
        <section className="rounded-2xl border border-[var(--app-border-soft)] p-4 dark:border-slate-800">
          <div className="mb-3 flex items-center gap-2 text-sm font-medium">
            <History size={15} />
            Event Timeline
          </div>
          <div className="max-h-[360px] space-y-2 overflow-auto pr-1">
            {detail.events.map((event) => (
              <div key={`${event.event_type}-${event.effective_date}-${event.transaction_id || 'manual'}`} className="rounded-xl bg-[var(--app-surface-2)] px-3 py-2 text-sm dark:bg-slate-900">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium capitalize">{eventLabel(event.event_type)}</span>
                  <span className="text-xs text-slate-500 dark:text-slate-400">{formatDay(event.effective_date)}</span>
                </div>
                <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                  {event.amount ? `${formatCurrency(event.amount, privacyMode, { currency: displayCurrency })} · ` : ''}{event.source}{event.account_last4 ? ` ••${event.account_last4}` : ''}
                </div>
              </div>
            ))}
          </div>
        </section>

        <section className="rounded-2xl border border-[var(--app-border-soft)] p-4 dark:border-slate-800">
          <div className="mb-3 flex items-center gap-2 text-sm font-medium">
            <Receipt size={15} />
            Linked Charges
          </div>
          <div className="max-h-[360px] space-y-2 overflow-auto pr-1">
            {detail.transactions.map((txn) => (
              <div key={txn.transaction_id} className="rounded-xl bg-[var(--app-surface-2)] px-3 py-2 text-sm dark:bg-slate-900">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium">{txn.merchant}</span>
                  <span>{formatCurrency(txn.amount, privacyMode, { currency: displayCurrency })}</span>
                </div>
                <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                  {formatDay(txn.date)} · {txn.source}{txn.account_last4 ? ` ••${txn.account_last4}` : ''} · {txn.category}
                </div>
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  )
}

function InfoPill({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl bg-[var(--app-surface-2)] px-3 py-3 dark:bg-slate-900">
      <div className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">{label}</div>
      <div className="mt-1 text-sm font-medium">{value}</div>
    </div>
  )
}

export function RecurringPage({ onBack }: { onBack: () => void }) {
  const privacyMode = useFilterStore((state) => state.privacyMode)
  const displayCurrency = useFilterStore((state) => state.displayCurrency)
  const startDate = useFilterStore((state) => state.startDate)
  const endDate = useFilterStore((state) => state.endDate)
  const [metric, setMetric] = useState('monthly_equivalent_total')
  const [groupBy, setGroupBy] = useState('service')
  const [chartMode, setChartMode] = useState<'stacked' | 'line'>('stacked')
  const [statusFilter, setStatusFilter] = useState('all')
  const [kindFilter, setKindFilter] = useState('all')
  const [sourceFilter, setSourceFilter] = useState('all')
  const [categoryFilter, setCategoryFilter] = useState('all')
  const [currencyFilter, setCurrencyFilter] = useState('all')
  const [search, setSearch] = useState('')
  const deferredSearch = useDeferredValue(search)
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const { data: catalog, isLoading: catalogLoading, error: catalogError } = useQuery({
    queryKey: ['recurring-catalog', startDate, endDate, displayCurrency],
    queryFn: () => api.getRecurringCatalog(startDate, endDate, displayCurrency),
    staleTime: 60_000,
})

  const { data: trends, isLoading: trendsLoading } = useQuery({
    queryKey: ['recurring-trends', startDate, endDate, metric, groupBy, displayCurrency],
    queryFn: () => api.getRecurringTrends(startDate, endDate, metric, groupBy, displayCurrency),
    staleTime: 60_000,
  })

  const filteredItems = useMemo(
    () =>
      filterItems(
        catalog?.items || [],
        deferredSearch,
        statusFilter,
        kindFilter,
        sourceFilter,
        categoryFilter,
        currencyFilter,
      ),
    [catalog?.items, deferredSearch, statusFilter, kindFilter, sourceFilter, categoryFilter, currencyFilter],
  )

  useEffect(() => {
    if (!filteredItems.length) {
      setSelectedId(null)
      return
    }
    if (!selectedId || !filteredItems.some((item) => item.id === selectedId)) {
      const preferred = filteredItems.find((item) => item.status === 'active') || filteredItems[0]
      setSelectedId(preferred.id)
    }
  }, [filteredItems, selectedId])

  const { data: detail } = useQuery({
    queryKey: ['recurring-detail', selectedId, displayCurrency],
    queryFn: () => api.getRecurringDetail(selectedId!, displayCurrency),
    enabled: !!selectedId,
    staleTime: 60_000,
  })

  const trendView = useMemo(() => buildTrendData(trends), [trends])

  const upcoming = useMemo(() => {
    const today = new Date()
    const cutoff = new Date(today)
    cutoff.setDate(today.getDate() + 45)
    return (catalog?.items || [])
      .filter((item) => item.status === 'active')
      .map((item) => ({ ...item, nextDate: new Date(`${item.next_expected}T12:00:00`) }))
      .filter((item) => item.nextDate >= today && item.nextDate <= cutoff)
      .sort((a, b) => a.nextDate.getTime() - b.nextDate.getTime())
      .slice(0, 5)
  }, [catalog?.items])

  return (
    <div className="app-shell min-h-screen p-4 text-slate-900 dark:bg-slate-900 dark:text-slate-100">
      <div className="mx-auto max-w-7xl">
        <div className="mb-6 flex flex-wrap items-center gap-3">
          <button
            onClick={onBack}
            className="inline-flex items-center gap-2 rounded-full border border-[var(--app-border)] bg-[var(--app-surface)] px-3 py-1.5 text-sm text-slate-700 transition-colors hover:border-slate-400 hover:text-slate-900 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200"
          >
            <ArrowLeft size={16} />
            Back
          </button>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-2xl font-semibold tracking-tight">Recurring</h1>
              <span className="rounded-full bg-slate-200 px-2.5 py-1 text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                {startDate} to {endDate}
              </span>
            </div>
            <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
              Lifecycle view for subscriptions, bills, and renewals with price and card-change history.
            </p>
          </div>
          <PeriodNav />
        </div>

        {catalogError ? (
          <div className="mb-6 rounded-2xl border border-rose-500/20 bg-rose-500/10 px-4 py-3 text-sm text-rose-300">
            Error loading recurring workspace: {String(catalogError)}
          </div>
        ) : null}

        <div className="mb-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          <SummaryCard label="Current Monthly Eq" value={formatCurrency(catalog?.summary.current_monthly_total || 0, privacyMode, { currency: displayCurrency })} meta="Active recurring cost right now" accent="text-teal-500" />
          <SummaryCard label="Active" value={formatCount(catalog?.summary.active_count || 0, privacyMode)} meta="Items still renewing" accent="text-blue-500" />
          <SummaryCard label="Ended In Range" value={formatCount(catalog?.summary.ended_in_range || 0, privacyMode)} meta="Services that dropped out" accent="text-slate-500" />
          <SummaryCard label="Price Changes" value={formatCount(catalog?.summary.price_changes_in_range || 0, privacyMode)} meta="Detected in selected window" accent="text-amber-400" />
          <SummaryCard label="Renewing Soon" value={formatCount(catalog?.summary.renewing_soon || 0, privacyMode)} meta="Due in the next 30 days" accent="text-rose-400" />
        </div>

        <section className="app-surface mb-6 rounded-2xl p-5 dark:border-slate-800 dark:bg-slate-950">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-lg font-semibold">Recurring Trend</h2>
              <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
                Track how recurring cost changed over time instead of only seeing the current active list.
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              <select value={metric} onChange={(event) => setMetric(event.target.value)} className="app-input rounded-full px-3 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-900">
                {METRICS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
              </select>
              <select value={groupBy} onChange={(event) => setGroupBy(event.target.value)} className="app-input rounded-full px-3 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-900">
                {GROUP_BY.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
              </select>
              <div className="flex rounded-full border border-[var(--app-border)] bg-[var(--app-surface)] p-1 text-xs dark:border-slate-700 dark:bg-slate-900">
                <button onClick={() => setChartMode('stacked')} className={`rounded-full px-3 py-1 ${chartMode === 'stacked' ? 'bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-900' : 'text-slate-500 dark:text-slate-400'}`}>Stacked</button>
                <button onClick={() => setChartMode('line')} className={`rounded-full px-3 py-1 ${chartMode === 'line' ? 'bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-900' : 'text-slate-500 dark:text-slate-400'}`}>Line</button>
              </div>
            </div>
          </div>

          <div className="chart-h">
            {!trendView.data.length && !trendsLoading ? (
              <EmptyState title="No recurring trend yet" body="Once recurring entities are inferred, trend history will appear here." />
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                {chartMode === 'stacked' ? (
                  <AreaChart data={trendView.data}>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(148,163,184,0.18)" />
                    <XAxis dataKey="date" tickFormatter={formatShortDate} minTickGap={30} tick={{ fontSize: 12, fill: '#94a3b8' }} />
                    <YAxis
                      width={80}
                      tickFormatter={(value) => metric === 'active_count' ? formatCount(Math.round(value), privacyMode) : formatAxisCurrency(value, privacyMode, displayCurrency)}
                      tick={{ fontSize: 12, fill: '#94a3b8' }}
                    />
                    <Tooltip content={<ChartTooltip metric={metric} privacyMode={privacyMode} displayCurrency={displayCurrency} />} />
                    {trendView.series.map((series) => (
                      <Area key={series.dataKey} type="monotone" dataKey={series.dataKey} stackId="recurring" stroke={series.color} fill={series.color} fillOpacity={0.18} name={series.label} />
                    ))}
                    <Line type="monotone" dataKey="total" stroke="#e2e8f0" strokeWidth={2} dot={false} name="Total" />
                  </AreaChart>
                ) : (
                  <AreaChart data={trendView.data}>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(148,163,184,0.18)" />
                    <XAxis dataKey="date" tickFormatter={formatShortDate} minTickGap={30} tick={{ fontSize: 12, fill: '#94a3b8' }} />
                    <YAxis
                      width={80}
                      tickFormatter={(value) => metric === 'active_count' ? formatCount(Math.round(value), privacyMode) : formatAxisCurrency(value, privacyMode, displayCurrency)}
                      tick={{ fontSize: 12, fill: '#94a3b8' }}
                    />
                    <Tooltip content={<ChartTooltip metric={metric} privacyMode={privacyMode} displayCurrency={displayCurrency} />} />
                    <Line type="monotone" dataKey="total" stroke="#0f766e" strokeWidth={3} dot={false} name="Total" />
                  </AreaChart>
                )}
              </ResponsiveContainer>
            )}
          </div>

          {trendView.series.length ? (
            <div className="mt-4 flex flex-wrap gap-2">
              {trendView.series.map((series) => (
                <span key={series.key} className="inline-flex items-center gap-2 rounded-full bg-slate-100 px-3 py-1 text-xs text-slate-600 dark:bg-slate-900 dark:text-slate-300">
                  <span className="h-2 w-2 rounded-full" style={{ backgroundColor: series.color }} />
                  {series.label}
                </span>
              ))}
            </div>
          ) : null}
        </section>

        <div className="mb-6 grid gap-6 xl:grid-cols-[minmax(0,1.3fr)_minmax(320px,0.9fr)]">
          <section className="app-surface rounded-2xl p-5 dark:border-slate-800 dark:bg-slate-950">
            <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
              <div>
                <h2 className="text-lg font-semibold">Catalog</h2>
                <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">{filteredItems.length} items match the current filters.</p>
              </div>
              <div className="relative min-w-[220px] flex-1 max-w-sm">
                <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
                <input
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                  placeholder="Search service, card, or category"
                  className="app-input w-full rounded-full py-2 pl-9 pr-4 text-sm dark:border-slate-700 dark:bg-slate-900"
                />
              </div>
            </div>

            <div className="mb-4 flex flex-wrap gap-2">
              {['all', 'active', 'ended'].map((value) => (
                <button key={value} onClick={() => setStatusFilter(value)} className={`rounded-full px-3 py-1.5 text-xs transition-colors ${statusFilter === value ? 'bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-900' : 'bg-slate-100 text-slate-600 dark:bg-slate-900 dark:text-slate-300'}`}>
                  {value}
                </button>
              ))}
              <select value={kindFilter} onChange={(event) => setKindFilter(event.target.value)} className="app-input rounded-full px-3 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-900">
                <option value="all">All types</option>
                {catalog?.filters.kinds.map((kind) => <option key={kind} value={kind}>{kind}</option>)}
              </select>
              <select value={sourceFilter} onChange={(event) => setSourceFilter(event.target.value)} className="app-input rounded-full px-3 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-900">
                <option value="all">All cards</option>
                {catalog?.filters.sources.map((source) => <option key={source} value={source}>{source}</option>)}
              </select>
              <select value={categoryFilter} onChange={(event) => setCategoryFilter(event.target.value)} className="app-input rounded-full px-3 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-900">
                <option value="all">All categories</option>
                {catalog?.filters.categories.map((category) => <option key={category} value={category}>{category}</option>)}
              </select>
              <select value={currencyFilter} onChange={(event) => setCurrencyFilter(event.target.value)} className="app-input rounded-full px-3 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-900">
                <option value="all">All currencies</option>
                {catalog?.filters.currencies.map((currency) => <option key={currency} value={currency}>{currency}</option>)}
              </select>
            </div>

            {catalogLoading ? (
              <EmptyState title="Loading recurring catalog" body="Pulling active and ended recurring items from transaction history." />
            ) : filteredItems.length === 0 ? (
              <EmptyState title="No recurring items match" body="Broaden the filters or search to inspect the full recurring catalog." />
            ) : (
              <div className="overflow-x-auto">
                <table className="min-w-full text-sm">
                  <thead className="text-left text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
                    <tr>
                      <th className="pb-3 pr-3">Service</th>
                      <th className="pb-3 pr-3">Status</th>
                      <th className="pb-3 pr-3">Current</th>
                      <th className="pb-3 pr-3">Monthly Eq</th>
                      <th className="pb-3 pr-3">Freq</th>
                      <th className="pb-3 pr-3">Source</th>
                      <th className="pb-3 pr-3">Next</th>
                      <th className="pb-3">Flags</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredItems.map((item) => (
                      <tr
                        key={item.id}
                        onClick={() => setSelectedId(item.id)}
                        className={`cursor-pointer border-t border-[var(--app-border-soft)] align-top transition-colors dark:border-slate-800 ${selectedId === item.id ? 'bg-teal-500/8' : 'hover:bg-[var(--app-surface-2)] dark:hover:bg-slate-900'}`}
                      >
                        <td className="py-3 pr-3">
                          <div className="font-medium">{item.display_name}</div>
                          <div className="text-xs text-slate-500 dark:text-slate-400">{item.top_category}</div>
                        </td>
                        <td className="py-3 pr-3">
                          <span className={`rounded-full px-2.5 py-1 text-xs uppercase tracking-wide ${statusClass(item.status)}`}>{item.status}</span>
                        </td>
                        <td className="py-3 pr-3">{formatCurrency(item.current_amount, privacyMode, { currency: displayCurrency })}</td>
                        <td className="py-3 pr-3">{formatCurrency(item.monthly_equivalent, privacyMode, { currency: displayCurrency })}</td>
                        <td className="py-3 pr-3 capitalize">{frequencyLabel(item.frequency)}</td>
                        <td className="py-3 pr-3">
                          <div>{item.current_source}</div>
                          {item.current_account_last4 ? <div className="text-xs text-slate-500 dark:text-slate-400">••{item.current_account_last4}</div> : null}
                        </td>
                        <td className="py-3 pr-3">{item.status === 'ended' ? formatDay(item.ended_at || item.last_seen) : formatDay(item.next_expected)}</td>
                        <td className="py-3">
                          <div className="flex flex-wrap gap-1">
                            {item.flags.length ? item.flags.map((flag) => (
                              <span key={flag} className="rounded-full bg-amber-500/10 px-2 py-1 text-[11px] text-amber-400">{flagLabel(flag)}</span>
                            )) : <span className="text-xs text-slate-400">clean</span>}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <div className="space-y-6">
              <DetailPanel detail={detail} privacyMode={privacyMode} displayCurrency={displayCurrency} />

            <section className="app-surface rounded-2xl p-5 dark:border-slate-800 dark:bg-slate-950">
              <div className="mb-3 flex items-center gap-2">
                <CalendarClock size={16} />
                <h2 className="text-lg font-semibold">Upcoming Renewals</h2>
              </div>
              {upcoming.length ? (
                <div className="space-y-2">
                  {upcoming.map((item) => (
                    <button key={item.id} onClick={() => setSelectedId(item.id)} className="flex w-full items-center justify-between rounded-xl bg-[var(--app-surface-2)] px-3 py-3 text-left text-sm transition-colors hover:bg-[var(--app-border-soft)] dark:bg-slate-900 dark:hover:bg-slate-800">
                      <div>
                        <div className="font-medium">{item.display_name}</div>
                        <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">{item.current_source}</div>
                      </div>
                      <div className="text-right">
                        <div>{formatDay(item.next_expected)}</div>
                        <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">{formatCurrency(item.current_amount, privacyMode, { currency: displayCurrency })}</div>
                      </div>
                    </button>
                  ))}
                </div>
              ) : (
                <EmptyState title="No renewals due soon" body="Nothing active is due in the next 45 days." />
              )}

              <div className="mt-4 flex flex-wrap gap-2 text-xs text-slate-500 dark:text-slate-400">
                <span className="inline-flex items-center gap-2 rounded-full bg-slate-100 px-3 py-1 dark:bg-slate-900">
                  <CreditCard size={12} />
                  Source filters stay on the catalog, not the whole dashboard
                </span>
                <span className="inline-flex items-center gap-2 rounded-full bg-slate-100 px-3 py-1 dark:bg-slate-900">
                  <LineChart size={12} />
                  Trend values use month-end active state
                </span>
              </div>
            </section>
          </div>
        </div>
      </div>
    </div>
  )
}
