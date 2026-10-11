import { test, expect, type Page } from '@playwright/test'

// These UI tests never reach a backend or provider. All financial values are synthetic.
type PreviewCase = { recordType: string; kind: string; payload: Record<string, unknown>; expected: string }
const previewCases: PreviewCase[] = [
  { recordType: 'transactions', kind: 'csv', payload: { beginning_balance: 100, ending_balance: 110 }, expected: 'Closing Balance' },
  { recordType: 'payslip', kind: 'payslip_pdf', payload: { employer: 'Synthetic Employer', pay_date: '2026-01-15', gross: 1200, net: 1050, total_taxes: 100, total_deductions: 50, taxes: { synthetic_tax: 100 }, deductions: { synthetic_benefit: 50 } }, expected: 'Pay-Cycle Snapshot' },
  { recordType: 'retirement', kind: 'retirement_csv', payload: { summary: { balance: 110, total_contributed: 10 }, transactions: [{ date: '2026-01-15', amount: 10, source_id: 'synthetic-activity', type: 'contribution' }] }, expected: 'Contributed' },
  { recordType: 'retirement', kind: 'retirement_statement_pdf', payload: { summary: { balance: 110, total_contributed: 10 }, statements: [{ period_start: '2026-01-01', period_end: '2026-01-31', employee_contributions: 10, employer_contributions: 0, market_change: 0, ending_balance: 110 }], activity: [] }, expected: 'Ending Balance' },
  { recordType: 'investment', kind: 'investment_csv', payload: { summary: { beginning_value: 100, ending_value: 110, change_in_investment: 10 }, holdings: [{ name: 'Synthetic Holding', value: 110 }] }, expected: 'Ending Value' },
]

const accounts = ['1001', '1002'].map((last4, index) => ({
  source: 'Synthetic Bank', account_key: `synthetic-account-${index}`, source_key: 'synthetic-bank',
  filter_source: 'synthetic-bank', account_last4: last4, group: 'bank_account',
  balance: 100, ledger_balance: 100, snapshot_balance: 100, connection_state: 'statement',
}))

async function mockApp(page: Page, previewCase = previewCases[0]) {
  const errors: string[] = []
  const keyWarnings: string[] = []
  const apiRequests: { path: string; method: string; search: string }[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => { if (/same key|unique.*key/i.test(message.text())) keyWarnings.push(message.text()) })
  await page.route('**/*', async route => {
    const url = new URL(route.request().url())
    if (!['127.0.0.1', 'localhost'].includes(url.hostname)) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.continue()
    apiRequests.push({ path: url.pathname, method: route.request().method(), search: url.search })
    let json: unknown = {}
    if (url.pathname === '/api/imports/sources') {
      json = [{ value: 'synthetic-source', label: 'Synthetic Account', group: 'Synthetic', hint: 'Synthetic test data', allowed_kinds: previewCase.kind === 'retirement_statement_pdf' ? ['retirement_csv', 'retirement_statement_pdf'] : [previewCase.kind] }]
    } else if (url.pathname === '/api/imports/preview') {
      json = { import_id: 'synthetic-import', filename: 'synthetic-statement', file_hash: 'synthetic-file-hash-000000', source: 'Synthetic Account', source_key: 'synthetic-source', kind: previewCase.kind, record_type: previewCase.recordType, currency: 'EUR', path: 'synthetic', created_at: '2026-01-15T12:00:00Z', already_imported: false, transactions: [], payload: previewCase.payload, duplicate_summary: { total_transactions: 1, importable_count: 1, duplicate_count: 0, items: [] } }
    } else if (url.pathname === '/api/imports/synthetic-import/commit') {
      json = { status: 'success', import_id: 'synthetic-import', imported: 1, skipped: 0, duplicate_count: 0 }
    } else if (url.pathname.includes('/analytics/summary')) {
      json = { income: 100, spending: 50, core_spending: 50, core_spending_excluding_rent: 50, transfers: 0, spend_income_ratio: 50, net_flow: 50, total_balance: 200, subscriptions_monthly: 0, start_date: '2026-01-01', end_date: '2026-01-31' }
    } else if (url.pathname.includes('/analytics/') || url.pathname.includes('/projects') || url.pathname.includes('/contacts')) {
      json = []
    } else if (url.pathname.includes('/transactions')) {
      json = { transactions: [], total: 0, offset: 0, limit: 50 }
    } else if (url.pathname.includes('/sync/sidebar-accounts')) {
      json = { accounts, exchange_rates: [] }
    } else if (url.pathname === '/api/meta') {
      json = { mode: 'demo', database_path: 'synthetic' }
    } else if (url.pathname.includes('/settings')) {
      json = { stats: { total_transactions: 0, connected_accounts: 0, distinct_categories: 0 }, accounts: [], vault: { connected: false, vault_id: null, provider: null, provider_email: null, last_backup_at: null, google_drive_ready: false } }
    } else if (url.pathname.includes('/payslips/')) {
      json = { payslips: ['Synthetic Employer A', 'Synthetic Employer B'].map(employer => ({ employer, pay_date: '2026-01-15', gross: 1200, net: 1050, total_taxes: 100, total_deductions: 50, taxes: { federal: 100 }, deductions: { medical: 50 }, earnings: { regular: 1200 }, benefits: {} })), totals: { effective_tax_rate: 100 / 1200 } }
    }
    return route.fulfill({ json })
  })
  await page.addInitScript(() => localStorage.setItem('finance-app-getting-started-done', 'true'))
  await page.goto('/')
  await expect(page.locator('[data-tour="tour-imports"]')).toBeVisible()
  return { errors, keyWarnings, apiRequests }
}

