import { test, expect, Page } from '@playwright/test'

const summary = {
  income: 5000,
  spending: 2500,
  core_spending: 1500,
  core_spending_excluding_rent: 1500,
  transfers: 0,
  spend_income_ratio: 50,
  net_flow: 2500,
  total_balance: 10000,
  subscriptions_monthly: 0,
  start_date: '2026-02-01',
  end_date: '2026-02-02',
}

const standardTrends = [
  { period: '2026-02-01', Income: 5000, Groceries: 50, total: 5050 },
  { period: '2026-02-02', Dining: 25, total: 25 },
]

const coreTrends = [
  { period: '2026-02-01', Income: 5000, Groceries: 50, total: 5050 },
  { period: '2026-02-02', Dining: 25, total: 25 },
]

async function setupFlowMocks(page: Page) {
  await page.route('**/api/**', async (route) => {
    const url = route.request().url()
    if (url.includes('/api/analytics/summary')) return route.fulfill({ json: summary })
    if (url.includes('/api/analytics/by-category')) return route.fulfill({ json: [{ category: 'Groceries', total: -50 }, { category: 'Dining', total: -25 }] })
    if (url.includes('/api/analytics/by-merchant')) return route.fulfill({ json: [{ merchant: 'Whole Foods', total: 50 }] })
    if (url.includes('/api/analytics/trends')) {
      const useCore = url.includes('core_expenses_only=1')
      return route.fulfill({ json: useCore ? coreTrends : standardTrends })
    }
    if (url.includes('/api/analytics/subscriptions')) return route.fulfill({ json: [] })
    if (url.includes('/api/transactions/')) return route.fulfill({ json: { transactions: [], total: 0, offset: 0, limit: 50 } })
    if (url.includes('/api/sync/sidebar-accounts')) return route.fulfill({ json: { accounts: [] } })
    if (url.includes('/api/settings/')) return route.fulfill({ json: { stats: { total_transactions: 0, connected_accounts: 0, distinct_categories: 0 }, accounts: [], vault: { vault_id: null, provider: null, provider_email: null, connected: false, last_backup_at: null, google_drive_ready: false } } })
    if (url.includes('/api/meta')) return route.fulfill({ json: { mode: 'test', database_path: '/tmp/test.db' } })
    return route.fulfill({ json: {} })
  })
}

test.describe('Flow chart signs', () => {
  test.beforeEach(async ({ page }) => {
    await setupFlowMocks(page)
    await page.addInitScript(() => {
      window.localStorage.setItem('finance-app-getting-started-done', 'true')
    })
    await page.goto('/')
  })

  test('shows positive income and positive outflow in the flow header', async ({ page }) => {
    await expect(page.getByText('I: $5,000.00')).toBeVisible()
    await expect(page.getByText('O: $50.00')).toBeVisible()
    await expect(page.getByText('N: $4,950.00')).toBeVisible()
  })

  test('preserves positive income when core expenses focus is enabled', async ({ page }) => {
    await page.getByRole('button', { name: /Core Expenses/i }).click()
    await expect(page.getByText('I: $5,000.00')).toBeVisible()
    await expect(page.getByText('O: $50.00')).toBeVisible()
    await expect(page.getByText('N: $4,950.00')).toBeVisible()
  })
})
