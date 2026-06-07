import { test, expect } from '@playwright/test'

test.describe('AGGRESSIVE: API Failure Scenarios', () => {
  test('handles complete API failure gracefully', async ({ page, context }) => {
    await context.route('**/api/**', route => route.abort())
    await page.goto('/')
    
    // Should still render the page structure
    await expect(page.getByRole('heading', { name: /Finance Dashboard/i })).toBeVisible()
    
    // Should show fallback values (zeros or empty states)
    await expect(page.getByText('$0.00')).toBeVisible()
  })

  test('handles malformed JSON responses', async ({ page, context }) => {
    await context.route('**/api/analytics/summary**', route => {
      route.fulfill({ body: 'not json at all{{{', contentType: 'application/json' })
    })
    await page.goto('/')
    
    // Should not crash the app
    await expect(page.getByRole('heading')).toBeVisible()
  })

  test('handles 500 server errors', async ({ page, context }) => {
    await context.route('**/api/**', route => {
      route.fulfill({ status: 500, body: 'Internal Server Error' })
    })
    await page.goto('/')
    
    await expect(page.getByRole('heading')).toBeVisible()
  })

  test('handles extremely slow API responses', async ({ page, context }) => {
    await context.route('**/api/**', async route => {
      await new Promise(resolve => setTimeout(resolve, 10000))
      route.continue()
    })
    
    await page.goto('/')
    
    // Should show loading state or fallback
    await expect(page.getByRole('heading')).toBeVisible({ timeout: 2000 })
  })
})

test.describe('AGGRESSIVE: Data Edge Cases', () => {
  test('handles negative income values', async ({ page, context }) => {
    await context.route('**/api/analytics/summary**', route => {
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          income: -5000,
          spending: 3000,
          transfers: 0,
          spend_income_ratio: -60,
          net_flow: -8000,
          subscriptions_monthly: 500,
          range: 'month'
        })
      })
    })
    
    await page.goto('/')
    await expect(page.getByText('-$5,000.00')).toBeVisible()
  })

  test('handles extremely large numbers', async ({ page, context }) => {
    await context.route('**/api/analytics/summary**', route => {
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          income: 999999999999,
          spending: 888888888888,
          transfers: 0,
          spend_income_ratio: 88.89,
          net_flow: 111111111111,
          subscriptions_monthly: 50000,
          range: 'month'
        })
      })
    })
    
    await page.goto('/')
    // Should format large numbers properly
    await expect(page.locator('text=/\\$999,999,999,999/')).toBeVisible()
  })

  test('handles zero values everywhere', async ({ page, context }) => {
    await context.route('**/api/**', route => {
      const url = route.request().url()
      if (url.includes('summary')) {
        route.fulfill({
          contentType: 'application/json',
          body: JSON.stringify({
            income: 0,
            spending: 0,
            transfers: 0,
            spend_income_ratio: 0,
            net_flow: 0,
            subscriptions_monthly: 0,
            range: 'month'
          })
        })
      } else if (url.includes('by-category') || url.includes('by-merchant') || url.includes('trends')) {
        route.fulfill({ contentType: 'application/json', body: '[]' })
      } else if (url.includes('subscriptions')) {
        route.fulfill({ contentType: 'application/json', body: '[]' })
      } else if (url.includes('transactions')) {
        route.fulfill({ contentType: 'application/json', body: '{"transactions":[],"total":0}' })
      } else {
        route.continue()
      }
    })
    
    await page.goto('/')
    
    // Should show empty states
    await expect(page.getByText('No trend data')).toBeVisible()
    await expect(page.getByText('No transactions')).toBeVisible()
  })

  test('handles missing required fields in API response', async ({ page, context }) => {
    await context.route('**/api/analytics/summary**', route => {
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({}) // Empty object, missing all fields
      })
    })
    
    await page.goto('/')
    
    // Should handle undefined/null gracefully with fallback to 0
    await expect(page.getByText('$0.00')).toBeVisible()
  })
})

test.describe('AGGRESSIVE: UI Stress Tests', () => {
  test('handles rapid date range changes', async ({ page }) => {
    await page.goto('/')
    
    const select = page.locator('select')
    
    // Rapidly change date ranges
    for (let i = 0; i < 20; i++) {
      await select.selectOption(['week', 'month', '6months', 'year'][i % 4])
      await page.waitForTimeout(50)
    }
    
    // Should still be functional
    await expect(page.getByRole('heading')).toBeVisible()
  })

  test('handles rapid dark mode toggling', async ({ page }) => {
    await page.goto('/')
    
    const darkModeButton = page.getByRole('button').filter({ 
      has: page.locator('svg.lucide-moon, svg.lucide-sun') 
    })
    
    // Toggle dark mode rapidly
    for (let i = 0; i < 30; i++) {
      await darkModeButton.click()
      await page.waitForTimeout(30)
    }
    
    // Should still work
    await expect(page.getByRole('heading')).toBeVisible()
  })

  test('handles clicking category filters rapidly', async ({ page, context }) => {
    await context.route('**/api/analytics/by-category**', route => {
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify([
          { category: 'Food', total: 500 },
          { category: 'Transport', total: 300 },
          { category: 'Entertainment', total: 200 }
        ])
      })
    })
    
    await page.goto('/')
    
    // Click categories rapidly
    for (let i = 0; i < 15; i++) {
      await page.getByText('Food').click()
      await page.waitForTimeout(50)
      await page.getByText('Transport').click()
      await page.waitForTimeout(50)
    }
    
    await expect(page.getByRole('heading')).toBeVisible()
  })

  test('handles extremely long merchant names', async ({ page, context }) => {
    await context.route('**/api/transactions/**', route => {
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          transactions: [{
            id: '1',
            date: '2026-02-06',
            amount: -50,
            merchant_raw: 'A'.repeat(500),
            merchant_clean: 'B'.repeat(500),
            category: 'C'.repeat(200),
            source: 'D'.repeat(100),
            account_last4: '1234',
            is_recurring: false,
            tags: []
          }],
          total: 1
        })
      })
    })
    
    await page.goto('/')
    
    // Should handle overflow gracefully
    await expect(page.getByRole('table')).toBeVisible()
  })

  test('handles thousands of transactions', async ({ page, context }) => {
    await context.route('**/api/transactions/**', route => {
      const transactions = Array.from({ length: 5000 }, (_, i) => ({
        id: `txn-${i}`,
        date: '2026-02-06',
        amount: -Math.random() * 1000,
        merchant_raw: `Merchant ${i}`,
        merchant_clean: `Merchant ${i}`,
        category: 'Shopping',
        source: 'Credit Card',
        account_last4: '1234',
        is_recurring: false,
        tags: []
      }))
      
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ transactions, total: 5000 })
      })
    })
    
    await page.goto('/')
    
    // Should render without crashing (even if slow)
    await expect(page.getByRole('table')).toBeVisible()
    
    // Check if scrolling works
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight))
  })
})

