import { useQuery } from '@tanstack/react-query'
import { Plus } from 'lucide-react'
import { api, fetchJson } from './api'
import { PlaidLinkButton } from './PlaidLink'
import { useFilterStore } from './store'
import { formatCurrency } from './privacy'

const BANK_LOGOS: Record<string, string> = {
  'bank of america': 'https://www.bankofamerica.com/favicon.ico',
  'chase': 'https://www.chase.com/favicon.ico',
  'american express': 'https://www.americanexpress.com/favicon.ico',
  'discover': 'https://www.discover.com/favicon.ico',
  'apple': 'https://www.apple.com/favicon.ico',
}

function getBankLogo(name: string): string | null {
  const lower = name.toLowerCase()
  for (const [key, url] of Object.entries(BANK_LOGOS)) {
    if (lower.includes(key)) return url
  }
  return null
}

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
        const logo = getBankLogo(acc.source)
        const isPositive = acc.balance >= 0
        return (
          <div key={acc.source} className="flex items-center gap-1.5" title={acc.source}>
            <div className="w-6 h-6 rounded-full bg-white dark:bg-slate-700 flex items-center justify-center overflow-hidden border dark:border-slate-600">
              {logo ? <img src={logo} alt="" className="w-4 h-4 object-contain" /> : <span className="text-xs">🏦</span>}
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
