import { useRef, useCallback, useMemo } from 'react'
import { useFilterStore } from './store'

interface Props {
  yearlyTrends: Record<string, any>[]
}

export function ChartNavigator({ yearlyTrends }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const dragging = useRef<'move' | 'left' | 'right' | null>(null)
  const dragStart = useRef({ x: 0, startIdx: 0, endIdx: 0 })

  const { startDate, endDate } = useFilterStore()

  // Aggregate raw daily data into monthly buckets
  const data = useMemo(() => {
    const map = new Map<string, { income: number; spending: number }>()
    for (const t of yearlyTrends) {
      const month = (t.period as string).slice(0, 7)
      const entry = map.get(month) || { income: 0, spending: 0 }
      for (const [k, v] of Object.entries(t)) {
        if (k === 'period' || k === 'total') continue
        if (k === 'Income' || k.startsWith('Income/')) entry.income += v as number
        else entry.spending += Math.abs(v as number)
      }
      map.set(month, entry)
    }
    return Array.from(map.entries()).map(([period, d]) => ({ period, ...d })).sort((a, b) => a.period.localeCompare(b.period))
  }, [yearlyTrends])

  const periods = useMemo(() => data.map(t => t.period), [data])
  const maxVal = useMemo(() => Math.max(...data.map(t => Math.max(t.income, t.spending)), 1), [data])

  const focusStart = useMemo(() => {
    const m = startDate.slice(0, 7)
    const idx = periods.indexOf(m)
    return idx >= 0 ? idx : 0
  }, [startDate, periods])

  const focusEnd = useMemo(() => {
    const m = endDate.slice(0, 7)
    const idx = periods.indexOf(m)
    return idx >= 0 ? idx : periods.length - 1
  }, [endDate, periods])

  const setRange = useCallback((si: number, ei: number) => {
    if (!periods.length) return
    si = Math.max(0, Math.min(si, periods.length - 1))
    ei = Math.max(si, Math.min(ei, periods.length - 1))
    const s = periods[si] + '-01'
    const eDate = new Date(periods[ei] + '-01T00:00:00')
    const last = new Date(eDate.getFullYear(), eDate.getMonth() + 1, 0)
    const e = `${last.getFullYear()}-${String(last.getMonth() + 1).padStart(2, '0')}-${String(last.getDate()).padStart(2, '0')}`
    if (si === ei) {
      useFilterStore.setState({ startDate: s, endDate: e, granularity: 'monthly', anchor: new Date(s + 'T00:00:00') })
    } else {
      useFilterStore.setState({ startDate: s, endDate: e, granularity: null })
    }
  }, [periods])

  const onPointerDown = useCallback((e: React.PointerEvent, type: 'move' | 'left' | 'right') => {
    e.preventDefault()
    dragging.current = type
    dragStart.current = { x: e.clientX, startIdx: focusStart, endIdx: focusEnd }
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
  }, [focusStart, focusEnd])

  const onPointerMove = useCallback((e: React.PointerEvent) => {
    if (!dragging.current || !containerRef.current) return
    const rect = containerRef.current.getBoundingClientRect()
    const barW = rect.width / periods.length
    const dx = Math.round((e.clientX - dragStart.current.x) / barW)
    if (dx === 0) return
    const { startIdx, endIdx } = dragStart.current
    if (dragging.current === 'move') {
      const width = endIdx - startIdx
      let ns = Math.max(0, Math.min(startIdx + dx, periods.length - 1 - width))
      setRange(ns, ns + width)
    } else if (dragging.current === 'left') {
      setRange(Math.min(startIdx + dx, endIdx), endIdx)
    } else {
      setRange(startIdx, Math.max(endIdx + dx, startIdx))
    }
  }, [periods, setRange])

  const onPointerUp = useCallback(() => { dragging.current = null }, [])

  const onClick = useCallback((e: React.MouseEvent) => {
    if (!containerRef.current || !periods.length) return
    const rect = containerRef.current.getBoundingClientRect()
    const idx = Math.floor(((e.clientX - rect.left) / rect.width) * periods.length)
    const width = focusEnd - focusStart
    const half = Math.floor(width / 2)
    let ns = Math.max(0, Math.min(idx - half, periods.length - 1 - width))
    setRange(ns, ns + width)
  }, [periods, focusStart, focusEnd, setRange])

  if (!periods.length) return null

  const barW = 100 / periods.length
  const focusL = (focusStart / periods.length) * 100
  const focusW = ((focusEnd - focusStart + 1) / periods.length) * 100

  return (
    <div className="mt-1 select-none">
      <div ref={containerRef} className="relative h-10 cursor-pointer" onClick={onClick}
        onPointerMove={onPointerMove} onPointerUp={onPointerUp}>
        {/* Mini bars */}
        <div className="absolute inset-0 flex items-end">
          {data.map(t => (
            <div key={t.period} className="flex flex-col items-center justify-end h-full" style={{ width: `${barW}%` }}>
              {t.income > 0 && <div className="w-2/3 rounded-sm" style={{ height: `${(t.income / maxVal) * 45}%`, backgroundColor: '#34d399', opacity: 0.3 }} />}
              {t.spending > 0 && <div className="w-2/3 rounded-sm" style={{ height: `${(t.spending / maxVal) * 45}%`, backgroundColor: '#fb7185', opacity: 0.3 }} />}
            </div>
          ))}
        </div>
        {/* Dimming overlays */}
        <div className="absolute inset-y-0 left-0 bg-black/30 dark:bg-black/40 pointer-events-none" style={{ width: `${focusL}%` }} />
        <div className="absolute inset-y-0 right-0 bg-black/30 dark:bg-black/40 pointer-events-none" style={{ width: `${100 - focusL - focusW}%` }} />
        {/* Focus window */}
        <div className="absolute inset-y-0 border border-slate-400 dark:border-slate-500 cursor-grab active:cursor-grabbing"
          style={{ left: `${focusL}%`, width: `${focusW}%` }}
          onPointerDown={e => { e.stopPropagation(); onPointerDown(e, 'move') }}>
          <div className="absolute left-0 inset-y-0 w-1.5 bg-slate-400 dark:bg-slate-500 cursor-ew-resize hover:bg-slate-300"
            onPointerDown={e => { e.stopPropagation(); onPointerDown(e, 'left') }} />
          <div className="absolute right-0 inset-y-0 w-1.5 bg-slate-400 dark:bg-slate-500 cursor-ew-resize hover:bg-slate-300"
            onPointerDown={e => { e.stopPropagation(); onPointerDown(e, 'right') }} />
        </div>
      </div>
      {/* Month labels */}
      <div className="flex text-[9px] text-slate-500">
        {periods.map(p => (
          <div key={p} className="text-center" style={{ width: `${barW}%` }}>
            {['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][parseInt(p.split('-')[1]) - 1]}
          </div>
        ))}
      </div>
    </div>
  )
}
