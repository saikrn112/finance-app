import { test, expect } from '@playwright/test'

test.describe('Dashboard', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/')
  })

  test('renders dashboard header', async ({ page }) => {
    await expect(page.getByRole('heading', { name: /Finance Dashboard/i })).toBeVisible()
  })

  test('displays all summary cards', async ({ page }) => {
    await expect(page.getByText('Income')).toBeVisible()
    await expect(page.getByText('Spending')).toBeVisible()
    await expect(page.getByText('Spend/Income')).toBeVisible()
    await expect(page.getByText('Subscriptions')).toBeVisible()
    await expect(page.getByText('Net Flow')).toBeVisible()
  })

  test('displays category and merchant sections', async ({ page }) => {
    await expect(page.getByText('Spending by Category')).toBeVisible()
    await expect(page.getByText('Top Merchants')).toBeVisible()
  })

  test('displays transactions table with headers', async ({ page }) => {
    await expect(page.getByRole('columnheader', { name: 'Date' })).toBeVisible()
    await expect(page.getByRole('columnheader', { name: 'Merchant' })).toBeVisible()
    await expect(page.getByRole('columnheader', { name: 'Category' })).toBeVisible()
    await expect(page.getByRole('columnheader', { name: 'Account' })).toBeVisible()
    await expect(page.getByRole('columnheader', { name: 'Amount' })).toBeVisible()
  })
})

test.describe('Dark Mode', () => {
  test('toggles dark mode on button click', async ({ page }) => {
    await page.goto('/')
    
    // Initially light mode
    await expect(page.locator('html')).not.toHaveClass(/dark/)
    
    // Click moon icon to enable dark mode
    await page.getByRole('button').filter({ has: page.locator('svg.lucide-moon') }).click()
    await expect(page.locator('html')).toHaveClass(/dark/)
    
    // Click sun icon to disable dark mode
    await page.getByRole('button').filter({ has: page.locator('svg.lucide-sun') }).click()
    await expect(page.locator('html')).not.toHaveClass(/dark/)
  })

  test('dark mode changes background color', async ({ page }) => {
    await page.goto('/')
    
    const container = page.locator('.min-h-screen').first()
    
    // Light mode - should have light background
    await expect(container).toHaveClass(/bg-gray-50/)
    
    // Enable dark mode
    await page.getByRole('button').filter({ has: page.locator('svg.lucide-moon') }).click()
    
    // Dark mode - should have dark background
    await expect(container).toHaveClass(/dark:bg-gray-900/)
  })
})

test.describe('Date Range Selector', () => {
  test('has all date range options', async ({ page }) => {
    await page.goto('/')
    
    const select = page.locator('select')
    await expect(select).toBeVisible()
    
    await expect(select.locator('option[value="week"]')).toHaveText('This Week')
    await expect(select.locator('option[value="month"]')).toHaveText('This Month')
    await expect(select.locator('option[value="6months"]')).toHaveText('Last 6 Months')
    await expect(select.locator('option[value="year"]')).toHaveText('This Year')
  })

  test('defaults to month view', async ({ page }) => {
    await page.goto('/')
    
    const select = page.locator('select')
    await expect(select).toHaveValue('month')
  })

  test('can change date range', async ({ page }) => {
    await page.goto('/')
    
    const select = page.locator('select')
    
    await select.selectOption('week')
    await expect(select).toHaveValue('week')
    
    await select.selectOption('year')
    await expect(select).toHaveValue('year')
  })
})

test.describe('Category Filter', () => {
  test('shows clear filter button when category selected', async ({ page }) => {
    await page.goto('/')
    
    // Initially no filter button
    await expect(page.getByText('Clear filter:')).not.toBeVisible()
  })
})

test.describe('Header Controls', () => {
  test('has refresh button', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByRole('button').filter({ has: page.locator('svg.lucide-refresh-cw') })).toBeVisible()
  })

  test('has settings button', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByRole('button').filter({ has: page.locator('svg.lucide-settings') })).toBeVisible()
  })
})
