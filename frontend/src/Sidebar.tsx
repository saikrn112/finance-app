import { RefreshCw, Settings, Moon, Sun, Eye, EyeOff, ChevronLeft, ChevronRight, FolderKanban, TrendingUp, Receipt, BookOpen, Upload, Landmark, CreditCard, PiggyBank, Tags, House } from 'lucide-react'
import type { SidebarAccount } from './api'
import { useFilterStore } from './store'
import { formatCurrency } from './privacy'
import { formatCompact } from './currency-config'

interface ExchangeRate {
  from_currency: string
  to: string
  rate: number
}

interface Props {
  accounts: SidebarAccount[]
  exchangeRates: ExchangeRate[]
  selectedAccounts: Set<string> | null
  onToggleAccount: (source: string) => void
  onResetAccounts: () => void
  dark: boolean
  onToggleDark: () => void
  onSync: () => void
  onOpenSettings: () => void
  onShowProjects: () => void
  onShowHome: () => void
  onShowInvestments: (source?: string) => void
  onShowRetirement: (source?: string) => void
  onShowPayslips: () => void
  onShowNetWorth: () => void
  onShowUncategorized: () => void
  onShowImports: () => void
  onOpenGettingStarted: () => void
  expanded: boolean
  onToggleExpanded: () => void
  syncing: boolean
}


const GROUP_META = {
  bank_account: { label: 'Bank Accounts', icon: Landmark },
  credit_card: { label: 'Credit Cards', icon: CreditCard },
  investment: { label: 'Investments', icon: TrendingUp },
  retirement: { label: 'Retirement 401k', icon: PiggyBank },
} as const

const GROUP_ORDER: Array<keyof typeof GROUP_META> = ['bank_account', 'credit_card', 'investment', 'retirement']

function connectionLight(state: SidebarAccount['connection_state']) {
  return state === 'plaid' ? 'bg-emerald-500' : 'bg-amber-400'
}



/* Sidebar accounts now return amounts converted to displayCurrency by the backend.
   All display uses displayCurrency — no per-account native currency. */

function accountAction(
  account: SidebarAccount,
  onToggleAccount: (source: string) => void,
  onShowInvestments: (source?: string) => void,
  onShowRetirement: (source?: string) => void,
) {
  if (account.group === 'investment') {
    return () => onShowInvestments(account.source)
  }
  if (account.group === 'retirement') return () => onShowRetirement(account.source_key || account.source)
  if (account.filter_source) return () => onToggleAccount(account.filter_source!)
  return undefined
}

