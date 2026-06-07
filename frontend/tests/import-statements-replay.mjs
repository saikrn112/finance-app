import path from 'node:path'
import fs from 'node:fs/promises'
import { chromium } from '@playwright/test'

const APP_URL = process.env.REPLAY_APP_URL || 'http://127.0.0.1:5175/'
const STATEMENTS_ROOT = process.env.STATEMENTS_ROOT || './test-statements'

const SOURCES = [
  { key: 'bofa', label: 'Bank of America', dir: 'bofa_statements' },
  { key: 'chase', label: 'Chase', dir: 'chase_statements' },
  { key: 'amex', label: 'American Express', dir: 'amex_statements' },
  { key: 'discover', label: 'Discover', dir: 'discover_statements' },
  { key: 'apple', label: 'Apple Card', dir: 'apple_statements' },
]

function sortFiles(files) {
  return files
    .filter((name) => name.toLowerCase().endsWith('.pdf'))
    .sort((left, right) => left.localeCompare(right, undefined, { numeric: true }))
}

async function listStatementFiles() {
  const result = []
  for (const source of SOURCES) {
    const dir = path.join(STATEMENTS_ROOT, source.dir)
    const names = sortFiles(await fs.readdir(dir))
    result.push({
      ...source,
      files: names.map((name) => path.join(dir, name)),
    })
  }
  return result
}

async function ensureSettingsOpen(page) {
  const modal = page.getByRole('heading', { name: 'Settings' })
  if (await modal.isVisible().catch(() => false)) {
    return
  }
  const openSettings = page.getByRole('button', { name: 'Open settings' })
  if (await openSettings.isVisible().catch(() => false)) {
    await openSettings.click()
  } else {
    await page.getByRole('button', { name: 'Settings' }).click()
  }
  await modal.waitFor({ state: 'visible' })
}

async function statCardValue(page, label) {
  try {
    const card = page.locator('div').filter({
      has: page.locator('div', { hasText: label }),
    }).filter({ hasText: new RegExp(`^${label}\\d+$`) }).first()
    const text = await card.textContent()
    return Number(text?.match(/\d+/)?.[0] ?? '0')
  } catch {
    return null
  }
}

async function uploadStatement(page, sourceLabel, filePath) {
  await page.getByRole('combobox', { name: 'SOURCE' }).selectOption({ label: sourceLabel })
  await page.getByRole('button', { name: 'Statement PDF' }).click()

  const input = page.locator('#import-file-input')
  await input.setInputFiles(filePath)

  const status = page.locator('text=/Preview ready|already imported/i').first()
  await status.waitFor({ timeout: 30000 })

  const alreadyImported = await page.locator('text=/already imported/i').first().isVisible().catch(() => false)
  if (!alreadyImported) {
    const commitButton = page.getByRole('button', { name: 'Commit Import' })
    await commitButton.click()
    await page.locator('text=/Imported \\d+ transaction\\(s\\), skipped \\d+\\.|already committed earlier/i').first().waitFor({ timeout: 30000 })
  }

  const rows = await statCardValue(page, 'Rows')
  const importable = await statCardValue(page, 'Importable')
  const duplicates = await statCardValue(page, 'Duplicates')

  return {
    file: path.basename(filePath),
    alreadyImported,
    rows,
    importable,
    duplicates,
  }
}

async function main() {
  const browser = await chromium.launch({ headless: true })
  const page = await browser.newPage()
  const plan = await listStatementFiles()

  await page.goto(APP_URL, { waitUntil: 'networkidle' })
  await ensureSettingsOpen(page)

  const summary = []
  for (const source of plan) {
    for (const filePath of source.files) {
      const result = await uploadStatement(page, source.label, filePath)
      summary.push({ source: source.label, ...result })
      console.log(`${source.label}: ${result.file} rows=${result.rows} importable=${result.importable} duplicates=${result.duplicates} already_imported=${result.alreadyImported}`)
    }
  }

  await browser.close()
  console.log(JSON.stringify(summary, null, 2))
}

main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
