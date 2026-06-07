/**
 * Currency display configuration — single source of truth.
 *
 * All number formatting in the app reads from this config.
 * Adding a new currency = adding one entry here. Nothing else changes.
 */

export interface CurrencyConfig {
  code: string
  symbol: string
  locale: string           // for toLocaleString
  compactUnits: CompactUnit[]  // ordered largest first
}

interface CompactUnit {
  threshold: number
  divisor: number
  suffix: string
}

const CURRENCY_CONFIGS: Record<string, CurrencyConfig> = {
  USD: {
    code: 'USD',
    symbol: '$',
    locale: 'en-US',
    compactUnits: [
      { threshold: 1_000_000_000, divisor: 1_000_000_000, suffix: 'B' },
      { threshold: 1_000_000, divisor: 1_000_000, suffix: 'M' },
      { threshold: 1_000, divisor: 1_000, suffix: 'k' },
    ],
  },
  EUR: {
    code: 'EUR',
    symbol: '€',
    locale: 'en-US',
    compactUnits: [
      { threshold: 1_000_000_000, divisor: 1_000_000_000, suffix: 'B' },
      { threshold: 1_000_000, divisor: 1_000_000, suffix: 'M' },
      { threshold: 1_000, divisor: 1_000, suffix: 'k' },
    ],
  },
  INR: {
    code: 'INR',
    symbol: '₹',
    locale: 'en-IN',
    compactUnits: [
      { threshold: 1_00_00_000, divisor: 1_00_00_000, suffix: 'Cr' },
      { threshold: 1_00_000, divisor: 1_00_000, suffix: 'L' },
      { threshold: 1_000, divisor: 1_000, suffix: 'k' },
    ],
  },
}

/**
 * Get config for a currency. Falls back to USD-like behavior for unknown currencies.
 */
export function getCurrencyConfig(code: string): CurrencyConfig {
  return CURRENCY_CONFIGS[code] || {
    code,
    symbol: code + ' ',
    locale: 'en-US',
    compactUnits: [
      { threshold: 1_000_000, divisor: 1_000_000, suffix: 'M' },
      { threshold: 1_000, divisor: 1_000, suffix: 'k' },
    ],
  }
}

/**
 * Format a number as currency using the full locale format.
 * e.g., $1,234.56 or ₹1,23,456.78
 */
export function formatFull(value: number, code: string): string {
  const config = getCurrencyConfig(code)
  return value.toLocaleString(config.locale, { style: 'currency', currency: config.code })
}

/**
 * Format a number in compact form for tight spaces (sidebar, chart axes).
 * e.g., $1.5M or ₹1.5Cr
 */
export function formatCompact(value: number, code: string): string {
  const config = getCurrencyConfig(code)
  const abs = Math.abs(value)
  const sign = value < 0 ? '-' : ''

  for (const unit of config.compactUnits) {
    if (abs >= unit.threshold) {
      const compact = Math.round(abs / unit.divisor)
      return `${sign}${config.symbol}${compact}${unit.suffix}`
    }
  }
  return `${sign}${config.symbol}${Math.round(abs)}`
}

/**
 * Get just the symbol for a currency.
 */
export function getSymbol(code: string): string {
  return getCurrencyConfig(code).symbol
}

/**
 * Get the locale for a currency (for axis labels, etc.)
 */
export function getLocale(code: string): string {
  return getCurrencyConfig(code).locale
}
