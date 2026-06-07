import { useQuery } from '@tanstack/react-query'
import { api } from './api'
import { useFilterStore } from './store'

const opts = { staleTime: 30_000 }

function accountsKey(accounts: Set<string> | null) {
  return accounts ? Array.from(accounts).sort().join(',') : 'all'
}

export function useDisplayCurrency(): string {
  return useFilterStore((s) => s.displayCurrency)
}

export function useSummary() {
  const { startDate, endDate, accounts, category, displayCurrency } = useFilterStore()
  const src = accountsKey(accounts)
  const cur = displayCurrency
  return useQuery({ queryKey: ['summary', startDate, endDate, src, category, cur], queryFn: () => api.getSummary(startDate, endDate, accounts, category, cur), ...opts })
}

export function useByCategory() {
  const { startDate, endDate, accounts, coreExpensesOnly, includeRentInCoreExpenses, displayCurrency } = useFilterStore()
  const src = accountsKey(accounts)
  const cur = displayCurrency
  return useQuery({
    queryKey: ['by-category', startDate, endDate, src, coreExpensesOnly, includeRentInCoreExpenses, cur],
    queryFn: () => api.getByCategory(startDate, endDate, accounts, coreExpensesOnly, includeRentInCoreExpenses, cur),
    ...opts,
  })
}

export function useByMerchant() {
  const { startDate, endDate, accounts, category, coreExpensesOnly, includeRentInCoreExpenses, displayCurrency } = useFilterStore()
  const src = accountsKey(accounts)
  const cur = displayCurrency
  return useQuery({
    queryKey: ['by-merchant', startDate, endDate, src, category, coreExpensesOnly, includeRentInCoreExpenses, cur],
    queryFn: () => api.getByMerchant(startDate, endDate, accounts, category, coreExpensesOnly, includeRentInCoreExpenses, cur),
    ...opts,
  })
}

export function useTrends() {
  const { startDate, endDate, granularity, displayCurrency } = useFilterStore()
  const cur = displayCurrency
  return useQuery({ queryKey: ['trends', startDate, endDate, granularity, cur], queryFn: () => api.getTrends(startDate, endDate, granularity, null, null, false, true, cur), ...opts })
}

export function useYearlyTrends() {
  const accounts = useFilterStore(s => s.accounts)
  const category = useFilterStore(s => s.category)
  const coreExpensesOnly = useFilterStore(s => s.coreExpensesOnly)
  const includeRentInCoreExpenses = useFilterStore(s => s.includeRentInCoreExpenses)
  const displayCurrency = useFilterStore(s => s.displayCurrency)
  const src = accountsKey(accounts)
  const cur = displayCurrency
  const today = new Date().toISOString().slice(0, 10)
  return useQuery({
    queryKey: ['all-trends', today, src, category, coreExpensesOnly, includeRentInCoreExpenses, cur],
    queryFn: () => api.getTrends('2022-01-01', today, 'daily', accounts, category, coreExpensesOnly, includeRentInCoreExpenses, cur),
    staleTime: 120_000,
  })
}

export function useSubscriptions() {
  const displayCurrency = useFilterStore(s => s.displayCurrency)
  return useQuery({ queryKey: ['subscriptions', displayCurrency], queryFn: () => api.getSubscriptions(displayCurrency), staleTime: 60_000 })
}

export function useTransactions() {
  const { startDate, endDate, accounts, category, search, coreExpensesOnly, includeRentInCoreExpenses, displayCurrency } = useFilterStore()
  const src = accountsKey(accounts)
  const cur = displayCurrency
  return useQuery({
    queryKey: ['transactions', startDate, endDate, src, category, search, coreExpensesOnly, includeRentInCoreExpenses, cur],
    queryFn: () => api.getTransactions({
      start_date: startDate, end_date: endDate,
      ...(accounts ? { source: Array.from(accounts).join(',') } : {}),
      ...(category ? { category } : {}),
      ...(search ? { search } : {}),
      ...(coreExpensesOnly ? { core_expenses_only: '1', include_rent: includeRentInCoreExpenses ? '1' : '0' } : {}),
      currency: cur,
    }),
    ...opts,
  })
}

export function useAccountBalances() {
  const displayCurrency = useFilterStore(s => s.displayCurrency)
  return useQuery({
    queryKey: ['account-balances', displayCurrency],
    queryFn: () => fetch(`/api/analytics/account-balances?currency=${encodeURIComponent(displayCurrency)}`).then(r => r.json()) as Promise<{ source: string; balance: number }[]>,
    staleTime: 60_000,
  })
}

export function useProjects(status?: string) {
  const displayCurrency = useFilterStore((s) => s.displayCurrency)
  return useQuery({ queryKey: ['projects', status, displayCurrency], queryFn: () => api.getProjects(displayCurrency, status), staleTime: 30_000 })
}

export function useProject(id: string | null) {
  const displayCurrency = useFilterStore((s) => s.displayCurrency)
  return useQuery({ queryKey: ['project', id, displayCurrency], queryFn: () => api.getProject(id!, displayCurrency), enabled: !!id, staleTime: 30_000 })
}
