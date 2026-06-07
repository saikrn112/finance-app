import { test, expect, Page } from '@playwright/test'

// Mock API responses
const mockSummary = {
  income: 5000,
  spending: 2500,
  transfers: 500,
  spend_income_ratio: 50,
  net_flow: 2000,
  subscriptions_monthly: 150,
  range: 'month',
  start_date: '2026-02-01',
  end_date: '2026-02-28',
}

const mockCategories = [
  { category: 'Groceries', total: 800 },
  { category: 'Dining', total: 600 },
  { category: 'Shopping', total: 500 },
  { category: 'Transportation', total: 300 },
  { category: 'Subscriptions', total: 150 },
]

const mockMerchants = [
  { merchant: 'Amazon', total: 450 },
  { merchant: 'Whole Foods', total: 350 },
  { merchant: 'Uber Eats', total: 280 },
  { merchant: 'Target', total: 200 },
  { merchant: 'Starbucks', total: 120 },
]

const mockTrends = [
  { period: '2026-02-01', total: 300 },
  { period: '2026-02-02', total: 450 },
  { period: '2026-02-03', total: 200 },
  { period: '2026-02-04', total: 380 },
  { period: '2026-02-05', total: 520 },
]

const mockSubscriptions = [
  { merchant: 'Netflix', amount: 15.99, frequency: 'monthly', last_charge: '2026-02-01', occurrences: 12 },
  { merchant: 'Spotify', amount: 10.99, frequency: 'monthly', last_charge: '2026-02-05', occurrences: 24 },
  { merchant: 'iCloud', amount: 2.99, frequency: 'monthly', last_charge: '2026-02-03', occurrences: 36 },
]

const mockTransactions = {
  transactions: [
    { id: '1', date: '2026-02-06', amount: -87.32, merchant_raw: 'WHOLE FOODS', merchant_clean: 'Whole Foods', category: 'Groceries', source: 'chase', account_last4: '4521', is_recurring: false, tags: [] },
    { id: '2', date: '2026-02-05', amount: -45.00, merchant_raw: 'SHELL GAS', merchant_clean: 'Shell', category: 'Transportation', source: 'amex', account_last4: '8832', is_recurring: false, tags: [] },
    { id: '3', date: '2026-02-05', amount: -15.99, merchant_raw: 'NETFLIX', merchant_clean: 'Netflix', category: 'Subscriptions', source: 'chase', account_last4: '4521', is_recurring: true, tags: [] },
    { id: '4', date: '2026-02-04', amount: -124.50, merchant_raw: 'AMAZON', merchant_clean: 'Amazon', category: 'Shopping', source: 'discover', account_last4: '3345', is_recurring: false, tags: [] },
    { id: '5', date: '2026-02-03', amount: 2600.00, merchant_raw: 'PAYROLL', merchant_clean: 'Payroll', category: 'Salary/Paycheck', source: 'bofa', account_last4: '9901', is_recurring: true, tags: [] },
  ],
  total: 5,
  offset: 0,
  limit: 50,
}

async function setupMocks(page: Page) {
  await page.route('**/api/analytics/summary*', route => route.fulfill({ json: mockSummary }))
  await page.route('**/api/analytics/by-category*', route => route.fulfill({ json: mockCategories }))
  await page.route('**/api/analytics/by-merchant*', route => route.fulfill({ json: mockMerchants }))
  await page.route('**/api/analytics/trends*', route => route.fulfill({ json: mockTrends }))
  await page.route('**/api/analytics/subscriptions*', route => route.fulfill({ json: mockSubscriptions }))
  await page.route('**/api/transactions/*', route => route.fulfill({ json: mockTransactions }))
  await page.route('**/api/sync/status', route => route.fulfill({ json: { accounts: [] } }))
}

test.describe('Summary Cards', () => {
  test.beforeEach(async ({ page }) => {
    await setupMocks(page)
    await page.goto('/')
  })

  test('displays income value', async ({ page }) => {
    await expect(page.getByText('$5,000.00')).toBeVisible()
  })

  test('displays spending value', async ({ page }) => {
    await expect(page.getByText('$2,500.00')).toBeVisible()
  })

  test('displays spend/income ratio', async ({ page }) => {
    await expect(page.getByText('50%')).toBeVisible()
  })

  test('displays subscription total', async ({ page }) => {
    await expect(page.getByText('$150.00/mo')).toBeVisible()
  })

  test('displays net flow', async ({ page }) => {
    await expect(page.getByText('$2,000.00')).toBeVisible()
  })

  test('highlights good spend/income ratio in green', async ({ page }) => {
    const ratioCard = page.locator('text=50%')
    await expect(ratioCard).toHaveClass(/text-green-600/)
  })

  test('highlights positive net flow in green', async ({ page }) => {
    const netFlowValue = page.locator('.text-xl.font-bold.text-green-600').filter({ hasText: '$2,000.00' })
    await expect(netFlowValue).toBeVisible()
  })
})

