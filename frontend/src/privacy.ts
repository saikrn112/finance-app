interface NumberFormatOptions {
  digits?: number
  signed?: boolean
  absolute?: boolean
}

function normalizedValue(value: number, options: NumberFormatOptions = {}) {
  const raw = Number.isFinite(value) ? value : 0
  const normalized = options.absolute ? Math.abs(raw) : raw
  const sign = options.signed ? (raw > 0 ? '+' : raw < 0 ? '-' : '') : ''
  return { raw, normalized, sign }
}

export function formatCurrency(value: number, privacyMode: boolean, options: NumberFormatOptions & { currency?: string } = {}) {
  const { normalized, sign } = normalizedValue(value, options)
  const currencyCode = options.currency || 'USD'
  const { symbol, locale } = await_getCurrencyConfig(currencyCode)
  if (privacyMode) return `${sign}${symbol}xxxx.xx`
  return `${sign}${normalized.toLocaleString(locale, { style: 'currency', currency: currencyCode })}`
}

function await_getCurrencyConfig(code: string) {
  // Inline to avoid circular imports — mirrors currency-config.ts
  const configs: Record<string, { symbol: string; locale: string }> = {
    USD: { symbol: '$', locale: 'en-US' },
    EUR: { symbol: '€', locale: 'en-US' },
    INR: { symbol: '₹', locale: 'en-IN' },
  }
  return configs[code] || { symbol: code + ' ', locale: 'en-US' }
}

export function formatPercent(value: number, privacyMode: boolean, options: NumberFormatOptions = {}) {
  const digits = options.digits ?? 1
  const { normalized, sign } = normalizedValue(value, options)
  if (privacyMode) return `${sign}xxx${digits > 0 ? '.x'.padEnd(digits + 2, 'x') : ''}%`
  return `${sign}${(normalized * 100).toFixed(digits)}%`
}

export function formatPlainNumber(value: number, privacyMode: boolean, options: NumberFormatOptions = {}) {
  const digits = options.digits ?? 2
  const { normalized, sign } = normalizedValue(value, options)
  if (privacyMode) return `${sign}${digits > 0 ? 'xxxx.xx' : 'xxxx'}`
  return `${sign}${normalized.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`
}

export function formatCount(value: number, privacyMode: boolean) {
  if (privacyMode) return 'xxx'
  return value.toLocaleString('en-US')
}

export function formatAxisCurrency(value: number, privacyMode: boolean, currency?: string) {
  const symbols: Record<string, string> = { USD: '$', INR: '₹', EUR: '€' }
  const symbol = symbols[currency || 'USD'] || (currency || 'USD') + ' '
  if (privacyMode) return `${symbol}xxx`
  return `${symbol}${Math.round(Math.abs(value)).toLocaleString('en-US')}`
}

