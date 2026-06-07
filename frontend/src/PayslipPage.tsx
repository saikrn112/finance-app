import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { ArrowLeft, BadgeDollarSign, CalendarRange, Clock3, Landmark, TrendingUp } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { AgGridReact } from 'ag-grid-react'
import { AllCommunityModule, ModuleRegistry, type ColDef, type SortDirection } from 'ag-grid-community'
import {
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
} from 'recharts'
import { createChart, ColorType, LineSeries } from 'lightweight-charts'
import type { IChartApi, Time } from 'lightweight-charts'
import { fetchJson } from './api'
import { getGridTheme } from './gridTheme'
import { getCssVar } from './themeUtils'
import { useFilterStore } from './store'
import { formatCount, formatCurrency, formatPercent, formatPlainNumber } from './privacy'

ModuleRegistry.registerModules([AllCommunityModule])

interface Payslip {
  employer: string
  pay_date: string
  period_start?: string
  period_end?: string
  gross: number
  net: number
  total_taxes: number
  total_deductions: number
  taxes: Record<string, number>
  deductions: Record<string, number>
  earnings: Record<string, number>
  benefits: Record<string, number>
  gross_ytd?: number
  hours?: number
}

const TAX_LABELS: Record<string, string> = {
  federal: 'Federal',
  social_security: 'Social Security',
  medicare: 'Medicare',
  state_tax: 'State Tax',
  state_disability: 'State Disability',
  ca_state: 'CA State',
  ca_sdi: 'CA SDI',
  md_state: 'MD State',
  md_county: 'MD County',
}

const DED_LABELS: Record<string, string> = {
  hsa: 'HSA',
  dental: 'Dental',
  medical: 'Medical',
  vision: 'Vision',
  '401k': '401k',
  accident_ins: 'Accident Ins',
  std: 'Short Term Disability',
}

const EARN_LABELS: Record<string, string> = {
  regular: 'Regular',
  sign_bonus: 'Sign-On Bonus',
  float_holiday: 'Float Holiday',
  holiday_pay: 'Holiday Pay',
  relo_gross_up: 'Relo Gross Up',
  relo: 'Relo Payment',
}

const BENEFIT_KEYS = new Set(['hsa', 'dental', 'medical', 'vision', 'accident_ins', 'std'])
const RETIREMENT_KEYS = new Set(['401k'])

const GRAPH_WINDOWS = [
  { key: '3m', label: '3M', days: 92 },
  { key: '6m', label: '6M', days: 183 },
  { key: 'y', label: 'Y', days: 365 },
  { key: 'a', label: 'A', days: null },
] as const

type GraphWindowKey = (typeof GRAPH_WINDOWS)[number]['key']
const DEFAULT_SORTING_ORDER: SortDirection[] = ['asc', 'desc']

function toIsoDate(value: string | undefined): string {
  if (!value) return ''
  if (value.includes('-')) return value
  const parts = value.split('/')
  if (parts.length === 3) {
    const [month, day, year] = parts
    return `${year}-${month.padStart(2, '0')}-${day.padStart(2, '0')}`
  }
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toISOString().slice(0, 10)
}

function formatShortDate(value: string | undefined): string {
  const iso = toIsoDate(value)
  if (!iso) return 'Unknown'
  return new Date(`${iso}T12:00:00`).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  })
}

function formatCompactDate(value: string | undefined): string {
  const iso = toIsoDate(value)
  if (!iso) return 'Unknown'
  return new Date(`${iso}T12:00:00`).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
  })
}

function payYear(payslip: Payslip): string {
  return toIsoDate(payslip.pay_date).slice(0, 4)
}

function meaningfulEntries(entries: Record<string, number>, labels: Record<string, string>) {
  return Object.entries(entries || {})
    .filter(([key, value]) => !key.endsWith('_ytd') && Math.abs(value || 0) > 0)
    .map(([key, value]) => ({ key, label: labels[key] || key, amount: value }))
    .sort((a, b) => b.amount - a.amount)
}

function staticEntries(
  entries: Record<string, number>,
  labels: Record<string, string>,
  orderedKeys: string[],
) {
  return orderedKeys.map((key) => ({
    key,
    label: labels[key] || key,
    amount: numericValue(entries?.[key]),
  }))
}

function sumAmounts(items: { amount: number }[]) {
  return items.reduce((sum, item) => sum + item.amount, 0)
}

function safeRatio(numerator: number, denominator: number) {
  return denominator > 0 ? numerator / denominator : 0
}

function numericValue(value: number | null | undefined) {
  return Number.isFinite(value) ? value : 0
}

function sumSelected(entries: Record<string, number>, keys: Set<string>) {
  return Object.entries(entries || {}).reduce((sum, [key, value]) => {
    if (key.endsWith('_ytd') || !keys.has(key)) return sum
    return sum + numericValue(value)
  }, 0)
}

function benefitTotal(payslip: Payslip) {
  return sumSelected(payslip.deductions || {}, BENEFIT_KEYS)
}

function retirementTotal(payslip: Payslip) {
  return sumSelected(payslip.deductions || {}, RETIREMENT_KEYS)
}

function miscTotal(payslip: Payslip) {
  const gross = numericValue(payslip.gross)
  const net = numericValue(payslip.net)
  const taxes = numericValue(payslip.total_taxes)
  const benefits = benefitTotal(payslip)
  const retirement = retirementTotal(payslip)
  return Math.max(gross - net - taxes - benefits - retirement, 0)
}

