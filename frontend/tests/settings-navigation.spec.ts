import { test, expect, type Page } from '@playwright/test'

async function setup(page: Page, options: { failSettings?: boolean; macos?: boolean; syncEnabled?: boolean; syncCompleted?: boolean; delayedDemoMode?: boolean } = {}) {
  const errors: string[] = []
  const writes: string[] = []
  let fail = options.failSettings ?? false
  page.on('pageerror', error => errors.push(error.message))
  await page.addInitScript(({ macos }) => {
    localStorage.setItem('finance-app-getting-started-done', 'true')
    if (macos) document.addEventListener('DOMContentLoaded', () => document.documentElement.classList.add('platform-macos'), { once: true })
  }, { macos: options.macos ?? false })
  await page.route('**/*', async route => {
    const url = new URL(route.request().url())
    if (!['127.0.0.1', 'localhost'].includes(url.hostname)) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.continue()
    if (route.request().method() !== 'GET' && !url.pathname.endsWith('/link-token')) writes.push(url.pathname)
    let json: unknown = {}
    if (url.pathname === '/api/settings/') {
      if (fail) return route.fulfill({ status: 503, json: { detail: 'Synthetic settings unavailable' } })
      // Keep loading visible long enough to verify retry state.
      await new Promise(resolve => setTimeout(resolve, 250))
      json = { stats: { total_transactions: 12, connected_accounts: 0 }, accounts: [], plaid_usage: { month_start: '2026-01-01', month_end_exclusive: '2026-02-01', total_estimated_cost: 0, billing_lines: [], devices: [], endpoints: [], scope: 'synthetic', scope_note: 'Synthetic usage only' }, vault: { connected: !options.delayedDemoMode, vault_id: 'synthetic-vault', provider: 'synthetic', provider_email: null, last_backup_at: null, google_drive_ready: true } }
    } else if (url.pathname === '/api/settings/device-sync/status') json = { enabled: options.syncEnabled ?? false, configured: true, reason: options.syncEnabled ? 'Device sync is enabled.' : 'Device sync is paused.', last_success: null, last_error: null, peer_count: 2, busy: false }
    else if (url.pathname === '/api/settings/device-sync/run') json = { ran: true, completed: options.syncCompleted ?? true }
    else if (url.pathname === '/api/settings/signout/preflight') json = { needs_backup: true, transaction_count: 12, last_backup_at: null }
    else if (url.pathname.includes('/snapshots')) json = { snapshots: [], retention: 7 }
    else if (url.pathname.includes('/analytics/summary')) json = { income: 100, spending: 50, core_spending: 50, core_spending_excluding_rent: 50, transfers: 0, spend_income_ratio: 50, net_flow: 50, total_balance: 100, subscriptions_monthly: 0 }
    else if (url.pathname.includes('/transactions')) json = { transactions: [], total: 0 }
    else if (url.pathname.includes('/sync/sidebar-accounts')) json = { accounts: [], exchange_rates: [] }
    else if (url.pathname === '/api/meta') {
      if (options.delayedDemoMode) await new Promise(resolve => setTimeout(resolve, 750))
      json = { mode: 'demo', database_path: 'synthetic' }
    }
    else if (url.pathname === '/api/splitwise/status') json = { connected: false, configured: false, credentials: { usable: false, auth_mode: 'none', api_key_present: false, client_id_present: false, client_secret_present: false } }
    else if (url.pathname === '/api/splitwise/links') json = { contacts: [] }
    else if (url.pathname.includes('/contacts')) json = [{ id: 'synthetic-person', name: 'Synthetic Person', color: '#123456' }]
    else if (url.pathname.includes('/projects') || url.pathname.includes('/analytics/') || url.pathname.includes('/sync/connected')) json = []
    return route.fulfill({ json })
  })
  await page.goto('/')
  const trigger = page.getByRole('button', { name: 'Settings', exact: true })
  await expect(trigger).toBeVisible()
  await trigger.click()
  const dialog = page.getByRole('dialog', { name: 'Settings', exact: true })
  await expect(dialog).toBeVisible()
  return { dialog, trigger, errors, writes, allowSettings: () => { fail = false } }
}

