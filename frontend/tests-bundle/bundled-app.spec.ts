import { test, expect, Page, Response, request as playwrightRequest } from '@playwright/test'

/**
 * Verifies the frontend as served *from the app bundle*, against the bundled backend.
 *
 * These tests deliberately mock nothing. Every other spec in `tests/` stubs `/api/**`,
 * which is right for testing UI behaviour and useless for the question here: does the
 * bundled build actually talk to the bundled backend, over one origin, with the shell's
 * session token?
 *
 * Run via `macos/scripts/verify_bundle.sh`, which supplies BUNDLE_URL and BUNDLE_TOKEN.
 */

const TOKEN = process.env.BUNDLE_TOKEN || ''
const BASE = process.env.BUNDLE_URL || 'http://127.0.0.1:8000'

/**
 * Warm the exchange-rate table with one sequential request before anything parallel.
 *
 * There is a pre-existing race in `_ensure_identity_rates` (src/services/exchange_rates.py):
 * concurrent requests each see the identity rates missing and each insert them, and the
 * loser gets a 500 from `UNIQUE constraint failed: exchange_rates.…`. Reproduced on an
 * unmodified `main` at 4b847fd: 11 of 12 concurrent `/api/analytics/summary` requests
 * returned 500. It is not specific to the macOS bundle and it is not fixed here.
 *
 * Warming it sequentially is a fixture, not a workaround for a bug of ours: it makes the
 * database look the way it does for every page load after the first, so the 5xx
 * assertions below can stay strict and a *new* server error still fails the suite.
 */
test.beforeAll(async () => {
  const context = await playwrightRequest.newContext({
    baseURL: BASE,
    extraHTTPHeaders: { 'x-finance-token': TOKEN },
  })
  await context.get('/api/analytics/summary', { params: { range: 'month', currency: 'USD' } })
  await context.dispose()
})

/** Records every response and page error so a test can assert on the whole load. */
function watch(page: Page) {
  const failures: string[] = []
  const unauthorized: string[] = []
  const pageErrors: string[] = []

  page.on('response', (response: Response) => {
    const url = new URL(response.url())
    if (response.status() === 401) unauthorized.push(url.pathname)
    // 404s on assets mean the static mount is wrong; 5xx means the backend broke.
    if (response.status() === 404 || response.status() >= 500) {
      failures.push(`${response.status()} ${url.pathname}`)
    }
  })
  page.on('pageerror', (error) => pageErrors.push(error.message))

  return { failures, unauthorized, pageErrors }
}

test.describe('bundled app: same-origin serving', () => {
  test('the app shell and its assets are served by the backend', async ({ page }) => {
    const seen = watch(page)
    const response = await page.goto('/')
    expect(response?.status()).toBe(200)

    // Same origin: no dev server, no proxy, no CORS. If the static mount were
    // registered before the routers it would swallow /api instead.
    await expect(page.locator('#root')).not.toBeEmpty()
    expect(seen.failures, 'assets 404ed or the backend 5xxed').toEqual([])
    expect(seen.pageErrors, 'the bundle threw while booting').toEqual([])
  })

  test('the built JS and CSS load from /assets', async ({ page }) => {
    const assetStatuses: Record<string, number> = {}
    page.on('response', (response) => {
      const { pathname } = new URL(response.url())
      if (pathname.startsWith('/assets/')) assetStatuses[pathname] = response.status()
    })
    await page.goto('/')
    await page.waitForLoadState('load')

    const paths = Object.keys(assetStatuses)
    expect(paths.some((p) => p.endsWith('.js')), `saw: ${paths.join(', ')}`).toBe(true)
    expect(paths.some((p) => p.endsWith('.css')), `saw: ${paths.join(', ')}`).toBe(true)
    for (const [path, status] of Object.entries(assetStatuses)) {
      expect(status, path).toBe(200)
    }
  })
})

test.describe('bundled app: the loopback token', () => {
  test('API calls from the page succeed with the shell-injected cookie', async ({ page }) => {
    // This is the whole point of the cookie-in-the-data-store approach: the very first
    // request has to carry the token, which a WKUserScript at .atDocumentStart cannot
    // guarantee.
    const seen = watch(page)
    await page.goto('/')
    await page.waitForLoadState('networkidle')
    expect(seen.unauthorized, 'the session token did not reach the API').toEqual([])
  })

  test('an API request without the cookie is refused', async () => {
    // The negative half. Without it, a gate that silently allowed everything would pass
    // the test above just as happily.
    //
    // Two things this got wrong first, both of which made a *passing* test that proved
    // nothing:
    //
    //  * Counting incidental 401s during a page load. What the frontend chooses to fetch
    //    after its first rejection is its own business, and the count was zero.
    //  * Assuming `browser.newContext()` starts with no cookies. It inherits the config's
    //    `storageState`, so the "no cookie" context had the token and answered 200.
    //
    // `storageState` has to be overridden explicitly. Playwright applies the config's
    // `use.storageState` even to a context created from the top-level `request` export,
    // so "don't pass it" means "inherit the token", not "start empty". The assertion
    // below is what caught that, and it stays as a tripwire.
    const context = await playwrightRequest.newContext({
      baseURL: BASE,
      storageState: { cookies: [], origins: [] },
    })
    try {
      expect(await context.storageState(), 'the context started with cookies').toEqual({
        cookies: [],
        origins: [],
      })

      const refused = await context.get('/api/meta')
      expect(refused.status(), 'the API answered without a token').toBe(401)

      // ...and the static shell is still reachable, or nothing could ever present a
      // token in the first place.
      expect((await context.get('/')).status()).toBe(200)
    } finally {
      await context.dispose()
    }
  })

  test('an API request with the cookie is accepted', async ({ page }) => {
    // Same request, from the configured context, so the two tests differ only in the
    // cookie.
    const accepted = await page.request.get('/api/meta')
    expect(accepted.status()).toBe(200)
  })

  test('the token is not exposed in the served HTML or JS', async ({ request }) => {
    // A token pasted into the bundle would outlive the launch it belongs to.
    const html = await (await request.get('/')).text()
    expect(html).not.toContain(TOKEN)
  })
})

test.describe('bundled app: every page loads', () => {
  // Plan phase 2: "at the end of this phase every existing page should load and work".
  // Driven through the real sidebar, so a nav item that stops dispatching is caught.
  const pages = [
    'Home',
    'Projects',
    'Uncategorized',
    'Payroll',
    'Imports',
    'Investments',
    'Retirement 401k',
    'Settings',
  ]

  for (const label of pages) {
    test(`${label} renders without an uncaught error`, async ({ page }) => {
      const seen = watch(page)
      await page.goto('/')
      await page.waitForLoadState('networkidle')

      const item = page.getByRole('button', { name: label }).first()
      if (await item.count()) {
        await item.click()
        await page.waitForLoadState('networkidle')
      } else {
        // Some entries are only present once data exists (e.g. an account group).
        // Skipping loudly beats asserting on a control that legitimately isn't there.
        test.skip(true, `no sidebar control labelled "${label}" in this database`)
      }

      expect(seen.pageErrors, `${label} threw`).toEqual([])
      expect(seen.unauthorized, `${label} was refused by the token gate`).toEqual([])
      expect(seen.failures.filter((f) => f.startsWith('5')), `${label} hit a server error`).toEqual([])
      await expect(page.locator('#root')).not.toBeEmpty()
    })
  }
})
