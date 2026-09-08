import { useEffect, useRef, useMemo, useState } from 'react'
import { createChart, HistogramSeries, ColorType } from 'lightweight-charts'
import type { Time, IChartApi } from 'lightweight-charts'
import { useFilterStore } from './store'
import { formatCurrency } from './privacy'
import { getCurrencySymbol } from './currency'
import { getCssVar } from './themeUtils'

interface RawTrend { period: string; total: number; [key: string]: any }

interface Props {
  data: RawTrend[]
  excludedCategories?: Set<string>
  dark: boolean
  filterButton?: React.ReactNode
}

function transformData(data: RawTrend[], excluded: Set<string>) {
  const spending: { time: Time; value: number; color: string }[] = []
  for (const t of data) {
    let spend = 0
    for (const [k, v] of Object.entries(t)) {
      if (k === 'period' || k === 'total' || excluded.has(k) || k === 'Income') continue
      spend += v as number
    }
    spending.push({ time: t.period as Time, value: spend, color: getCssVar('--chart-spending') })
  }
  return { spending }
}

const CHART_HEIGHT_KEY = 'finance-flow-chart-height'

/**
 * The flow chart's default height.
 *
 * Was a flat 500px, which on a laptop window pushed everything below it off-screen and
 * spent most of that space on empty plot area. Proportional to the viewport with a clamp
 * instead: tall enough to read, never more than about a third of the window.
 */
function defaultChartHeight() {
  const viewport = typeof window === 'undefined' ? 900 : window.innerHeight
  return Math.round(Math.min(420, Math.max(240, viewport * 0.34)))
}

/** The height the user last dragged to, if any. */
function storedChartHeight() {
  try {
    const raw = window.localStorage.getItem(CHART_HEIGHT_KEY)
    const value = raw ? Number(raw) : NaN
    // Bounded by the same limits the drag handle enforces, so a hand-edited or stale
    // value cannot produce a chart that is unusable or invisible.
    if (Number.isFinite(value) && value >= 150 && value <= 600) return value
  } catch {
    // Storage can be unavailable; the default is fine.
  }
  return defaultChartHeight()
}

