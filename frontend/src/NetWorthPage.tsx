import { useMemo, useState, type ReactNode } from 'react'
import { ArrowLeft, Info, Landmark, LineChart, Wallet } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import {
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api, type NetWorthSourceRow, type NetWorthTrackedHistory } from './api'
import { PeriodNav } from './PeriodNav'
import { useFilterStore } from './store'
import { formatAxisCurrency, formatCurrency } from './privacy'

type GroupKey = 'bank_accounts' | 'cash_like' | 'brokerage' | 'retirement' | 'credit_cards'
type VisibleGroupKey = 'bank_accounts' | 'brokerage' | 'retirement' | 'credit_cards'

interface SnapshotSource {
  source: string
  source_key?: string
  value: number | null
  group?: string
}

interface Props {
  onBack: () => void
  snapshotSources: SnapshotSource[]
}

type DisplaySourceRow = Omit<NetWorthSourceRow, 'group'> & { group: VisibleGroupKey }

const GROUP_META: Record<
  VisibleGroupKey,
  { label: string; color: string; pillClass: string; description: string }
> = {
  bank_accounts: {
    label: 'Bank Accounts',
    color: '#3b82f6',
    pillClass: 'bg-blue-500/10 text-blue-500',
    description: 'Checking, savings, and other bank balances.',
  },
  brokerage: {
    label: 'Investments',
    color: '#22c55e',
    pillClass: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
    description: 'Brokerage and investment account value.',
  },
  retirement: {
    label: 'Retirement 401k',
    color: '#f59e0b',
    pillClass: 'bg-amber-500/10 text-amber-400',
    description: '401k or retirement balances sourced from stored account snapshots.',
  },
  credit_cards: {
    label: 'Credit Cards',
    color: '#f43f5e',
    pillClass: 'bg-rose-500/10 text-rose-400',
    description: 'Credit card liabilities derived from synced balances and card transaction history.',
  },
}

function classifySnapshotSource(_source: string, group?: string): VisibleGroupKey {
  if (group === 'retirement') return 'retirement'
  if (group === 'investment' || group === 'brokerage') return 'brokerage'
  if (group === 'credit_card' || group === 'credit_cards') return 'credit_cards'
  // Fallback for sources without a group provided
  return 'bank_accounts'
}

function normalizeGroup(group: GroupKey, _source: string): VisibleGroupKey | null {
  if (group === 'credit_cards') return null
  if (group === 'cash_like') return 'bank_accounts'
  if (group === 'brokerage') return 'brokerage'
  if (group === 'retirement') return 'retirement'
  return 'bank_accounts'
}

function formatShortDate(value: string) {
  return new Date(`${value}T12:00:00`).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
  })
}

function buildFallbackPoints(startDate: string, endDate: string) {
  const points: NetWorthTrackedHistory['points'] = []
  const day = new Date(`${startDate}T12:00:00`)
  const end = new Date(`${endDate}T12:00:00`)
  while (day <= end) {
    points.push({
      date: day.toISOString().slice(0, 10),
      bank_accounts: 0,
      credit_cards: 0,
      cash_like: 0,
      brokerage: 0,
      retirement: 0,
      tracked_total: 0,
      total: 0,
    })
    day.setDate(day.getDate() + 1)
  }
  return points
}

