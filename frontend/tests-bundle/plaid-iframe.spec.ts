import { test, expect } from '@playwright/test'

/**
 * Third-party iframes must load in place, not be treated as navigation.
 *
 * `react-plaid-link` renders Plaid's hosted flow in an iframe on cdn.plaid.com. The shell's
 * navigation policy applied its off-origin rule to *every* frame, so each of those loads was
 * cancelled and handed to the system browser: opening Settings produced a dozen Safari tabs and no
 * working Link. The shell now only applies the rule to the main frame.
 *
 * Playwright cannot exercise WKWebView's policy handler, so this guards the other half — that
 * opening Settings really does try to load a cdn.plaid.com subresource, which is what makes the
 * shell-side check matter. The shell side is asserted by counting
 * "sent to the system browser" lines in shell.log after dispatching open:settings.
 */
test('opening Settings loads Plaid Link from cdn.plaid.com in a frame, not the main frame', async ({ page }) => {
  await page.addInitScript(() => {
    window.localStorage.setItem('finance-app-getting-started-done', 'true')
  })

  const plaidRequests: string[] = []
  const mainFrameNavigations: string[] = []
  page.on('request', (request) => {
    if (!request.url().includes('cdn.plaid.com')) return
    plaidRequests.push(request.url())
    // A main-frame document request to Plaid would mean the app was being replaced.
    if (request.isNavigationRequest() && request.frame() === page.mainFrame()) {
      mainFrameNavigations.push(request.url())
    }
  })

  await page.goto('/')
  await page.waitForLoadState('networkidle')
  await page.waitForFunction(() => Boolean(window.__financeCommandBus))
  expect(await page.evaluate(() => window.__financeCommandBus!.dispatch('open:settings'))).toBe(true)

  // Give Plaid's script time to load and build its iframe.
  await page.waitForTimeout(3000)

  expect(plaidRequests.length, 'Settings should reach for Plaid Link').toBeGreaterThan(0)
  // The load is never a main-frame navigation, so the shell has no business redirecting it.
  expect(mainFrameNavigations, 'Plaid must never navigate the main frame').toEqual([])
  // And the app is still the app.
  await expect(page.locator('#root')).not.toBeEmpty()
})