export function FlowChart({ data, excludedCategories = new Set(), dark, filterButton }: Props) {
  const privacyMode = useFilterStore((state) => state.privacyMode)
  const displayCurrency = useFilterStore((state) => state.displayCurrency)
  const fmt = (n: number) => formatCurrency(n, privacyMode, { absolute: true, currency: displayCurrency })
  const chartRef = useRef<HTMLDivElement>(null)
  const minimapRef = useRef<HTMLDivElement>(null)
  const minimapWrapRef = useRef<HTMLDivElement>(null)
  const chartApiRef = useRef<IChartApi | null>(null)
  const spendSeriesRef = useRef<any>(null)
  const miniSpendRef = useRef<any>(null)
  const skipSync = useRef(false)
  const hoverSnapshotRef = useRef<string | null>(null)
  const [hoverData, setHoverData] = useState<{ period: string; income: number; spending: number; net: number } | null>(null)
  const [chartHeight, setChartHeight] = useState(storedChartHeight)
  // Mirrors chartHeight so the pointerup handler can read the value the drag ended on.
  // The handler's closure captured the height at pointerdown, so reading `chartHeight`
  // there would persist the height the drag *started* from.
  const chartHeightRef = useRef(chartHeight)
  useEffect(() => { chartHeightRef.current = chartHeight }, [chartHeight])
  const incomeByDay = useRef<Map<string, number>>(new Map())

  const { spending: spendingData, incomeBarData } = useMemo(() => {
    const result = transformData(data, excludedCategories)
    const map = new Map<string, number>()
    for (const t of data) {
      let inc = 0
      for (const [k, v] of Object.entries(t)) {
        if (k === 'Income') inc += v as number
      }
      if (inc > 0) map.set(t.period, inc)
    }
    incomeByDay.current = map
    const incBars = result.spending.map(d => ({
      time: d.time, value: map.get(d.time as string) || 0, color: getCssVar('--chart-income'),
    }))
    return { ...result, incomeBarData: incBars }
  }, [data, excludedCategories, dark])

  const defaultHover = useMemo(() => {
    if (!spendingData.length) return null
    const s = spendingData[0]
    const inc = incomeByDay.current.get(s.time as string) || 0
    return { period: s.time as string, income: inc, spending: s.value, net: inc - s.value }
  }, [spendingData])

  const { bg, text, grid: gridLine } = useMemo(() => ({
    bg: getCssVar('--chart-bg'),
    text: getCssVar('--chart-text'),
    grid: getCssVar('--chart-grid'),
  }), [dark])
  const spendingDataRef = useRef(spendingData)
  spendingDataRef.current = spendingData

  const [localRange, setLocalRange] = useState<{ from: string; to: string } | null>(null)
  const bracketRef = useRef<HTMLDivElement>(null)
  const dimLeftRef = useRef<HTMLDivElement>(null)
  const dimRightRef = useRef<HTMLDivElement>(null)

  const setHoveredPoint = (next: { period: string; income: number; spending: number; net: number } | null) => {
    const snapshot = next ? `${next.period}|${next.income}|${next.spending}|${next.net}` : null
    if (hoverSnapshotRef.current === snapshot) return
    hoverSnapshotRef.current = snapshot
    setHoverData(next)
  }

  const zoomVisibleRange = (deltaY: number) => {
    const chart = chartApiRef.current
    const sd = spendingDataRef.current
    if (!chart || !sd.length) return
    const ts = chart.timeScale()
    const lr = ts.getVisibleLogicalRange()
    if (!lr) return

    const total = sd.length
    const width = lr.to - lr.from
    const center = (lr.from + lr.to) / 2
    const factor = deltaY > 0 ? 1.05 : 0.95
    const newWidth = Math.max(3, Math.min(total, width * factor))
    const newFrom = Math.max(0, center - newWidth / 2)
    const newTo = Math.min(total, center + newWidth / 2)

    skipSync.current = true
    ts.setVisibleLogicalRange({ from: newFrom, to: newTo })
    const range = ts.getVisibleRange()
    if (range) {
      useFilterStore.setState({ startDate: range.from as string, endDate: range.to as string, granularity: null })
    }
    setTimeout(() => { skipSync.current = false }, 50)
  }

  // Create charts
  useEffect(() => {
    if (!chartRef.current || !minimapRef.current) return

    const chart = createChart(chartRef.current, {
      height: storedChartHeight(),
      layout: { attributionLogo: false, background: { type: ColorType.Solid, color: bg }, textColor: text, fontFamily: 'ui-monospace, monospace' },
      localization: {
        locale: 'en-US',
        priceFormatter: (price: number) => {
          const symbol = getCurrencySymbol(useFilterStore.getState().displayCurrency)
          return privacyMode
            ? `${symbol}xxx`
            : `${symbol}${Math.abs(price).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
        },
      },
      grid: { vertLines: { color: gridLine }, horzLines: { color: gridLine } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, fixLeftEdge: true, fixRightEdge: true },
      crosshair: { vertLine: { labelVisible: false }, horzLine: { visible: false, labelVisible: false } },
      handleScale: { mouseWheel: false, pinch: true, axisPressedMouseMove: true },
      handleScroll: { mouseWheel: false, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
    })
    const spend = chart.addSeries(HistogramSeries, { lastValueVisible: false, baseLineVisible: false }, 0)
    spend.setData(spendingData)
    const incSeries = chart.addSeries(HistogramSeries, { lastValueVisible: false, baseLineVisible: false }, 1)
    incSeries.setData(incomeBarData)
    try { chart.panes()[1]?.setHeight(100) } catch {}
    chartApiRef.current = chart
    spendSeriesRef.current = spend

    // Minimap — no interaction, no axes
    const mini = createChart(minimapRef.current, {
      height: 40,
      layout: { attributionLogo: false, background: { type: ColorType.Solid, color: bg }, textColor: text },
      localization: { locale: 'en-US' },
      grid: { vertLines: { visible: false }, horzLines: { visible: false } },
      rightPriceScale: { visible: false },
      timeScale: { visible: false },
      handleScroll: false,
      handleScale: false,
      crosshair: { vertLine: { visible: false }, horzLine: { visible: false } },
    })
    const miniSpend = mini.addSeries(HistogramSeries, { lastValueVisible: false })
    miniSpend.setData(spendingData.map(d => ({ ...d, color: getCssVar('--chart-spending') })))
    miniSpendRef.current = miniSpend
    mini.timeScale().fitContent()

    // Hover
    chart.subscribeCrosshairMove((param) => {
      if (!param.time || !param.seriesData) {
        setHoveredPoint(null)
        return
      }
      const spendVal = param.seriesData.get(spend) as any
      const s = spendVal?.value || 0
      const inc = incomeByDay.current.get(param.time as string) || 0
      setHoveredPoint({ period: param.time as string, income: inc, spending: s, net: inc - s })
    })

    // Sync chart scroll → local bracket (instant) + store (debounced)
    let debounce: ReturnType<typeof setTimeout> | null = null
    chart.timeScale().subscribeVisibleLogicalRangeChange((lr) => {
      if (skipSync.current || !lr) return
      // Clamp to data bounds
      const total = spendingData.length
      if (lr.from < 0 || lr.to > total) {
        const width = lr.to - lr.from
        let from = Math.max(0, lr.from)
        let to = Math.min(total, lr.to)
        if (to - from < width) {
          if (from === 0) to = Math.min(total, width)
          else from = Math.max(0, to - width)
        }
        skipSync.current = true
        chart.timeScale().setVisibleLogicalRange({ from, to })
        setTimeout(() => { skipSync.current = false }, 50)
        return
      }
      const range = chart.timeScale().getVisibleRange()
      if (range) {
        if (debounce) clearTimeout(debounce)
        debounce = setTimeout(() => {
          if (!useFilterStore.getState().granularity) {
            useFilterStore.setState({ startDate: range.from as string, endDate: range.to as string })
          }
        }, 100)
      }
    })

    // Set initial visible range
    const { startDate, endDate } = useFilterStore.getState()
    try { chart.timeScale().setVisibleRange({ from: startDate as Time, to: endDate as Time }) } catch {}

    // Ctrl/Cmd + scroll to zoom (prevent accidental zoom while scrolling page)
    const chartEl = chartRef.current
    const wheelHandler = (e: WheelEvent) => {
      if (!e.ctrlKey && !e.metaKey) return
      e.preventDefault()
      zoomVisibleRange(e.deltaY)
    }
    chartEl.addEventListener('wheel', wheelHandler, { passive: false })

    // Resize observer
    const ro = new ResizeObserver(() => {
      if (chartRef.current) chart.applyOptions({ width: chartRef.current.clientWidth })
      if (minimapRef.current) mini.applyOptions({ width: minimapRef.current.clientWidth })
    })
    ro.observe(chartRef.current)
    ro.observe(minimapRef.current)

    return () => { ro.disconnect(); chartEl.removeEventListener('wheel', wheelHandler); chart.remove(); mini.remove() }
  }, [dark, privacyMode, displayCurrency])

  // Sync store → chart
  const { startDate, endDate } = useFilterStore()

  const hasDataInRange = useMemo(() => {
    if (!spendingData.length) return false
    const first = spendingData[0].time as string
    const last = spendingData[spendingData.length - 1].time as string
    return startDate <= last && endDate >= first
  }, [spendingData, startDate, endDate])

  // Fill every day in range so chart shows full date range
  const paddedData = useMemo(() => {
    if (!startDate || !endDate) return spendingData
    const map = new Map(spendingData.map(d => [d.time as string, d]))
    const result: { time: Time; value: number; color: string }[] = []
    const d = new Date(startDate + 'T00:00:00')
    const end = new Date(endDate + 'T00:00:00')
    while (d <= end) {
      const key = d.toISOString().slice(0, 10)
      result.push(map.get(key) || { time: key as Time, value: 0, color: 'transparent' })
      d.setDate(d.getDate() + 1)
    }
    return result
  }, [spendingData, startDate, endDate])

  // Update data and sync visible range together
  useEffect(() => {
    if (!spendSeriesRef.current || !chartApiRef.current) return
    spendSeriesRef.current.setData(paddedData)
    const miniData = spendingData.map(d => ({ ...d, color: getCssVar('--chart-spending') }))
    miniSpendRef.current?.setData(miniData)
    skipSync.current = true
    try { chartApiRef.current.timeScale().setVisibleRange({ from: startDate as Time, to: endDate as Time }) } catch {}
    setTimeout(() => { skipSync.current = false }, 1000)
  }, [paddedData, spendingData, startDate, endDate])

  // Resize chart height
  useEffect(() => {
    if (!chartApiRef.current) return
    chartApiRef.current.applyOptions({ height: chartHeight })
  }, [chartHeight])

  // Forward minimap wheel → main chart zoom (prevent page scroll)
  useEffect(() => {
    const el = minimapWrapRef.current
    if (!el) return
    const handler = (e: WheelEvent) => {
      if (!e.ctrlKey && !e.metaKey) return
      e.preventDefault()
      e.stopPropagation()
      zoomVisibleRange(e.deltaY)
    }
    el.addEventListener('wheel', handler, { passive: false })
    return () => el.removeEventListener('wheel', handler)
  }, [])

  // Bracket position — initial from store
  const bracket = useMemo(() => {
    if (!spendingData.length) return { left: 0, width: 100 }
    const first = spendingData[0].time as string
    const last = spendingData[spendingData.length - 1].time as string
    const totalMs = new Date(last).getTime() - new Date(first).getTime() || 1
    const clampedStart = startDate < first ? first : startDate
    const clampedEnd = endDate > last ? last : endDate
    const startMs = new Date(clampedStart).getTime() - new Date(first).getTime()
    const endMs = new Date(clampedEnd).getTime() - new Date(first).getTime()
    const left = Math.max(0, (startMs / totalMs) * 100)
    const width = Math.min(100 - left, ((endMs - startMs) / totalMs) * 100)
    return { left, width: Math.max(width, 2) }
  }, [spendingData, startDate, endDate])

  // Minimap drag handlers
  const minimapDrag = useRef<{ startX: number; startLeft: number; startWidth: number; type: 'move' | 'left' | 'right' } | null>(null)

  const onMinimapPointerDown = (e: React.PointerEvent, type: 'move' | 'left' | 'right') => {
    e.preventDefault()
    e.stopPropagation()
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
    minimapDrag.current = { startX: e.clientX, startLeft: bracket.left, startWidth: bracket.width, type }
  }

  const onMinimapPointerMove = (e: React.PointerEvent) => {
    const drag = minimapDrag.current
    const container = minimapWrapRef.current
    if (!drag || !container || !spendingData.length) return
    const rect = container.getBoundingClientRect()
    const dxPct = ((e.clientX - drag.startX) / rect.width) * 100
    const first = spendingData[0].time as string
    const last = spendingData[spendingData.length - 1].time as string
    const totalMs = new Date(last).getTime() - new Date(first).getTime() || 1

    let newLeft = drag.startLeft, newWidth = drag.startWidth
    if (drag.type === 'move') {
      newLeft = Math.max(0, Math.min(100 - drag.startWidth, drag.startLeft + dxPct))
    } else if (drag.type === 'left') {
      newLeft = Math.max(0, Math.min(drag.startLeft + drag.startWidth - 2, drag.startLeft + dxPct))
      newWidth = drag.startWidth - (newLeft - drag.startLeft)
    } else {
      newWidth = Math.max(2, Math.min(100 - drag.startLeft, drag.startWidth + dxPct))
    }
    const startMs = (newLeft / 100) * totalMs + new Date(first).getTime()
    const endMs = ((newLeft + newWidth) / 100) * totalMs + new Date(first).getTime()
    useFilterStore.setState({ startDate: new Date(startMs).toISOString().slice(0, 10), endDate: new Date(endMs).toISOString().slice(0, 10), granularity: null })
  }

  const onMinimapPointerUp = () => { minimapDrag.current = null }

  const onMinimapClick = (e: React.MouseEvent) => {
    if (!minimapWrapRef.current || !spendingData.length) return
    const rect = minimapWrapRef.current.getBoundingClientRect()
    const clickPct = ((e.clientX - rect.left) / rect.width) * 100
    const halfW = bracket.width / 2
    const newLeft = Math.max(0, Math.min(100 - bracket.width, clickPct - halfW))
    const first = spendingData[0].time as string
    const last = spendingData[spendingData.length - 1].time as string
    const totalMs = new Date(last).getTime() - new Date(first).getTime() || 1
    const startMs = (newLeft / 100) * totalMs + new Date(first).getTime()
    const endMs = ((newLeft + bracket.width) / 100) * totalMs + new Date(first).getTime()
    useFilterStore.setState({ startDate: new Date(startMs).toISOString().slice(0, 10), endDate: new Date(endMs).toISOString().slice(0, 10), granularity: null })
  }

  // Income ticks
  const incomeTicks = useMemo(() => {
    if (!spendingData.length) return []
    const entries = Array.from(incomeByDay.current.entries())
    if (!entries.length) return []
    const maxInc = Math.max(...entries.map(([, v]) => v), 1)
    const first = new Date(spendingData[0].time as string).getTime()
    const last = new Date(spendingData[spendingData.length - 1].time as string).getTime()
    const totalMs = last - first || 1
    return entries.map(([day, amt]) => ({
      day,
      x: ((new Date(day).getTime() - first) / totalMs) * 100,
      height: Math.max(10, (amt / maxInc) * 90),
    }))
  }, [spendingData])

  const d = hoverData || defaultHover

  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <div className="flex items-center gap-0">
          <h2 className="text-sm font-mono font-bold">I/O Flow</h2>
          {filterButton}
        </div>
        {d && (
          <div className="flex flex-nowrap gap-4 text-sm font-mono font-bold" style={{ color: 'var(--text-primary)' }}>
            <span className="min-w-[110px]">{d.period}</span>
            <span className="min-w-[175px] whitespace-nowrap">I: <span style={{ color: 'var(--color-positive)' }}>{fmt(d.income)}</span></span>
            <span className="min-w-[175px] whitespace-nowrap">O: <span style={{ color: 'var(--color-negative)' }}>{fmt(d.spending)}</span></span>
            <span className="min-w-[175px] whitespace-nowrap">N: <span style={{ color: d.net >= 0 ? 'var(--color-positive)' : 'var(--color-negative)' }}>{fmt(d.net)}</span></span>
          </div>
        )}
      </div>
      <div ref={chartRef} />
      {/* Minimap */}
      <div ref={minimapWrapRef} className="relative mt-1 select-none"
        onClick={onMinimapClick} onPointerMove={onMinimapPointerMove} onPointerUp={onMinimapPointerUp}>
        <div ref={minimapRef} />
        {/* Income ticks */}
        {incomeTicks.map(t => (
          <div key={t.day} className="absolute bottom-0 w-px z-[5] pointer-events-none" style={{ left: `${t.x}%`, height: `${t.height}%`, background: 'var(--color-positive)' }} />
        ))}
        {/* Dim overlays */}
        <div ref={dimLeftRef} className="absolute inset-y-0 left-0 z-10 pointer-events-none" style={{ width: `${bracket.left}%`, background: 'var(--overlay-dim)' }} />
        <div ref={dimRightRef} className="absolute inset-y-0 right-0 z-10 pointer-events-none" style={{ width: `${Math.max(0, 100 - bracket.left - bracket.width)}%`, background: 'var(--overlay-dim)' }} />
        {/* Bracket */}
        <div ref={bracketRef} className="absolute inset-y-0 z-20 cursor-grab active:cursor-grabbing"
          style={{ left: `${bracket.left}%`, width: `${bracket.width}%` }}
          onClick={e => e.stopPropagation()}
          onPointerDown={e => onMinimapPointerDown(e, 'move')}>
          <div className="absolute left-0 inset-y-0 w-1.5 cursor-ew-resize z-30" style={{ background: 'var(--border-strong)' }}
            onPointerDown={e => { e.stopPropagation(); onMinimapPointerDown(e, 'left') }} />
          <div className="absolute right-0 inset-y-0 w-1.5 cursor-ew-resize z-30" style={{ background: 'var(--border-strong)' }}
            onPointerDown={e => { e.stopPropagation(); onMinimapPointerDown(e, 'right') }} />
          <div className="absolute inset-0 border" style={{ borderColor: 'var(--border-soft)' }} />
        </div>
      </div>
      {/* Resize handle */}
      <div className="flex justify-center cursor-row-resize select-none py-1 group"
        onDoubleClick={() => setChartHeight(defaultChartHeight())}
        onPointerDown={(e) => {
          e.preventDefault()
          const startY = e.clientY
          const startH = chartHeight
          const onMove = (ev: PointerEvent) => setChartHeight(Math.max(150, Math.min(600, startH + ev.clientY - startY)))
          const onUp = () => {
            window.removeEventListener('pointermove', onMove)
            window.removeEventListener('pointerup', onUp)
            // Persist on release rather than on every move: one write per drag, and the
            // height survives a reload, which it previously did not.
            try { window.localStorage.setItem(CHART_HEIGHT_KEY, String(chartHeightRef.current)) } catch { /* storage unavailable */ }
          }
          window.addEventListener('pointermove', onMove)
          window.addEventListener('pointerup', onUp)
        }}>
        <div className="w-10 h-1 rounded-full transition-colors" style={{ background: 'var(--border-default)' }} />
      </div>
    </div>
  )
}
