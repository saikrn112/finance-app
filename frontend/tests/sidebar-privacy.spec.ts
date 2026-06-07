import { test, expect, Page } from '@playwright/test'

const sidebarAccounts = [
  {
    source: 'Bank of America',
    group: 'bank_account',
    balance: 117798.31,
    ledger_balance: 117798.31,
    snapshot_balance: 117798.31,
    filter_source: 'Bank of America',
    connection_state: 'plaid',
  },
]

const summary = {
  income: 5000,
  spending: 2500,
  core_spending: 1500,
  core_spending_excluding_rent: 1500,
  transfers: 0,
  spend_income_ratio: 50,
  net_flow: 2500,
  total_balance: 117798.31,
  subscriptions_monthly: 0,
  start_date: '2026-02-01',
  end_date: '2026-02-02',
}

async function setupDashboardMocks(page: Page) {
  await page.route('**/api/**', async (route) => {
    const url = route.request().url()
    if (url.includes('/api/analytics/summary')) return route.fulfill({ json: summary })
    if (url.includes('/api/analytics/by-category')) return route.fulfill({ json: [] })
    if (url.includes('/api/analytics/by-merchant')) return route.fulfill({ json: [] })
    if (url.includes('/api/analytics/trends')) return route.fulfill({ json: [] })
    if (url.includes('/api/analytics/subscriptions')) return route.fulfill({ json: [] })
    if (url.includes('/api/transactions/')) return route.fulfill({ json: { transactions: [], total: 0, offset: 0, limit: 50 } })
    if (url.includes('/api/sync/sidebar-accounts')) return route.fulfill({ json: { accounts: sidebarAccounts } })
    if (url.includes('/api/settings/')) {
      return route.fulfill({
        json: {
          stats: { total_transactions: 0, connected_accounts: 1, distinct_categories: 0 },
          accounts: [],
          vault: { vault_id: null, provider: null, provider_email: null, connected: false, last_backup_at: null, google_drive_ready: false },
        },
      })
    }
    if (url.includes('/api/meta')) return route.fulfill({ json: { mode: 'test', database_path: '/tmp/test.db' } })
    return route.fulfill({ json: {} })
  })
}

test.describe('Collapsed sidebar privacy', () => {
  test.beforeEach(async ({ page }) => {
    await setupDashboardMocks(page)
    await page.addInitScript(() => {
      window.localStorage.setItem('finance-app-getting-started-done', 'true')
    })
    await page.goto('/')
  })

  test('shows compact net worth when privacy mode is off', async ({ page }) => {
    await expect(page.getByRole('button', { name: /118k\$/i })).toBeVisible()
  })

  test('masks compact net worth when privacy mode is on', async ({ page }) => {
    await page.getByTitle('Enable privacy mode').click()
    await expect(page.getByRole('button', { name: /\$\$/i })).toBeVisible()
    await expect(page.getByRole('button', { name: /118k\$/i })).not.toBeVisible()
  })
})
