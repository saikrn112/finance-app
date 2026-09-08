import { useEffect, useState, useMemo } from 'react'
import { useQueryClient, useQuery } from '@tanstack/react-query'
import { useFilterStore } from './store'
import { useSummary, useByCategory, useByMerchant, useSubscriptions, useTransactions, useYearlyTrends } from './hooks'
import { Sidebar } from './Sidebar'
import { SettingsPanel } from './SettingsPanel'
import { RecurringPage } from './RecurringPage'
import { ProjectsPage } from './ProjectsPage'
import { InvestmentsPage } from './InvestmentsPage'
import { RetirementPage } from './RetirementPage'
import { PayslipPage } from './PayslipPage'
import { NetWorthPage } from './NetWorthPage'
import { ImportsPanel } from './ImportsPanel'
import { UncategorizedPage } from './UncategorizedPage'
import { Ledger } from './Ledger'
import { PeriodNav } from './PeriodNav'
import { FlowChart } from './FlowChart'
import { getCategoryColor, CATEGORY_COLORS, CATEGORY_ORDER } from './colors'
import { api } from './api'
import { GETTING_STARTED_DONE_KEY } from './AppTour'
import { AppTour } from './AppTour'
import { OnboardingGate } from './OnboardingGate'
import { formatCurrency } from './privacy'
import { SUPPORTED_CURRENCIES } from './currency'
import { installCommandBus, registerCommands } from './commandBus'
import { X } from 'lucide-react'

type DashboardView = 'dashboard' | 'recurring' | 'projects' | 'investments' | 'retirement' | 'payroll' | 'net-worth' | 'uncategorized'
const SIDEBAR_COLLAPSED_WIDTH = 52
const SIDEBAR_EXPANDED_WIDTH = 260

const VIEW_HASH: Record<DashboardView, string> = {
  dashboard: '',
  recurring: '#recurring',
  projects: '#projects',
  investments: '#investments',
  retirement: '#retirement',
  payroll: '#payroll',
  'net-worth': '#net-worth',
  uncategorized: '#uncategorized',
}

function viewFromHash(hash: string): DashboardView {
  const normalized = hash.toLowerCase()
  const match = (Object.entries(VIEW_HASH) as Array<[DashboardView, string]>).find(([, value]) => value === normalized)
  return match?.[0] || 'dashboard'
}

function investmentSourceFromUrl() {
  const params = new URLSearchParams(window.location.search)
  const value = params.get('investment_source')
  return value && value.trim() ? value : null
}

function retirementSourceFromUrl() {
  const params = new URLSearchParams(window.location.search)
  const value = params.get('retirement_source')
  return value && value.trim() ? value : null
}

function hrefForView(view: DashboardView, investmentSource?: string | null, retirementSource?: string | null) {
  const hash = VIEW_HASH[view]
  const url = new URL(window.location.href)
  if (view === 'investments' && investmentSource) {
    url.searchParams.set('investment_source', investmentSource)
  } else {
    url.searchParams.delete('investment_source')
  }
  if (view === 'retirement' && retirementSource) {
    url.searchParams.set('retirement_source', retirementSource)
  } else {
    url.searchParams.delete('retirement_source')
  }
  url.hash = hash
  if (view === 'dashboard') {
    url.hash = ''
  }
  return `${url.pathname}${url.search}${url.hash}`
}