test.describe('Category Chart', () => {
  test.beforeEach(async ({ page }) => {
    await setupMocks(page)
    await page.goto('/')
  })

  test('displays all categories', async ({ page }) => {
    for (const cat of mockCategories) {
      await expect(page.getByText(cat.category)).toBeVisible()
    }
  })

  test('displays category amounts', async ({ page }) => {
    await expect(page.getByText('$800.00')).toBeVisible()
    await expect(page.getByText('$600.00')).toBeVisible()
  })

  test('clicking category filters transactions', async ({ page }) => {
    await page.getByText('Groceries').click()
    await expect(page.getByText('Clear filter: Groceries')).toBeVisible()
  })

  test('clicking same category twice clears filter', async ({ page }) => {
    await page.getByText('Groceries').click()
    await expect(page.getByText('Clear filter: Groceries')).toBeVisible()
    
    await page.getByText('Groceries').click()
    await expect(page.getByText('Clear filter:')).not.toBeVisible()
  })
})

test.describe('Top Merchants', () => {
  test.beforeEach(async ({ page }) => {
    await setupMocks(page)
    await page.goto('/')
  })

  test('displays merchant list', async ({ page }) => {
    await expect(page.getByText('1. Amazon')).toBeVisible()
    await expect(page.getByText('2. Whole Foods')).toBeVisible()
  })

  test('displays merchant amounts', async ({ page }) => {
    await expect(page.getByText('$450.00')).toBeVisible()
    await expect(page.getByText('$350.00')).toBeVisible()
  })

  test('shows top 5 merchants', async ({ page }) => {
    const merchantSection = page.locator('text=Top Merchants').locator('..')
    await expect(merchantSection.getByText('5. Starbucks')).toBeVisible()
  })
})

test.describe('Subscriptions Section', () => {
  test.beforeEach(async ({ page }) => {
    await setupMocks(page)
    await page.goto('/')
  })

  test('displays subscription list', async ({ page }) => {
    await expect(page.getByText('Netflix')).toBeVisible()
    await expect(page.getByText('Spotify')).toBeVisible()
    await expect(page.getByText('iCloud')).toBeVisible()
  })

  test('displays subscription amounts with frequency', async ({ page }) => {
    await expect(page.getByText('$15.99/mo')).toBeVisible()
    await expect(page.getByText('$10.99/mo')).toBeVisible()
  })
})

test.describe('Transactions Table', () => {
  test.beforeEach(async ({ page }) => {
    await setupMocks(page)
    await page.goto('/')
  })

  test('displays transaction rows', async ({ page }) => {
    await expect(page.getByText('Whole Foods')).toBeVisible()
    await expect(page.getByText('Shell')).toBeVisible()
    await expect(page.getByText('Netflix')).toBeVisible()
  })

  test('displays transaction dates', async ({ page }) => {
    await expect(page.getByText('2026-02-06')).toBeVisible()
    await expect(page.getByText('2026-02-05')).toBeVisible()
  })

  test('displays transaction amounts', async ({ page }) => {
    await expect(page.getByText('-$87.32')).toBeVisible()
    await expect(page.getByText('-$45.00')).toBeVisible()
  })

  test('displays positive amounts in green', async ({ page }) => {
    const incomeRow = page.locator('tr').filter({ hasText: 'Payroll' })
    await expect(incomeRow.locator('.text-green-600')).toBeVisible()
  })

  test('displays transaction categories', async ({ page }) => {
    const table = page.locator('table')
    await expect(table.getByText('Groceries')).toBeVisible()
    await expect(table.getByText('Transportation')).toBeVisible()
  })

  test('displays transaction sources', async ({ page }) => {
    await expect(page.getByRole('cell', { name: 'chase' })).toBeVisible()
    await expect(page.getByRole('cell', { name: 'amex' })).toBeVisible()
  })
})

test.describe('Trend Chart', () => {
  test.beforeEach(async ({ page }) => {
    await setupMocks(page)
    await page.goto('/')
  })

  test('displays trend section', async ({ page }) => {
    await expect(page.getByText('Spending Trend')).toBeVisible()
  })

  test('renders chart container', async ({ page }) => {
    const chartContainer = page.locator('.recharts-responsive-container')
    await expect(chartContainer).toBeVisible()
  })
})

test.describe('Date Range Filtering', () => {
  test.beforeEach(async ({ page }) => {
    await setupMocks(page)
    await page.goto('/')
  })

  test('changing date range triggers API calls', async ({ page }) => {
    let apiCalls: string[] = []
    await page.route('**/api/analytics/summary*', route => {
      apiCalls.push(route.request().url())
      return route.fulfill({ json: mockSummary })
    })

    await page.locator('select').selectOption('week')
    await page.waitForTimeout(100)
    
    expect(apiCalls.some(url => url.includes('range=week'))).toBeTruthy()
  })

  test('date range persists across interactions', async ({ page }) => {
    await page.locator('select').selectOption('year')
    await expect(page.locator('select')).toHaveValue('year')
    
    // Click a category
    await page.getByText('Groceries').click()
    
    // Date range should still be year
    await expect(page.locator('select')).toHaveValue('year')
  })
})