export function Sidebar({
  accounts,
  exchangeRates,
  selectedAccounts,
  onToggleAccount,
  onResetAccounts,
  dark,
  onToggleDark,
  onSync,
  onOpenSettings,
  onShowProjects,
  onShowHome,
  onShowInvestments,
  onShowRetirement,
  onShowPayslips,
  onShowNetWorth,
  onShowUncategorized,
  onShowImports,
  onOpenGettingStarted,
  expanded,
  onToggleExpanded,
  syncing,
}: Props) {
  const privacyMode = useFilterStore((state) => state.privacyMode)
  const togglePrivacyMode = useFilterStore((state) => state.togglePrivacyMode)
  const displayCurrency = useFilterStore((state) => state.displayCurrency)
  const fmt = (n: number) => formatCurrency(n, privacyMode, { currency: displayCurrency })
  const isSelected = (source: string) => !selectedAccounts || selectedAccounts.has(source)
  const allSelected = !selectedAccounts
  const collapsedItemClass = expanded ? '' : 'justify-center px-0'
  const utilityItems = [
    { key: 'getting-started', label: 'Getting Started', icon: <BookOpen size={15} />, onClick: onOpenGettingStarted },
    { key: 'home', label: 'Home', icon: <House size={15} />, onClick: onShowHome },
    { key: 'projects', label: 'Projects', icon: <FolderKanban size={15} />, onClick: onShowProjects },
    { key: 'uncategorized', label: 'Uncategorized', icon: <Tags size={15} />, onClick: onShowUncategorized },
    { key: 'payroll', label: 'Payroll', icon: <Receipt size={15} />, onClick: onShowPayslips },
    { key: 'imports', label: 'Imports', icon: <Upload size={15} />, onClick: onShowImports, dataTour: 'tour-imports' },
    { key: 'theme', label: dark ? 'Light Mode' : 'Dark Mode', icon: dark ? <Sun size={15} /> : <Moon size={15} />, onClick: onToggleDark },
    { key: 'privacy', label: 'Privacy', icon: privacyMode ? <EyeOff size={15} /> : <Eye size={15} />, onClick: togglePrivacyMode, title: privacyMode ? 'Disable privacy mode' : 'Enable privacy mode' },
    { key: 'settings', label: 'Settings', icon: <Settings size={15} />, onClick: onOpenSettings, dataTour: 'tour-settings' },
  ]
  const utilityCols = 8
  const utilityRows = (() => {
    if (!expanded) return []
    const firstRowCount = utilityItems.length > utilityCols ? (utilityItems.length % utilityCols || utilityCols) : utilityItems.length
    const rows = [utilityItems.slice(0, firstRowCount)]
    for (let i = firstRowCount; i < utilityItems.length; i += utilityCols) {
      rows.push(utilityItems.slice(i, i + utilityCols))
    }
    return rows
  })()
  const grouped = GROUP_ORDER.map((group) => ({
    group,
    items: accounts.filter((account) => account.group === group),
  })).filter((section) => section.items.length > 0)
  const netWorth = accounts
    .reduce((sum, account) => sum + (account.balance || 0), 0)

  return (
    <div
      data-tour="sidebar"
      data-tour-expanded={expanded ? 'true' : undefined}
      className="fixed right-0 top-0 h-screen z-30 flex flex-col transition-all duration-200 border-l"
      style={{ background: 'var(--sidebar-bg)', borderColor: 'var(--sidebar-border)', width: expanded ? 260 : 52 }}
    >
      <div className={`px-2 ${expanded ? 'pt-3' : 'pt-2'}`}>
        <div className={`flex ${expanded ? 'justify-start' : 'justify-center'} mb-2`}>
          <button onClick={onToggleExpanded} className="p-0.5 rounded sidebar-hover">
            {expanded ? <ChevronRight size={16} /> : <ChevronLeft size={16} />}
          </button>
        </div>
        <div className={`flex items-center ${expanded ? 'gap-2 px-1 pb-3' : 'justify-center pb-2'}`}>
          {expanded && <span className="font-bold text-sm whitespace-nowrap flex-1">Financials</span>}
          <button
            data-tour="tour-sync"
            onClick={onSync}
            className={`rounded-lg border px-2 py-1 text-[11px] font-medium sidebar-hover ${expanded ? '' : 'flex w-full items-center justify-center px-1.5'}`}
            style={{ borderColor: 'var(--border-default)', background: 'var(--surface-secondary)' }}
            title="Sync Plaid snapshots"
          >
            <span className="inline-flex items-center gap-1.5">
              <RefreshCw size={12} className={syncing ? 'animate-spin' : ''} />
              {expanded ? 'Sync' : null}
            </span>
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-2 pb-2">
        {expanded && (
          <div className="mb-2 flex justify-between rounded px-2 py-1 text-[10px]" style={{ color: 'var(--text-muted)' }}>
            <span>Forex</span>
            <span>1 USD = {exchangeRates.length > 0 ? `${exchangeRates[0].rate.toFixed(2)} ${exchangeRates[0].from_currency}` : '1.00 USD'}</span>
          </div>
        )}
        <button
          onClick={onResetAccounts}
          className={`flex items-center gap-2 w-full rounded px-2 py-1.5 text-xs transition-colors ${collapsedItemClass} ${allSelected ? 'app-selected' : 'sidebar-hover'}`}
        >
          <span className="w-2 h-2 rounded-full bg-slate-400" />
          {expanded && <span className="truncate">All Transaction Accounts</span>}
        </button>

        {grouped.map(({ group, items }) => {
          const meta = GROUP_META[group]
          const GroupIcon = meta.icon
          const tourAttr = (group === 'bank_account' || group === 'credit_card')
            ? 'tour-accounts-section'
            : (group === 'investment' || group === 'retirement')
              ? 'tour-investments-section'
              : undefined
          return (
            <div key={group} className="mt-3" data-tour={tourAttr}>
              {expanded ? (
                <div className="mb-1 flex items-center gap-2 px-1 text-[10px] uppercase tracking-wider text-slate-500">
                  <GroupIcon size={12} />
                  <span>{meta.label}</span>
                </div>
              ) : null}
              <div className="space-y-1">
                {items.map((account) => {
                  const action = accountAction(account, onToggleAccount, onShowInvestments, onShowRetirement)
                  const active = account.filter_source ? isSelected(account.filter_source) && !allSelected : false
                  const discrepancy =
                    account.ledger_balance !== null && account.snapshot_balance !== null
                      ? Math.abs(account.ledger_balance - account.snapshot_balance)
                      : null
                  const primaryBalance = account.ledger_balance ?? account.balance
                  const secondaryBalance =
                    account.ledger_balance !== null && account.snapshot_balance !== null
                      ? account.snapshot_balance
                      : null
                  return (
                    <button
                      key={`${group}:${account.source}`}
                      onClick={action}
                      className={`flex items-center gap-2 w-full rounded px-2 py-1.5 text-xs transition-colors ${
                        active ? 'app-selected' : 'sidebar-hover'
                      } ${collapsedItemClass} ${account.filter_source && !isSelected(account.filter_source) ? 'opacity-40' : ''}`}
                    >
                      <span className={`w-2 h-2 rounded-full ${connectionLight(account.connection_state)}`} />
                      {account.icon_url ? (
                        <img src={account.icon_url} alt="" className="w-4 h-4 rounded-full shrink-0 object-contain" />
                      ) : (
                        <span className="w-4 h-4 rounded-full shrink-0 flex items-center justify-center text-[8px] font-bold" style={{ background: 'var(--surface-secondary)', color: 'var(--text-muted)' }}>
                          {account.source.charAt(0)}
                        </span>
                      )}
                      {expanded ? (
                        <span className="min-w-0 flex-1 text-left">
                          <span className="block truncate">{account.source}</span>
                          {secondaryBalance !== null ? (
                            <span className="block truncate text-[10px]" style={{ color: 'var(--text-muted)' }}>
                              Acct {fmt(secondaryBalance)}
                            </span>
                          ) : null}
                        </span>
                      ) : null}
                      {expanded && discrepancy !== null && discrepancy > 1 ? (
                        <span className="text-[10px] text-amber-400" title={`Local ledger is ${fmt(Math.abs(account.ledger_balance! - account.snapshot_balance!))} ${account.ledger_balance! > account.snapshot_balance! ? 'higher' : 'lower'} than last ${account.connection_state === 'plaid' ? 'Plaid sync' : 'statement balance'}`}>⚠</span>
                      ) : null}
                      {expanded && primaryBalance !== null ? (
                        <span className="text-[10px] font-medium" style={{ color: primaryBalance >= 0 ? 'var(--color-positive)' : 'var(--color-negative)' }}>
                          {fmt(primaryBalance)}
                        </span>
                      ) : null}
                      {expanded && primaryBalance === null ? <span className="text-[10px] text-slate-500">Sync</span> : null}
                    </button>
                  )
                })}
              </div>
            </div>
          )
        })}
      </div>

      <div className="border-t border-[var(--border-soft)] px-2 py-2">
        <button onClick={onShowNetWorth} className={`flex items-center gap-2 w-full rounded px-2 py-1.5 text-xs transition-colors sidebar-hover ${collapsedItemClass}`}>
          {expanded ? (
            <>
              <span className="font-semibold flex-1">Net Worth</span>
              <span className="font-bold" style={{ color: netWorth >= 0 ? 'var(--color-positive)' : 'var(--color-negative)' }}>{fmt(netWorth)}</span>
            </>
          ) : (
            <span className="font-bold text-[10px]" style={{ color: netWorth >= 0 ? 'var(--color-positive)' : 'var(--color-negative)' }}>
              {privacyMode ? '$$' : formatCompact(netWorth, displayCurrency)}
            </span>
          )}
        </button>
      </div>

      <div className="border-t border-[var(--border-soft)] px-2 py-2">
        {expanded ? (
          <div className="space-y-0.5">
            {utilityRows.map((row, rowIndex) => (
              <div key={`utility-row-${rowIndex}`} className={`flex gap-0.5 ${rowIndex === 0 ? 'justify-end' : 'justify-start'}`}>
                {row.map((item) => (
                  <button
                    key={item.key}
                    onClick={item.onClick}
                    title={item.title || item.label}
                    aria-label={item.label}
                    data-tour={item.dataTour}
                    className="flex h-7 w-7 items-center justify-center rounded transition-colors sidebar-hover"
                    style={{ color: 'var(--sidebar-text-muted)' }}
                  >
                    {item.icon}
                  </button>
                ))}
              </div>
            ))}
          </div>
        ) : (
          <div className="space-y-1">
            {utilityItems.map((item) => (
              <button
                key={item.key}
                onClick={item.onClick}
                title={item.title || item.label}
                aria-label={item.label}
                data-tour={item.dataTour}
                className={`flex items-center gap-2 w-full rounded px-2 py-1.5 text-xs sidebar-hover transition-colors ${collapsedItemClass}`}
              >
                {item.icon}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
