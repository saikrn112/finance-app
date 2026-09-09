import { test, expect, Page } from '@playwright/test'

/**
 * The feedback panel, driven against the bundled backend with no mocks.
 *
 * Covers what the shell's menu item can reach and what the user does next: write a note, attach a
 * screenshot, edit it, resolve it, delete it. The panel is opened through the command bus, which
 * is exactly how ⇧⌘F reaches it, so a broken registration fails here too.
 */

/** A real 2x2 PNG. Built rather than stubbed, because the API sniffs the bytes. */
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEUlEQVR4nGP8z8DAwMDAwAAADAAB/pJ9jwAAAABJRU5ErkJggg==',
  'base64'
)

/**
 * Suppress the getting-started tour.
 *
 * In demo mode the vault reads as connected, so the twelve-step tour opens on load and its
 * overlay sits at z-index 10000 intercepting every click — which presents as a click that
 * simply times out, with the target plainly visible in the screenshot.
 */
async function skipTour(page: Page) {
  await page.addInitScript(() => {
    window.localStorage.setItem('finance-app-getting-started-done', 'true')
    window.localStorage.setItem('finance-app-getting-started-dashboard-read', 'true')
  })
}

async function openPanel(page: Page) {
  await skipTour(page)
  await page.goto('/')
  await page.waitForLoadState('networkidle')
  await page.waitForFunction(() => Boolean(window.__financeCommandBus))
  const handled = await page.evaluate(() => window.__financeCommandBus!.dispatch('open:feedback'))
  expect(handled, 'open:feedback is not registered').toBe(true)
  await expect(page.getByRole('heading', { name: 'Feedback' })).toBeVisible()
}

/** Clear anything earlier tests left behind, so counts are predictable. */
async function deleteAllNotes(page: Page) {
  const list = await page.request.get('/api/feedback/')
  for (const note of (await list.json()).items) {
    await page.request.delete(`/api/feedback/${note.id}`)
  }
}

test.describe('feedback panel', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/')
    await deleteAllNotes(page)
  })

  test.afterEach(async ({ page }) => {
    await deleteAllNotes(page)
  })

  test('a note can be written and appears in the list', async ({ page }) => {
    await openPanel(page)
    const composer = page.getByPlaceholder(/What's wrong/)
    await composer.fill('The ledger scrolls oddly')
    await page.getByRole('button', { name: 'Add', exact: true }).click()

    await expect(page.getByText('The ledger scrolls oddly')).toBeVisible()
    // Numbered, and the number is shown — it is how a note gets referred to later.
    await expect(page.getByText('#1')).toBeVisible()
    await expect(page.getByText('1 open')).toBeVisible()
  })

  test('an existing note can be edited', async ({ page }) => {
    await openPanel(page)
    await page.getByPlaceholder(/What's wrong/).fill('origonal text')
    await page.getByRole('button', { name: 'Add', exact: true }).click()
    await expect(page.getByText('origonal text')).toBeVisible()

    await page.getByTitle('Edit').click()
    const editor = page.locator('textarea').nth(1)
    await editor.fill('corrected text')
    await page.getByTitle('Save').click()

    await expect(page.getByText('corrected text')).toBeVisible()
    await expect(page.getByText('origonal text')).toHaveCount(0)
    // Edits are visible as such, so a note that reads differently from memory is explained.
    await expect(page.getByText(/· edited/)).toBeVisible()
  })

  test('a note can be resolved, hidden, and reopened', async ({ page }) => {
    await openPanel(page)
    await page.getByPlaceholder(/What's wrong/).fill('fix this thing')
    await page.getByRole('button', { name: 'Add', exact: true }).click()

    await page.getByTitle('Mark done').click()
    // Done notes are hidden by default: the list is meant to be what is left to do.
    await expect(page.getByText('fix this thing')).toHaveCount(0)
    await expect(page.getByText('0 open')).toBeVisible()

    await page.getByLabel('Show done').check()
    await expect(page.getByText('fix this thing')).toBeVisible()

    await page.getByTitle('Reopen').click()
    await expect(page.getByText('1 open')).toBeVisible()
  })

  test('an image can be attached to a new note and opens full size', async ({ page }) => {
    await openPanel(page)
    await page.getByPlaceholder(/What's wrong/).fill('see the screenshot')
    await page.locator('input[type="file"]').first().setInputFiles({
      name: 'shot.png',
      mimeType: 'image/png',
      buffer: PNG,
    })
    // Queued first — the upload happens on Add, and the queued thumbnail is a blob: URL.
    await expect(page.locator('img[alt="pending shot.png"]')).toBeVisible()
    await expect(page.getByText('attached on add')).toBeVisible()
    await page.getByRole('button', { name: 'Add', exact: true }).click()

    // The stored one, served over http, is a different element from the queued one.
    const thumb = page.locator('li img[alt="shot.png"]')
    await expect(thumb).toBeVisible()
    // The image really is served, rather than the row merely existing.
    const src = await thumb.getAttribute('src')
    const fetched = await page.request.get(src!)
    expect(fetched.status()).toBe(200)
    expect(fetched.headers()['content-type']).toBe('image/png')
  })

  test('an image can be added to and removed from an existing note', async ({ page }) => {
    await openPanel(page)
    await page.getByPlaceholder(/What's wrong/).fill('needs a picture')
    await page.getByRole('button', { name: 'Add', exact: true }).click()

    // Attaching does not require entering an edit.
    await page.getByTitle('Attach another image').locator('input[type="file"]').setInputFiles({
      name: 'added.png',
      mimeType: 'image/png',
      buffer: PNG,
    })
    await expect(page.locator('img[alt="added.png"]')).toBeVisible()

    // Removing does, so a stray click in the list cannot destroy an image.
    await expect(page.getByTitle('Remove this image')).toHaveCount(0)
    await page.getByTitle('Edit').click()
    await page.getByTitle('Remove this image').click()
    await expect(page.locator('img[alt="added.png"]')).toHaveCount(0)

    // And the edit is still in progress: removing an image must not end it, which is what the
    // blur-commit bug did.
    await expect(page.locator('li textarea')).toHaveCount(1)
  })

  test('a note can be deleted', async ({ page }) => {
    await openPanel(page)
    await page.getByPlaceholder(/What's wrong/).fill('written by mistake')
    await page.getByRole('button', { name: 'Add', exact: true }).click()
    await expect(page.getByText('written by mistake')).toBeVisible()

    await page.getByTitle(/^Delete/).click()
    await expect(page.getByText('written by mistake')).toHaveCount(0)
    await expect(page.getByText('Nothing open.')).toBeVisible()
  })

  test('notes survive closing and reopening the panel', async ({ page }) => {
    await openPanel(page)
    await page.getByPlaceholder(/What's wrong/).fill('persist me')
    await page.getByRole('button', { name: 'Add', exact: true }).click()
    await expect(page.getByText('persist me')).toBeVisible()

    await page.getByTitle('Close').click()
    await expect(page.getByRole('heading', { name: 'Feedback' })).toHaveCount(0)

    await page.evaluate(() => window.__financeCommandBus!.dispatch('open:feedback'))
    await expect(page.getByText('persist me')).toBeVisible()
  })

  test('the sidebar has a way in that does not need the menu bar', async ({ page }) => {
    // The web app has no menu bar, so the sidebar button is the only route there — and it is
    // what was actually asked for.
    await skipTour(page)
    await page.goto('/')
    await page.waitForLoadState('networkidle')
    await page.getByRole('button', { name: 'Feedback' }).first().click()
    await expect(page.getByRole('heading', { name: 'Feedback' })).toBeVisible()
  })
})