test.describe('Refresh Functionality', () => {
  test('refresh button triggers sync', async ({ page }) => {
    await setupMocks(page)
    
    let syncCalled = false
    await page.route('**/api/sync/plaid/sync', route => {
      syncCalled = true
      return route.fulfill({ json: { status: 'synced', added: 0 } })
    })
    
    await page.goto('/')
    await page.getByRole('button').filter({ has: page.locator('svg.lucide-refresh-cw') }).click()
    
    await page.waitForTimeout(100)
    expect(syncCalled).toBeTruthy()
  })
})

test.describe('Empty States', () => {
  test('shows no data message for empty categories', async ({ page }) => {
    await page.route('**/api/analytics/summary*', route => route.fulfill({ json: mockSummary }))
    await page.route('**/api/analytics/by-category*', route => route.fulfill({ json: [] }))
    await page.route('**/api/analytics/by-merchant*', route => route.fulfill({ json: [] }))
    await page.route('**/api/analytics/trends*', route => route.fulfill({ json: [] }))
    await page.route('**/api/analytics/subscriptions*', route => route.fulfill({ json: [] }))
    await page.route('**/api/transactions/*', route => route.fulfill({ json: { transactions: [], total: 0, offset: 0, limit: 50 } }))
    await page.route('**/api/sync/status', route => route.fulfill({ json: { accounts: [] } }))
    
    await page.goto('/')
    
    await expect(page.getByText('No data')).toBeVisible()
  })

  test('shows no transactions message', async ({ page }) => {
    await page.route('**/api/analytics/summary*', route => route.fulfill({ json: mockSummary }))
    await page.route('**/api/analytics/by-category*', route => route.fulfill({ json: mockCategories }))
    await page.route('**/api/analytics/by-merchant*', route => route.fulfill({ json: mockMerchants }))
    await page.route('**/api/analytics/trends*', route => route.fulfill({ json: mockTrends }))
    await page.route('**/api/analytics/subscriptions*', route => route.fulfill({ json: mockSubscriptions }))
    await page.route('**/api/transactions/*', route => route.fulfill({ json: { transactions: [], total: 0, offset: 0, limit: 50 } }))
    await page.route('**/api/sync/status', route => route.fulfill({ json: { accounts: [] } }))
    
    await page.goto('/')
    
    await expect(page.getByText('No transactions')).toBeVisible()
  })

  test('shows no subscriptions message', async ({ page }) => {
    await page.route('**/api/analytics/summary*', route => route.fulfill({ json: mockSummary }))
    await page.route('**/api/analytics/by-category*', route => route.fulfill({ json: mockCategories }))
    await page.route('**/api/analytics/by-merchant*', route => route.fulfill({ json: mockMerchants }))
    await page.route('**/api/analytics/trends*', route => route.fulfill({ json: mockTrends }))
    await page.route('**/api/analytics/subscriptions*', route => route.fulfill({ json: [] }))
    await page.route('**/api/transactions/*', route => route.fulfill({ json: mockTransactions }))
    await page.route('**/api/sync/status', route => route.fulfill({ json: { accounts: [] } }))
    
    await page.goto('/')
    
    await expect(page.getByText('No subscriptions detected')).toBeVisible()
  })
})

test.describe('API Error Handling', () => {
  test('handles summary API error gracefully', async ({ page }) => {
    await page.route('**/api/analytics/summary*', route => route.fulfill({ status: 500 }))
    await page.route('**/api/analytics/by-category*', route => route.fulfill({ json: mockCategories }))
    await page.route('**/api/analytics/by-merchant*', route => route.fulfill({ json: mockMerchants }))
    await page.route('**/api/analytics/trends*', route => route.fulfill({ json: mockTrends }))
    await page.route('**/api/analytics/subscriptions*', route => route.fulfill({ json: mockSubscriptions }))
    await page.route('**/api/transactions/*', route => route.fulfill({ json: mockTransactions }))
    await page.route('**/api/sync/status', route => route.fulfill({ json: { accounts: [] } }))
    
    await page.goto('/')
    
    // Page should still render without crashing
    await expect(page.getByRole('heading', { name: /Finance Dashboard/i })).toBeVisible()
  })
})

test.describe('Responsive Layout', () => {
  test('displays 5 summary cards in grid', async ({ page }) => {
    await setupMocks(page)
    await page.goto('/')
    
    const grid = page.locator('.grid.grid-cols-5')
    await expect(grid).toBeVisible()
  })

  test('displays 3-column chart grid', async ({ page }) => {
    await setupMocks(page)
    await page.goto('/')
    
    const chartGrid = page.locator('.grid.grid-cols-3')
    await expect(chartGrid).toBeVisible()
  })
})
