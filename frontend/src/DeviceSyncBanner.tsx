import { useCallback, useEffect, useState } from 'react'

type Attention = { needed: boolean; reason: string | null; last_success: string | null }

/** How often to re-ask while all is well. Matches the backend's sync poll. */
const POLL_MS = 5 * 60 * 1000
/**
 * How often to re-ask while the banner is showing. Someone looking at it is usually fixing the
 * cause -- reconnecting Google -- and at the five-minute rate it kept saying "stopped" for minutes
 * after sync had already recovered.
 */
const POLL_WHILE_SHOWING_MS = 30 * 1000

function ago(iso: string | null): string {
  if (!iso) return 'never'
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000)
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.round(minutes / 60)
  if (hours < 48) return `${hours} h ago`
  return `${Math.round(hours / 24)} days ago`
}

/**
 * "Device sync has stopped", on every page.
 *
 * Sync was dead for a week once and nothing said so: both apps kept pulling from Plaid, so every
 * screen looked current while no edit crossed between them. The verdict comes from the backend
 * (`attention` on the status endpoint) so every client raises the same alarm from the same facts.
 */
export function DeviceSyncBanner({ onOpenSettings }: { onOpenSettings: () => void }) {
  const [attention, setAttention] = useState<Attention | null>(null)

  const check = useCallback(async () => {
    try {
      const response = await fetch('/api/settings/device-sync/status')
      if (!response.ok) return
      const body = await response.json()
      setAttention(body?.attention ?? null)
    } catch {
      // A status call that fails is not itself evidence that sync stopped; keep the last answer.
    }
  }, [])

  const showing = Boolean(attention?.needed)

  useEffect(() => {
    void check()
    const timer = window.setInterval(() => void check(), showing ? POLL_WHILE_SHOWING_MS : POLL_MS)
    const onFocus = () => void check()
    window.addEventListener('focus', onFocus)
    return () => {
      window.clearInterval(timer)
      window.removeEventListener('focus', onFocus)
    }
  }, [check, showing])

  if (!attention?.needed) return null
  return (
    <div
      className="mb-3 flex items-center justify-between gap-3 rounded-lg border border-amber-400/50 bg-amber-500/10 px-4 py-3 text-sm text-amber-700 dark:text-amber-300"
      role="alert"
    >
      <span>
        <strong>Device sync stopped</strong> (last synced {ago(attention.last_success)}). {attention.reason}
      </span>
      <button type="button" className="app-button-secondary shrink-0 rounded px-3 py-1" onClick={onOpenSettings}>
        Open Settings
      </button>
    </div>
  )
}
