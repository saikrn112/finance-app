import { defineConfig } from '@playwright/test'

/**
 * Drives the frontend as it is served *from the app bundle*, against the bundled
 * backend — not against the Vite dev server.
 *
 * This is the automatable half of verifying the macOS app (see the plan's §7).
 * Playwright drives Chromium rather than WKWebView, so it proves the frontend/backend
 * contract and nothing about the shell: menus, window lifecycle and the OAuth return
 * still need a human or XCTest. That is a real limit, not a formality — but the
 * frontend/backend contract is where the expensive failures have been.
 *
 * `macos/scripts/verify_bundle.sh` starts the backend, exports BUNDLE_URL and
 * BUNDLE_TOKEN, and runs this. There is deliberately no `webServer` block: starting a
 * dev server here would silently test the thing this config exists to avoid.
 */
export default defineConfig({
  testDir: './tests-bundle',
  fullyParallel: false,
  reporter: [['list']],
  use: {
    baseURL: process.env.BUNDLE_URL || 'http://127.0.0.1:8000',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    // The loopback token gate rejects any /api request without this. Set as a cookie
    // because that is exactly how the shell hands it to WKWebView, so a mistake here
    // is a mistake in the real path too.
    storageState: {
      cookies: [
        {
          name: 'finance_token',
          value: process.env.BUNDLE_TOKEN || '',
          domain: '127.0.0.1',
          path: '/',
          expires: -1,
          httpOnly: false,
          secure: false,
          sameSite: 'Lax',
        },
      ],
      origins: [],
    },
  },
  outputDir: './test-results-bundle',
})