for (const fixture of previewCases) {
  test(`${fixture.kind} preview renders source currency and can explicitly commit`, async ({ page }, testInfo) => {
    const { errors, apiRequests } = await mockApp(page, fixture)
    await page.locator('[data-tour="tour-imports"]').click()
    await page.getByRole('combobox', { name: 'Account' }).selectOption('synthetic-source')
    const extension = fixture.kind.endsWith('pdf') ? 'pdf' : 'csv'
    await page.locator('#import-file-input').setInputFiles({ name: `synthetic-statement.${extension}`, mimeType: extension === 'pdf' ? 'application/pdf' : 'text/csv', buffer: Buffer.from('SYNTHETIC UI ONLY') })
    await expect(page.getByRole('heading', { name: 'Imports', exact: true })).toBeVisible()
    await expect(page.getByText(fixture.expected, { exact: true }).first()).toBeVisible()
    const commit = page.getByRole('button', { name: 'Commit Import', exact: true })
    await expect(commit).toBeEnabled()
    await expect(page.getByText(/€110\.00|€1,200\.00/).first()).toBeVisible()
    if (fixture.recordType === 'payslip') {
      await expect(page.getByText('€100.00', { exact: true }).first()).toBeVisible()
      await expect(page.getByText('synthetic tax', { exact: true })).toBeVisible()
    }
    expect(apiRequests.filter(r => r.path.endsWith('/commit'))).toHaveLength(0)
    await page.screenshot({ path: testInfo.outputPath('preview.png'), fullPage: true })
    await commit.click()
    await expect(page.getByText('Commit complete: imported 1, skipped 0.')).toBeVisible()
    expect(apiRequests.filter(r => r.path.endsWith('/commit'))).toHaveLength(1)
    await expect(commit).toBeDisabled()
    await page.getByRole('button', { name: 'Remove', exact: true }).click()
    await expect(commit).toHaveCount(0)
    expect(errors).toEqual([])
  })
}

test('same-source accounts retain separate sidebar entries and one source filter', async ({ page }, testInfo) => {
  const { errors, keyWarnings, apiRequests } = await mockApp(page)
  const accountFilter = page.locator('select').first()
  await expect(accountFilter.locator('option[value="synthetic-bank"]')).toHaveCount(1)
  await page.locator('[data-tour="sidebar"] button').first().click()
  const bankButtons = page.getByRole('button', { name: /Synthetic Bank/ })
  await expect(bankButtons).toHaveCount(2)
  await expect(bankButtons.first()).toContainText('1001')
  await expect(bankButtons.last()).toContainText('1002')
  await accountFilter.selectOption('synthetic-bank')
  await expect.poll(() => apiRequests.some(r => r.path.includes('/transactions') && new URLSearchParams(r.search).get('source') === 'synthetic-bank')).toBe(true)
  await expect(bankButtons.first()).toHaveClass(/app-selected/)
  await expect(bankButtons.last()).toHaveClass(/app-selected/)
  await bankButtons.first().click()
  await expect(accountFilter).toHaveValue('')
  await expect(bankButtons.first()).not.toHaveClass(/app-selected/)
  await expect(bankButtons.last()).not.toHaveClass(/app-selected/)
  await page.screenshot({ path: testInfo.outputPath('account-filter.png'), fullPage: true })
  expect(errors).toEqual([])
  expect(keyWarnings).toEqual([])
})

test('payroll chart aggregates same-date payslips and survives privacy and currency changes', async ({ page }, testInfo) => {
  const { errors } = await mockApp(page)
  await page.locator('select').filter({ has: page.locator('option[value="EUR"]') }).selectOption('EUR')
  await page.getByTitle('Payroll', { exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Selected Pay Cycle' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Employer Comparison' })).toBeVisible()
  await expect(page.getByText('1 payslips', { exact: true })).toHaveCount(2)
  await expect(page.getByText('€1,200.00', { exact: true }).first()).toBeVisible()
  await page.getByTitle('Enable privacy mode').click()
  await expect(page.getByText('xxx payslips', { exact: true })).toHaveCount(2)
  await page.getByTitle('Disable privacy mode').click()
  await page.screenshot({ path: testInfo.outputPath('payroll.png'), fullPage: true })
  expect(errors).toEqual([])
})
