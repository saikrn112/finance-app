import { chromium } from 'playwright'

function extractCurrency(text) {
  const match = text?.match(/\$[0-9,]+\.\d{2}/)
  return match ? match[0] : null
}

async function main() {
  const browser = await chromium.connectOverCDP('http://127.0.0.1:9222')
  const context = browser.contexts()[0] ?? (await browser.newContext())
  const page = await context.newPage()

  try {
    await page.goto('http://127.0.0.1:5174', { waitUntil: 'networkidle' })

    await page.locator('button').first().click()

    const netWorthButton = page.getByRole('button', { name: /Net Worth/ })
    const sidebarNetWorthText = await netWorthButton.textContent()
    const sidebarTotal = extractCurrency(sidebarNetWorthText)

    await page.getByRole('button', { name: 'Payroll' }).click()
    await page.getByRole('heading', { name: 'Payroll', exact: true }).waitFor()
    await page.getByRole('heading', { name: 'Selected Pay Cycle' }).waitFor()
    await page.getByRole('heading', { name: 'Employer Rollup' }).waitFor()

    await page.getByRole('button', { name: 'Back' }).click()

    await netWorthButton.click()
    await page.getByRole('heading', { name: 'Net Worth', exact: true }).waitFor()
    const latestTotalLabel = page.getByText(/Latest Total/i).first()
    const errorBanner = page.getByText(/Error loading net worth history/i)
    try {
      await Promise.race([latestTotalLabel.waitFor(), errorBanner.waitFor()])
    } catch (error) {
      const bodyText = await page.locator('body').textContent()
      console.error(bodyText)
      throw error
    }
    if (await errorBanner.count()) {
      const bodyText = await page.locator('body').textContent()
      throw new Error(`Net Worth page failed to load: ${bodyText}`)
    }
    await page.getByRole('heading', { name: 'Coverage Notes' }).waitFor()

    if (sidebarTotal) {
      await page.getByText(sidebarTotal, { exact: true }).first().waitFor()
    }

    await page.getByRole('button', { name: 'Back' }).click()

    await page.getByRole('button', { name: /more/i }).click()
    await page.getByRole('heading', { name: 'Recurring', exact: true }).waitFor()
    const recurringError = page.getByText(/Error loading recurring workspace/i)
    const recurringCatalog = page.getByRole('heading', { name: 'Catalog' })
    try {
      await Promise.race([recurringCatalog.waitFor(), recurringError.waitFor()])
    } catch (error) {
      const bodyText = await page.locator('body').textContent()
      console.error(bodyText)
      throw error
    }
    if (await recurringError.count()) {
      const bodyText = await page.locator('body').textContent()
      throw new Error(`Recurring page failed to load: ${bodyText}`)
    }
    await page.getByRole('heading', { name: 'Upcoming Renewals' }).waitFor()
    const recurringBody = await page.locator('body').textContent()
    const recurring = recurringBody?.includes('Aven Apartments') && recurringBody?.includes('Upcoming Renewals')

    console.log(
      JSON.stringify(
        {
          ok: true,
          payroll: true,
          netWorth: true,
          recurring,
          sidebarTotal,
        },
        null,
        2,
      ),
    )
  } finally {
    await page.close()
    await browser.close()
  }
}

main().catch((error) => {
  console.error(error)
  process.exit(1)
})
