export const CATEGORY_COLORS: Record<string, string> = {
  Salary: '#22c55e',
  Rent: '#f59e0b',
  'Credit Card': '#64748b',
  Investment: '#8b5cf6',
  Remittance: '#06b6d4',
  Dining: '#f97316',
  Groceries: '#10b981',
  Health: '#14b8a6',
  Transportation: '#3b82f6',
  Shopping: '#ec4899',
  Personal: '#a855f7',
  Loan: '#78716c',
  Tax: '#ef4444',
  Subscriptions: '#f472b6',
  Uncategorized: '#94a3b8',
}

const CATEGORY_ORDER: string[] = [
  'Salary', 'Investment', 'Remittance', 'Loan', 'Rent',
  'Dining', 'Groceries', 'Transportation', 'Health', 'Shopping',
  'Subscriptions', 'Personal', 'Tax', 'Uncategorized',
]

export { CATEGORY_ORDER }

export function getCategoryColor(category: string): string {
  const top = category.split('/')[0]
  return CATEGORY_COLORS[top] || CATEGORY_COLORS.Uncategorized
}
