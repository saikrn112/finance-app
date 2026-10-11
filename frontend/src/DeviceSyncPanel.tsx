import { useEffect, useState } from 'react'

type Status = { enabled: boolean; configured: boolean; reason: string; last_success: string | null; last_error: string | null; peer_count: number; busy: boolean }
export function DeviceSyncPanel() {
  const [status, setStatus] = useState<Status | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const read = async (): Promise<Status> => {
    const response = await fetch('/api/settings/device-sync/status')
    if (!response.ok) throw new Error('Device sync status is unavailable.')
    const next: Status = await response.json()
    if (typeof next.enabled !== 'boolean') throw new Error('Device sync status is unavailable.')
    return next
  }
  useEffect(() => {
    let active = true
    read().then(next => { if (active) setStatus(next) }).catch(() => { if (active) setError('Device sync status is unavailable.') })
    return () => { active = false }
  }, [])
  const refresh = async () => {
    setError(null)
    try { setStatus(await read()) } catch { setError('Device sync status is unavailable. Try again.') }
  }
  const run = async () => {
    setBusy(true); setError(null); setMessage(null)
    try {
      const response = await fetch('/api/settings/device-sync/run', { method: 'POST' })
      const result = await response.json()
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Device sync could not complete.')
      setStatus(await read())
      if (result.ran && result.completed) setMessage('Device sync completed.')
      else setError('Device sync needs attention. Review its status and any pending merge above.')
    } catch (err) { setError(err instanceof Error ? err.message : 'Device sync could not complete.') }
    finally { setBusy(false) }
  }
  return <section className="mb-6" aria-label="Device sync">
    <h3 className="font-medium mb-2">Device Sync</h3>
    <p className="mb-3 text-sm text-gray-500">Exchange records between devices that keep separate databases. Bank refresh and vault backup are separate actions.</p>
    <div className="rounded bg-[var(--app-surface-2)] p-3 space-y-2 text-sm">
      {status ? <>
        <p>{status.reason}</p>
        <p>Last successful sync: {status.last_success ? new Date(status.last_success).toLocaleString() : 'Never'}</p>
        <p>Known devices: {status.peer_count}</p>
        {!status.enabled && <p className="text-xs text-gray-500">Enable sync through the macOS Device Sync controls or your desktop launch settings.</p>}
        {status.last_error && <p role="status" className="text-amber-600">{status.last_error}</p>}
        <button className="app-button-secondary rounded px-3 py-1.5 disabled:opacity-50" disabled={busy || !status.enabled || !status.configured || status.busy} onClick={run}>{busy ? 'Syncing devices…' : 'Sync Devices Now'}</button>
      </> : !error && <p role="status">Loading device sync status…</p>}
      {error && <div role="alert"><p className="text-red-500">{error}</p><button className="app-button-secondary rounded px-3 py-1 mt-2" onClick={refresh}>Refresh sync status</button></div>}
      {message && <p role="status">{message}</p>}
    </div>
  </section>
}
