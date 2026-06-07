import { chromium } from 'playwright'

const BASE_URL = process.env.BASE_URL || 'http://127.0.0.1:5173/'
const MODIFIER = process.platform === 'darwin' ? 'Meta' : 'Control'
const CDP_URL = process.env.CDP_URL || 'http://127.0.0.1:9222'

async function dismissGettingStarted(page) {
  await page.evaluate(() => {
    const close = Array.from(document.querySelectorAll('button')).find((el) => (el.textContent || '').trim() === 'Close')
    if (close) close.click()
  })
  await page.waitForTimeout(300)
}

async function expandSidebar(page) {
  await page.locator('button').first().click()
  await page.waitForTimeout(300)
}

async function openSidebarPage(page, label) {
  await page.getByRole('button', { name: new RegExp(label, 'i') }).click()
  await page.waitForTimeout(1000)
}

async function main() {
  let browser
  try {
    browser = await chromium.connectOverCDP(CDP_URL)
  } catch {
    try {
      browser = await chromium.launch({ channel: 'chrome', headless: true })
    } catch {
      browser = await chromium.launch({ headless: true })
    }
  }
  const existingContext = browser.contexts?.()[0]
  const context = existingContext || await browser.newContext()
  const page = await context.newPage()
  const errors = []

  page.on('pageerror', (err) => errors.push(err.message))
  page.on('console', (msg) => {
    if (msg.type() === 'error' && !msg.text().includes('ERR_BLOCKED_BY_RESPONSE.NotSameOrigin')) {
      errors.push(`console:${msg.text()}`)
    }
  })

  await page.goto(BASE_URL, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1200)
  await dismissGettingStarted(page)

  const bodyBefore = await page.locator('body').innerText()
  if (!bodyBefore.includes('I/O Flow')) {
    throw new Error('Dashboard did not render')
  }

  const chartCanvas = page.locator('canvas').first()
  await chartCanvas.hover({ force: true })
  await page.keyboard.down(MODIFIER)
  await page.mouse.wheel(0, -400)
  await page.keyboard.up(MODIFIER)
  await page.waitForTimeout(600)

  const minimap = page.locator('canvas').nth(1)
  if (await minimap.count()) {
    await minimap.hover({ force: true })
    await page.keyboard.down(MODIFIER)
    await page.mouse.wheel(0, -300)
    await page.keyboard.up(MODIFIER)
    await page.waitForTimeout(600)
  }

  await expandSidebar(page)
  await openSidebarPage(page, 'Projects')
  const projectsHeading = await page.locator('h1').first().textContent()
  await page.goBack()
  await page.waitForTimeout(800)

  await expandSidebar(page)
  await openSidebarPage(page, 'Payroll')
  const payrollHeading = await page.locator('h1').first().textContent()
  await page.goBack()
  await page.waitForTimeout(800)

  await expandSidebar(page)
  await openSidebarPage(page, 'Net Worth')
  const netWorthHeading = await page.locator('h1').first().textContent()
  await page.goBack()
  await page.waitForTimeout(800)

  console.log(
    JSON.stringify({
      ok: errors.length === 0,
      baseUrl: BASE_URL,
      cdpUrl: CDP_URL,
      projectsHeading,
      payrollHeading,
      netWorthHeading,
      errors,
    })
  )

  await browser.close()
}

main().catch((error) => {
  console.error(error)
  process.exit(1)
})
