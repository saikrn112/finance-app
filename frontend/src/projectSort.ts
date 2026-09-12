import type { ProjectSummary } from './api'

/**
 * Most recent activity first.
 *
 * The last transaction date says more about when a trip actually happened than when it was
 * marked complete, which can be weeks later or never. Falls back through end_date to
 * created_at so a project with no transactions yet still lands somewhere sensible.
 *
 * Shared so the sidebar and the "Add to project" menu can never drift apart.
 */
export function byRecentActivity(a: ProjectSummary, b: ProjectSummary): number {
  const key = (p: ProjectSummary) =>
    p.latest_transaction_date || p.end_date || p.created_at || ''
  return key(b).localeCompare(key(a))
}

export function sortByRecentActivity<T extends ProjectSummary>(projects: T[]): T[] {
  return projects.slice().sort(byRecentActivity)
}
