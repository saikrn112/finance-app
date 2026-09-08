/**
 * Category colours.
 *
 * ## Why this is no longer fifteen hues
 *
 * It used to be one raw Tailwind-500 hex per category — `#22c55e`, `#3b82f6`, `#f43f5e`,
 * `#ec4899`, `#06b6d4`, `#a855f7`, `#14b8a6` and so on. Fifteen maximum-chroma hues, fixed
 * regardless of appearance, so the same vivid green sat on a cream surface and on near-black.
 * That was the single biggest reason the pages read as "colours all over the place".
 *
 * Fifteen distinguishable hues do not exist. Four hand-picked ones already fail: validating
 * green/red/blue/orange against this app's surfaces measured orange↔red at ΔE 7.1 for normal
 * vision — under the 15 floor, so full-colour readers cannot reliably tell them apart — and
 * orange↔green failed colour-vision separation outright. Adding hues past that point makes
 * identification worse, not better.
 *
 * ## What it is instead
 *
 * Three hues that carry meaning, plus a neutral:
 *
 *   blue    — living and lifestyle spending
 *   orange  — obligations: rent, debt, tax, credit
 *   aqua    — money coming in
 *   neutral — uncategorised, which is an absence rather than a category
 *
 * Those three are the first three slots of the categorical palette, taken in the documented
 * order. The order is the colour-vision safety mechanism rather than decoration, and the
 * first three are the ones that clear every gate with *all* pairs in play — the right gate
 * here, because the category list is sorted by amount, so any two rows can end up adjacent.
 *
 * Verified with the palette validator against this app's own chart surfaces (`#fff8ef` light,
 * `#0f172a` dark) rather than the defaults:
 *
 *   light — worst all-pairs ΔE 9.2 (deutan), 24.0 normal vision: all checks pass
 *   dark  — worst all-pairs ΔE 9.4 (deutan), 20.9 normal vision: all checks pass
 *
 * Light-mode aqua measures 2.67:1 against the cream surface, below 3:1, so the validator
 * requires relief. It is already satisfied: every use of these colours is a bar or a chip
 * *beside a visible category label*, never colour alone.
 *
 * Members within a family are separated by lightness derived from the family hue, not by a
 * new hue — composite encoding, which is the sanctioned answer once the hue budget is spent.
 * The steps are computed rather than hand-picked so that adding a category cannot silently
 * drift one out of the validated band.
 *
 * The dark hues are a set selected for the dark surface, not an automatic flip of the light
 * ones.
 */

/** The three validated hues plus a neutral, per appearance. */
const FAMILY_HUES = {
  light: {
    living: '#2a78d6',
    obligation: '#eb6834',
    income: '#1baf7a',
    none: '#8d8378',
  },
  dark: {
    living: '#3987e5',
    obligation: '#d95926',
    income: '#199e70',
    none: '#8a8f98',
  },
} as const

type Family = keyof (typeof FAMILY_HUES)['light']

/**
 * Which family each category belongs to, and its position within that family.
 *
 * The position is fixed per category, not derived from the data, so a category's colour
 * cannot change because a filter removed the row above it.
 */
const CATEGORY_FAMILY: Record<string, { family: Family; step: number }> = {
  Salary: { family: 'income', step: 0 },
  Investment: { family: 'income', step: 1 },
  Remittance: { family: 'income', step: 2 },

  Rent: { family: 'obligation', step: 0 },
  Loan: { family: 'obligation', step: 1 },
  Tax: { family: 'obligation', step: 2 },
  'Credit Card': { family: 'obligation', step: 3 },

  Groceries: { family: 'living', step: 0 },
  Dining: { family: 'living', step: 1 },
  Transportation: { family: 'living', step: 2 },
  Health: { family: 'living', step: 3 },
  Shopping: { family: 'living', step: 4 },
  Subscriptions: { family: 'living', step: 5 },
  Personal: { family: 'living', step: 6 },

  Uncategorized: { family: 'none', step: 0 },
}

const CATEGORY_ORDER: string[] = [
  'Salary', 'Investment', 'Remittance', 'Loan', 'Rent',
  'Dining', 'Groceries', 'Transportation', 'Health', 'Shopping',
  'Subscriptions', 'Personal', 'Tax', 'Uncategorized',
]

export { CATEGORY_ORDER }

function currentAppearance(): 'light' | 'dark' {
  return typeof document !== 'undefined'
    && document.documentElement.classList.contains('dark')
    ? 'dark'
    : 'light'
}

/**
 * A lightness step of `hue`, as a `color-mix` against the chart surface.
 *
 * Left as CSS rather than resolved to a hex so it re-evaluates when the appearance changes;
 * resolving here would bake in whichever mode happened to be active. Step 0 is the validated
 * hue itself, so the leading member of each family is exactly the colour that was measured.
 */
function step(hue: string, index: number): string {
  if (index === 0) return hue
  // Even increments toward the surface, capped so the deepest fade stays visible against it.
  // Seven is the largest family, and 12% a step leaves the last one at 28% of the hue.
  const towardSurface = Math.min(index * 12, 72)
  return `color-mix(in oklab, ${hue} ${100 - towardSurface}%, var(--chart-bg))`
}

/** The colour for a category. A `Top/Sub` string is decided by its top level. */
export function getCategoryColor(category: string): string {
  const top = category.split('/')[0]
  const entry = CATEGORY_FAMILY[top] ?? CATEGORY_FAMILY.Uncategorized
  return step(FAMILY_HUES[currentAppearance()][entry.family], entry.step)
}

/**
 * The category colour, faded toward the surface.
 *
 * Exists because a caller used to append `bb` to the hex for alpha. That worked only while
 * these values were literal hexes; it silently produces an invalid colour now, and the bar
 * would simply not paint.
 */
export function getCategoryColorFaded(category: string, percent = 70): string {
  return `color-mix(in oklab, ${getCategoryColor(category)} ${percent}%, var(--chart-bg))`
}

/**
 * For callers that enumerate the known categories.
 *
 * A proxy rather than a literal object because the values depend on the current appearance —
 * a snapshot taken at module load would be stale after the first theme change.
 */
export const CATEGORY_COLORS: Record<string, string> = new Proxy(
  {} as Record<string, string>,
  {
    get: (_target, key) => getCategoryColor(String(key)),
    has: (_target, key) => String(key) in CATEGORY_FAMILY,
    ownKeys: () => Object.keys(CATEGORY_FAMILY),
    getOwnPropertyDescriptor: () => ({ enumerable: true, configurable: true }),
  }
)
