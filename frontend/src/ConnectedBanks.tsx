import { useQuery } from '@tanstack/react-query'
import { Landmark } from 'lucide-react'
import { fetchJson } from './api'
import { PlaidLinkButton } from './PlaidLink'
import { useFilterStore } from './store'
import { formatCurrency } from './privacy'

export function ConnectedBanks({ onSuccess }: { onSuccess?: () => void }) {
  const privacyMode = useFilterStore((state) => state.privacyMode)
  const displayCurrency = useFilterStore((state) => state.displayCurrency)
  const fmt = (n: number) => formatCurrency(n, privacyMode, { currency: displayCurrency })
  const { data: balances } = useQuery({
    queryKey: ['account-balances', displayCurrency],
    queryFn: () => fetchJson<{ source: string; balance: number }[]>(`/analytics/account-balances?currency=${encodeURIComponent(displayCurrency)}`),
  })

  return (
    <div className="flex items-center gap-3">
      {balances?.map((acc) => {
        const isPositive = acc.balance >= 0
        return (
          <div key={acc.source} className="flex items-center gap-1.5" title={acc.source}>
            <div className="w-6 h-6 rounded-full bg-white dark:bg-slate-700 flex items-center justify-center overflow-hidden border dark:border-slate-600">
              <Landmark size={14} aria-hidden="true" />
            </div>
            <span className={`text-xs font-medium ${isPositive ? 'text-emerald-400' : 'text-rose-400'}`}>
              {fmt(acc.balance)}
            </span>
          </div>
        )
      })}
      <PlaidLinkButton onSuccess={onSuccess} compact />
    </div>
  )
}
