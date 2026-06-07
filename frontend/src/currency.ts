/**
 * Currency formatting utilities for multi-currency display support.
 */

export const SUPPORTED_CURRENCIES = ['USD', 'INR', 'EUR'] as const
export type SupportedCurrency = (typeof SUPPORTED_CURRENCIES)[number]

const CURRENCY_SYMBOLS: Record<string, string> = {
  USD: '$',
  INR: '₹',
  EUR: '€',
}

/**
 * Format an amount with the appropriate currency symbol.
 * Falls back to using the currency code as a prefix if the symbol is unknown.
 */
export function formatCurrencyWithSymbol(
  amount: number,
  currency: string = 'USD',
  privacyMode: boolean = false,
): string {
  if (privacyMode) {
    const symbol = CURRENCY_SYMBOLS[currency] || currency + ' '
    return `${symbol}xxxx.xx`
  }
  const symbol = CURRENCY_SYMBOLS[currency] || currency + ' '
  const normalized = Number.isFinite(amount) ? amount : 0
  const sign = normalized < 0 ? '-' : ''
  const formatted = Math.abs(normalized).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
  return `${sign}${symbol}${formatted}`
}

/**
 * Get the symbol for a currency code.
 */
export function getCurrencySymbol(currency: string = 'USD'): string {
  return CURRENCY_SYMBOLS[currency] || currency + ' '
}

/**
 * Get display currency from localStorage.
 */
export function getDisplayCurrency(): string {
  return localStorage.getItem('finance-app-display-currency') || 'USD'
}

/**
 * Set display currency in localStorage.
 */
export function setDisplayCurrency(currency: string): void {
  localStorage.setItem('finance-app-display-currency', currency)
}

/**
 * Build a currency query param string for analytics API calls.
 * Always sends the param so the backend performs currency conversion
 * (e.g. converting INR transactions to USD).
 */
export function currencyParam(displayCurrency: string): string {
  return `&currency=${encodeURIComponent(displayCurrency || 'USD')}`
}