test('settings sections preserve unsaved form fields and focus across parent rerenders', async ({ page }) => {
  const { dialog, errors, writes } = await setup(page)
  const nav = dialog.getByRole('navigation', { name: 'Settings sections' })
  await nav.getByRole('button', { name: 'Sharing' }).click()
  const input = dialog.getByPlaceholder('paste API key')
  await input.fill('synthetic-unsaved-token')
  await nav.getByRole('button', { name: 'General' }).click()
  await expect(input).toBeHidden()
  const currency = dialog.getByLabel('Currency', { exact: true })
  await currency.focus()
  await currency.selectOption('EUR')
  await expect(currency).toBeFocused()
  await expect(dialog.getByText('Analytics totals will be shown in EUR')).toBeVisible()
  await nav.getByRole('button', { name: 'Sharing' }).click()
  await expect(input).toHaveValue('synthetic-unsaved-token')
  await nav.getByRole('button', { name: 'Banks' }).click()
  await expect(dialog.getByText('Connected Institutions', { exact: true })).toBeVisible()
  await expect(dialog.getByText('Danger Zone')).toBeHidden()
  expect(errors).toEqual([])
  expect(writes).toEqual([])
})

test('settings load failure is visible and retry shows loading then recovers', async ({ page }) => {
  const { dialog, allowSettings, errors } = await setup(page, { failSettings: true })
  await expect(dialog.getByRole('alert')).toContainText('Synthetic settings unavailable')
  allowSettings()
  await dialog.getByRole('button', { name: 'Try again' }).click()
  await expect(dialog.getByRole('status')).toHaveText('Loading settings…')
  await expect(dialog.getByRole('status')).toHaveCount(0)
  await expect(dialog.getByRole('alert')).toHaveCount(0)
  await expect(dialog.getByText('Total transactions: 12')).toBeVisible()
  expect(errors).toEqual([])
})

test('settings and nested signout contain tab focus, Escape restores each trigger', async ({ page }) => {
  const { dialog, trigger, errors, writes } = await setup(page)
  const close = dialog.getByRole('button', { name: 'Close settings', exact: true })
  await expect(close).toBeFocused()
  await page.keyboard.press('Shift+Tab')
  await expect(dialog.getByLabel('Currency', { exact: true })).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(close).toBeFocused()
  await dialog.getByRole('navigation').getByRole('button', { name: 'Backup', exact: true }).click()
  const signout = dialog.getByRole('button', { name: 'Sign Out', exact: true })
  await signout.click()
  const confirm = page.getByRole('alertdialog', { name: 'Sign Out' })
  await expect(confirm.getByRole('button', { name: 'Backup & Sign Out' })).toBeFocused()
  await page.keyboard.press('Shift+Tab')
  await expect(confirm.getByRole('button', { name: 'Cancel' })).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(confirm.getByRole('button', { name: 'Backup & Sign Out' })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(confirm).toHaveCount(0)
  await expect(signout).toBeFocused()
  await expect(dialog).toBeVisible()
  // Dismissing the nested backdrop must not bubble and close the Settings dialog.
  await signout.click()
  await expect(confirm).toBeVisible()
  await confirm.locator('..').click({ position: { x: 2, y: 2 } })
  await expect(confirm).toHaveCount(0)
  await expect(dialog).toBeVisible()
  await expect(signout).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
  await expect(trigger).toBeFocused()
  expect(errors).toEqual([])
  expect(writes).toEqual([])
})

for (const theme of ['light', 'dark']) {
  test(`macOS settings ${theme} layout stays usable on mobile`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 390, height: 844 })
    const { dialog, errors } = await setup(page, { macos: true })
    if (theme === 'light') {
      // Use the app command that the native shell uses for system appearance.
      await page.evaluate(() => window.__financeCommandBus?.dispatch('set:appearance', 'light'))
    }
    await expect(page.locator('html')).toHaveClass(/platform-macos/)
    await expect(page.locator('.app-shell')).toHaveCSS('background-image', 'none')
    if (theme === 'light') await expect(page.locator('html')).not.toHaveClass(/dark/)
    else await expect(page.locator('html')).toHaveClass(/dark/)
    await dialog.getByRole('navigation').getByRole('button', { name: 'Sharing' }).click()
    await expect(dialog.getByPlaceholder('paste API key')).toBeVisible()
    const bounds = await dialog.boundingBox()
    expect(bounds?.x).toBeGreaterThanOrEqual(0)
    expect((bounds?.x ?? 0) + (bounds?.width ?? 0)).toBeLessThanOrEqual(390)
    expect((bounds?.y ?? 0) + (bounds?.height ?? 0)).toBeLessThanOrEqual(844)
    expect(await dialog.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true)
    await page.screenshot({ path: testInfo.outputPath(`macos-settings-${theme}-mobile.png`), fullPage: true })
    await page.setViewportSize({ width: 1440, height: 1000 })
    await dialog.getByRole('navigation').getByRole('button', { name: 'General' }).click()
    await page.screenshot({ path: testInfo.outputPath(`macos-settings-${theme}-desktop.png`), fullPage: true })
    expect(errors).toEqual([])
  })
}