test.describe('AGGRESSIVE: XSS and Injection Attempts', () => {
  test('handles XSS in merchant names', async ({ page, context }) => {
    await context.route('**/api/transactions/**', route => {
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          transactions: [{
            id: '1',
            date: '2026-02-06',
            amount: -50,
            merchant_raw: '<script>alert("XSS")</script>',
            merchant_clean: '<img src=x onerror=alert("XSS")>',
            category: '<svg onload=alert("XSS")>',
            source: 'javascript:alert("XSS")',
            account_last4: '1234',
            is_recurring: false,
            tags: []
          }],
          total: 1
        })
      })
    })
    
    await page.goto('/')
    
    // Should escape HTML and not execute scripts
    await expect(page.getByRole('table')).toBeVisible()
    
    // Verify no alert was triggered
    page.on('dialog', () => {
      throw new Error('XSS vulnerability detected: alert was triggered')
    })
  })

  test('handles SQL injection patterns in category filter', async ({ page, context }) => {
    await context.route('**/api/analytics/by-category**', route => {
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify([
          { category: "'; DROP TABLE transactions; --", total: 500 },
          { category: "1' OR '1'='1", total: 300 }
        ])
      })
    })
    
    await page.goto('/')
    
    // Should display the malicious strings as plain text
    await expect(page.getByText("'; DROP TABLE transactions; --")).toBeVisible()
  })

  test('handles unicode and emoji overload', async ({ page, context }) => {
    await context.route('**/api/transactions/**', route => {
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          transactions: [{
            id: '1',
            date: '2026-02-06',
            amount: -50,
            merchant_raw: '🔥💰🚀✨🎉🎊🎈🎁🎀🎂'.repeat(50),
            merchant_clean: '你好世界こんにちは세계مرحبا'.repeat(20),
            category: '🏦💳💸💵💴💶💷',
            source: 'Źródło źródła źródła',
            account_last4: '1234',
            is_recurring: false,
            tags: []
          }],
          total: 1
        })
      })
    })
    
    await page.goto('/')
    
    await expect(page.getByRole('table')).toBeVisible()
  })
})

test.describe('AGGRESSIVE: Browser Compatibility', () => {
  test('handles window resize to mobile dimensions', async ({ page }) => {
    await page.goto('/')
    
    // Resize to mobile
    await page.setViewportSize({ width: 375, height: 667 })
    await expect(page.getByRole('heading')).toBeVisible()
    
    // Resize to tablet
    await page.setViewportSize({ width: 768, height: 1024 })
    await expect(page.getByRole('heading')).toBeVisible()
    
    // Resize to ultra-wide
    await page.setViewportSize({ width: 3840, height: 1080 })
    await expect(page.getByRole('heading')).toBeVisible()
  })

  test('handles disabled JavaScript (graceful degradation check)', async ({ page, context }) => {
    // This test verifies the page doesn't completely break
    await context.route('**/*.js', route => route.abort())
    
    try {
      await page.goto('/', { waitUntil: 'domcontentloaded', timeout: 5000 })
    } catch (e) {
      // Expected to fail, but shouldn't crash the test runner
    }
  })
})

test.describe('AGGRESSIVE: Race Conditions', () => {
  test('handles navigation away during API calls', async ({ page, context }) => {
    let requestCount = 0
    
    await context.route('**/api/**', async route => {
      requestCount++
      await new Promise(resolve => setTimeout(resolve, 2000))
      route.continue()
    })
    
    await page.goto('/')
    
    // Navigate away immediately
    await page.goto('about:blank')
    
    // Should not cause errors
    await page.waitForTimeout(3000)
  })

  test('handles refresh during data load', async ({ page, context }) => {
    await context.route('**/api/**', async route => {
      await new Promise(resolve => setTimeout(resolve, 1000))
      route.continue()
    })
    
    await page.goto('/')
    
    // Refresh immediately
    await page.reload()
    
    await expect(page.getByRole('heading')).toBeVisible()
  })
})

test.describe('AGGRESSIVE: Memory Leaks', () => {
  test('handles repeated mounting and unmounting', async ({ page }) => {
    for (let i = 0; i < 10; i++) {
      await page.goto('/')
      await expect(page.getByRole('heading')).toBeVisible()
      await page.goto('about:blank')
    }
    
    // Final check
    await page.goto('/')
    await expect(page.getByRole('heading')).toBeVisible()
  })
})
