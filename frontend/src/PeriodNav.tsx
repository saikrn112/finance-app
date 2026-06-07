import { useState, useRef, useEffect } from 'react'
import { ChevronLeft, ChevronRight } from 'lucide-react'
import { useFilterStore, type Granularity } from './store'
import { format, parse } from 'date-fns'

const CHIPS: { value: Granularity; label: string }[] = [
  { value: 'weekly', label: 'W' },
  { value: 'monthly', label: 'M' },
  { value: '6monthly', label: '6M' },
  { value: 'yearly', label: 'Y' },
  { value: 'yearplus', label: 'Y+' },
  { value: 'alltime', label: 'A' },
]

function formatPeriodLabel(g: Granularity, start: string, end: string): string {
  const s = parse(start, 'yyyy-MM-dd', new Date())
  const e = parse(end, 'yyyy-MM-dd', new Date())
  return `${format(s, 'MMM d, yyyy')} – ${format(e, 'MMM d, yyyy')}`
}

export function PeriodNav() {
  const { granularity, startDate, endDate, setGranularity, stepBack, stepForward, setCustomRange, canStepForward } = useFilterStore()
  const [popover, setPopover] = useState(false)
  const [customStart, setCustomStart] = useState(startDate)
  const [customEnd, setCustomEnd] = useState(endDate)
  const popRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!popover) return
    setCustomStart(startDate)
    setCustomEnd(endDate)
    const handler = (e: MouseEvent) => { if (popRef.current && !popRef.current.contains(e.target as Node)) setPopover(false) }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [popover, startDate, endDate])

  const forward = canStepForward()

  return (
    <div className="app-surface flex items-center gap-1 dark:bg-slate-800 rounded-lg px-2 py-1 border dark:border-slate-700">
      <div className="flex gap-0.5">
        {CHIPS.map(c => (
          <button key={c.value} onClick={() => setGranularity(c.value!)}
            className={`px-2 py-0.5 text-xs rounded-full font-medium transition-colors ${
              granularity === c.value
                ? 'bg-blue-500 text-white'
                : 'bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300 hover:bg-slate-200 dark:hover:bg-slate-600'
            }`}>
            {c.label}
          </button>
        ))}
      </div>

      {(
        <button onClick={stepBack} className="p-0.5 rounded hover:bg-slate-100 dark:hover:bg-slate-700">
          <ChevronLeft size={14} />
        </button>
      )}

      <div className="relative">
        <button onClick={() => setPopover(!popover)}
          className="px-1 py-0.5 text-xs font-medium hover:underline whitespace-nowrap w-52 text-center">
          {formatPeriodLabel(granularity, startDate, endDate)}
        </button>
        {popover && (
          <div ref={popRef} className="app-surface absolute top-8 left-0 z-50 dark:bg-slate-800 border dark:border-slate-700 rounded-lg shadow-lg p-3 flex flex-col gap-2">
            <label className="text-xs text-slate-500">From</label>
            <input type="date" value={customStart} onChange={e => setCustomStart(e.target.value)}
              className="px-2 py-1 text-xs rounded border dark:bg-slate-700 dark:border-slate-600" />
            <label className="text-xs text-slate-500">To</label>
            <input type="date" value={customEnd} onChange={e => setCustomEnd(e.target.value)}
              className="px-2 py-1 text-xs rounded border dark:bg-slate-700 dark:border-slate-600" />
            <div className="flex gap-2 mt-1">
              <button onClick={() => { setCustomRange(customStart, customEnd); setPopover(false) }}
                className="px-3 py-1 text-xs bg-blue-500 text-white rounded">Apply</button>
              <button onClick={() => setPopover(false)}
                className="px-3 py-1 text-xs bg-slate-200 dark:bg-slate-600 rounded">Cancel</button>
            </div>
          </div>
        )}
      </div>

      {(
        <button onClick={stepForward} disabled={!forward}
          className={`p-0.5 rounded hover:bg-slate-100 dark:hover:bg-slate-700 ${!forward ? 'opacity-30 cursor-not-allowed' : ''}`}>
          <ChevronRight size={14} />
        </button>
      )}
    </div>
  )
}