test('guided tour reveals Settings panes before spotlighting them and releases Imports', async ({ page }) => {
  const { dialog, errors } = await setup(page)
  await dialog.getByRole('button', { name: 'Close settings' }).click()
  await page.getByRole('button', { name: 'Getting Started', exact: true }).click()
  await page.getByRole('button', { name: 'Next', exact: true }).click()
  await page.getByRole('button', { name: 'Next', exact: true }).click()
  await page.locator('[data-tour="sidebar"] button').first().click()
  await expect(page.getByRole('heading', { name: 'Your Accounts', exact: true })).toBeVisible()
  for (let index = 0; index < 3; index++) await page.getByRole('button', { name: 'Next', exact: true }).click()
  await page.getByRole('button', { name: 'Settings', exact: true }).click()
  await expect(dialog.getByRole('heading', { name: 'Vault Backup', exact: true })).toBeVisible()
  await expect(page.locator('[data-tour="tour-vault-section"]')).toBeVisible()
  await page.getByRole('button', { name: 'Next', exact: true }).click()
  await expect(page.locator('[data-tour="tour-plaid-section"]')).toBeVisible()
  await page.getByRole('button', { name: 'Next', exact: true }).click()
  await expect(dialog).toHaveCount(0)
  await page.getByRole('button', { name: 'Imports', exact: true }).click()
  await expect(page.locator('[data-tour="tour-import-upload"]')).toBeVisible()
  expect(errors).toEqual([])
})

test('paused device sync is visible and cannot publish', async ({ page }) => {
  const { dialog, writes } = await setup(page)
  await dialog.getByRole('navigation').getByRole('button', { name: 'Backup', exact: true }).click()
  const sync = dialog.getByRole('region', { name: 'Device sync' })
  await expect(sync.getByText('Device sync is paused.')).toBeVisible()
  await expect(sync.getByRole('button', { name: 'Sync Devices Now' })).toBeDisabled()
  expect(writes).toEqual([])
})

for (const completed of [true, false]) {
  test(`manual device sync reports ${completed ? 'convergence' : 'attention required'}`, async ({ page }) => {
    const { dialog, writes, errors } = await setup(page, { syncEnabled: true, syncCompleted: completed })
    await dialog.getByRole('navigation').getByRole('button', { name: 'Backup', exact: true }).click()
    const sync = dialog.getByRole('region', { name: 'Device sync' })
    await sync.getByRole('button', { name: 'Sync Devices Now' }).click()
    if (completed) await expect(sync.getByRole('status')).toHaveText('Device sync completed.')
    else await expect(sync.getByRole('alert')).toContainText('Device sync needs attention.')
    expect(writes).toEqual(['/api/settings/device-sync/run'])
    expect(errors).toEqual([])
  })
}

test('period controls expose hover help, selected state and labeled date inputs', async ({ page }) => {
  const { dialog } = await setup(page)
  await dialog.getByRole('button', { name: 'Close settings', exact: true }).click()
  const allTime = page.getByRole('button', { name: 'All time', exact: true })
  await allTime.hover()
  await expect(allTime).toHaveAttribute('title', 'All time')
  await allTime.click()
  await expect(allTime).toHaveAttribute('aria-pressed', 'true')
  const choose = page.getByRole('button', { name: 'Choose date range' })
  await choose.click()
  await expect(choose).toHaveAttribute('aria-expanded', 'true')
  await expect(page.getByLabel('From', { exact: true })).toBeVisible()
  await expect(page.getByLabel('To', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Cancel', exact: true }).click()
  await expect(choose).toHaveAttribute('aria-expanded', 'false')
})


test('demo mode arriving after disconnected Settings never opens the live Google gate', async ({ page }) => {
  const { dialog, errors } = await setup(page, { delayedDemoMode: true })
  await expect(page.getByText('Demo mode is active. The app is using isolated sample data.')).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Connect your Google account', exact: true })).toHaveCount(0)
  await dialog.getByRole('button', { name: 'Close settings', exact: true }).click()
  await page.getByRole('button', { name: 'All time', exact: true }).click()
  expect(errors).toEqual([])
})