function collateKeys(payslips: Payslip[], accessor: (payslip: Payslip) => Record<string, number>, labels: Record<string, string>) {
  const seen = new Set<string>()
  payslips.forEach((payslip) => {
    Object.entries(accessor(payslip) || {}).forEach(([key, value]) => {
      if (key.endsWith('_ytd') || Math.abs(value || 0) <= 0) return
      seen.add(key)
    })
  })
  return Object.keys(labels)
    .filter((key) => seen.has(key))
    .sort((left, right) => {
      const li = Object.keys(labels).indexOf(left)
      const ri = Object.keys(labels).indexOf(right)
      return li - ri
    })
}

function chartSliceColor(index: number, palette: string[]) {
  return palette[index % palette.length]
}

type PayrollGraphPoint = {
  date: string
  label: string
  gross: number
  net: number
  taxes: number
  benefits: number
  retirement: number
  misc: number
}

export function PayslipPage({ onBack }: { onBack: () => void }) {
  const privacyMode = useFilterStore((state) => state.privacyMode)
  const displayCurrency = useFilterStore((state) => state.displayCurrency)
  const dark = useFilterStore((state) => state.dark)
  const gridTheme = useMemo(() => getGridTheme(), [dark])
  const fmt = (n: number | undefined | null) => formatCurrency(n ?? 0, privacyMode, { currency: displayCurrency })
  const pct = (n: number) => formatPercent(n, privacyMode)
  const num = (n: number, digits = 1) => formatPlainNumber(n, privacyMode, { digits })
  const { data, isLoading, error } = useQuery({
    queryKey: ['payslips', displayCurrency],
    queryFn: () => fetchJson<{ payslips: Payslip[]; totals: { effective_tax_rate: number } }>(`/payslips/?currency=${encodeURIComponent(displayCurrency)}`),
    staleTime: 60_000,
  })

  const payslips = useMemo(
    () =>
      (data?.payslips || []).slice().sort((a, b) => {
        return toIsoDate(a.pay_date).localeCompare(toIsoDate(b.pay_date))
      }),
    [data],
  )

  const [selectedIdx, setSelectedIdx] = useState<number | null>(null)
  const [graphWindow, setGraphWindow] = useState<GraphWindowKey>('6m')
  const [tableSearch, setTableSearch] = useState('')
  const [tableEmployer, setTableEmployer] = useState('')
  const activeIndex = selectedIdx ?? Math.max(payslips.length - 1, 0)
  const selectedPayslip = payslips[activeIndex]
  const previousPayslip = activeIndex > 0 ? payslips[activeIndex - 1] : null

  const taxKeys = useMemo(() => collateKeys(payslips, (item) => item.taxes || {}, TAX_LABELS), [payslips])
  const deductionKeys = useMemo(() => collateKeys(payslips, (item) => item.deductions || {}, DED_LABELS), [payslips])
  const taxItems = staticEntries(selectedPayslip?.taxes || {}, TAX_LABELS, taxKeys)
  const deductionItems = staticEntries(selectedPayslip?.deductions || {}, DED_LABELS, deductionKeys)
  const earningItems = meaningfulEntries(selectedPayslip?.earnings || {}, EARN_LABELS)

  const employerYearPayslips = useMemo(() => {
    if (!selectedPayslip) return []
    const year = payYear(selectedPayslip)
    const cutoff = toIsoDate(selectedPayslip.pay_date)
    return payslips.filter(
      (p) =>
        p.employer === selectedPayslip.employer &&
        payYear(p) === year &&
        toIsoDate(p.pay_date) <= cutoff,
    )
  }, [payslips, selectedPayslip])

  const employerYearGross = employerYearPayslips.reduce((sum, item) => sum + numericValue(item.gross), 0)
  const employerYearNet = employerYearPayslips.reduce((sum, item) => sum + numericValue(item.net), 0)
  const employerYearTaxes = employerYearPayslips.reduce((sum, item) => sum + numericValue(item.total_taxes), 0)
  const employerYearDeductions = employerYearPayslips.reduce((sum, item) => sum + numericValue(item.total_deductions), 0)

  const currentTakeHome = safeRatio(selectedPayslip?.net || 0, selectedPayslip?.gross || 0)
  const currentTaxRate = safeRatio(selectedPayslip?.total_taxes || 0, selectedPayslip?.gross || 0)
  const currentDeductionRate = safeRatio(selectedPayslip?.total_deductions || 0, selectedPayslip?.gross || 0)
  const priorTakeHome = previousPayslip ? safeRatio(previousPayslip.net, previousPayslip.gross) : null
  const selectedComposition = useMemo(() => {
    if (!selectedPayslip) return null
    const palette = ['#22c55e', '#fb7185', '#f43f5e', '#e11d48', '#be123c', '#fbbf24', '#f59e0b', '#d97706', '#b45309', '#94a3b8']
    const net = numericValue(selectedPayslip.net)
    const taxSlices = taxItems
      .filter((item) => item.amount > 0)
      .map((item, index) => ({
        ...item,
        group: 'Taxes' as const,
        color: chartSliceColor(index + 1, palette),
      }))
    const deductionSlices = deductionItems
      .filter((item) => item.amount > 0)
      .map((item, index) => ({
        ...item,
        group: 'Deductions & Benefits' as const,
        color: chartSliceColor(index + 5, palette),
      }))
    const slices = [
      { key: 'net', label: 'Net Deposit', amount: net, group: 'Net' as const, color: '#22c55e' },
      ...taxSlices,
      ...deductionSlices,
    ]
    return {
      gross: numericValue(selectedPayslip.gross),
      total: slices.reduce((sum, item) => sum + item.amount, 0),
      slices,
      groups: [
        { label: 'Net', items: slices.filter((item) => item.group === 'Net') },
        { label: 'Taxes', items: slices.filter((item) => item.group === 'Taxes') },
        { label: 'Deductions & Benefits', items: slices.filter((item) => item.group === 'Deductions & Benefits') },
      ],
    }
  }, [selectedPayslip, taxItems, deductionItems])

  const graphPayslips = useMemo(() => {
    if (!payslips.length) return []
    const latestIso = toIsoDate(payslips[payslips.length - 1]?.pay_date)
    const windowDef = GRAPH_WINDOWS.find((item) => item.key === graphWindow)
    if (!latestIso || !windowDef || windowDef.days === null) return payslips
    const start = new Date(`${latestIso}T12:00:00`)
    start.setDate(start.getDate() - windowDef.days)
    const startIso = start.toISOString().slice(0, 10)
    return payslips.filter((item) => toIsoDate(item.pay_date) >= startIso)
  }, [graphWindow, payslips])

  const graphData = useMemo<PayrollGraphPoint[]>(
    () => {
      const byDate = new Map<string, PayrollGraphPoint>()
      graphPayslips.forEach((item) => {
        const date = toIsoDate(item.pay_date)
        const existing = byDate.get(date)
        const next = {
          date,
          label: formatCompactDate(item.pay_date),
          gross: numericValue(item.gross),
          net: numericValue(item.net),
          taxes: -numericValue(item.total_taxes),
          benefits: -benefitTotal(item),
          retirement: -retirementTotal(item),
          misc: -miscTotal(item),
        }
        if (!existing) {
          byDate.set(date, next)
          return
        }
        existing.gross += next.gross
        existing.net += next.net
        existing.taxes += next.taxes
        existing.benefits += next.benefits
        existing.retirement += next.retirement
        existing.misc += next.misc
      })
      return Array.from(byDate.values()).sort((a, b) => a.date.localeCompare(b.date))
    },
    [graphPayslips],
  )

  const employers = useMemo(
    () => Array.from(new Set(payslips.map((item) => item.employer))).sort((a, b) => a.localeCompare(b)),
    [payslips],
  )

  const employerComparison = useMemo(
    () =>
      employers.map((employer) => {
        const rows = payslips.filter((item) => item.employer === employer)
        const gross = rows.reduce((sum, item) => sum + numericValue(item.gross), 0)
        const net = rows.reduce((sum, item) => sum + numericValue(item.net), 0)
        const taxes = rows.reduce((sum, item) => sum + numericValue(item.total_taxes), 0)
        const deductions = rows.reduce((sum, item) => sum + numericValue(item.total_deductions), 0)
        return { employer, rows: rows.length, gross, net, taxes, deductions }
      }),
    [employers, payslips],
  )

  const payrollRows = useMemo(
    () =>
      payslips.map((payslip) => ({
        id: `${payslip.employer}-${payslip.pay_date}`,
        pay_date: toIsoDate(payslip.pay_date),
        period: payslip.period_start && payslip.period_end
          ? `${formatCompactDate(payslip.period_start)} - ${formatCompactDate(payslip.period_end)}`
          : '—',
        employer: payslip.employer,
        gross: payslip.gross,
        taxes: payslip.total_taxes,
        deductions: payslip.total_deductions,
        net: payslip.net,
        take_home: safeRatio(payslip.net, payslip.gross),
      })),
    [payslips],
  )

  const filteredPayrollRows = useMemo(() => {
    return payrollRows.filter((row) => {
      const matchesEmployer = !tableEmployer || row.employer === tableEmployer
      const q = tableSearch.trim().toLowerCase()
      const matchesSearch =
        !q ||
        row.employer.toLowerCase().includes(q) ||
        row.period.toLowerCase().includes(q) ||
        row.pay_date.includes(q)
      return matchesEmployer && matchesSearch
    })
  }, [payrollRows, tableEmployer, tableSearch])

  const payrollColDefs = useMemo<ColDef[]>(() => [
    {
      field: 'pay_date',
      headerName: 'Pay Date',
      sort: 'desc',
      width: 140,
      valueFormatter: (params) => formatShortDate(params.value),
    },
    { field: 'period', headerName: 'Period', flex: 1.2 },
    { field: 'employer', headerName: 'Employer', width: 140 },
    {
      field: 'gross',
      headerName: 'Gross',
      width: 130,
      type: 'rightAligned',
      valueFormatter: (params) => fmt(params.value),
    },
    {
      field: 'taxes',
      headerName: 'Taxes',
      width: 130,
      type: 'rightAligned',
      valueFormatter: (params) => fmt(params.value),
      cellClass: 'text-rose-400',
    },
    {
      field: 'deductions',
      headerName: 'Deductions',
      width: 140,
      type: 'rightAligned',
      valueFormatter: (params) => fmt(params.value),
      cellClass: 'text-amber-400',
    },
    {
      field: 'net',
      headerName: 'Net',
      width: 130,
      type: 'rightAligned',
      valueFormatter: (params) => fmt(params.value),
      cellClass: 'text-emerald-400 font-semibold',
    },
    {
      field: 'take_home',
      headerName: 'Take-Home',
      width: 120,
      type: 'rightAligned',
      valueFormatter: (params) => pct(params.value),
    },
  ], [fmt, pct])

  const payrollDefaultColDef = useMemo(
    () => ({ sortable: true, resizable: false, filter: false, sortingOrder: DEFAULT_SORTING_ORDER }),
    [],
  )

  if (isLoading) {
    return <div className="text-sm text-slate-500">Loading payroll...</div>
  }

  if (error) {
    return <div className="text-sm text-rose-500">Error: {String(error)}</div>
  }

  if (!selectedPayslip) {
    return <div className="text-sm text-slate-500">No payslips found</div>
  }

  return (
    <div className="app-shell text-slate-900 dark:text-slate-100">
      <div className="mx-auto max-w-7xl">
        <div className="mb-5 flex items-center gap-3">
          <button onClick={onBack} className="text-sm text-blue-500 hover:underline">← Back</button>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-lg font-bold">Payroll</h1>
              <span className="text-xs text-slate-500 dark:text-slate-400">{selectedPayslip.employer}</span>
              <span className="text-xs text-slate-500 dark:text-slate-400">· Latest {formatShortDate(selectedPayslip.pay_date)}</span>
            </div>
          </div>
        </div>

        <div className="mb-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
          <MetricCard
            icon={<BadgeDollarSign size={15} />}
            label="Net Pay"
            value={fmt(selectedPayslip.net)}
            tone="emerald"
            meta={previousPayslip ? `${fmt(selectedPayslip.net - previousPayslip.net)} vs prior` : 'Latest deposit'}
          />
          <MetricCard
            icon={<TrendingUp size={15} />}
            label="Gross Pay"
            value={fmt(selectedPayslip.gross)}
            tone="blue"
            meta={selectedPayslip.gross_ytd ? `YTD gross ${fmt(selectedPayslip.gross_ytd)}` : 'Current pay cycle'}
          />
          <MetricCard
            icon={<Landmark size={15} />}
            label="Taxes"
            value={fmt(selectedPayslip.total_taxes)}
            tone="rose"
            meta={`${pct(currentTaxRate)} effective`}
          />
          <MetricCard
            icon={<Landmark size={15} />}
            label="Deductions"
            value={fmt(selectedPayslip.total_deductions)}
            tone="amber"
            meta={`${pct(currentDeductionRate)} of gross`}
          />
          <MetricCard
            icon={<Clock3 size={15} />}
            label="Take-Home"
            value={pct(currentTakeHome)}
            tone="slate"
            meta={
              priorTakeHome !== null
                ? `${(currentTakeHome - priorTakeHome >= 0 ? '+' : '') + ((currentTakeHome - priorTakeHome) * 100).toFixed(1)} pts vs prior`
                : 'First payslip in range'
            }
          />
          <MetricCard
            icon={<CalendarRange size={15} />}
            label="YTD Net"
            value={fmt(employerYearNet)}
            tone="emerald"
            meta={`${employerYearPayslips.length} payslips in ${payYear(selectedPayslip)}`}
          />
        </div>

        <section className="app-surface mb-6 rounded-lg p-5 shadow-sm">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-lg font-semibold">Payroll Cashflow</h2>
              <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
                Local graph window only. Gross and net stay positive; taxes, benefits, retirement, and misc are plotted as negatives.
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              {GRAPH_WINDOWS.map((window) => (
                <button
                  key={window.key}
                  type="button"
                  onClick={() => setGraphWindow(window.key)}
                  className={`rounded-full px-3 py-1.5 text-xs font-medium transition-colors ${
                    graphWindow === window.key
                      ? 'bg-blue-500/10 text-blue-500'
                      : 'bg-slate-200 text-slate-600 hover:bg-slate-300 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700'
                  }`}
                >
                  {window.label}
                </button>
              ))}
            </div>
          </div>
          <div className="h-[300px]">
            <PayrollTrendChart data={graphData} dark={dark} privacyMode={privacyMode} displayCurrency={displayCurrency} />
          </div>
        </section>

        <div className="mb-6 grid gap-4 xl:grid-cols-[1.2fr_0.8fr]">
          <div className="space-y-4">
            <section className="app-surface rounded-lg p-5 shadow-sm">
              <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h2 className="text-lg font-semibold">Selected Pay Cycle</h2>
                  <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
                    {formatShortDate(selectedPayslip.pay_date)}
                    {selectedPayslip.period_start && selectedPayslip.period_end
                      ? ` · ${formatCompactDate(selectedPayslip.period_start)} to ${formatCompactDate(selectedPayslip.period_end)}`
                      : ''}
                  </p>
                </div>
                <div className="flex flex-wrap gap-2 text-xs">
                  {selectedPayslip.hours ? (
                    <Chip label={`${num(selectedPayslip.hours, 1)} hrs`} />
                  ) : null}
                  {earningItems.some((item) => item.key === 'sign_bonus') ? <Chip label="Bonus cycle" tone="blue" /> : null}
                  <Chip label={selectedPayslip.employer} tone="slate" />
                </div>
              </div>

              <div className="mb-4 overflow-hidden rounded-lg border border-[var(--app-border-soft)] bg-[var(--app-surface-2)] dark:border-slate-700 dark:bg-slate-900/70">
                <div className="flex h-3 w-full">
                  <div
                    className="bg-emerald-500/85"
                    style={{ width: `${Math.max(currentTakeHome * 100, 0)}%` }}
                    title={`Net ${fmt(selectedPayslip.net)}`}
                  />
                  <div
                    className="bg-rose-500/80"
                    style={{ width: `${Math.max(currentTaxRate * 100, 0)}%` }}
                    title={`Taxes ${fmt(selectedPayslip.total_taxes)}`}
                  />
                  <div
                    className="bg-amber-400/85"
                    style={{ width: `${Math.max(currentDeductionRate * 100, 0)}%` }}
                    title={`Deductions ${fmt(selectedPayslip.total_deductions)}`}
                  />
                </div>
                <div className="grid gap-3 border-t border-[var(--app-border-soft)] p-4 text-sm dark:border-slate-700 md:grid-cols-4">
                  <BridgeStep label="Gross" value={fmt(selectedPayslip.gross)} accent="text-blue-500" />
                  <BridgeStep label="Taxes" value={fmt(selectedPayslip.total_taxes)} accent="text-rose-500 dark:text-rose-400" />
                  <BridgeStep label="Deductions" value={fmt(selectedPayslip.total_deductions)} accent="text-amber-400" />
                  <BridgeStep label="Net Deposit" value={fmt(selectedPayslip.net)} accent="text-emerald-600 dark:text-emerald-400" />
                </div>
              </div>

              <PayCycleCompositionPanel
                composition={selectedComposition}
                formatAmount={fmt}
              />
            </section>

            <section className="app-surface rounded-lg p-5 shadow-sm">
              <div className="mb-4 flex items-center justify-between gap-3">
                <div>
                  <h2 className="text-lg font-semibold">Earnings Mix</h2>
                  <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
                    Useful for spotting bonus-heavy cycles and irregular compensation.
                  </p>
                </div>
                <span className="text-xs text-slate-500 dark:text-slate-400">{fmt(sumAmounts(earningItems))} parsed earnings</span>
              </div>
              <div className="grid gap-3 md:grid-cols-2">
                {earningItems.length > 0 ? (
                  earningItems.map((item) => (
                    <div key={item.key} className="rounded-lg border border-[var(--app-border-soft)] bg-[var(--app-surface-2)] p-3 dark:border-slate-700 dark:bg-slate-900/40">
                      <div className="mb-2 flex items-center justify-between gap-3 text-sm">
                        <span className="font-medium">{item.label}</span>
                        <span className="font-medium text-blue-500">{fmt(item.amount)}</span>
                      </div>
                      <div className="h-2 rounded-full bg-slate-200 dark:bg-slate-800">
                        <div
                          className="h-2 rounded-full bg-blue-500"
                          style={{ width: `${Math.max((item.amount / Math.max(selectedPayslip.gross, 1)) * 100, 3)}%` }}
                        />
                      </div>
                    </div>
                  ))
                ) : (
                  <div className="rounded-xl border border-dashed border-slate-300 p-4 text-sm text-slate-500 dark:border-slate-700 dark:text-slate-400 md:col-span-2">
                    No earnings breakdown was parsed for this payslip.
                  </div>
                )}
              </div>
            </section>
          </div>

          <div className="space-y-4 xl:sticky xl:top-4 xl:self-start">
            <section className="app-surface max-h-[calc(100vh-2rem)] overflow-y-auto rounded-lg shadow-sm flex flex-col">
              <div className="sticky top-0 z-10 border-b border-[var(--app-border-soft)] bg-[var(--app-surface)] px-5 py-4 dark:border-slate-700 dark:bg-slate-800">
                <h2 className="text-lg font-semibold">Payroll Timeline</h2>
                <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
                  Pick a payslip to update the snapshot and compare it to the prior check.
                </p>
              </div>
              <div className="space-y-2 px-5 pb-5 pt-4">
                {payslips
                  .slice()
                  .reverse()
                  .map((payslip) => {
                    const idx = payslips.findIndex((entry) => entry === payslip)
                    const prior = idx > 0 ? payslips[idx - 1] : null
                    const delta = prior ? payslip.net - prior.net : null
                    const isSelected = idx === activeIndex
                    return (
                      <button
                        key={`${payslip.employer}-${payslip.pay_date}`}
                        onClick={() => setSelectedIdx(idx)}
                        className={`w-full rounded-lg border p-3 text-left transition-colors ${
                          isSelected
                            ? 'border-blue-500 bg-blue-500/5'
                            : 'border-[var(--app-border-soft)] hover:border-[var(--app-border)] hover:bg-[var(--app-surface-2)] dark:border-slate-700 dark:hover:border-slate-600 dark:hover:bg-slate-900/60'
                        }`}
                      >
                        <div className="mb-2 flex items-start justify-between gap-3">
                          <div>
                            <div className="font-medium">{formatShortDate(payslip.pay_date)}</div>
                            <div className="text-xs text-slate-500 dark:text-slate-400">
                              {payslip.employer}
                              {payslip.period_start && payslip.period_end
                                ? ` · ${formatCompactDate(payslip.period_start)}-${formatCompactDate(payslip.period_end)}`
                                : ''}
                            </div>
                          </div>
                          <div className="text-right">
                            <div className="font-semibold text-emerald-600 dark:text-emerald-400">{fmt(payslip.net)}</div>
                            <div className="text-xs text-slate-500 dark:text-slate-400">Net deposit</div>
                          </div>
                        </div>
                        <div className="mb-2 space-y-1">
                          <div className="flex items-center justify-between text-[11px] text-slate-500 dark:text-slate-400">
                            <span>Composition</span>
                            <span>{fmt(payslip.gross)} gross</span>
                          </div>
                          <div className="flex h-2.5 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-800">
                            <div
                              className="bg-emerald-500"
                              style={{ width: `${safeRatio(payslip.net, Math.max(payslip.gross, 1)) * 100}%` }}
                            />
                            <div
                              className="bg-rose-500"
                              style={{ width: `${safeRatio(payslip.total_taxes, Math.max(payslip.gross, 1)) * 100}%` }}
                            />
                            <div
                              className="bg-amber-400"
                              style={{ width: `${safeRatio(payslip.total_deductions, Math.max(payslip.gross, 1)) * 100}%` }}
                            />
                          </div>
                        </div>
                        <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
                          <span className="text-slate-500 dark:text-slate-400">
                            Take-home {pct(safeRatio(payslip.net, payslip.gross))}
                          </span>
                          <span
                            className={`rounded-full px-2 py-1 ${
                              delta === null || Math.abs(delta) < 1
                                ? 'bg-slate-200 text-slate-600 dark:bg-slate-800 dark:text-slate-300'
                                : delta >= 0
                                  ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
                                  : 'bg-rose-500/10 text-rose-500 dark:text-rose-400'
                            }`}
                          >
                            {delta === null ? 'Baseline' : Math.abs(delta) < 1 ? 'No change' : `${delta >= 0 ? '+' : ''}${fmt(delta)} vs prior`}
                          </span>
                        </div>
                      </button>
                    )
                  })}
              </div>
            </section>

          </div>
        </div>

        <section className="app-surface rounded-lg p-5 shadow-sm">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-lg font-semibold">All Payslips</h2>
              <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
                Table remains the verification surface. Click a row to drive the selected snapshot above.
              </p>
            </div>
            <span className="rounded-full bg-slate-200 px-2.5 py-1 text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-300">
              {payslips.length} payslips
            </span>
          </div>
          <div className="mb-3 flex items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <select
                value={tableEmployer}
                onChange={(event) => setTableEmployer(event.target.value)}
                className="app-input rounded px-2 py-1.5 text-xs"
              >
                <option value="">All Employers</option>
                {employers.map((employer) => (
                  <option key={employer} value={employer}>{employer}</option>
                ))}
              </select>
              <input
                type="text"
                value={tableSearch}
                onChange={(event) => setTableSearch(event.target.value)}
                placeholder="Search period or employer..."
                className="app-input w-56 rounded px-2 py-1.5 text-xs"
              />
              {(tableEmployer || tableSearch) ? (
                <button
                  type="button"
                  onClick={() => {
                    setTableEmployer('')
                    setTableSearch('')
                  }}
                  className="text-xs text-blue-500 hover:underline"
                >
                  Clear
                </button>
              ) : null}
            </div>
          </div>
          <div style={{ width: '100%', height: 12 * 42 + 90 }}>
            <AgGridReact
              theme={gridTheme}
              rowData={filteredPayrollRows}
              columnDefs={payrollColDefs}
              defaultColDef={payrollDefaultColDef}
              pagination={true}
              paginationPageSize={100}
              paginationPageSizeSelector={[50, 100, 200]}
              animateRows={true}
              getRowId={(params) => params.data.id}
              suppressHorizontalScroll={true}
              ensureDomOrder={true}
              onRowClicked={(event) => {
                const idx = payslips.findIndex(
                  (item) => `${item.employer}-${toIsoDate(item.pay_date)}` === event.data.id,
                )
                if (idx >= 0) setSelectedIdx(idx)
              }}
            />
          </div>
        </section>

        <section className="app-surface mt-6 rounded-lg p-5 shadow-sm">
          <div className="mb-4">
            <h2 className="text-lg font-semibold">Employer Comparison</h2>
            <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
              Compare the overall gross-to-net picture across employers without collapsing them into one timeline.
            </p>
          </div>
          <div className="grid gap-4 xl:grid-cols-3">
            {employerComparison.map((item) => (
              <EmployerSummaryCard key={item.employer} item={item} formatAmount={fmt} />
            ))}
          </div>
        </section>
      </div>
    </div>
  )
}

