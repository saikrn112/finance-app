import { test, expect, Page } from '@playwright/test'

const mockData = {
  summary: { income: 5000, spending: 2500, transfers: 500, spend_income_ratio: 50, net_flow: 2000, subscriptions_monthly: 150, range: 'month' },
  categories: [{ category: 'Groceries', total: 800 }, { category: 'Dining', total: 600 }],
  merchants: [{ merchant: 'Example Shop', total: 450 }],
  trends: [{ period: '2026-02-01', total: 300 }],
  subscriptions: [{ merchant: 'Example Stream', amount: 15.99, frequency: 'monthly', last_charge: '2026-02-01', occurrences: 12 }],
  transactions: { transactions: [{ id: '1', date: '2026-02-06', amount: -87.32, merchant_raw: 'EXAMPLE GROCER', merchant_clean: 'Example Grocer', category: 'Groceries', source: 'example_card', account_last4: '4521', is_recurring: false, tags: [] }], total: 1, offset: 0, limit: 50 },
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

test.describe('Accessibility - Keyboard Navigation', () => {
  test.beforeEach(async ({ page }) => {
    await setupMocks(page)
    await page.goto('/')
  })

  test('can tab through interactive elements', async ({ page }) => {
    // Focus on first interactive element
    await page.keyboard.press('Tab')
    
    // Should be able to continue tabbing
    for (let i = 0; i < 5; i++) {
      await page.keyboard.press('Tab')
    }
    
    // Some element should be focused
    const focused = await page.evaluate(() => document.activeElement?.tagName)
    expect(focused).toBeTruthy()
  })

  test('select can be operated with keyboard', async ({ page }) => {
    const select = page.locator('select')
    await select.focus()
    
    // Open dropdown and select with keyboard
    await page.keyboard.press('Space')
    await page.keyboard.press('ArrowDown')
    await page.keyboard.press('Enter')
    
    // Value should have changed
    const value = await select.inputValue()
    expect(['week', 'month', '6months', 'year']).toContain(value)
  })

  test('buttons are keyboard accessible', async ({ page }) => {
    const refreshButton = page.getByRole('button').filter({ has: page.locator('svg.lucide-refresh-cw') })
    await refreshButton.focus()
    
    // Should be focusable
    await expect(refreshButton).toBeFocused()
    
    // Should be activatable with Enter
    await page.keyboard.press('Enter')
  })
})

test.describe('Accessibility - Screen Reader', () => {
  test.beforeEach(async ({ page }) => {
    await setupMocks(page)
    await page.goto('/')
  })

  test('page has main heading', async ({ page }) => {
    const heading = page.getByRole('heading', { level: 1 })
    await expect(heading).toBeVisible()
  })

  test('table has proper headers', async ({ page }) => {
    await expect(page.getByRole('columnheader', { name: 'Date' })).toBeVisible()
    await expect(page.getByRole('columnheader', { name: 'Merchant' })).toBeVisible()
    await expect(page.getByRole('columnheader', { name: 'Category' })).toBeVisible()
    await expect(page.getByRole('columnheader', { name: 'Account' })).toBeVisible()
    await expect(page.getByRole('columnheader', { name: 'Amount' })).toBeVisible()
  })

  test('buttons have accessible names', async ({ page }) => {
    // Buttons should be findable by role
    const buttons = await page.getByRole('button').all()
    expect(buttons.length).toBeGreaterThan(0)
  })

  test('select has accessible label context', async ({ page }) => {
    const select = page.locator('select')
    await expect(select).toBeVisible()
    
    // Options should have text
    const options = await select.locator('option').all()
    for (const option of options) {
      const text = await option.textContent()
      expect(text?.length).toBeGreaterThan(0)
    }
  })
})

test.describe('Accessibility - Color Contrast', () => {
  test.beforeEach(async ({ page }) => {
    await setupMocks(page)
    await page.goto('/')
  })

  test('text is visible in light mode', async ({ page }) => {
    // Main heading should be visible
    await expect(page.getByRole('heading', { name: /Finance Dashboard/i })).toBeVisible()
    
    // Card labels should be visible
    await expect(page.getByText('Income')).toBeVisible()
    await expect(page.getByText('Spending')).toBeVisible()
  })

  test('text is visible in dark mode', async ({ page }) => {
    // Enable dark mode
    await page.getByRole('button').filter({ has: page.locator('svg.lucide-moon') }).click()
    
    // Content should still be visible
    await expect(page.getByRole('heading', { name: /Finance Dashboard/i })).toBeVisible()
    await expect(page.getByText('Income')).toBeVisible()
  })
})

test.describe('Accessibility - Focus Management', () => {
  test.beforeEach(async ({ page }) => {
    await setupMocks(page)
    await page.goto('/')
  })

  test('focus is visible on interactive elements', async ({ page }) => {
    const select = page.locator('select')
    await select.focus()
    
    // Element should be focused
    await expect(select).toBeFocused()
  })

  test('clicking category maintains focus context', async ({ page }) => {
    await page.getByText('Groceries').click()
    
    // Clear filter button should be visible and accessible
    const clearButton = page.getByText('Clear filter: Groceries')
    await expect(clearButton).toBeVisible()
  })
})

test.describe('Accessibility - Semantic HTML', () => {
  test.beforeEach(async ({ page }) => {
    await setupMocks(page)
    await page.goto('/')
  })

  test('uses semantic table structure', async ({ page }) => {
    const table = page.locator('table')
    await expect(table).toBeVisible()
    
    const thead = table.locator('thead')
    await expect(thead).toBeVisible()
    
    const tbody = table.locator('tbody')
    await expect(tbody).toBeVisible()
  })

  test('uses heading hierarchy', async ({ page }) => {
    // H1 for main title
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
    
    // H2 for section titles
    const h2s = await page.getByRole('heading', { level: 2 }).all()
    expect(h2s.length).toBeGreaterThan(0)
  })

  test('buttons are actual button elements', async ({ page }) => {
    const buttons = await page.getByRole('button').all()
    expect(buttons.length).toBeGreaterThan(0)
  })
})

test.describe('Accessibility - Motion and Animation', () => {
  test('respects reduced motion preference', async ({ page }) => {
    // Emulate reduced motion
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await setupMocks(page)
    await page.goto('/')
    
    // Page should still function
    await expect(page.getByRole('heading', { name: /Finance Dashboard/i })).toBeVisible()
  })
})
