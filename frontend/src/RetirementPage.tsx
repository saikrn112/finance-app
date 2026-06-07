import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchJson } from './api'
import { useFilterStore } from './store'
import { formatCount, formatCurrency, formatPercent, formatPlainNumber } from './privacy'

interface RetirementTxn {
  type: string
  date: string
  source: string
  fund: string
  units: number
  unit_price: number
  amount: number
}

interface RetirementSummary {
  balance: number
  total_units?: number
  unit_price?: number
  total_contributed: number
  employee_contributed: number
  employer_match: number
  fees?: number
  gain: number
  fund_name?: string
  date_range: string[]
  vested_balance?: number
  rate_of_return?: number
  latest_statement_end?: string
}

interface StatementRow {
  period_start: string
  period_end: string
  beginning_balance: number
  employee_contributions: number
  employer_contributions: number
  market_change: number
  ending_balance: number
  vested_balance: number
  rate_of_return: number
  source_file: string
}

interface ActivityRow {
  date: string
  investment: string
  transaction_type: string
  amount: number
  shares: number
  unit_price: number
}

interface RetirementPayload {
  source_type?: string
  plan_name?: string | null
  history_date_range?: string | null
  transactions?: RetirementTxn[]
  summary?: RetirementSummary
  statements?: StatementRow[]
  activity?: ActivityRow[]
}

interface RetirementSourceOption {
  value: string
  label: string
}

function useRetirementSources() {
  const { data } = useQuery({
    queryKey: ['import-sources'],
    queryFn: () => fetch('/api/imports/sources').then((r) => r.json()),
    staleTime: 300_000,
  })

  return useMemo(() => {
    const all = (data || []) as Array<{ value: string; label: string; group: string }>
    const retirementSources = all
      .filter((item) => item.group === 'Retirement')
      .map((item) => ({ value: item.value, label: item.label }))
    return retirementSources.length > 0 ? retirementSources : []
  }, [data])
}

function normalizeRetirementSource(value?: string | null, sources?: RetirementSourceOption[]): string {
  const normalized = (value || '').trim().toLowerCase()
  if (sources?.length) {
    const match = sources.find((s) => s.value.toLowerCase() === normalized || s.label.toLowerCase() === normalized)
    if (match) return match.value
  }
  return sources?.[0]?.value || normalized || ''
}

function prettyDate(value: string | null | undefined) {
  if (!value) return 'Unknown'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
}

function hasRetirementSummary(summary: RetirementSummary | undefined): summary is RetirementSummary {
  return !!summary && Number.isFinite(summary.balance)
}

