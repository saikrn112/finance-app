import { create } from 'zustand'
import { startOfWeek, endOfWeek, startOfMonth, endOfMonth, subWeeks, addWeeks, subMonths, addMonths, subYears, addYears, format, startOfDay } from 'date-fns'
import { getDisplayCurrency, setDisplayCurrency as persistDisplayCurrency } from './currency'

export type Granularity = 'weekly' | 'monthly' | '6monthly' | 'yearly' | 'yearplus' | 'alltime' | null

function toStr(d: Date) { return format(d, 'yyyy-MM-dd') }
function todayDate() { return startOfDay(new Date()) }

function computeRange(g: Granularity, anchor: Date = todayDate()): { start: string; end: string } {
  const today = todayDate()
  let s: Date, e: Date
  if (g === 'weekly') { s = startOfWeek(anchor, { weekStartsOn: 1 }); e = endOfWeek(anchor, { weekStartsOn: 1 }) }
  else if (g === 'monthly') { s = startOfMonth(anchor); e = endOfMonth(anchor) }
  else if (g === '6monthly') { s = subMonths(startOfMonth(anchor), 5); e = endOfMonth(anchor) }
  else if (g === 'yearly') { s = new Date(anchor.getFullYear(), 0, 1); e = new Date(anchor.getFullYear(), 11, 31) }
  else if (g === 'yearplus') { s = subYears(anchor, 1); e = anchor }
  else if (g === 'alltime') { s = new Date(2022, 5, 1); e = today }
  else { s = subYears(anchor, 1); e = anchor }
  return { start: toStr(s), end: toStr(e) }
}

interface FilterState {
  granularity: Granularity
  anchor: Date
  startDate: string
  endDate: string
  privacyMode: boolean
  coreExpensesOnly: boolean
  includeRentInCoreExpenses: boolean
  displayCurrency: string
  dark: boolean
  setDark: (next: boolean) => void
  category: string | null
  search: string
  accounts: Set<string> | null
  setGranularity: (g: Granularity) => void
  stepBack: () => void
  stepForward: () => void
  setCustomRange: (start: string, end: string) => void
  setCategory: (cat: string | null) => void
  setSearch: (search: string) => void
  setAccounts: (accounts: Set<string> | null) => void
  toggleAccount: (source: string) => void
  togglePrivacyMode: () => void
  toggleDark: () => void
  setCoreExpensesOnly: (enabled: boolean) => void
  setIncludeRentInCoreExpenses: (enabled: boolean) => void
  setDisplayCurrency: (currency: string) => void
  clearFilters: () => void
  canStepForward: () => boolean
}

export const useFilterStore = create<FilterState>((set, get) => {
  const init = computeRange('monthly')
  return {
    granularity: 'monthly' as Granularity,
    anchor: todayDate(),
    startDate: init.start,
    endDate: init.end,
    privacyMode: false,
    coreExpensesOnly: false,
    includeRentInCoreExpenses: true,
    dark: true,
    displayCurrency: getDisplayCurrency(),
    category: null,
    search: '',
    accounts: null,
    setGranularity: (granularity) => {
      const anchor = todayDate()
      const { start, end } = computeRange(granularity, anchor)
      set({ granularity, anchor, startDate: start, endDate: end })
    },
    stepBack: () => {
      let { granularity, anchor, startDate } = get()
      if (!granularity) { granularity = 'monthly'; anchor = new Date(startDate + 'T00:00:00') }
      if (granularity === 'alltime') return
      let next: Date
      if (granularity === 'weekly') next = subWeeks(anchor, 1)
      else if (granularity === 'monthly') next = subMonths(anchor, 1)
      else if (granularity === '6monthly') next = subMonths(anchor, 6)
      else next = subYears(anchor, 1)
      if (next < new Date('2022-01-01')) return
      const { start, end } = computeRange(granularity, next)
      set({ granularity, anchor: next, startDate: start, endDate: end })
    },
    stepForward: () => {
      let { granularity, anchor, startDate } = get()
      if (!granularity) { granularity = 'monthly'; anchor = new Date(startDate + 'T00:00:00') }
      if (granularity === 'alltime') return
      if (!get().canStepForward()) return
      let next: Date
      if (granularity === 'weekly') next = addWeeks(anchor, 1)
      else if (granularity === 'monthly') next = addMonths(anchor, 1)
      else if (granularity === '6monthly') next = addMonths(anchor, 6)
      else next = addYears(anchor, 1)
      const { start, end } = computeRange(granularity, next)
      set({ granularity, anchor: next, startDate: start, endDate: end })
    },
    setCustomRange: (startDate, endDate) => set({ granularity: null, startDate, endDate }),
    setCategory: (category) => set({ category }),
    setSearch: (search) => set({ search }),
    setAccounts: (accounts) => set({ accounts }),
    setCoreExpensesOnly: (coreExpensesOnly) => set({ coreExpensesOnly }),
    setIncludeRentInCoreExpenses: (includeRentInCoreExpenses) => set({ includeRentInCoreExpenses }),
    setDisplayCurrency: (displayCurrency) => { persistDisplayCurrency(displayCurrency); set({ displayCurrency }) },
    toggleAccount: (source) => {
      const { accounts } = get()
      if (!accounts) {
        set({ accounts: new Set([source]) })
      } else {
        const next = new Set(accounts)
        if (next.has(source)) next.delete(source)
        else next.add(source)
        set({ accounts: next.size === 0 ? null : next })
        }
    },
    togglePrivacyMode: () => set((state) => ({ privacyMode: !state.privacyMode })),
    toggleDark: () => set((state) => {
      const next = !state.dark
      document.documentElement.classList.toggle('dark', next)
      return { dark: next }
    }),
    setDark: (next: boolean) => set(() => {
      // Separate from toggleDark because a host shell needs to *set* the appearance to
      // match the system, not flip whatever it currently is -- flipping from an unknown
      // state gets it right only half the time.
      document.documentElement.classList.toggle('dark', next)
      return { dark: next }
    }),
    clearFilters: () => set({ category: null, search: '', accounts: null, coreExpensesOnly: false }),
    canStepForward: () => {
      const { endDate, granularity } = get()
      if (granularity === 'alltime') return false
      return endDate < toStr(todayDate())
    },
  }
})