export function Dashboard() {
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [activeView, setActiveView] = useState<DashboardView>(() => viewFromHash(window.location.hash))
  const [showImports, setShowImports] = useState(false)
  const [showGettingStarted, setShowGettingStarted] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [syncError, setSyncError] = useState<string | null>(null)
  const [sidebarExpanded, setSidebarExpanded] = useState(false)
  const [selectedInvestmentSource, setSelectedInvestmentSource] = useState<string | null>(() => investmentSourceFromUrl())
  const [selectedRetirementSource, setSelectedRetirementSource] = useState<string | null>(() => retirementSourceFromUrl())
  const { category, setCategory, setSearch, accounts, toggleAccount, setAccounts, setGranularity, clearFilters, privacyMode, coreExpensesOnly, setCoreExpensesOnly, includeRentInCoreExpenses, setIncludeRentInCoreExpenses, displayCurrency, setDisplayCurrency, dark, toggleDark } = useFilterStore()
  const search = useFilterStore(s => s.search)
  const togglePrivacyMode = useFilterStore(s => s.togglePrivacyMode)
  const setDark = useFilterStore(s => s.setDark)
  const fmt = (n: number) => formatCurrency(n, privacyMode, { currency: displayCurrency })

  const queryClient = useQueryClient()
  const { data: summary } = useSummary()
  const { data: categories } = useByCategory()
  const { data: merchants } = useByMerchant()
  const { data: yearlyTrends } = useYearlyTrends()
  const { data: subscriptions } = useSubscriptions()
  const { data: txnData } = useTransactions()
  const { data: sidebarAccountsData } = useQuery({
    queryKey: ['sidebar-accounts', displayCurrency],
    queryFn: () => api.getSidebarAccounts(displayCurrency),
    staleTime: 60_000,
  })

  const { data: appMeta } = useQuery({
    queryKey: ['app-meta'],
    queryFn: () => fetch('/api/meta').then(r => r.json()) as Promise<{ mode: string; database_path: string }>,
    staleTime: 300_000,
  })

  const { data: settingsData, refetch: refetchSettings } = useQuery({
    queryKey: ['settings'],
    queryFn: () => api.getSettings(),
    staleTime: 60_000,
  })

  const [needsOnboarding, setNeedsOnboarding] = useState<boolean | null>(null)

  const sidebarAccounts = sidebarAccountsData?.accounts || []
  const exchangeRates = sidebarAccountsData?.exchange_rates || []
  const filterableAccounts = useMemo(
    () => sidebarAccounts.filter((account) => account.filter_source),
    [sidebarAccounts],
  )
  const snapshotSources = useMemo(
    () =>
      sidebarAccounts
        .filter((account) => account.balance !== null && account.ledger_balance === null)
        .map((account) => ({ source: account.source, source_key: account.source_key, value: account.balance, group: account.group })),
    [sidebarAccounts],
  )

  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark)
  }, [dark])

  useEffect(() => {
    if (needsOnboarding !== null || !settingsData) return
    if (appMeta?.mode === 'demo') {
      setNeedsOnboarding(false)
      return
    }
    setNeedsOnboarding(!settingsData.vault.connected)
  }, [appMeta?.mode, needsOnboarding, settingsData])

  useEffect(() => {
    if (!settingsData?.vault.connected || needsOnboarding !== false) return
    const done = window.localStorage.getItem(GETTING_STARTED_DONE_KEY) === 'true'
    if (!done) setShowGettingStarted(true)
  }, [needsOnboarding, settingsData?.vault.connected])

  useEffect(() => {
    if (needsOnboarding === false) {
      void api.prefetchVaultPanel().catch(() => undefined)
    }
  }, [needsOnboarding])

  useEffect(() => {
    const onPopState = () => {
      setActiveView(viewFromHash(window.location.hash))
      setSelectedInvestmentSource(investmentSourceFromUrl())
      setSelectedRetirementSource(retirementSourceFromUrl())
    }
    window.history.replaceState(
      { financeView: activeView, investmentSource: selectedInvestmentSource, retirementSource: selectedRetirementSource },
      '',
      hrefForView(activeView, selectedInvestmentSource, selectedRetirementSource),
    )
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  const navigateToView = (nextView: DashboardView, options?: { replace?: boolean; investmentSource?: string | null; retirementSource?: string | null }) => {
    setActiveView(nextView)
    const nextInvestmentSource = nextView === 'investments'
      ? (options?.investmentSource ?? selectedInvestmentSource)
      : null
    const nextRetirementSource = nextView === 'retirement'
      ? (options?.retirementSource ?? selectedRetirementSource)
      : null
    setSelectedInvestmentSource(nextInvestmentSource)
    setSelectedRetirementSource(nextRetirementSource)
    const method = options?.replace ? 'replaceState' : 'pushState'
    window.history[method](
      { financeView: nextView, investmentSource: nextInvestmentSource, retirementSource: nextRetirementSource },
      '',
      hrefForView(nextView, nextInvestmentSource, nextRetirementSource),
    )
  }

  const handleBackToDashboard = () => {
    if (activeView !== 'dashboard' && window.history.length > 1) {
      window.history.back()
      return
    }
    navigateToView('dashboard', { replace: true })
  }

  const handleRefresh = async () => {
    setSyncing(true)
    setSyncError(null)
    try {
      const result = await api.syncPlaid()
      if (result.status === 'partial_error') {
        setSyncError((result.errors || []).map((error) =>
          `${error.source} ${error.stage}: ${error.display_message || error.message || 'sync failed'}`
        ).join(' | '))
      }
      queryClient.invalidateQueries()
    } catch (error) {
      setSyncError(error instanceof Error ? error.message : 'Sync failed')
    } finally {
      setSyncing(false)
    }
  }

  const handleDataChange = () => queryClient.invalidateQueries()
  const handleShowHome = () => navigateToView('dashboard')
  const handleToggleCoreExpensesFocus = () => {
    const next = !coreExpensesOnly
    if (next) setCategory(null)
    setCoreExpensesOnly(next)
  }
  const handleShowNetWorth = () => {
    setGranularity('alltime')
    navigateToView('net-worth')
  }
  const handleCompleteGettingStarted = () => {
    window.localStorage.setItem(GETTING_STARTED_DONE_KEY, 'true')
    setShowGettingStarted(false)
  }

  const handleOnboardingFreshStart = async () => {
    await refetchSettings()
    setNeedsOnboarding(false)
    const done = window.localStorage.getItem(GETTING_STARTED_DONE_KEY) === 'true'
    if (!done) {
      setShowGettingStarted(true)
    }
  }

  // Commands a host shell can invoke. The macOS app's menu bar drives these instead of
  // synthesising clicks on CSS selectors, which would break silently the first time a
  // class name changed and would give no way to tell "did nothing" from "not wired".
  // Inert in a browser: nothing ever dispatches.
  //
  // No dependency array on purpose. These handlers close over current state, and this
  // effect only writes to a module-level Map — so re-registering on every render is
  // cheap and always correct, whereas a dependency list would go stale exactly when
  // someone adds a command that reads new state.
  useEffect(() => {
    installCommandBus()
    return registerCommands({
      'navigate:home': handleShowHome,
      'navigate:projects': () => navigateToView('projects'),
      'navigate:uncategorized': () => navigateToView('uncategorized'),
      'navigate:payroll': () => navigateToView('payroll'),
      'navigate:recurring': () => navigateToView('recurring'),
      'navigate:net-worth': handleShowNetWorth,
      'navigate:investments': () => navigateToView('investments'),
      'navigate:retirement': () => navigateToView('retirement'),
      'open:settings': () => setSettingsOpen(true),
      'open:imports': () => setShowImports(true),
      'open:getting-started': () => setShowGettingStarted(true),
      'toggle:privacy': togglePrivacyMode,
      'toggle:dark': toggleDark,
      // The shell sets this from NSApp.effectiveAppearance, so the app follows System
      // Settings the way a Mac app does. `set`, not `toggle`: flipping from an unknown
      // state gets it right only half the time.
      'set:appearance': (mode) => setDark(mode === 'dark'),
      'toggle:sidebar': () => setSidebarExpanded((expanded) => !expanded),
      sync: handleRefresh,
      'clear:filters': clearFilters,
      // Dispatched by the macOS shell once a provider OAuth flow it carried out to the
      // system browser has landed. The page cannot notice on its own: the popup it
      // expected to watch opened in another application.
      'refresh:settings': async () => {
        await refetchSettings()
        await queryClient.invalidateQueries()
      },
      'focus:search': () => {
        // Queried by a stable data attribute rather than threading a ref through the
        // shared JSX block this input lives in. The attribute exists for this and is
        // not a styling hook.
        const input = document.querySelector<HTMLInputElement>('[data-command="search-merchant"]')
        input?.focus()
        input?.select()
      },
    })
  })

  const allCats = useMemo(() => {
    const map = new Map(Object.keys(CATEGORY_COLORS).map(c => [c, 0]))
    categories?.forEach((c: any) => map.set(c.category, c.total))
    return Array.from(map.entries()).map(([category, total]) => ({ category, total }))
      .sort((a, b) => {
        const ai = CATEGORY_ORDER.indexOf(a.category), bi = CATEGORY_ORDER.indexOf(b.category)
        return (ai === -1 ? 999 : ai) - (bi === -1 ? 999 : bi)
      })
  }, [categories])
  const maxCatAbs = allCats.length ? Math.max(...allCats.map(c => Math.abs(c.total)), 1) : 1

  // Compute summary from filtered trends (synced with chart)
  const filteredSummary = useMemo(() => {
    if (!summary) return { income: 0, spending: 0, coreSpending: 0, net: 0 }
    const { income, spending, net_flow, core_spending, core_spending_excluding_rent } = summary
    return {
      income,
      spending,
      coreSpending: includeRentInCoreExpenses ? core_spending : core_spending_excluding_rent,
      net: net_flow,
    }
  }, [includeRentInCoreExpenses, summary])

  let content: React.ReactNode
  if (activeView === 'recurring') content = <RecurringPage onBack={handleBackToDashboard} />
  else if (activeView === 'projects') content = <ProjectsPage onBack={handleBackToDashboard} />
  else if (activeView === 'investments') content = <InvestmentsPage onBack={handleBackToDashboard} source={selectedInvestmentSource} />
  else if (activeView === 'retirement') content = (
    <RetirementPage
      onBack={handleBackToDashboard}
      source={selectedRetirementSource}
    />
  )
  else if (activeView === 'payroll') content = <PayslipPage onBack={handleBackToDashboard} />
  else if (activeView === 'net-worth') content = <NetWorthPage onBack={handleBackToDashboard} snapshotSources={snapshotSources} />
  else if (activeView === 'uncategorized') content = <UncategorizedPage onBack={handleBackToDashboard} />
  else content = (
    <>
      {appMeta?.mode === 'demo' && (
        <div className="app-badge-warning mb-4 rounded-lg border px-3 py-2 text-xs" style={{ borderColor: 'var(--color-warning)' }}>
          Demo mode is active. The app is using isolated data at `{appMeta.database_path}`.
        </div>
      )}
      {/* Global Filters. data-app-toolbar lets the macOS shell lift this row into the
          window's title bar area, the way a Mac app's toolbar sits beside the traffic
          lights rather than below them. */}
      <div data-app-toolbar className="flex items-center gap-3 mb-5 flex-wrap">
        <select value={accounts ? Array.from(accounts).join(',') : ''} onChange={e => setAccounts(e.target.value ? new Set([e.target.value]) : null)}
          className="app-input text-xs rounded px-2 py-1.5">
          <option value="">All Accounts</option>
          {filterableAccounts.map((account) => (
            <option key={account.source} value={account.filter_source || account.source}>{account.source}</option>
          ))}
        </select>
        <select value={category || ''} onChange={e => setCategory(e.target.value || null)}
          className="app-input text-xs rounded px-2 py-1.5">
          <option value="">All Categories</option>
          {CATEGORY_ORDER.map(c => <option key={c} value={c}>{c}</option>)}
        </select>
        <input type="text" placeholder="Search merchant..."
          data-command="search-merchant"
          value={search}
          onChange={e => setSearch(e.target.value)}
          className="app-input text-xs rounded px-2 py-1.5 w-40" />
        <div className="flex-1" />
        <select
          value={displayCurrency}
          onChange={e => setDisplayCurrency(e.target.value)}
          className="app-input text-xs rounded px-2 py-1.5"
          title="Display currency"
        >
          {SUPPORTED_CURRENCIES.map(c => <option key={c} value={c}>{c}</option>)}
        </select>
        <PeriodNav />
      </div>
      {coreExpensesOnly ? (
        <div className="mb-4 flex items-center gap-2 text-xs">
          <span className="app-badge-positive rounded-full px-2.5 py-1">
            Core Expenses only{includeRentInCoreExpenses ? ' · rent included' : ' · rent excluded'}
          </span>
          <button
            type="button"
            onClick={() => setCoreExpensesOnly(false)}
            className="inline-flex items-center gap-1 rounded-full border px-2 py-1 app-hover"
            style={{ borderColor: 'var(--border-default)', color: 'var(--text-muted)' }}
          >
            <X size={12} />
            Clear
          </button>
        </div>
      ) : null}

    {/* Pulse: Summary Cards */}
    <div className="grid grid-cols-5 gap-3 mb-5">
      <Card label="Inflow" value={fmt(filteredSummary.income)} color="var(--color-positive)" />
      <Card label="Outflow" value={fmt(filteredSummary.spending)} />
      <Card
        label="Core Expenses"
        value={fmt(filteredSummary.coreSpending)}
        active={coreExpensesOnly}
        onClick={handleToggleCoreExpensesFocus}
        action={(
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation()
              setIncludeRentInCoreExpenses(!includeRentInCoreExpenses)
            }}
            className={`rounded-full border px-2 py-0.5 text-[10px] font-medium transition-colors ${
              includeRentInCoreExpenses
                ? 'app-badge-positive border-[var(--color-positive)]'
                : 'app-hover'
            }`}
            style={!includeRentInCoreExpenses ? { borderColor: 'var(--border-default)', color: 'var(--text-muted)' } : undefined}
            aria-pressed={includeRentInCoreExpenses}
            title={includeRentInCoreExpenses ? 'Include rent in core expenses' : 'Exclude rent from core expenses'}
          >
            Rent
          </button>
        )}
      />
      <Card label="Recurring" value={`${fmt(summary?.subscriptions_monthly ?? 0)}/mo`} />
      <Card label="Net Flow" value={fmt(filteredSummary.net)} color={filteredSummary.net >= 0 ? 'var(--color-positive)' : 'var(--color-negative)'} />
    </div>

    {/* Flow Chart */}
    <div data-tour="tour-main-chart" className="app-surface rounded-lg p-4 shadow-sm mb-5">
      {yearlyTrends?.length ? (
        <FlowChart data={yearlyTrends} dark={dark} />
      ) : (
        <p className="text-slate-500 h-[220px] flex items-center justify-center text-sm">No trend data</p>
      )}
    </div>

    {/* Breakdown Row */}
    <div className="grid grid-cols-3 gap-4 mb-5">
      {/* By Category - Diverging bars */}
      <div className="app-surface rounded-lg p-4 shadow-sm">
        <h2 className="font-semibold text-sm mb-3">By Category</h2>
        {allCats.length > 0 ? (
          <div className="space-y-1.5">
            {allCats.map((c: any) => {
              const isIncome = c.total > 0
              const pct = (Math.abs(c.total) / maxCatAbs) * 50
              return (
                <div key={c.category}
                  className={`flex items-center gap-1 cursor-pointer rounded px-1 py-0.5 transition-colors ${category === c.category ? 'app-selected ring-1 ring-[var(--border-default)]' : 'app-hover'}`}
                  onClick={() => setCategory(category === c.category ? null : c.category)}>
                  <div className="w-28 text-xs truncate text-right pr-1">{c.category}</div>
                  <div className="flex-1 flex h-4">
                    <div className="w-1/2 flex justify-end">
                      {!isIncome && <div className="h-full rounded-l" style={{
                        width: `${pct}%`, backgroundColor: getCategoryColor(c.category),
                      }} />}
                    </div>
                    <div className="w-px" style={{ background: 'var(--border-default)' }} />
                    <div className="w-1/2">
                      {isIncome && <div className="h-full rounded-r" style={{
                        width: `${pct}%`, backgroundColor: getCategoryColor(c.category),
                      }} />}
                    </div>
                  </div>
                  <div className="w-20 text-xs text-right">{fmt(c.total)}</div>
                </div>
              )
            })}
          </div>
        ) : <p className="text-slate-500 text-sm">No data</p>}
      </div>

      <div className="app-surface rounded-lg p-4 shadow-sm">
        <h2 className="font-semibold text-sm mb-3">Top Merchants</h2>
        {merchants && merchants.length > 0 ? (
          <ol className="space-y-1.5">
            {merchants.map((m, i) => (
              <li key={m.merchant} className="flex text-sm cursor-pointer app-hover rounded px-1 -mx-1"
                onClick={() => setSearch(m.merchant)}>
                <span className="text-slate-500 w-5 shrink-0 text-right mr-1" style={{ fontVariantNumeric: 'tabular-nums' }}>{i + 1}.</span>
                <span className="flex-1 truncate">{m.merchant}</span>
                <span className="font-medium text-right w-24 shrink-0">{fmt(m.total)}</span>
              </li>
            ))}
          </ol>
        ) : <p className="text-slate-500 text-sm">No data</p>}
      </div>

      <div className="app-surface rounded-lg p-4 shadow-sm">
        <div className="flex justify-between items-center mb-3">
          <h2 className="font-semibold text-sm">Live Recurring</h2>
          <button onClick={() => navigateToView('recurring')} className="text-xs text-blue-500 hover:underline">more →</button>
        </div>
        {subscriptions && subscriptions.length > 0 ? (
          <ul className="space-y-1.5">
            {subscriptions.slice(0, 5).map((s) => (
              <li key={`${s.merchant}-${s.amount}`} className="flex justify-between text-sm">
                <span className="truncate">{s.merchant}</span>
                <span className="text-slate-500 ml-2">{fmt(s.amount)}/{s.frequency.slice(0, 2)}</span>
              </li>
            ))}
          </ul>
        ) : <p className="text-slate-500 text-sm">No recurring detected</p>}
      </div>
    </div>

    <div className="app-surface rounded-lg p-4 shadow-sm">
      <Ledger transactions={txnData?.transactions || []} />
    </div>
    </>
  )

  return (
    <div className="app-shell min-h-screen font-[system-ui]" style={{ fontVariantNumeric: 'tabular-nums', color: 'var(--text-primary)' }}>
      <Sidebar
        accounts={sidebarAccounts}
        exchangeRates={exchangeRates}
        selectedAccounts={accounts}
        onToggleAccount={toggleAccount}
        onResetAccounts={() => setAccounts(null)}
        dark={dark}
        onToggleDark={toggleDark}
        onSync={handleRefresh}
        onOpenSettings={() => setSettingsOpen(true)}
        onShowHome={handleShowHome}
        onShowProjects={() => navigateToView('projects')}
        onShowInvestments={(source) => navigateToView('investments', { investmentSource: source || null })}
        onShowRetirement={(source) => navigateToView('retirement', { retirementSource: source || null })}
        onShowPayslips={() => navigateToView('payroll')}
        onShowNetWorth={handleShowNetWorth}
        onShowUncategorized={() => navigateToView('uncategorized')}
        onShowImports={() => setShowImports(true)}
        onOpenGettingStarted={() => setShowGettingStarted(true)}
        syncing={syncing}
        expanded={sidebarExpanded}
        onToggleExpanded={() => setSidebarExpanded(!sidebarExpanded)}
      />
      <OnboardingGate
        open={needsOnboarding === true}
        settings={settingsData ?? null}
        onRefreshSettings={async () => (await refetchSettings()).data}
        onCompleteFreshStart={() => {
          void handleOnboardingFreshStart()
        }}
      />
      <AppTour
        open={showGettingStarted}
        onClose={() => setShowGettingStarted(false)}
        onComplete={handleCompleteGettingStarted}
      />
      <ImportsPanel open={showImports} onClose={() => setShowImports(false)} onSuccess={handleDataChange} />
      <SettingsPanel open={settingsOpen} onClose={() => setSettingsOpen(false)} onDataChange={handleDataChange} />
      <div
        data-tour="tour-dashboard"
        className="p-4 transition-all duration-200"
        style={{ marginRight: (sidebarExpanded ? SIDEBAR_EXPANDED_WIDTH : SIDEBAR_COLLAPSED_WIDTH) + 12 }}
      >
        {syncError ? (
          <div className="mb-3 rounded-lg border border-rose-400/50 bg-rose-500/10 px-4 py-3 text-sm text-rose-600 dark:text-rose-300" role="alert">
            <strong>Sync incomplete:</strong> {syncError}
          </div>
        ) : null}
        {content}
      </div>
    </div>
  )
}

function Card({ label, value, color, action, onClick, active }: { label: string; value: string; color?: string; action?: React.ReactNode; onClick?: () => void; active?: boolean }) {
  return (
    <div
      onClick={onClick}
      className={`app-surface rounded-lg p-3 shadow-sm text-left ${onClick ? 'cursor-pointer app-hover' : ''} ${active ? 'ring-2 ring-emerald-400/70' : ''}`}
      role={onClick ? 'button' : undefined}
      tabIndex={onClick ? 0 : undefined}
      onKeyDown={onClick ? (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onClick()
        }
      } : undefined}
    >
      <div className="mb-0.5 flex items-center justify-between gap-2">
        <div className="text-xs" style={{ color: 'var(--text-muted)' }}>{label}</div>
        {action}
      </div>
      <div className="text-lg font-bold" style={color ? { color } : undefined}>{value}</div>
    </div>
  )
}