export function NetWorthPage({ onBack, snapshotSources }: Props) {
  const startDate = useFilterStore((state) => state.startDate)
  const endDate = useFilterStore((state) => state.endDate)
  const privacyMode = useFilterStore((state) => state.privacyMode)
  const displayCurrency = useFilterStore((state) => state.displayCurrency)
  const fmt = (n: number) => formatCurrency(n, privacyMode, { currency: displayCurrency })
  const [hiddenGroups, setHiddenGroups] = useState<Set<VisibleGroupKey>>(new Set())

  const { data, isLoading, error } = useQuery({
    queryKey: ['net-worth-tracked-history', startDate, endDate, displayCurrency],
    queryFn: () => api.getNetWorthTrackedHistory(startDate, endDate, displayCurrency),
    staleTime: 60_000,
  })

  const snapshotRows = useMemo<DisplaySourceRow[]>(
    () =>
      snapshotSources
        .filter((item) => item.value !== null)
        .map((item) => ({
          key: item.source,
          source_key: item.source_key || '',
          label: item.source,
          group: classifySnapshotSource(item.source, item.group),
          current: item.value || 0,
          history_mode: 'latest_only' as const,
        }))
        .sort((a, b) => Math.abs(b.current) - Math.abs(a.current)),
    [snapshotSources],
  )

  const allTrackedPoints = data?.points?.length ? data.points : buildFallbackPoints(startDate, endDate)

  const chartData = useMemo(() => {
    return allTrackedPoints.map((point) => ({
      date: point.date,
      bank_accounts: (point.bank_accounts || 0) + (point.cash_like || 0),
      credit_cards: point.credit_cards || 0,
      brokerage: point.brokerage || 0,
      retirement: point.retirement || 0,
      total: point.total || 0,
    }))
  }, [allTrackedPoints])

  const latestPoint = chartData[chartData.length - 1]
  const firstPoint = chartData[0]
  const latestRows = useMemo<DisplaySourceRow[]>(
    () => {
      const apiRows: DisplaySourceRow[] = (data?.latest_sources || [])
        .map((row) => {
          const group = normalizeGroup(row.group as GroupKey, row.label)
          return group ? { ...row, group } : null
        })
        .filter((row): row is DisplaySourceRow => row !== null)
      const existingKeys = new Set(apiRows.map((row) => row.key))
      const existingLabels = new Set(apiRows.map((row) => row.label))
      const fallbackRows = snapshotRows.filter((row) => !existingKeys.has(row.key) && !existingKeys.has(row.source_key || '') && !existingLabels.has(row.label))
      return [...apiRows, ...fallbackRows]
        .sort((a, b) => {
          const groupOrder: Record<string, number> = { bank_accounts: 0, credit_cards: 1, brokerage: 2, retirement: 3 }
          const ga = groupOrder[a.group] ?? 99
          const gb = groupOrder[b.group] ?? 99
          if (ga !== gb) return ga - gb
          return Math.abs(b.current) - Math.abs(a.current)
        })
    },
    [data?.latest_sources, snapshotRows],
  )

  const changeOverRange = (latestPoint?.total || 0) - (firstPoint?.total || 0)
  const latestOnlyCount = latestRows.filter((row) => row.history_mode === 'latest_only').length

  const largestMover = useMemo(() => {
    if (!chartData.length) return { label: 'No movement', delta: 0 }
    const candidates: { label: string; delta: number }[] = (Object.keys(GROUP_META) as VisibleGroupKey[]).map((key) => ({
      label: GROUP_META[key].label,
      delta: (latestPoint?.[key] || 0) - (firstPoint?.[key] || 0),
    }))
    candidates.sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta))
    return candidates[0]
  }, [chartData, firstPoint, latestPoint])

  const visibleGroups = (Object.keys(GROUP_META) as VisibleGroupKey[]).filter((key) => !hiddenGroups.has(key))

  const toggleGroup = (group: VisibleGroupKey) => {
    setHiddenGroups((current) => {
      const next = new Set(current)
      if (next.has(group)) next.delete(group)
      else next.add(group)
      return next
    })
  }

  return (
    <div className="text-slate-900 dark:text-slate-100">
      <div className="mx-auto max-w-7xl">
        <div className="mb-6 flex flex-wrap items-center gap-3">
          <button
            onClick={onBack}
            className="app-surface inline-flex items-center gap-2 rounded-full border border-slate-300 px-3 py-1.5 text-sm text-slate-700 transition-colors hover:border-slate-400 hover:text-slate-900 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200"
          >
            <ArrowLeft size={16} />
            Back
          </button>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-2xl font-semibold tracking-tight">Net Worth</h1>
              <span className="rounded-full bg-slate-200 px-2.5 py-1 text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                {startDate} to {endDate}
              </span>
            </div>
            <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
              Total balance with explicit coverage labels for historical series versus latest-only fallback sources.
            </p>
          </div>
          <div className="ml-auto">
            <PeriodNav />
          </div>
        </div>

        {isLoading ? <CardShell className="mb-6">Loading net worth history...</CardShell> : null}
        {error ? <CardShell className="mb-6 text-rose-400">Error loading net worth history: {String(error)}</CardShell> : null}

        {!isLoading && !error ? (
          <>
            <div className="mb-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
              <SummaryCard
                icon={<Wallet size={15} />}
                label="Latest Total"
                value={fmt(latestPoint?.total || 0)}
                meta="Bank accounts + investments + retirement - credit cards"
                tone="emerald"
              />
              <SummaryCard
                icon={<LineChart size={15} />}
                label="Range Change"
                value={`${changeOverRange >= 0 ? '+' : ''}${fmt(changeOverRange)}`}
                meta={latestOnlyCount > 0 ? 'Latest-only sources stay flat until another sync lands' : 'All visible sources have historical coverage'}
                tone={changeOverRange >= 0 ? 'emerald' : 'rose'}
              />
              <SummaryCard
                icon={<Landmark size={15} />}
                label="Bank Accounts"
                value={fmt(latestPoint?.bank_accounts || 0)}
                meta="Checking and savings balances"
                tone="blue"
              />
              <SummaryCard
                icon={<Landmark size={15} />}
                label="Investments"
                value={fmt(latestPoint?.brokerage || 0)}
                meta="Brokerage and investment balances"
                tone="emerald"
              />
              <SummaryCard
                icon={<Info size={15} />}
                label="Retirement 401k"
                value={fmt(latestPoint?.retirement || 0)}
                meta={latestOnlyCount > 0 ? 'Flat until another sync lands' : 'Historical retirement coverage available'}
                tone="amber"
              />
              <SummaryCard
                icon={<Info size={15} />}
                label="Credit Cards"
                value={fmt(latestPoint?.credit_cards || 0)}
                meta="Liability balances"
                tone="rose"
              />
            </div>

            <section className="app-surface mb-6 rounded-lg p-5 dark:border-slate-800 dark:bg-slate-950">
              <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
                <div>
                  <h2 className="text-lg font-semibold">Net Worth History</h2>
                  <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">Bank accounts, investments, retirement, and credit cards are shown here. The chart uses separate lines so flat balances stay visually flat.</p>
                </div>
                <div className="flex flex-wrap gap-2">
                  {(Object.keys(GROUP_META) as VisibleGroupKey[]).map((group) => {
                    const meta = GROUP_META[group]
                    const hidden = hiddenGroups.has(group)
                    const mode =
                      data?.groups?.find((item) => item.key === group || (group === 'bank_accounts' && item.key === 'cash_like'))?.history_mode ||
                      (group === 'bank_accounts' ? 'historical' : 'latest_only')
                    return (
                      <button
                        key={group}
                        onClick={() => toggleGroup(group)}
                        className={`rounded-full border px-3 py-1.5 text-xs transition-colors ${
                          hidden
                            ? 'border-slate-300 text-slate-500 dark:border-slate-700 dark:text-slate-400'
                            : 'border-transparent text-slate-900 dark:text-slate-100'
                        }`}
                        style={{
                          backgroundColor: hidden ? 'transparent' : `${meta.color}18`,
                        }}
                        >
                        <span className="mr-1 inline-block h-2 w-2 rounded-full align-middle" style={{ backgroundColor: meta.color }} />
                        {meta.label}
                        <span className="ml-2 text-[10px] uppercase tracking-wide text-slate-500 dark:text-slate-400">{mode === 'historical' ? 'history' : 'latest only'}</span>
                      </button>
                    )
                  })}
                </div>
              </div>

              <div className="h-[380px]">
                <ResponsiveContainer width="100%" height="100%">
                  <ComposedChart data={chartData}>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(148,163,184,0.18)" />
                    <XAxis
                      dataKey="date"
                      tickFormatter={formatShortDate}
                      minTickGap={32}
                      tick={{ fontSize: 12, fill: '#94a3b8' }}
                    />
                    <YAxis
                      tickFormatter={(value) => formatAxisCurrency(value, privacyMode, displayCurrency)}
                      tick={{ fontSize: 12, fill: '#94a3b8' }}
                      width={86}
                    />
                    <Tooltip content={<NetWorthTooltip visibleGroups={visibleGroups} privacyMode={privacyMode} displayCurrency={displayCurrency} />} />
                    {visibleGroups.includes('bank_accounts') ? (
                      <Line type="monotone" dataKey="bank_accounts" stroke={GROUP_META.bank_accounts.color} strokeWidth={2.2} dot={false} />
                    ) : null}
                    {visibleGroups.includes('brokerage') ? (
                      <Line type="monotone" dataKey="brokerage" stroke={GROUP_META.brokerage.color} strokeWidth={2.2} dot={false} />
                    ) : null}
                    {visibleGroups.includes('retirement') ? (
                      <Line type="monotone" dataKey="retirement" stroke={GROUP_META.retirement.color} strokeWidth={2.2} dot={false} />
                    ) : null}
                    {visibleGroups.includes('credit_cards') ? (
                      <Line type="monotone" dataKey="credit_cards" stroke={GROUP_META.credit_cards.color} strokeWidth={2.2} dot={false} />
                    ) : null}
                    <Line type="monotone" dataKey="total" stroke="#cdb79c" strokeWidth={2.4} dot={false} />
                  </ComposedChart>
                </ResponsiveContainer>
              </div>
            </section>

            <div className="grid gap-4 xl:grid-cols-[1.15fr_0.85fr]">
              <section className="app-surface rounded-lg p-5 dark:border-slate-800 dark:bg-slate-950">
                <div className="mb-4 flex items-center justify-between gap-3">
                  <div>
                    <h2 className="text-lg font-semibold">Latest Source Breakdown</h2>
                    <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
                      Largest balances first.
                    </p>
                  </div>
                  <span className="rounded-full bg-slate-200 px-2.5 py-1 text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                    {latestRows.length} sources
                  </span>
                </div>
                <div className="overflow-x-auto">
                  <table className="min-w-full text-sm">
                    <thead>
                      <tr className="border-b text-left text-xs uppercase tracking-wide text-slate-500 dark:border-slate-800">
                        <th className="pb-3 pr-4">Source</th>
                        <th className="pb-3 pr-4">Group</th>
                        <th className="pb-3 text-right">Current</th>
                      </tr>
                    </thead>
                    <tbody>
                      {latestRows.map((row) => {
                        const meta = GROUP_META[row.group]
                        return (
                          <tr key={row.key} className="border-b dark:border-slate-800">
                            <td className="py-3 pr-4 font-medium">{row.label}</td>
                            <td className="py-3 pr-4">
                              <span className={`rounded-full px-2 py-1 text-xs ${meta.pillClass}`}>{meta.label}</span>
                            </td>
                            <td className={`py-3 text-right font-semibold ${row.current >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-400'}`}>
                              {formatCurrency(row.current, privacyMode, { currency: displayCurrency })}
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              </section>

            </div>
          </>
        ) : null}
      </div>
    </div>
  )
}

function SummaryCard({
  icon,
  label,
  value,
  meta,
  tone,
}: {
  icon: ReactNode
  label: string
  value: string
  meta: string
  tone: 'emerald' | 'blue' | 'rose' | 'amber'
}) {
  const toneClass =
    tone === 'emerald'
      ? 'text-emerald-600 dark:text-emerald-400 bg-emerald-500/10'
      : tone === 'blue'
        ? 'text-blue-500 bg-blue-500/10'
        : tone === 'rose'
          ? 'text-rose-400 bg-rose-500/10'
          : 'text-amber-400 bg-amber-500/10'

  return (
    <div className="app-surface rounded-lg p-4 dark:border-slate-800 dark:bg-slate-950">
      <div className="mb-3 flex items-center gap-2">
        <span className={`rounded-full p-2 ${toneClass}`}>{icon}</span>
        <span className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">{label}</span>
      </div>
      <div className="text-xl font-semibold tracking-tight">{value}</div>
      <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">{meta}</div>
    </div>
  )
}

function CardShell({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <section className={`app-surface rounded-lg p-5 dark:border-slate-800 dark:bg-slate-950 ${className}`}>{children}</section>
}

function CoverageItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-slate-500 dark:text-slate-400">{label}</span>
      <span className="font-medium">{value}</span>
    </div>
  )
}

function NetWorthTooltip({ active, payload, label, visibleGroups, privacyMode, displayCurrency }: any) {
  if (!active || !payload?.length) return null
  const point = payload[0]?.payload
  if (!point) return null

  return (
    <div className="app-surface-strong rounded-lg p-3 text-sm dark:border-slate-800 dark:bg-slate-950">
      <div className="mb-2 font-medium">{new Date(`${label}T12:00:00`).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}</div>
      <div className="mb-3 text-base font-semibold">{formatCurrency(point.total || 0, privacyMode, { currency: displayCurrency })}</div>
      <div className="space-y-1.5">
        {visibleGroups.map((group: VisibleGroupKey) => (
          <div key={group} className="flex items-center justify-between gap-4">
            <span className="flex items-center gap-2">
              <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: GROUP_META[group].color }} />
              <span>{GROUP_META[group].label}</span>
            </span>
            <span className={point[group] >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-400'}>{formatCurrency(point[group] || 0, privacyMode, { currency: displayCurrency })}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
