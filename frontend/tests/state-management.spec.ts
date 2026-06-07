import { test, expect, Page } from '@playwright/test'

const mockData = {
  summary: { income: 5000, spending: 2500, transfers: 500, spend_income_ratio: 50, net_flow: 2000, subscriptions_monthly: 150, range: 'month' },
  categories: [{ category: 'Groceries', total: 800 }],
  merchants: [{ merchant: 'Amazon', total: 450 }],
  trends: [{ period: '2026-02-01', total: 300 }],
  subscriptions: [{ merchant: 'Netflix', amount: 15.99, frequency: 'monthly', last_charge: '2026-02-01', occurrences: 12 }],
  transactions: { transactions: [{ id: '1', date: '2026-02-06', amount: -87.32, merchant_raw: 'WHOLE FOODS', merchant_clean: 'Whole Foods', category: 'Groceries', source: 'chase', account_last4: '4521', is_recurring: false, tags: [] }], total: 1, offset: 0, limit: 50 },
}

async function setupMocks(page: Page) {
  await page.route('**/api/analytics/summary*', route => route.fulfill({ json: mockData.summary }))
  await page.route('**/api/analytics/by-category*', route => route.fulfill({ json: mockData.categories }))
  await page.route('**/api/analytics/by-merchant*', route => route.fulfill({ json: mockData.merchants }))
  await page.route('**/api/analytics/trends*', route => route.fulfill({ json: mockData.trends }))
  await page.route('**/api/analytics/subscriptions*', route => route.fulfill({ json: mockData.subscriptions }))
  await page.route('**/api/transactions/*', route => route.fulfill({ json: mockData.transactions }))
  await page.route('**/api/sync/status', route => route.fulfill({ json: { accounts: [] } }))
}

test.describe('State Management - Filter Store', () => {
  test.beforeEach(async ({ page }) => {
    await setupMocks(page)
    await page.goto('/')
  })

  test('date range state persists', async ({ page }) => {
    await page.locator('select').selectOption('year')
    await expect(page.locator('select')).toHaveValue('year')
    
    // Interact with page
    await page.getByText('Groceries').click()
    
    // State should persist
    await expect(page.locator('select')).toHaveValue('year')
  })

  test('category filter state updates UI', async ({ page }) => {
    await page.getByText('Groceries').click()
    await expect(page.getByText('Clear filter: Groceries')).toBeVisible()
  })

  test('clearing filter updates state', async ({ page }) => {
    await page.getByText('Groceries').click()
    await expect(page.getByText('Clear filter: Groceries')).toBeVisible()
    
    await page.getByText('Clear filter: Groceries').click()
    await expect(page.getByText('Clear filter:')).not.toBeVisible()
  })

  test('multiple state changes work correctly', async ({ page }) => {
    // Change date range
    await page.locator('select').selectOption('week')
    
    // Set category filter
    await page.getByText('Groceries').click()
    
    // Both should be active
    await expect(page.locator('select')).toHaveValue('week')
    await expect(page.getByText('Clear filter: Groceries')).toBeVisible()
    
    // Clear filter
    await page.getByText('Clear filter: Groceries').click()
    
    // Date range should still be week
    await expect(page.locator('select')).toHaveValue('week')
  })
})

test.describe('State Management - Dark Mode', () => {
  test.beforeEach(async ({ page }) => {
    await setupMocks(page)
    await page.goto('/')
  })

  test('dark mode state toggles correctly', async ({ page }) => {
    // Initially light
    await expect(page.locator('html')).not.toHaveClass(/dark/)
    
    // Toggle to dark
    await page.getByRole('button').filter({ has: page.locator('svg.lucide-moon') }).click()
    await expect(page.locator('html')).toHaveClass(/dark/)
    
    // Toggle back to light
    await page.getByRole('button').filter({ has: page.locator('svg.lucide-sun') }).click()
    await expect(page.locator('html')).not.toHaveClass(/dark/)
  })

  test('dark mode persists during interactions', async ({ page }) => {
    // Enable dark mode
    await page.getByRole('button').filter({ has: page.locator('svg.lucide-moon') }).click()
    await expect(page.locator('html')).toHaveClass(/dark/)
    
    // Change date range
    await page.locator('select').selectOption('year')
    
    // Dark mode should persist
    await expect(page.locator('html')).toHaveClass(/dark/)
  })
})

test.describe('State Management - Query Invalidation', () => {
  test('refresh invalidates all queries', async ({ page }) => {
    const callCounts: Record<string, number> = {}
    
    await page.route('**/api/**', route => {
      const url = route.request().url()
      const endpoint = url.split('/api/')[1]?.split('?')[0] || 'unknown'
      callCounts[endpoint] = (callCounts[endpoint] || 0) + 1
      
      if (url.includes('summary')) return route.fulfill({ json: mockData.summary })
      if (url.includes('by-category')) return route.fulfill({ json: mockData.categories })
      if (url.includes('by-merchant')) return route.fulfill({ json: mockData.merchants })
      if (url.includes('trends')) return route.fulfill({ json: mockData.trends })
      if (url.includes('subscriptions')) return route.fulfill({ json: mockData.subscriptions })
      if (url.includes('transactions')) return route.fulfill({ json: mockData.transactions })
      if (url.includes('sync/status')) return route.fulfill({ json: { accounts: [] } })
      if (url.includes('sync/plaid/sync')) return route.fulfill({ json: { status: 'synced', added: 0 } })
      return route.continue()
    })
    
    await page.goto('/')
    await page.waitForTimeout(200)
    
    const initialCounts = { ...callCounts }
    
    // Trigger refresh
    await page.getByRole('button').filter({ has: page.locator('svg.lucide-refresh-cw') }).click()
    await page.waitForTimeout(300)
    
    // At least some endpoints should be called again
    const totalInitial = Object.values(initialCounts).reduce((a, b) => a + b, 0)
    const totalAfter = Object.values(callCounts).reduce((a, b) => a + b, 0)
    expect(totalAfter).toBeGreaterThan(totalInitial)
  })
})

test.describe('State Management - URL State', () => {
  test('initial state from defaults', async ({ page }) => {
    await setupMocks(page)
    await page.goto('/')
    
    // Default date range should be month
    await expect(page.locator('select')).toHaveValue('month')
    
    // No category filter by default
    await expect(page.getByText('Clear filter:')).not.toBeVisible()
  })
})

test.describe('State Management - Component State', () => {
  test('summary cards update with new data', async ({ page }) => {
    await setupMocks(page)
    await page.goto('/')
    
    await expect(page.getByText('$5,000.00')).toBeVisible()
    
    // Change to different data
    await page.route('**/api/analytics/summary*', route => route.fulfill({ 
      json: { ...mockData.summary, income: 10000 } 
    }))
    
    // Trigger refetch by changing date range
    await page.locator('select').selectOption('year')
    await page.waitForTimeout(200)
    
    await expect(page.getByText('$10,000.00')).toBeVisible()
  })
})