export function RetirementPage({
  onBack,
  source,
}: {
  onBack: () => void
  source?: string | null
}) {
  const privacyMode = useFilterStore((state) => state.privacyMode)
  const displayCurrency = useFilterStore((state) => state.displayCurrency)
  const retirementSources = useRetirementSources()
  const [selectedSource, setSelectedSource] = useState<string>(() => normalizeRetirementSource(source, retirementSources))
  const fmt = (n: number) => formatCurrency(n, privacyMode, { currency: displayCurrency })
  const pct = (n: number) => formatPercent(n, privacyMode, { signed: true })
  const num = (n: number, digits = 2) => formatPlainNumber(n, privacyMode, { digits })

  const { data, isLoading, error } = useQuery({
    queryKey: ['retirement', selectedSource, displayCurrency],
    queryFn: () => fetchJson<RetirementPayload>(`/retirement/?source=${selectedSource}&currency=${encodeURIComponent(displayCurrency)}`),
    enabled: !!selectedSource,
    staleTime: 60_000,
  })

  const summary = hasRetirementSummary(data?.summary) ? data.summary : null
  const txns = data?.transactions || []
  const statements = useMemo(() => (data?.statements || []).slice().sort((a, b) => a.period_end.localeCompare(b.period_end)), [data?.statements])
  const activity = useMemo(() => (data?.activity || []).slice().sort((a, b) => b.date.localeCompare(a.date)), [data?.activity])
  const sourceLabel = retirementSources.find((s) => s.value === selectedSource)?.label || selectedSource
  const gainPct = summary && summary.total_contributed > 0 ? summary.gain / summary.total_contributed : 0

  useEffect(() => {
    setSelectedSource(normalizeRetirementSource(source, retirementSources))
  }, [source, retirementSources])

  if (isLoading) {
    return <div className="text-sm text-slate-500">Loading retirement...</div>
  }

  if (error) {
    return <div className="text-sm text-rose-500">Error loading retirement: {String(error)}</div>
  }

  return (
    <div className="text-slate-900 dark:text-slate-100">
      <div className="mb-5 flex items-center gap-3">
        <button onClick={onBack} className="text-sm text-blue-500 hover:underline">← Back</button>
        <h1 className="text-lg font-bold">Retirement</h1>
        <span className="text-xs text-slate-500">{sourceLabel}</span>
      </div>

      {!summary ? (
        <div className="text-sm text-slate-500">No {sourceLabel} data found.</div>
      ) : (statements.length > 0 || activity.length > 0) ? (
        <>
          <div className="grid grid-cols-5 gap-3 mb-5">
            <div className="app-surface dark:bg-slate-950 dark:border-slate-800 rounded-lg p-3">
              <div className="text-xs text-slate-500">Balance</div>
              <div className="text-xl font-bold">{fmt(summary.balance)}</div>
            </div>
            <div className="app-surface dark:bg-slate-950 dark:border-slate-800 rounded-lg p-3">
              <div className="text-xs text-slate-500">Your Contributions</div>
              <div className="text-lg font-bold">{fmt(summary.employee_contributed)}</div>
            </div>
            <div className="app-surface dark:bg-slate-950 dark:border-slate-800 rounded-lg p-3">
              <div className="text-xs text-slate-500">Employer Match</div>
              <div className="text-lg font-bold text-emerald-600 dark:text-emerald-400">{fmt(summary.employer_match)}</div>
            </div>
            <div className="app-surface dark:bg-slate-950 dark:border-slate-800 rounded-lg p-3">
              <div className="text-xs text-slate-500">Gain/Loss</div>
              <div className={`text-lg font-bold ${summary.gain >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-400'}`}>
                {fmt(summary.gain)}
              </div>
            </div>
            <div className="app-surface dark:bg-slate-950 dark:border-slate-800 rounded-lg p-3">
              <div className="text-xs text-slate-500">Total Contributions</div>
              <div className="text-lg font-bold">{fmt(summary.total_contributed)}</div>
              <div className="text-[10px] text-slate-500">{pct(summary.rate_of_return || 0)} latest period</div>
            </div>
          </div>

          <div className="app-surface dark:bg-slate-950 dark:border-slate-800 rounded-lg p-4 mb-5">
            <div className="mb-3 flex items-center justify-between gap-3">
              <div>
                <h2 className="font-semibold text-sm">Monthly Statements</h2>
                <p className="text-xs text-slate-500">{data?.plan_name || 'Retirement Plan'}</p>
              </div>
              <span className="text-xs text-slate-500">{formatCount(statements.length, privacyMode)} statements</span>
            </div>
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-slate-500 border-b dark:border-slate-800">
                  <th className="pb-2">Month</th>
                  <th className="pb-2 text-right">Employee</th>
                  <th className="pb-2 text-right">Employer</th>
                  <th className="pb-2 text-right">Market</th>
                  <th className="pb-2 text-right">End</th>
                </tr>
              </thead>
              <tbody>
                {statements.map((row) => (
                  <tr key={row.period_end} className="border-b dark:border-slate-800/70">
                    <td className="py-2">{prettyDate(row.period_end)}</td>
                    <td className="py-2 text-right font-medium">{fmt(row.employee_contributions)}</td>
                    <td className="py-2 text-right font-medium">{fmt(row.employer_contributions)}</td>
                    <td className={`py-2 text-right font-medium ${row.market_change >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-400'}`}>{fmt(row.market_change)}</td>
                    <td className="py-2 text-right font-medium">{fmt(row.ending_balance)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="app-surface dark:bg-slate-950 dark:border-slate-800 rounded-lg p-4">
            <div className="mb-3 flex items-center justify-between gap-3">
              <div>
                <h2 className="font-semibold text-sm">Contribution Activity</h2>
                <p className="text-xs text-slate-500">{data?.history_date_range || 'Statement export'}</p>
              </div>
              <span className="text-xs text-slate-500">{formatCount(activity.length, privacyMode)} rows</span>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs text-slate-500 border-b dark:border-slate-800">
                    <th className="pb-2">Date</th>
                    <th className="pb-2">Investment</th>
                    <th className="pb-2">Type</th>
                    <th className="pb-2 text-right">Shares</th>
                    <th className="pb-2 text-right">Amount</th>
                  </tr>
                </thead>
                <tbody>
                  {activity.map((row) => (
                    <tr key={`${row.date}|${row.investment}|${row.amount}|${row.shares}`} className="border-b dark:border-slate-800/70">
                      <td className="py-1.5">{row.date}</td>
                      <td className="truncate max-w-[220px]">{row.investment}</td>
                      <td>{row.transaction_type}</td>
                      <td className="text-right font-mono">{num(row.shares, 3)}</td>
                      <td className="text-right font-medium text-emerald-600 dark:text-emerald-400">{fmt(row.amount)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      ) : (
        <>
          <div className="grid grid-cols-5 gap-3 mb-5">
            <div className="app-surface dark:bg-slate-950 dark:border-slate-800 rounded-lg p-3">
              <div className="text-xs text-slate-500">Balance</div>
              <div className="text-xl font-bold">{fmt(summary.balance)}</div>
            </div>
            <div className="app-surface dark:bg-slate-950 dark:border-slate-800 rounded-lg p-3">
              <div className="text-xs text-slate-500">Your Contributions</div>
              <div className="text-lg font-bold">{fmt(summary.employee_contributed)}</div>
            </div>
            <div className="app-surface dark:bg-slate-950 dark:border-slate-800 rounded-lg p-3">
              <div className="text-xs text-slate-500">Employer Match</div>
              <div className="text-lg font-bold text-emerald-600 dark:text-emerald-400">{fmt(summary.employer_match)}</div>
            </div>
            <div className="app-surface dark:bg-slate-950 dark:border-slate-800 rounded-lg p-3">
              <div className="text-xs text-slate-500">Gain/Loss</div>
              <div className={`text-lg font-bold ${summary.gain >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-400'}`}>
                {fmt(summary.gain)} ({pct(gainPct)})
              </div>
            </div>
            <div className="app-surface dark:bg-slate-950 dark:border-slate-800 rounded-lg p-3">
              <div className="text-xs text-slate-500">Unit Price</div>
              <div className="text-lg font-bold">{privacyMode ? '$xxxx.xx' : `$${(summary.unit_price || 0).toFixed(2)}`}</div>
              <div className="text-[10px] text-slate-500">{privacyMode ? 'xxxx.xx units' : `${(summary.total_units || 0).toFixed(2)} units`}</div>
            </div>
          </div>

          <div className="app-surface dark:bg-slate-950 dark:border-slate-800 rounded-lg p-4 mb-5">
            <h2 className="font-semibold text-sm mb-3">Contributions</h2>
            <div className="flex h-6 rounded-full overflow-hidden mb-2">
              <div className="h-full bg-blue-500" style={{ width: `${summary.employee_contributed / summary.total_contributed * 100}%` }} title={`Employee ${fmt(summary.employee_contributed)}`} />
              <div className="h-full bg-emerald-500" style={{ width: `${summary.employer_match / summary.total_contributed * 100}%` }} title={`Match ${fmt(summary.employer_match)}`} />
            </div>
            <div className="flex gap-4 text-xs">
              <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-blue-500" />Employee {fmt(summary.employee_contributed)}</span>
              <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-emerald-500" />Match {fmt(summary.employer_match)}</span>
              <span className="flex items-center gap-1 text-slate-500">Fees {fmt(summary.fees || 0)}</span>
            </div>
          </div>

          <div className="app-surface dark:bg-slate-950 dark:border-slate-800 rounded-lg p-4">
            <h2 className="font-semibold text-sm mb-3">Activity ({formatCount(txns.length, privacyMode)})</h2>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs text-slate-500 border-b dark:border-slate-800">
                    <th className="pb-2">Date</th>
                    <th className="pb-2">Type</th>
                    <th className="pb-2">Source</th>
                    <th className="pb-2 text-right">Units</th>
                    <th className="pb-2 text-right">Price</th>
                    <th className="pb-2 text-right">Amount</th>
                  </tr>
                </thead>
                <tbody>
                  {txns.slice().reverse().map((t, i) => (
                    <tr key={i} className="border-b dark:border-slate-800/70">
                      <td className="py-1.5">{t.date}</td>
                      <td>{t.type}</td>
                      <td className="text-slate-500 truncate max-w-[150px]">{t.source}</td>
                      <td className="text-right font-mono">{num(t.units, 4)}</td>
                      <td className="text-right">{privacyMode ? '$xxxx.xx' : `$${t.unit_price.toFixed(2)}`}</td>
                      <td className={`text-right font-medium ${t.amount >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-400'}`}>{fmt(t.amount)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
