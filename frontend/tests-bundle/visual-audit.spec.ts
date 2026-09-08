import { test, expect, Page } from '@playwright/test'
import { mkdirSync } from 'node:fs'
import { writeFile } from 'node:fs/promises'

/**
 * Screenshots every page in both appearances, and reports the colours and spacing each one
 * actually computes to.
 *
 * The point is not the screenshots by themselves — it is the report. "Colours are all over
 * the place" is not something to fix by eye across 22 files and two themes; the useful
 * question is *which distinct values are in use for the same role*, and that is measurable.
 * So this collects every computed background, text colour, border colour, radius and
 * padding on the page, groups them, and writes JSON next to the images.
 *
 * Runs against the bundled backend via `macos/scripts/verify_bundle.sh`, with
 * `html.platform-macos` applied so the numbers match what the desktop app renders rather
 * than what a browser would.
 */

const OUT = 'test-results-bundle/visual-audit'

const VIEWS: Array<{ name: string; hash: string }> = [
  { name: 'dashboard', hash: '' },
  { name: 'net-worth', hash: '#net-worth' },
  { name: 'projects', hash: '#projects' },
  { name: 'recurring', hash: '#recurring' },
  { name: 'uncategorized', hash: '#uncategorized' },
  { name: 'payroll', hash: '#payroll' },
  { name: 'investments', hash: '#investments' },
  { name: 'retirement', hash: '#retirement' },
]

/** What the desktop shell injects, so the audit measures what the app renders. */
async function applyPlatformClass(page: Page) {
  // Playwright's init script can run before <html> is parsed, so documentElement may be
  // null. (WKUserScript at .atDocumentStart, which the real shell uses, does not have this
  // problem -- this guard is the harness's, not the app's.)
  await page.addInitScript(() => {
    const apply = () => {
      document.documentElement.classList.add('platform-macos')
      document.documentElement.style.setProperty('--titlebar-height', '28px')
    }
    if (document.documentElement) apply()
    else document.addEventListener('readystatechange', apply, { once: true })
  })
}

/**
 * Switch appearance through the command bus, exactly as the shell does.
 *
 * Not by seeding localStorage: the filter store is not persisted, so `dark` comes from its
 * default on every load and a seeded value would be ignored. Going through the bus also
 * means this audit exercises the same path the shell uses.
 */
async function setAppearance(page: Page, dark: boolean) {
  await page.waitForFunction(() => Boolean(window.__financeCommandBus))
  const handled = await page.evaluate(
    (mode) => window.__financeCommandBus!.dispatch('set:appearance', mode),
    dark ? 'dark' : 'light'
  )
  expect(handled, 'set:appearance is not registered').toBe(true)
  await expect
    .poll(() => page.evaluate(() => document.documentElement.classList.contains('dark')))
    .toBe(dark)
}

/**
 * Collect the distinct computed values in use, with counts.
 *
 * Transparent and fully-transparent values are dropped: they are not a colour choice, and
 * they would swamp the counts.
 */
async function collectStyleUsage(page: Page) {
  return page.evaluate(() => {
    const tally = (map: Map<string, number>, key: string | undefined) => {
      if (!key) return
      if (key === 'rgba(0, 0, 0, 0)' || key === 'transparent' || key === 'none') return
      map.set(key, (map.get(key) ?? 0) + 1)
    }
    const backgrounds = new Map<string, number>()
    const texts = new Map<string, number>()
    const borders = new Map<string, number>()
    const radii = new Map<string, number>()
    const paddings = new Map<string, number>()
    const gaps = new Map<string, number>()
    const fontSizes = new Map<string, number>()

    for (const element of Array.from(document.querySelectorAll<HTMLElement>('body *'))) {
      // Skip anything not actually rendered; its computed style is not what the user sees.
      const box = element.getBoundingClientRect()
      if (box.width === 0 || box.height === 0) continue
      const style = getComputedStyle(element)
      tally(backgrounds, style.backgroundColor)
      tally(texts, style.color)
      if (style.borderTopWidth !== '0px') tally(borders, style.borderTopColor)
      tally(radii, style.borderTopLeftRadius)
      for (const side of [style.paddingTop, style.paddingLeft] as string[]) {
        if (side !== '0px') tally(paddings, side)
      }
      if (style.display.includes('flex') || style.display.includes('grid')) {
        if (style.rowGap !== 'normal' && style.rowGap !== '0px') tally(gaps, style.rowGap)
      }
      tally(fontSizes, style.fontSize)
    }

    const sorted = (map: Map<string, number>) =>
      [...map.entries()].sort((a, b) => b[1] - a[1]).map(([value, count]) => ({ value, count }))

    return {
      backgrounds: sorted(backgrounds),
      texts: sorted(texts),
      borders: sorted(borders),
      radii: sorted(radii),
      paddings: sorted(paddings),
      gaps: sorted(gaps),
      fontSizes: sorted(fontSizes),
    }
  })
}

test.describe('visual audit', () => {
  test.beforeAll(() => {
    mkdirSync(OUT, { recursive: true })
  })

  for (const appearance of ['dark', 'light'] as const) {
    for (const view of VIEWS) {
      test(`${view.name} · ${appearance}`, async ({ browser }) => {
        const context = await browser.newContext({
          baseURL: process.env.BUNDLE_URL,
          extraHTTPHeaders: { 'x-finance-token': process.env.BUNDLE_TOKEN || '' },
          viewport: { width: 1440, height: 950 },
          deviceScaleFactor: 2,
          colorScheme: appearance,
        })
        const page = await context.newPage()
        await applyPlatformClass(page)

        const errors: string[] = []
        page.on('pageerror', (error) => errors.push(error.message))

        await page.goto(`/${view.hash}`)
        await page.waitForLoadState('networkidle')
        await setAppearance(page, appearance === 'dark')
        // Charts animate in; a screenshot taken during that is not comparable between runs.
        await page.waitForTimeout(1200)

        await page.screenshot({
          path: `${OUT}/${view.name}-${appearance}.png`,
          fullPage: false,
        })
        const usage = await collectStyleUsage(page)
        await writeFile(
          `${OUT}/${view.name}-${appearance}.json`,
          JSON.stringify(usage, null, 2)
        )

        expect(errors, `${view.name} threw`).toEqual([])
        await context.close()
      })
    }
  }
})
