import { useQuery } from '@tanstack/react-query'
import { fetchJson } from './api'
import { useFilterStore } from './store'
import { formatCount, formatCurrency, formatPercent, formatPlainNumber } from './privacy'

interface Holding {
  ticker: string
  name: string
  quantity: number
  price: number
  value: number
  cost_basis: number
  type: string
  source: string
}

interface InvestmentHistoryPoint {
  synced_at: string
  source: string
  value: number
  inflow?: number
  market_gain?: number
  ending_value?: number
}

interface AccountActivity {
  id: string
  date: string
  description: string
  merchant?: string
  type: 'interest' | 'transfer' | 'dividend' | 'trade' | 'other'
  amount: number
  pending: boolean
}

function prettyStatementDate(value: string | null | undefined) {
  if (!value) return 'Unknown'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
}

export function InvestmentsPage({ onBack, source }: { onBack: () => void; source?: string | null }) {
  const privacyMode = useFilterStore((state) => state.privacyMode)
  const displayCurrency = useFilterStore((state) => state.displayCurrency)
  const fmt = (n: number) => formatCurrency(n, privacyMode, { currency: displayCurrency })
  const pct = (n: number) => formatPercent(n, privacyMode, { signed: true })
  const num = (n: number, digits = 4) => formatPlainNumber(n, privacyMode, { digits })
  const { data, isLoading } = useQuery({
    queryKey: ['investments', displayCurrency],
    queryFn: () => fetchJson<{ holdings: Holding[]; accounts: any[] }>(`/sync/plaid/investments?currency=${encodeURIComponent(displayCurrency)}`),
    staleTime: 60_000,
  })
  const { data: historyData } = useQuery({
    queryKey: ['investment-history', displayCurrency],
    queryFn: () => fetchJson<{ history: InvestmentHistoryPoint[] }>(`/sync/plaid/investments/history?currency=${encodeURIComponent(displayCurrency)}`),
    staleTime: 60_000,
  })
  const visibleSource = source || null
  const { data: activityData } = useQuery({
    queryKey: ['investment-activity', visibleSource, displayCurrency],
    queryFn: () => fetchJson<{ activity: AccountActivity[] }>(`/sync/plaid/investments/activity?source=${encodeURIComponent(visibleSource || '')}&currency=${encodeURIComponent(displayCurrency)}`),
    enabled: Boolean(visibleSource),
    staleTime: 60_000,
  })

  const sourceHoldingsRaw = visibleSource ? (data?.holdings || []).filter((h) => h.source === visibleSource) : (data?.holdings || [])
  const holdings = sourceHoldingsRaw.filter(h => h.ticker !== 'CUR:USD').sort((a, b) => (b.value || 0) - (a.value || 0))
  const cash = sourceHoldingsRaw.find(h => h.ticker === 'CUR:USD')
  const holdingsValue = holdings.reduce((s, h) => s + (h.value || 0), 0) + (cash?.value || 0)
  const totalCost = holdings.reduce((s, h) => s + (h.cost_basis || 0), 0)
  const history = (historyData?.history || []).filter((row) => !visibleSource || row.source === visibleSource)
  const latestHistory = history.length ? history[history.length - 1] : null
  const totalValue = visibleSource && latestHistory ? latestHistory.value : holdingsValue
  const totalGL = holdingsValue - totalCost - (cash?.value || 0)
  const totalPct = totalCost > 0 ? totalGL / totalCost : 0
  const title = visibleSource || 'Investments'
  const monthlyHistory = [...history].sort((a, b) => a.synced_at.localeCompare(b.synced_at)).slice(-12)
  const activity = activityData?.activity || []

  return (
    <div className="app-shell min-h-screen dark:bg-slate-900 text-slate-900 dark:text-slate-100 p-4">
      <div className="flex items-center gap-3 mb-5">
        <button onClick={onBack} className="text-sm text-blue-500 hover:underline">← Back</button>
        <h1 className="text-lg font-bold">Investments</h1>
        <span className="text-xs text-slate-500">{title}</span>
      </div>

      {isLoading ? <p className="text-slate-500">Loading...</p> : (
        <>
          <div className={`grid gap-3 mb-5 ${holdings.length ? 'grid-cols-4' : 'grid-cols-2'}`}>
            <div className="app-surface dark:bg-slate-800 rounded-lg p-3">
              <div className="text-xs text-slate-500">{holdings.length ? 'Portfolio Value' : 'Current Balance'}</div>
              <div className="text-xl font-bold">{fmt(totalValue)}</div>
            </div>
            {holdings.length ? <div className="app-surface dark:bg-slate-800 rounded-lg p-3">
              <div className="text-xs text-slate-500">Total Cost</div>
              <div className="text-lg font-bold">{fmt(totalCost)}</div>
            </div> : null}
            {holdings.length ? <div className="app-surface dark:bg-slate-800 rounded-lg p-3">
              <div className="text-xs text-slate-500">Gain/Loss</div>
              <div className={`text-lg font-bold ${totalGL >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-400'}`}>
                {fmt(totalGL)} ({pct(totalPct)})
              </div>
            </div> : null}
            <div className="app-surface dark:bg-slate-800 rounded-lg p-3">
              <div className="text-xs text-slate-500">Latest Snapshot</div>
              <div className="text-lg font-bold">{prettyStatementDate(latestHistory?.synced_at || null)}</div>
            </div>
          </div>

          {holdings.length ? <div className="app-surface dark:bg-slate-800 rounded-lg p-4">
            <h2 className="font-semibold text-sm mb-3">Holdings ({formatCount(holdings.length, privacyMode)})</h2>
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-slate-500 border-b dark:border-slate-700">
                  <th className="pb-2 w-16">Ticker</th>
                  <th className="pb-2">Name</th>
                  <th className="pb-2 text-right">Shares</th>
                  <th className="pb-2 text-right">Price</th>
                  <th className="pb-2 text-right">Value</th>
                  <th className="pb-2 text-right">Cost</th>
                  <th className="pb-2 text-right">Gain/Loss</th>
                  <th className="pb-2 text-right">%</th>
                </tr>
              </thead>
              <tbody>
                {holdings.map(h => {
                  const gl = (h.value || 0) - (h.cost_basis || 0)
                  const glPct = h.cost_basis > 0 ? gl / h.cost_basis : 0
                  return (
                    <tr key={h.ticker} className="border-b dark:border-slate-700/50 hover:bg-[var(--app-surface-2)] dark:hover:bg-slate-700/30">
                      <td className="py-2 font-mono font-medium">{h.ticker}</td>
                      <td className="py-2 truncate max-w-[200px]">{h.name}</td>
                      <td className="py-2 text-right font-mono">{num(h.quantity, 4)}</td>
                      <td className="py-2 text-right">{fmt(h.price)}</td>
                      <td className="py-2 text-right font-medium">{fmt(h.value)}</td>
                      <td className="py-2 text-right text-slate-500">{fmt(h.cost_basis)}</td>
                      <td className={`py-2 text-right font-medium ${gl >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-400'}`}>
                        {fmt(gl)}
                      </td>
                      <td className={`py-2 text-right text-xs ${gl >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-400'}`}>
                        {pct(glPct)}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
              <tfoot>
                <tr className="border-t-2 dark:border-slate-600 font-bold">
                  <td className="pt-2" colSpan={4}>Total</td>
                  <td className="pt-2 text-right">{fmt(totalValue)}</td>
                  <td className="pt-2 text-right text-slate-500">{fmt(totalCost)}</td>
                  <td className={`pt-2 text-right ${totalGL >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-400'}`}>{fmt(totalGL)}</td>
                  <td className={`pt-2 text-right text-xs ${totalGL >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-400'}`}>{pct(totalPct)}</td>
                </tr>
              </tfoot>
            </table>
          </div> : null}

          {holdings.length ? <div className="app-surface dark:bg-slate-800 rounded-lg p-4 mt-4">
            <h2 className="font-semibold text-sm mb-3">Allocation</h2>
            <div className="flex h-6 rounded-full overflow-hidden">
              {holdings.map((h, i) => {
                const weight = totalValue > 0 ? (h.value || 0) / totalValue * 100 : 0
                if (weight < 1) return null
                const colors = ['#3b82f6', '#22c55e', '#eab308', '#f97316', '#ef4444', '#8b5cf6', '#ec4899', '#06b6d4', '#14b8a6', '#f43f5e']
                return (
                  <div key={h.ticker} title={`${h.ticker} ${weight.toFixed(1)}%`}
                    className="h-full transition-all" style={{ width: `${weight}%`, backgroundColor: colors[i % colors.length] }} />
                )
              })}
            </div>
            <div className="flex flex-wrap gap-3 mt-2">
              {holdings.map((h, i) => {
                const weight = totalValue > 0 ? (h.value || 0) / totalValue * 100 : 0
                if (weight < 1) return null
                const colors = ['#3b82f6', '#22c55e', '#eab308', '#f97316', '#ef4444', '#8b5cf6', '#ec4899', '#06b6d4', '#14b8a6', '#f43f5e']
                return (
                  <span key={h.ticker} className="flex items-center gap-1 text-xs">
                    <span className="w-2 h-2 rounded-full" style={{ backgroundColor: colors[i % colors.length] }} />
                    {h.ticker} {privacyMode ? 'xxx.x%' : `${weight.toFixed(1)}%`}
                  </span>
                )
              })}
            </div>
          </div> : null}

          {visibleSource ? (
            <div className="app-surface dark:bg-slate-800 rounded-lg p-4 mt-4">
              <div className="mb-3 flex items-center justify-between gap-3">
                <h2 className="font-semibold text-sm">Account Activity</h2>
                <span className="text-xs text-slate-500">{formatCount(activity.length, privacyMode)} trades and cash events</span>
              </div>
              {activity.length ? (
                <div className="max-h-[28rem] overflow-auto rounded-md border border-slate-200 dark:border-slate-700">
                  <table className="w-full min-w-[42rem] text-sm">
                    <thead className="sticky top-0 z-10 bg-[var(--app-surface)] dark:bg-slate-800">
                      <tr className="text-left text-xs text-slate-500 border-b dark:border-slate-700">
                        <th className="p-2">Date</th><th className="p-2">Activity</th><th className="p-2">Type</th><th className="p-2 text-right">Amount</th>
                      </tr>
                    </thead>
                    <tbody>{activity.map((row) => (
                      <tr key={row.id} className="border-b last:border-b-0 dark:border-slate-700/50">
                        <td className="p-2 whitespace-nowrap">{prettyStatementDate(row.date)}</td>
                        <td className="p-2">{row.merchant || row.description}{row.pending ? <span className="ml-2 text-xs text-slate-500">pending</span> : null}</td>
                        <td className="p-2 capitalize text-slate-500">{row.type}</td>
                        <td className={`p-2 text-right font-medium whitespace-nowrap ${row.amount >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-400'}`}>{fmt(row.amount)}</td>
                      </tr>
                    ))}</tbody>
                  </table>
                </div>
              ) : <p className="text-sm text-slate-500">No account activity has been synced yet.</p>}
            </div>
          ) : null}

          {visibleSource ? (
            <div className="app-surface dark:bg-slate-800 rounded-lg p-4 mt-4">
              <div className="mb-3 flex items-center justify-between gap-3">
                <h2 className="font-semibold text-sm">Balance History</h2>
                <span className="text-xs text-slate-500">{formatCount(monthlyHistory.length, privacyMode)} snapshots</span>
              </div>
              {monthlyHistory.length ? (
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-xs text-slate-500 border-b dark:border-slate-700">
                      <th className="pb-2">Month</th>
                      <th className="pb-2 text-right">Inflow</th>
                      <th className="pb-2 text-right">Gain</th>
                      <th className="pb-2 text-right">End</th>
                    </tr>
                  </thead>
                  <tbody>
                    {monthlyHistory.map((row) => (
                      <tr key={`${row.source}-${row.synced_at}`} className="border-b dark:border-slate-700/50">
                        <td className="py-2">{prettyStatementDate(row.synced_at)}</td>
                        <td className="py-2 text-right font-medium">{row.inflow !== undefined ? fmt(row.inflow) : '—'}</td>
                        <td className={`py-2 text-right font-medium ${((row.market_gain || 0) >= 0) ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-400'}`}>
                          {row.market_gain !== undefined ? fmt(row.market_gain) : '—'}
                        </td>
                        <td className="py-2 text-right font-medium">{fmt(row.ending_value ?? row.value)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <p className="text-sm text-slate-500">No historical snapshots yet.</p>
              )}
            </div>
          ) : null}
        </>
      )}
    </div>
  )
}
