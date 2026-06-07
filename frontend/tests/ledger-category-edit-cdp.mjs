import { chromium } from 'playwright'

async function editRowCategory(page, row, currentCategoryLabel, topCategory, subcategory) {
  await row.locator('button', { hasText: currentCategoryLabel }).first().click({ force: true })

  await page.getByText(/Edit category/i).waitFor()
  const selects = page.locator('select')
  const selectCount = await selects.count()
  const categorySelect = selects.nth(selectCount - 2)
  const subcategorySelect = selects.nth(selectCount - 1)

  await categorySelect.selectOption(topCategory)
  if (await subcategorySelect.count()) {
    await subcategorySelect.selectOption(subcategory || '')
  }

  await page.getByRole('button', { name: 'Save' }).last().click()
  await page.getByText(/Edit category/i).waitFor({ state: 'detached' })
}

async function main() {
  const browser = await chromium.connectOverCDP('http://127.0.0.1:9222')
  const context = browser.contexts()[0] ?? (await browser.newContext())
  const page = await context.newPage()

  try {
    await page.goto('http://127.0.0.1:5174', { waitUntil: 'networkidle' })
    await page.getByRole('heading', { name: /Transactions \(/ }).waitFor()

    const row = page.locator('.ag-row').filter({ hasText: 'Apple Store' }).first()
    await row.waitFor()

    await editRowCategory(page, row, 'Shopping', 'Dining', '')
    await row.waitFor({ state: 'visible' })
    const diningRowText = await row.textContent()
    if (!diningRowText?.includes('Dining') || !diningRowText.includes('Apple Store')) {
      throw new Error(`Expected Apple Store row to update to Dining, got: ${diningRowText}`)
    }

    const bodyAfterDining = await page.locator('body').textContent()
    if (!bodyAfterDining?.includes('Dining') || !bodyAfterDining.includes('-$249.00')) {
      throw new Error('Dashboard did not refresh after changing Apple Store to Dining')
    }

    await editRowCategory(page, row, 'Dining', 'Shopping', 'Tech')
    await row.waitFor({ state: 'visible' })
    const restoredRowText = await row.textContent()
    if (!restoredRowText?.includes('Shopping') || !restoredRowText.includes('Tech')) {
      throw new Error(`Expected Apple Store row to restore to Shopping/Tech, got: ${restoredRowText}`)
    }

    console.log(JSON.stringify({ ok: true, restored: true }, null, 2))
  } finally {
    await page.close()
    await browser.close()
  }
}

main().catch((error) => {
  console.error(error)
  process.exit(1)
})
