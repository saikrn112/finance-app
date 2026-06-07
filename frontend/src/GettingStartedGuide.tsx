import { useQuery } from '@tanstack/react-query'
import { BookOpen, CheckCircle2, ChevronRight, Circle, FolderKanban, LayoutDashboard, Link2, RefreshCw, Tags, X } from 'lucide-react'
import { api } from './api'
import { useProjects } from './hooks'

import { GETTING_STARTED_DONE_KEY, GETTING_STARTED_DASHBOARD_KEY } from './AppTour'
export { GETTING_STARTED_DONE_KEY, GETTING_STARTED_DASHBOARD_KEY }

interface Props {
  open: boolean
  mode?: string
  syncing: boolean
  onClose: () => void
  onComplete: () => void
  onExpandSidebar: () => void
  onOpenImports: () => void
  onSync: () => Promise<void>
  onShowProjects: () => void
  onFocusUncategorized: () => void
  onResetDashboard: () => void
}

type Step = {
  title: string
  description: string
  done: boolean
  actionLabel: string
  action: () => void | Promise<void>
  icon: typeof Link2
}

export function GettingStartedGuide({
  open,
  mode,
  syncing,
  onClose,
  onComplete,
  onExpandSidebar,
  onOpenImports,
  onSync,
  onShowProjects,
  onFocusUncategorized,
  onResetDashboard,
}: Props) {
  const { data: settingsData } = useQuery({
    queryKey: ['settings'],
    queryFn: () => api.getSettings(),
    staleTime: 60_000,
  })

  const { data: connectedAccounts } = useQuery({
    queryKey: ['connected-accounts'],
    queryFn: () => fetch('/api/sync/connected').then(r => r.json()) as Promise<{ source: string; type: string }[]>,
    staleTime: 60_000,
  })

  const { data: projects } = useProjects()

  const { data: uncategorized } = useQuery({
    queryKey: ['getting-started', 'uncategorized'],
    queryFn: () => api.getTransactions({ category: 'Uncategorized', limit: '1' }),
    staleTime: 60_000,
  })

  if (!open) return null

  const totalTransactions = settingsData?.stats.total_transactions ?? 0
  const connectedCount = connectedAccounts?.length ?? 0
  const uncategorizedCount = uncategorized?.total ?? 0
  const projectCount = projects?.length ?? 0
  const categorizedEnough = totalTransactions > 0 && uncategorizedCount / totalTransactions < 0.1
  const dashboardRead = window.localStorage.getItem(GETTING_STARTED_DASHBOARD_KEY) === 'true'

  const steps: Step[] = [
    {
      title: mode === 'demo' ? 'Inspect seeded demo data' : 'Connect or import data',
      description:
        mode === 'demo'
          ? 'Demo mode is isolated from your live database. Expand the sidebar and inspect the seeded bank, card, investment, and payroll data.'
          : 'Use Settings → Connected Institutions to link bank accounts via Plaid, or open Imports for manual CSV and statement uploads.',
      done: totalTransactions > 0 || connectedCount > 0,
      actionLabel: mode === 'demo' ? 'Open sidebar' : 'Open imports',
      action: mode === 'demo' ? onExpandSidebar : onOpenImports,
      icon: Link2,
    },
    {
      title: 'Sync latest balances and transactions',
      description:
        mode === 'demo'
          ? 'Demo sync is safe and refreshes the mock responses, so you can validate the full flow without touching production data.'
          : 'Run a sync after connecting accounts so balances, transactions, and investments populate before review.',
      done: connectedCount > 0 && totalTransactions > 0,
      actionLabel: syncing ? 'Syncing…' : 'Run sync',
      action: onSync,
      icon: RefreshCw,
    },
    {
      title: 'Review uncategorized transactions',
      description:
        totalTransactions === 0
          ? 'Once transactions exist, review any Uncategorized entries and tighten categories before relying on dashboard totals.'
          : `${uncategorizedCount} transactions are still Uncategorized. Review those first so summaries and trends stay trustworthy.`,
      done: categorizedEnough,
      actionLabel: 'Show Uncategorized',
      action: onFocusUncategorized,
      icon: Tags,
    },
    {
      title: 'Track work with projects',
      description:
        projectCount > 0
          ? `${projectCount} project${projectCount === 1 ? '' : 's'} already exist. Use them to group spending and income tied to a specific initiative.`
          : 'Create projects for travel, home, side work, or any bounded effort so spend and income are attributable.',
      done: projectCount > 0,
      actionLabel: 'Open projects',
      action: onShowProjects,
      icon: FolderKanban,
    },
    {
      title: 'Read the dashboard with a clean baseline',
      description:
        'Reset filters, read Inflow, Outflow, Out/In, Recurring, and Net Flow first, then drill into the category, merchant, recurring, and ledger sections.',
      done: dashboardRead,
      actionLabel: 'Open dashboard',
      action: () => {
        window.localStorage.setItem(GETTING_STARTED_DASHBOARD_KEY, 'true')
        onResetDashboard()
      },
      icon: LayoutDashboard,
    },
  ]

  const completedSteps = steps.filter(step => step.done).length

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/70 px-4">
      <div className="app-surface w-full max-w-4xl rounded-2xl shadow-2xl dark:border-slate-700 dark:bg-slate-900">
        <div className="flex items-start justify-between gap-4 border-b border-[var(--app-border-soft)] px-6 py-5 dark:border-slate-800">
          <div>
            <div className="mb-2 inline-flex items-center gap-2 rounded-full bg-blue-50 px-3 py-1 text-xs font-medium text-blue-700 dark:bg-blue-950/60 dark:text-blue-200">
              <BookOpen size={14} />
              Getting Started
            </div>
            <h2 className="text-xl font-semibold">Getting Started</h2>
            <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
              Use this as a guided walkthrough after account setup. It stays state-aware and does not touch your live data in demo mode.
            </p>
          </div>
          <button
            onClick={onClose}
            className="rounded-md p-2 text-slate-500 transition-colors hover:bg-[var(--app-surface-2)] hover:text-slate-900 dark:hover:bg-slate-800 dark:hover:text-white"
            aria-label="Close getting started"
          >
            <X size={18} />
          </button>
        </div>

        <div className="border-b border-[var(--app-border-soft)] px-6 py-4 dark:border-slate-800">
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <span className="font-medium">{completedSteps} / {steps.length} steps complete</span>
            <span className="text-slate-400">•</span>
            <span className="text-slate-600 dark:text-slate-300">Transactions: {totalTransactions}</span>
            <span className="text-slate-400">•</span>
            <span className="text-slate-600 dark:text-slate-300">Connected accounts: {connectedCount}</span>
            <span className="text-slate-400">•</span>
            <span className="text-slate-600 dark:text-slate-300">Projects: {projectCount}</span>
          </div>
        </div>

        <div className="grid gap-3 p-6">
          {steps.map((step) => {
            const Icon = step.icon
            return (
              <div key={step.title} className="flex items-start gap-4 rounded-xl border border-[var(--app-border-soft)] p-4 dark:border-slate-800">
                <div className="mt-0.5">
                  {step.done ? <CheckCircle2 className="text-emerald-500" size={20} /> : <Circle className="text-slate-400" size={20} />}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <Icon size={16} className="text-slate-500" />
                    <h3 className="font-medium">{step.title}</h3>
                  </div>
                  <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">{step.description}</p>
                </div>
                <button
                  onClick={() => void step.action()}
                  disabled={syncing && step.icon === RefreshCw}
                  className="inline-flex shrink-0 items-center gap-1 rounded-md border border-[var(--app-border)] px-3 py-2 text-sm transition-colors hover:bg-[var(--app-surface-2)] disabled:cursor-not-allowed disabled:opacity-60 dark:border-slate-700 dark:hover:bg-slate-800"
                >
                  {step.actionLabel}
                  <ChevronRight size={14} />
                </button>
              </div>
            )
          })}
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-[var(--app-border-soft)] px-6 py-4 dark:border-slate-800">
          <p className="text-xs text-slate-500 dark:text-slate-400">
            You can reopen this from the sidebar any time after onboarding.
          </p>
          <div className="flex items-center gap-2">
            <button
              onClick={onClose}
              className="rounded-md px-3 py-2 text-sm text-slate-600 transition-colors hover:bg-[var(--app-surface-2)] dark:text-slate-300 dark:hover:bg-slate-800"
            >
              Close
            </button>
            <button
              onClick={onComplete}
              className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-blue-500"
            >
              Mark done
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
