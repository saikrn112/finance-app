import { test, expect, Page } from '@playwright/test'

// Mock data for API integration tests
const mockData = {
  summary: {
    income: 5000,
    spending: 2500,
    transfers: 500,
    spend_income_ratio: 50,
    net_flow: 2000,
    subscriptions_monthly: 150,
    range: 'month',
  },
  categories: [
    { category: 'Groceries', total: 800 },
    { category: 'Dining', total: 600 },
  ],
  merchants: [
    { merchant: 'Example Shop', total: 450 },
    { merchant: 'Example Grocer', total: 350 },
  ],
  trends: [
    { period: '2026-02-01', total: 300 },
    { period: '2026-02-02', total: 450 },
  ],
  subscriptions: [
    { merchant: 'Example Stream', amount: 15.99, frequency: 'monthly', last_charge: '2026-02-01', occurrences: 12 },
  ],
  transactions: {
    transactions: [
      { id: '1', date: '2026-02-06', amount: -87.32, merchant_raw: 'EXAMPLE GROCER', merchant_clean: 'Example Grocer', category: 'Groceries', source: 'example_card', account_last4: '4521', is_recurring: false, tags: [] },
    ],
    total: 1,
    offset: 0,
    limit: 50,
  },
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

test.describe('API Integration - Summary Endpoint', () => {
  test('fetches summary with correct date range parameter', async ({ page }) => {
    let capturedUrl = ''
    await page.route('**/api/analytics/summary*', route => {
      capturedUrl = route.request().url()
      return route.fulfill({ json: mockData.summary })
    })
    await page.route('**/api/analytics/by-category*', route => route.fulfill({ json: mockData.categories }))
    await page.route('**/api/analytics/by-merchant*', route => route.fulfill({ json: mockData.merchants }))
    await page.route('**/api/analytics/trends*', route => route.fulfill({ json: mockData.trends }))
    await page.route('**/api/analytics/subscriptions*', route => route.fulfill({ json: mockData.subscriptions }))
    await page.route('**/api/transactions/*', route => route.fulfill({ json: mockData.transactions }))
    await page.route('**/api/sync/status', route => route.fulfill({ json: { accounts: [] } }))
    
    await page.goto('/')
    await page.waitForTimeout(100)
    
    expect(capturedUrl).toContain('range=month')
  })

  test('refetches summary when date range changes', async ({ page }) => {
    const calls: string[] = []
    await page.route('**/api/analytics/summary*', route => {
      calls.push(route.request().url())
      return route.fulfill({ json: mockData.summary })
    })
    await page.route('**/api/analytics/by-category*', route => route.fulfill({ json: mockData.categories }))
    await page.route('**/api/analytics/by-merchant*', route => route.fulfill({ json: mockData.merchants }))
    await page.route('**/api/analytics/trends*', route => route.fulfill({ json: mockData.trends }))
    await page.route('**/api/analytics/subscriptions*', route => route.fulfill({ json: mockData.subscriptions }))
    await page.route('**/api/transactions/*', route => route.fulfill({ json: mockData.transactions }))
    await page.route('**/api/sync/status', route => route.fulfill({ json: { accounts: [] } }))
    
    await page.goto('/')
    await page.locator('select').selectOption('week')
    await page.waitForTimeout(200)
    
    expect(calls.some(url => url.includes('range=week'))).toBeTruthy()
  })
})

test.describe('API Integration - Transactions Endpoint', () => {
  test('fetches transactions with category filter', async ({ page }) => {
    let capturedUrl = ''
    await page.route('**/api/analytics/summary*', route => route.fulfill({ json: mockData.summary }))
    await page.route('**/api/analytics/by-category*', route => route.fulfill({ json: mockData.categories }))
    await page.route('**/api/analytics/by-merchant*', route => route.fulfill({ json: mockData.merchants }))
    await page.route('**/api/analytics/trends*', route => route.fulfill({ json: mockData.trends }))
    await page.route('**/api/analytics/subscriptions*', route => route.fulfill({ json: mockData.subscriptions }))
    await page.route('**/api/transactions/*', route => {
      capturedUrl = route.request().url()
      return route.fulfill({ json: mockData.transactions })
    })
    await page.route('**/api/sync/status', route => route.fulfill({ json: { accounts: [] } }))
    
    await page.goto('/')
    await page.getByText('Groceries').click()
    await page.waitForTimeout(200)
    
    expect(capturedUrl).toContain('category=Groceries')
  })
})

test.describe('API Integration - Sync Endpoints', () => {
  test('calls sync endpoint on refresh', async ({ page }) => {
    await setupMocks(page)
    
    let syncCalled = false
    await page.route('**/api/sync/plaid/sync', route => {
      syncCalled = true
      return route.fulfill({ json: { status: 'synced', added: 5 } })
    })
    
    await page.goto('/')
    await page.getByRole('button').filter({ has: page.locator('svg.lucide-refresh-cw') }).click()
    await page.waitForTimeout(100)
    
    expect(syncCalled).toBeTruthy()
  })

  test('invalidates queries after sync', async ({ page }) => {
    await setupMocks(page)
    
    let summaryCallCount = 0
    await page.route('**/api/analytics/summary*', route => {
      summaryCallCount++
      return route.fulfill({ json: mockData.summary })
    })
    await page.route('**/api/sync/plaid/sync', route => {
      return route.fulfill({ json: { status: 'synced', added: 5 } })
    })
    
    await page.goto('/')
    await page.waitForTimeout(100)
    const initialCount = summaryCallCount
    
    await page.getByRole('button').filter({ has: page.locator('svg.lucide-refresh-cw') }).click()
    await page.waitForTimeout(300)
    
    expect(summaryCallCount).toBeGreaterThan(initialCount)
  })
})

test.describe('API Error Scenarios', () => {
  test('handles 500 error on summary', async ({ page }) => {
    await page.route('**/api/analytics/summary*', route => route.fulfill({ status: 500, body: 'Internal Server Error' }))
    await page.route('**/api/analytics/by-category*', route => route.fulfill({ json: mockData.categories }))
    await page.route('**/api/analytics/by-merchant*', route => route.fulfill({ json: mockData.merchants }))
    await page.route('**/api/analytics/trends*', route => route.fulfill({ json: mockData.trends }))
    await page.route('**/api/analytics/subscriptions*', route => route.fulfill({ json: mockData.subscriptions }))
    await page.route('**/api/transactions/*', route => route.fulfill({ json: mockData.transactions }))
    await page.route('**/api/sync/status', route => route.fulfill({ json: { accounts: [] } }))
    
    await page.goto('/')
    
    // Should show $0 for failed data
    await expect(page.getByText('$0.00')).toBeVisible()
  })

  test('handles network timeout', async ({ page }) => {
    await page.route('**/api/analytics/summary*', route => route.abort('timedout'))
    await page.route('**/api/analytics/by-category*', route => route.fulfill({ json: mockData.categories }))
    await page.route('**/api/analytics/by-merchant*', route => route.fulfill({ json: mockData.merchants }))
    await page.route('**/api/analytics/trends*', route => route.fulfill({ json: mockData.trends }))
    await page.route('**/api/analytics/subscriptions*', route => route.fulfill({ json: mockData.subscriptions }))
    await page.route('**/api/transactions/*', route => route.fulfill({ json: mockData.transactions }))
    await page.route('**/api/sync/status', route => route.fulfill({ json: { accounts: [] } }))
    
    await page.goto('/')
    
    // Page should still render
    await expect(page.getByRole('heading', { name: /Finance Dashboard/i })).toBeVisible()
  })

  test('handles malformed JSON response', async ({ page }) => {
    await page.route('**/api/analytics/summary*', route => route.fulfill({ 
      status: 200, 
      body: 'not valid json',
      contentType: 'application/json'
    }))
    await page.route('**/api/analytics/by-category*', route => route.fulfill({ json: mockData.categories }))
    await page.route('**/api/analytics/by-merchant*', route => route.fulfill({ json: mockData.merchants }))
    await page.route('**/api/analytics/trends*', route => route.fulfill({ json: mockData.trends }))
    await page.route('**/api/analytics/subscriptions*', route => route.fulfill({ json: mockData.subscriptions }))
    await page.route('**/api/transactions/*', route => route.fulfill({ json: mockData.transactions }))
    await page.route('**/api/sync/status', route => route.fulfill({ json: { accounts: [] } }))
    
    await page.goto('/')
    
    // Page should still render without crashing
    await expect(page.getByRole('heading', { name: /Finance Dashboard/i })).toBeVisible()
  })
})

test.describe('API Response Caching', () => {
  test('uses cached data on navigation', async ({ page }) => {
    let callCount = 0
    await page.route('**/api/analytics/summary*', route => {
      callCount++
      return route.fulfill({ json: mockData.summary })
    })
    await page.route('**/api/analytics/by-category*', route => route.fulfill({ json: mockData.categories }))
    await page.route('**/api/analytics/by-merchant*', route => route.fulfill({ json: mockData.merchants }))
    await page.route('**/api/analytics/trends*', route => route.fulfill({ json: mockData.trends }))
    await page.route('**/api/analytics/subscriptions*', route => route.fulfill({ json: mockData.subscriptions }))
    await page.route('**/api/transactions/*', route => route.fulfill({ json: mockData.transactions }))
    await page.route('**/api/sync/status', route => route.fulfill({ json: { accounts: [] } }))
    
    await page.goto('/')
    await page.waitForTimeout(100)
    const initialCount = callCount
    
    // Interact without changing date range
    await page.getByText('Groceries').click()
    await page.waitForTimeout(100)
    
    // Summary should not be refetched (same date range)
    expect(callCount).toBe(initialCount)
  })
})

test.describe('Concurrent API Requests', () => {
  test('handles multiple simultaneous requests', async ({ page }) => {
    const requestTimes: number[] = []
    
    await page.route('**/api/**', route => {
      requestTimes.push(Date.now())
      if (route.request().url().includes('summary')) {
        return route.fulfill({ json: mockData.summary })
      }
      if (route.request().url().includes('by-category')) {
        return route.fulfill({ json: mockData.categories })
      }
      if (route.request().url().includes('by-merchant')) {
        return route.fulfill({ json: mockData.merchants })
      }
      if (route.request().url().includes('trends')) {
        return route.fulfill({ json: mockData.trends })
      }
      if (route.request().url().includes('subscriptions')) {
        return route.fulfill({ json: mockData.subscriptions })
      }
      if (route.request().url().includes('transactions')) {
        return route.fulfill({ json: mockData.transactions })
      }
      if (route.request().url().includes('sync/status')) {
        return route.fulfill({ json: { accounts: [] } })
      }
      return route.continue()
    })
    
    await page.goto('/')
    await page.waitForTimeout(200)
    
    // Multiple requests should be made
    expect(requestTimes.length).toBeGreaterThan(3)
  })
})