function MetricCard({
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
  tone: 'emerald' | 'blue' | 'rose' | 'amber' | 'slate'
}) {
  const toneClass =
    tone === 'emerald'
      ? 'text-emerald-400 bg-emerald-500/10'
      : tone === 'blue'
        ? 'text-blue-500 bg-blue-500/10'
        : tone === 'rose'
          ? 'text-rose-400 bg-rose-500/10'
          : tone === 'amber'
            ? 'text-amber-400 bg-amber-500/10'
            : 'text-slate-500 bg-slate-200 dark:bg-slate-800 dark:text-slate-300'

  return (
    <div className="app-surface rounded-lg p-4 shadow-sm">
      <div className="mb-3 flex items-center gap-2">
        <span className={`rounded-full p-2 ${toneClass}`}>{icon}</span>
        <span className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">{label}</span>
      </div>
      <div className="text-xl font-semibold tracking-tight">{value}</div>
      <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">{meta}</div>
    </div>
  )
}

function BridgeStep({ label, value, accent }: { label: string; value: string; accent: string }) {
  return (
    <div className="rounded-lg border border-[var(--app-border-soft)] bg-[var(--app-surface-2)] p-3 dark:border-slate-700 dark:bg-slate-900/40">
      <div className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">{label}</div>
      <div className={`mt-1 text-lg font-semibold ${accent}`}>{value}</div>
    </div>
  )
}

function PayCycleCompositionPanel({
  composition,
  formatAmount,
}: {
  composition: {
    gross: number
    total: number
    slices: { key: string; label: string; amount: number; group: 'Net' | 'Taxes' | 'Deductions & Benefits'; color: string }[]
    groups: { label: string; items: { key: string; label: string; amount: number; group: 'Net' | 'Taxes' | 'Deductions & Benefits'; color: string }[] }[]
  } | null
  formatAmount: (value: number) => string
}) {
  return (
    <div className="rounded-lg border border-[var(--app-border-soft)] bg-[var(--app-surface-2)] p-4 dark:border-slate-700 dark:bg-slate-900/40 min-h-[380px]">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <h3 className="font-medium">Gross Composition</h3>
          <p className="text-xs text-slate-500 dark:text-slate-400">One gross-pay chart with net, tax, and deduction slices.</p>
        </div>
        <span className="text-xs font-medium text-slate-500 dark:text-slate-400">{formatAmount(composition?.gross || 0)} gross pay</span>
      </div>
      {composition && composition.slices.length > 0 ? (
        <div className="grid gap-6 lg:grid-cols-[1.1fr_0.9fr]">
          <div className="h-[300px]">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie
                  data={composition.slices}
                  dataKey="amount"
                  nameKey="label"
                  innerRadius={72}
                  outerRadius={118}
                  stroke="none"
                >
                  {composition.slices.map((entry, index) => (
                    <Cell key={`${entry.color}-${index}`} fill={entry.color} />
                  ))}
                </Pie>
                <Tooltip content={<PayrollPieTooltip privacyMode={false} formatAmount={formatAmount} />} />
              </PieChart>
            </ResponsiveContainer>
          </div>
          <div className="space-y-4">
            {composition.groups.map((group) => (
              <div key={group.label}>
                <div className="mb-2 flex items-center justify-between gap-3">
                  <h4 className="text-sm font-medium">{group.label}</h4>
                  <span className="text-xs text-slate-500 dark:text-slate-400">
                    {formatAmount(group.items.reduce((sum, item) => sum + item.amount, 0))}
                  </span>
                </div>
                <div className="space-y-2">
                  {group.items.map((item) => (
                    <div key={item.key} className="flex items-center justify-between gap-3 text-sm">
                      <div className="flex min-w-0 items-center gap-2">
                        <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ backgroundColor: item.color }} />
                        <span className="truncate">{item.label}</span>
                      </div>
                      <span className="font-medium">{formatAmount(item.amount)}</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
            <div className="border-t border-[var(--app-border-soft)] pt-3 text-xs text-slate-500 dark:border-slate-700 dark:text-slate-400">
              Full circle = {formatAmount(composition.gross)} gross pay
            </div>
          </div>
        </div>
      ) : (
        <div className="rounded-xl border border-dashed border-slate-300 p-4 text-sm text-slate-500 dark:border-slate-700 dark:text-slate-400">
          No taxes or deductions were parsed for this payslip.
        </div>
      )}
    </div>
  )
}

function Chip({ label, tone = 'slate' }: { label: string; tone?: 'blue' | 'slate' }) {
  return (
    <span
      className={`rounded-full px-2.5 py-1 ${
        tone === 'blue'
          ? 'bg-blue-500/10 text-blue-500'
          : 'bg-slate-200 text-slate-600 dark:bg-slate-800 dark:text-slate-300'
      }`}
    >
      {label}
    </span>
  )
}

function EmployerSummaryCard({
  item,
  formatAmount,
}: {
  item: { employer: string; rows: number; gross: number; net: number; taxes: number; deductions: number }
  formatAmount: (value: number) => string
}) {
  const chartData = [
    { label: 'Net', amount: item.net, color: '#22c55e' },
    { label: 'Taxes', amount: item.taxes, color: '#f43f5e' },
    { label: 'Deductions', amount: item.deductions, color: '#f59e0b' },
  ]
  return (
    <div className="rounded-lg border border-[var(--app-border-soft)] bg-[var(--app-surface-2)] p-4 dark:border-slate-700 dark:bg-slate-900/40">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <h3 className="font-medium">{item.employer}</h3>
          <p className="text-xs text-slate-500 dark:text-slate-400">{formatCount(item.rows)} payslips</p>
        </div>
        <div className="text-right">
          <div className="text-xs text-slate-500 dark:text-slate-400">Gross total</div>
          <div className="font-semibold text-blue-500">{formatAmount(item.gross)}</div>
        </div>
      </div>
      <div className="grid gap-4 lg:grid-cols-[0.9fr_1.1fr]">
        <div className="h-[180px]">
          <ResponsiveContainer width="100%" height="100%">
            <PieChart>
              <Pie data={chartData} dataKey="amount" nameKey="label" innerRadius={44} outerRadius={70} stroke="none">
                {chartData.map((entry) => (
                  <Cell key={entry.label} fill={entry.color} />
                ))}
              </Pie>
              <Tooltip content={<PayrollPieTooltip privacyMode={false} formatAmount={formatAmount} />} />
            </PieChart>
          </ResponsiveContainer>
        </div>
        <div className="space-y-2.5 text-sm">
          {chartData.map((entry) => (
            <div key={entry.label} className="flex items-center justify-between gap-3">
              <span className="inline-flex items-center gap-2">
                <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ backgroundColor: entry.color }} />
                {entry.label}
              </span>
              <span className="font-medium">{formatAmount(entry.amount)}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

function PayrollTrendChart({
  data,
  dark,
  privacyMode,
  displayCurrency,
}: {
  data: PayrollGraphPoint[]
  dark: boolean
  privacyMode: boolean
  displayCurrency: string
}) {
  const chartRef = useRef<HTMLDivElement>(null)
  const chartApiRef = useRef<IChartApi | null>(null)
  const [hovered, setHovered] = useState<PayrollGraphPoint | null>(null)
  const fmt = (n: number) => formatCurrency(n, privacyMode, { currency: displayCurrency })
  const bg = useMemo(() => getCssVar('--chart-bg'), [dark])
  const text = useMemo(() => getCssVar('--chart-text'), [dark])
  const grid = useMemo(() => getCssVar('--chart-grid'), [dark])

  const defaultPoint = data[data.length - 1] || null
  const point = hovered || defaultPoint

  useEffect(() => {
    if (!chartRef.current) return
    const chart = createChart(chartRef.current, {
      height: 300,
      layout: {
        attributionLogo: false,
        background: { type: ColorType.Solid, color: bg },
        textColor: text,
        fontFamily: 'ui-monospace, monospace',
      },
      grid: { vertLines: { color: grid }, horzLines: { color: grid } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, fixLeftEdge: true, fixRightEdge: true },
      crosshair: { vertLine: { labelVisible: false }, horzLine: { labelVisible: false } },
      localization: {
        locale: 'en-US',
        priceFormatter: (price: number) => formatCurrency(price, privacyMode, { currency: displayCurrency }),
      },
    })
    chartApiRef.current = chart

    const seriesDefs = [
      { key: 'gross', color: '#3b82f6', width: 2.5 },
      { key: 'net', color: '#22c55e', width: 2.5 },
      { key: 'taxes', color: '#fb7185', width: 2 },
      { key: 'benefits', color: '#fbbf24', width: 2 },
      { key: 'retirement', color: '#6b7280', width: 2 },
      { key: 'misc', color: getCssVar('--text-secondary'), width: 2 },
    ] as const

    const seriesList = seriesDefs.map((def) => {
      const series = chart.addSeries(LineSeries, {
        color: def.color,
        lineWidth: def.width,
        lastValueVisible: false,
        priceLineVisible: false,
        crosshairMarkerRadius: 4,
      })
      series.setData(
        data.map((item) => ({
          time: item.date as Time,
          value: item[def.key],
        })),
      )
      return { def, series }
    })

    chart.timeScale().fitContent()
    chart.subscribeCrosshairMove((param) => {
      if (!param.time) {
        setHovered(null)
        return
      }
      const match = data.find((item) => item.date === param.time)
      setHovered(match || null)
    })

    const ro = new ResizeObserver(() => {
      if (chartRef.current) chart.applyOptions({ width: chartRef.current.clientWidth })
    })
    ro.observe(chartRef.current)

    return () => {
      ro.disconnect()
      seriesList.forEach(() => {})
      chart.remove()
      chartApiRef.current = null
    }
  }, [bg, dark, data, displayCurrency, grid, privacyMode, text])

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center justify-between gap-3 font-mono text-sm">
        <div className="text-slate-500 dark:text-slate-400">{point?.label || '—'}</div>
        {point ? (
          <div className="flex flex-wrap items-center gap-4">
            <span className="text-blue-500">G {fmt(point.gross)}</span>
            <span className="text-emerald-600 dark:text-emerald-400">N {fmt(point.net)}</span>
            <span className="text-rose-500 dark:text-rose-300">T {fmt(point.taxes)}</span>
            <span className="text-yellow-300">B {fmt(point.benefits)}</span>
            <span className="text-gray-400">R {fmt(point.retirement)}</span>
            <span className="text-slate-200">M {fmt(point.misc)}</span>
          </div>
        ) : null}
      </div>
      <div ref={chartRef} />
    </div>
  )
}

function PayrollPieTooltip({
  active,
  payload,
  formatAmount,
}: {
  active?: boolean
  payload?: Array<{ name?: string; value?: number; payload?: { label?: string } }>
  formatAmount: (value: number) => string
  privacyMode?: boolean
}) {
  if (!active || !payload?.length) return null
  const item = payload[0]
  return (
    <div className="app-surface-strong rounded-lg px-3 py-2 text-sm shadow-lg dark:border-slate-700 dark:bg-slate-800/95">
      <div className="font-medium">{item.payload?.label || item.name}</div>
      <div className="mt-1">{formatAmount(item.value ?? 0)}</div>
    </div>
  )
}
