import { useEffect, useState } from 'react'
import { X, Trash2 } from 'lucide-react'
import { api, type Contact, type SplitwiseContactLink, type SplitwiseCredentialsInfo, type SplitwiseFriend, type SplitwiseStatus, type VaultBackupJob, type VaultBackupRow, type VaultDiscoveryRow, type VaultRestoreJob } from './api'
import { GETTING_STARTED_DASHBOARD_KEY, GETTING_STARTED_DONE_KEY } from './GettingStartedGuide'
import { PlaidLinkButton } from './PlaidLink'
import { SUPPORTED_CURRENCIES } from './currency'
import { useFilterStore } from './store'

interface SettingsData {
  stats: { total_transactions: number; connected_accounts: number }
  plaid_usage: {
    month_start: string
    month_end_exclusive: string
    total_estimated_cost: number
    billing_lines: Array<{ label: string; quantity: number; unit: string; unit_price: number; estimated_cost: number }>
    scope: string
    scope_note: string
    devices: Array<{ device_key: string; device_label: string; environment?: string | null; database_id?: string | null; database_name?: string | null; call_count: number; balance_calls: number; endpoints: Record<string, number> }>
    endpoints: Array<{ endpoint: string; call_count: number; units: number; estimated_cost: number }>
  }
  vault: {
    vault_id: string | null
    provider: string | null
    provider_email: string | null
    connected: boolean
    last_backup_at: string | null
    last_restore_at?: string | null
    drive_folder_name?: string | null
    drive_folder_id?: string | null
    google_drive_ready: boolean
  }
  accounts: Array<{
    id: string
    plaid_item_id?: string | null
    source: string
    status: string
    last_sync: string | null
    last_sync_error?: {
      type?: string | null
      message?: string | null
      code?: string | null
      display_message?: string | null
      documentation_url?: string | null
    } | null
    // Item no longer exists at Plaid; Reconnect cannot repair it.
    item_gone?: boolean
  }>
}

export function SettingsPanel({ open, onClose, onDataChange }: { 
  open: boolean
  onClose: () => void
  onDataChange: () => void 
}) {
  const [data, setData] = useState<SettingsData | null>(null)
  const [vaultBusy, setVaultBusy] = useState(false)
  const [backups, setBackups] = useState<VaultBackupRow[]>([])
  const [discoveredVaults, setDiscoveredVaults] = useState<VaultDiscoveryRow[]>([])
  const [backupJob, setBackupJob] = useState<VaultBackupJob | null>(null)
  const [restoreJob, setRestoreJob] = useState<VaultRestoreJob | null>(null)
  const [historyLoading, setHistoryLoading] = useState(false)
  const [signoutConfirm, setSignoutConfirm] = useState(false)
  const backupRunning = backupJob?.status === 'running'
  const restoreRunning = restoreJob?.status === 'running'

  const preferredRemoteVaultId =
    discoveredVaults.find((vault) => Boolean(vault.latest_backup_id))?.vault_id ??
    null
  const selectedVaultId =
    preferredRemoteVaultId ||
    discoveredVaults[0]?.vault_id ||
    data?.vault.vault_id ||
    null

  useEffect(() => {
    if (open) {
      api.getSettings().then(setData)
      api.discoverGoogleVaults().then((result) => setDiscoveredVaults(result.vaults)).catch(() => setDiscoveredVaults([]))
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    const targetVaultId = selectedVaultId
    if (!data?.vault.connected) {
      setBackups([])
      setHistoryLoading(false)
      return
    }
    if (!targetVaultId) {
      setBackups([])
      setHistoryLoading(true)
      return
    }
    setHistoryLoading((prev) => prev || backups.length === 0)
    api.listGoogleBackups(targetVaultId)
      .then((result) => setBackups(result.backups))
      .catch(() => setBackups([]))
      .finally(() => setHistoryLoading(false))
  }, [open, selectedVaultId, data?.vault.connected])

  useEffect(() => {
    if (!backupJob || backupJob.status !== 'running') return
    const timer = window.setInterval(async () => {
      try {
        const next = await api.getGoogleDriveBackupJob(backupJob.job_id)
        setBackupJob(next)
        if (next.status !== 'running') {
          window.clearInterval(timer)
          setVaultBusy(false)
          if (next.status === 'success' && next.result) {
            api.invalidateVaultPanelCache()
            const completedBackup: VaultBackupRow = {
              backup_id: next.result.backup_id,
              parent_backup_id: null,
              created_at: next.result.backup_created_at,
              device_id: 'app',
              device_label: 'app',
              archive_name: next.result.backup_name,
              archive_file_id: next.result.file_id,
              manifest_file_id: next.result.manifest_file_id,
              archive_sha256: null,
            }
            setBackups((current) => {
              const deduped = current.filter((row) => row.backup_id !== completedBackup.backup_id)
              return [completedBackup, ...deduped]
            })
            setData((current) => current ? {
              ...current,
              vault: {
                ...current.vault,
                last_backup_at: next.result?.backup_created_at ?? current.vault.last_backup_at,
              },
            } : current)
          }
          refreshSettings()
        }
      } catch {
        window.clearInterval(timer)
        setVaultBusy(false)
      }
    }, 1000)
    return () => window.clearInterval(timer)
  }, [backupJob])

  useEffect(() => {
    if (!restoreJob || restoreJob.status !== 'running') return
    const timer = window.setInterval(async () => {
      try {
        const next = await api.getRestoreJob(restoreJob.job_id)
        setRestoreJob(next)
        if (next.status !== 'running') {
          window.clearInterval(timer)
          setVaultBusy(false)
          if (next.status === 'success') {
            window.localStorage.setItem(GETTING_STARTED_DONE_KEY, 'true')
            window.localStorage.setItem(GETTING_STARTED_DASHBOARD_KEY, 'true')
            api.invalidateVaultPanelCache()
            refreshSettings()
          }
        }
      } catch {
        window.clearInterval(timer)
        setVaultBusy(false)
      }
    }, 1000)
    return () => window.clearInterval(timer)
  }, [restoreJob])

  useEffect(() => {
    const listener = (event: MessageEvent) => {
      if (event.data?.type === 'vault-google-connected') {
        refreshSettings()
      }
    }
    window.addEventListener('message', listener)
    return () => window.removeEventListener('message', listener)
  }, [])

  const handleDisconnect = async (id: string) => {
    await api.disconnectAccount(id)
    refreshSettings()
  }

  const refreshSettings = () => {
    api.invalidateVaultPanelCache()
    onDataChange()
    api.getSettings({ fresh: true }).then(setData)
    api.discoverGoogleVaults({ fresh: true }).then((result) => setDiscoveredVaults(result.vaults)).catch(() => setDiscoveredVaults([]))
  }

  const handleClearAll = async () => {
    if (confirm('Delete all transactions? This cannot be undone.')) {
      await api.clearTransactions()
      onDataChange()
      api.getSettings({ fresh: true }).then(setData)
    }
  }

  const handleConnectGoogleDrive = () => {
    const popup = window.open(api.startGoogleDriveConnect(), 'google-drive-connect', 'width=560,height=720')
    if (!popup) return
    const timer = window.setInterval(() => {
      if (popup.closed) {
        window.clearInterval(timer)
        refreshSettings()
      }
    }, 750)
  }

  const handleBackupNow = async () => {
    setVaultBusy(true)
    try {
      const job = await api.startGoogleDriveBackupJob()
      setBackupJob(job)
    } catch (err: any) {
      if (err?.status === 401) {
        if (confirm('Google Drive session expired. Reconnect now?')) {
          handleConnectGoogleDrive()
        }
      } else {
        const now = new Date().toISOString()
        setBackupJob({
          job_id: '',
          status: 'error',
          stage: 'error',
          progress: 0,
          message: '',
          error: err?.message || 'Backup failed',
          created_at: now,
          updated_at: now,
        })
      }
    } finally {
      if (!backupRunning) {
        setVaultBusy(false)
      }
    }
  }

  const handleRestoreLatest = async () => {
    const targetVaultId = selectedVaultId
    if (!targetVaultId) return
    if (!confirm(`Restore the latest backup for vault ${targetVaultId}? This will replace local app state.`)) return
    setVaultBusy(true)
    try {
      const job = await api.startRestore(targetVaultId)
      setRestoreJob(job)
    } catch {
      setVaultBusy(false)
    }
  }

  const [signoutPreflight, setSignoutPreflight] = useState<{ needs_backup: boolean; transaction_count: number; last_backup_at: string | null } | null>(null)

  const handleSignOutClick = async () => {
    try {
      const pf = await api.signoutPreflight()
      setSignoutPreflight(pf)
    } catch {
      setSignoutPreflight({ needs_backup: true, transaction_count: 0, last_backup_at: null })
    }
    setSignoutConfirm(true)
  }

  const executeSignOut = async (backupFirst: boolean) => {
    setSignoutConfirm(false)
    setVaultBusy(true)
    try {
      if (backupFirst) {
        const job = await api.startGoogleDriveBackupJob()
        setBackupJob(job)
        await new Promise<void>((resolve, reject) => {
          const timer = window.setInterval(async () => {
            try {
              const next = await api.getGoogleDriveBackupJob(job.job_id)
              if (next.status === 'success') { window.clearInterval(timer); resolve() }
              else if (next.status === 'error') { window.clearInterval(timer); reject(new Error(next.error || 'Backup failed')) }
            } catch (err) { window.clearInterval(timer); reject(err) }
          }, 1000)
        })
      }
      await api.signOut()
      window.localStorage.removeItem(GETTING_STARTED_DONE_KEY)
      window.localStorage.removeItem(GETTING_STARTED_DASHBOARD_KEY)
      window.location.replace('/')
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Sign out failed')
      setVaultBusy(false)
    }
  }

  if (!open) return null

  const backupStageLabel: Record<NonNullable<VaultBackupJob['stage']>, string> = {
    queued: 'Queued',
    preparing: 'Preparing backup',
    uploading: 'Uploading archive',
    finalizing: 'Finalizing manifest',
    success: 'Backup complete',
    error: 'Backup failed',
  }

  const currentVaultHistory = backups.slice(0, 5)

  const otherVaultCount = data?.vault.connected
    ? discoveredVaults.filter((vault) => vault.vault_id !== selectedVaultId && vault.latest_backup_id).length
    : 0

  const displayDevice = (backup: VaultBackupRow) => {
    const label = backup.device_label || backup.device_id || 'Unknown device'
    return label === 'app' ? 'This device' : label
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose} data-tour="tour-settings-panel">
      <div
        className="app-surface dark:bg-gray-800 rounded-lg p-6 w-full max-w-4xl max-h-[85vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex justify-between items-center mb-4">
          <h2 className="text-lg font-semibold">Settings</h2>
          <button onClick={onClose} className="p-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded">
            <X size={20} />
          </button>
        </div>

        {/* Stats */}
        <div className="mb-6 p-3 bg-[var(--app-surface-2)] dark:bg-gray-700 rounded">
          <p className="text-sm">Total transactions: <span className="font-medium">{data?.stats.total_transactions ?? 0}</span></p>
        </div>

        {/* Normalizing Currency */}
        <div className="mb-6">
          <h3 className="font-medium mb-2">Normalizing Currency</h3>
          <p className="mb-3 text-sm text-gray-500 dark:text-gray-400">
            All amounts across the app are converted to this currency.
          </p>
          <div className="rounded bg-[var(--app-surface-2)] dark:bg-gray-700 p-3">
            <DisplayCurrencySelector />
          </div>
        </div>

        <div className="mb-6" data-tour="tour-vault-section">
          <h3 className="font-medium mb-2">Vault Backup</h3>
          <p className="mb-3 text-sm text-gray-500 dark:text-gray-400">
            Connect Google Drive so the app can create managed vault backups for this finance database.
          </p>
          <div className="rounded bg-[var(--app-surface-2)] dark:bg-gray-700 p-3 space-y-2">
            <p className="text-sm">
              Vault ID: <span className="font-medium">{data?.vault.vault_id ?? 'Not created yet'}</span>
            </p>
            <p className="text-sm">
              Google Drive: <span className="font-medium">{data?.vault.connected ? `Connected${data?.vault.provider_email ? ` as ${data.vault.provider_email}` : ''}` : 'Not connected'}</span>
            </p>
            <p className="text-sm">
              Drive folder: <span className="font-medium">{data?.vault.drive_folder_name ?? 'Will be created on first backup'}</span>
            </p>
            <p className="text-sm">
              Last backup: <span className="font-medium">{data?.vault.last_backup_at ? new Date(data.vault.last_backup_at).toLocaleString('en-US') : 'Never'}</span>
            </p>
            <p className="text-sm">
              Last restore: <span className="font-medium">{data?.vault.last_restore_at ? new Date(data.vault.last_restore_at).toLocaleString('en-US') : 'Never'}</span>
            </p>
            {backupJob ? (
              <div className="rounded border border-slate-300/60 bg-slate-100/60 dark:border-slate-600 dark:bg-slate-800/60 p-3">
                <div className="flex items-center justify-between gap-3 text-sm">
                  <span className="font-medium">{backupStageLabel[backupJob.stage]}</span>
                  <span className="tabular-nums text-xs text-gray-500 dark:text-gray-400">{backupJob.progress}%</span>
                </div>
                <div className="mt-2 h-2 rounded bg-slate-300 dark:bg-slate-700 overflow-hidden">
                  <div
                    className={`h-full transition-all ${backupJob.status === 'error' ? 'bg-rose-500' : 'bg-emerald-500'}`}
                    style={{ width: `${backupJob.progress}%` }}
                  />
                </div>
                <p className={`mt-2 text-xs ${backupJob.status === 'error' ? 'text-rose-500' : 'text-gray-500 dark:text-gray-400'}`}>
                  {backupJob.error || backupJob.message}
                  {backupJob.status === 'error' && backupJob.error?.includes('expired') ? (
                    <button onClick={handleConnectGoogleDrive} className="ml-2 underline text-blue-500">Reconnect</button>
                  ) : null}
                </p>
              </div>
            ) : null}
            <div className="flex gap-2 pt-1">
              {data?.vault.connected ? (
                <>
                  <button
                    onClick={handleBackupNow}
                    disabled={vaultBusy || backupRunning}
                    className="px-3 py-2 bg-emerald-500 text-white rounded text-sm hover:bg-emerald-600 disabled:opacity-50"
                  >
                    {backupRunning || vaultBusy ? 'Backing up...' : 'Backup Vault Now'}
                  </button>
                  <button
                    onClick={handleRestoreLatest}
                    disabled={vaultBusy || backupRunning || restoreRunning || !selectedVaultId}
                    className="px-3 py-2 bg-indigo-500 text-white rounded text-sm hover:bg-indigo-600 disabled:opacity-50"
                  >
                    {restoreRunning ? `Restoring ${restoreJob?.progress ?? 0}%` : 'Restore Latest'}
                  </button>
                  <button
                    onClick={handleSignOutClick}
                    disabled={vaultBusy || backupRunning || restoreRunning}
                    className="px-3 py-2 bg-slate-600 text-white rounded text-sm hover:bg-slate-700 disabled:opacity-50"
                  >
                    Sign Out
                  </button>
                </>
              ) : (
                <button
                  onClick={handleConnectGoogleDrive}
                  className="px-3 py-2 bg-emerald-500 text-white rounded text-sm hover:bg-emerald-600"
                >
                  Connect Google Drive
                </button>
              )}
            </div>
          </div>
        </div>

        <div className="mb-6">
          <h3 className="font-medium mb-2">Vault History</h3>
          <div className="rounded bg-[var(--app-surface-2)] dark:bg-gray-700 p-3 space-y-2">
            {!data?.vault.connected ? (
              <p className="text-xs text-gray-500 dark:text-gray-400">Connect Google Drive to see backup history.</p>
            ) : historyLoading ? (
              <p className="text-xs text-gray-500 dark:text-gray-400">Loading backup history...</p>
            ) : currentVaultHistory.length ? (
              <>
                <ul className="space-y-2">
                  {currentVaultHistory.map((backup) => (
                    <li key={backup.backup_id} className="rounded border border-[var(--app-border-soft)] dark:border-gray-600 p-2 text-sm">
                      <div className="flex items-center justify-between gap-3">
                        <span className="font-medium">{new Date(backup.created_at).toLocaleString('en-US')}</span>
                        <span className="text-xs rounded-full bg-emerald-500/15 px-2 py-0.5 text-emerald-400">Successful</span>
                      </div>
                      <div className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                        {displayDevice(backup)}
                      </div>
                      <div className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                        ID: <span className="font-mono break-all">{backup.backup_id}</span>
                      </div>
                    </li>
                  ))}
                </ul>
                {otherVaultCount ? (
                  <p className="pt-1 text-xs text-gray-500 dark:text-gray-400">
                    Other vaults on Drive: {otherVaultCount}
                  </p>
                ) : null}
              </>
            ) : (
              <p className="text-xs text-gray-500 dark:text-gray-400">No backups yet for this vault.</p>
            )}
          </div>
        </div>

        <div className="mb-6" data-tour="tour-plaid-section">
          <div className="mb-2 flex items-center justify-between gap-3">
            <div>
              <h3 className="font-medium">Connected Institutions</h3>
              <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
                Use Imports from the main navigation for CSV and statement uploads. Add new Plaid institutions here.
              </p>
            </div>
            {open && <PlaidLinkButton onSuccess={refreshSettings} compact />}
          </div>
          {data?.accounts.length ? (
            <ul className="space-y-2">
              {data.accounts.map((a) => (
                <li key={a.id} className="flex justify-between items-center gap-3 p-2 bg-[var(--app-surface-2)] dark:bg-gray-700 rounded">
                  <div className="min-w-0 text-sm">
                    <div>
                      {a.source} ({a.item_gone ? 'link required' : a.last_sync_error?.code === 'ITEM_LOGIN_REQUIRED' ? 'login required' : a.last_sync_error ? 'sync incomplete' : a.status})
                      {a.last_sync ? <span className="ml-2 text-xs text-gray-500 dark:text-gray-400">last sync {new Date(a.last_sync).toLocaleDateString('en-US')}</span> : null}
                    </div>
                    {a.item_gone ? (
                      <div className="mt-1 text-xs text-rose-500 dark:text-rose-400">
                        This connection was removed at Plaid, so it can't be reconnected.
                        Use Link again to replace it while keeping your stored history.
                      </div>
                    ) : a.last_sync_error ? (
                      <div className="mt-1 text-xs text-rose-500 dark:text-rose-400">
                        {a.last_sync_error.code || 'Sync error'}: {a.last_sync_error.display_message || a.last_sync_error.message}
                      </div>
                    ) : null}
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    {/* Reconnect is update mode, which needs a live Item. Offering it on a
                        dead one only produces a confusing Plaid error. */}
                    {a.item_gone || !a.last_sync_error || a.last_sync_error.code === 'ITEM_LOGIN_REQUIRED' ? (
                      <PlaidLinkButton onSuccess={refreshSettings} accountId={a.id} label={a.item_gone ? 'Link again' : 'Reconnect'} />
                    ) : <span className="text-xs text-gray-500">Will retry on a later sync</span>}
                    <button
                      onClick={() => handleDisconnect(a.id)}
                      title={a.item_gone ? 'Remove this dead connection' : 'Disconnect'}
                      className="text-red-500 hover:text-red-600 p-1"
                    >
                      <Trash2 size={16} />
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-gray-500">No accounts connected</p>
          )}
        </div>

        <div className="mb-6">
          <h3 className="font-medium mb-2">Plaid Usage</h3>
          <div className="rounded bg-[var(--app-surface-2)] dark:bg-gray-700 p-3 space-y-2">
            <p className="text-sm">
              Current month estimated cost: <span className="font-medium">${(data?.plaid_usage.total_estimated_cost ?? 0).toFixed(2)}</span>
            </p>
            <p className="text-xs text-gray-500 dark:text-gray-400">
              {data?.plaid_usage.month_start ?? '—'} to {(data?.plaid_usage.month_end_exclusive ?? '—')} (month-end exclusive)
            </p>
            {data?.plaid_usage.billing_lines?.length ? (
              <ul className="space-y-1 pt-1">
                {data.plaid_usage.billing_lines.map((row) => (
                  <li key={row.label} className="flex justify-between gap-3 text-xs">
                    <span className="truncate">{row.label}</span>
                    <span className="text-right tabular-nums">
                      {row.quantity} {row.unit} × ${row.unit_price.toFixed(2)} · ${row.estimated_cost.toFixed(2)}
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-xs text-gray-500 dark:text-gray-400">No tracked Plaid usage yet this month.</p>
            )}
            <p className="text-xs text-amber-600 dark:text-amber-300">
              {data?.plaid_usage.scope_note}
            </p>
            {data?.plaid_usage.endpoints?.length ? (
              <details className="pt-1 text-xs text-gray-500 dark:text-gray-400">
                <summary className="cursor-pointer">Local API call audit</summary>
                <div className="mt-2 space-y-2">
                  {data.plaid_usage.devices.map((device) => (
                    <div key={device.device_key} className="rounded border border-slate-300/50 p-2 dark:border-slate-500/50">
                      <div className="flex justify-between gap-3 text-[11px] font-medium text-slate-700 dark:text-slate-200">
                        <span>{device.device_label === 'app' ? 'This device' : device.device_label} · {device.environment || 'unknown'} · {device.device_key}</span>
                        <span>{device.call_count} calls</span>
                      </div>
                      <div className="mt-0.5 truncate text-[10px]">{device.database_name || 'Database unknown'} · {device.database_id || 'legacy'}</div>
                      <ul className="mt-1 space-y-0.5">
                        {Object.entries(device.endpoints).sort(([a], [b]) => a.localeCompare(b)).map(([endpoint, calls]) => (
                          <li key={endpoint} className="flex justify-between gap-3"><span>{endpoint}</span><span>{calls}</span></li>
                        ))}
                      </ul>
                    </div>
                  ))}
                </div>
              </details>
            ) : null}
          </div>
        </div>

        {/* Members */}
        <ContactsManager />

        {/* Splitwise (optional) */}
        <SplitwiseManager />

        {/* Danger Zone */}
        <div className="border-t dark:border-gray-700 pt-4">
          <h3 className="font-medium mb-2 text-red-500">Danger Zone</h3>
          <button
            onClick={handleClearAll}
            className="px-3 py-2 bg-red-500 text-white rounded text-sm hover:bg-red-600"
          >
            Clear All Transactions
          </button>
        </div>
      </div>
      {signoutConfirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={() => setSignoutConfirm(false)}>
          <div className="app-surface mx-4 max-w-sm rounded-xl p-6 shadow-xl dark:border-slate-700 dark:bg-slate-800" onClick={(e) => e.stopPropagation()}>
            <h3 className="text-lg font-semibold mb-2">Sign Out</h3>
            {signoutPreflight?.needs_backup ? (
              <p className="text-sm text-rose-500 dark:text-rose-400 mb-5">
                You have unsaved changes since your last backup. All local data will be deleted.
              </p>
            ) : (
              <p className="text-sm text-slate-600 dark:text-slate-300 mb-5">
                All local data will be deleted. You can restore from Google Drive later.
              </p>
            )}
            <div className="flex flex-col gap-2">
              {signoutPreflight?.needs_backup ? (
                <>
                  <button
                    onClick={() => executeSignOut(true)}
                    className="w-full rounded px-4 py-2 text-sm font-medium bg-blue-500 text-white hover:bg-blue-600"
                  >
                    Backup &amp; Sign Out
                  </button>
                  <button
                    onClick={() => executeSignOut(false)}
                    className="w-full rounded px-4 py-2 text-sm font-medium bg-red-500/70 text-white hover:bg-red-500/85"
                  >
                    Sign Out Without Backup
                  </button>
                </>
              ) : (
                <button
                  onClick={() => executeSignOut(false)}
                  className="w-full rounded px-4 py-2 text-sm font-medium bg-blue-500 text-white hover:bg-blue-600"
                >
                  Sign Out
                </button>
              )}
              <button
                onClick={() => setSignoutConfirm(false)}
                className="w-full rounded px-4 py-2 text-sm font-medium bg-slate-200 text-slate-700 hover:bg-slate-300 dark:bg-slate-700 dark:text-slate-200 dark:hover:bg-slate-600"
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function DisplayCurrencySelector() {
  const displayCurrency = useFilterStore((s) => s.displayCurrency)
  const setDisplayCurrency = useFilterStore((s) => s.setDisplayCurrency)

  return (
    <div className="flex items-center gap-3">
      <label htmlFor="display-currency-select" className="text-sm font-medium">
        Currency
      </label>
      <select
        id="display-currency-select"
        value={displayCurrency}
        onChange={(e) => setDisplayCurrency(e.target.value)}
        className="app-input rounded px-3 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800"
      >
        {SUPPORTED_CURRENCIES.map((c) => (
          <option key={c} value={c}>{c}</option>
        ))}
      </select>
      <span className="text-xs text-gray-500 dark:text-gray-400">
        Analytics totals will be shown in {displayCurrency}
      </span>
    </div>
  )
}


function ContactsManager() {
  const [contacts, setContacts] = useState<Contact[]>([])
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editName, setEditName] = useState('')
  const [editColor, setEditColor] = useState('')
  const [deleting, setDeleting] = useState<{ id: string; splits: number; projects: number } | null>(null)

  useEffect(() => {
    api.getContacts().then(setContacts).catch(() => {})
  }, [])

  const startEdit = (c: Contact) => {
    setEditingId(c.id)
    setEditName(c.name)
    setEditColor(c.color)
  }

  const saveEdit = async () => {
    if (!editingId || !editName.trim()) return
    try {
      const updated = await api.updateContact(editingId, { name: editName.trim(), color: editColor })
      setContacts(prev => prev.map(c => c.id === editingId ? updated : c))
      setEditingId(null)
    } catch { /* dupe name */ }
  }

  const confirmDelete = async (c: Contact) => {
    const usage = await api.getContactUsage(c.id)
    setDeleting({ id: c.id, splits: usage.split_count, projects: usage.project_count })
  }

  const executeDelete = async () => {
    if (!deleting) return
    try {
      await api.deleteContact(deleting.id)
      setContacts(prev => prev.filter(c => c.id !== deleting.id))
    } catch (e) {
      console.error('Failed to delete contact:', e)
    }
    setDeleting(null)
  }

  if (contacts.length === 0) return null

  return (
    <div className="border-t dark:border-gray-700 pt-4">
      <h3 className="font-medium mb-3">Members (Contacts)</h3>
      <div className="space-y-2">
        {contacts.map(c => (
          <div key={c.id} className="flex items-center gap-2 text-sm">
            {editingId === c.id ? (
              <>
                <input type="color" value={editColor} onChange={e => setEditColor(e.target.value)} className="w-5 h-5 rounded cursor-pointer" />
                <input
                  value={editName}
                  onChange={e => setEditName(e.target.value)}
                  className="app-input px-2 py-1 text-sm rounded flex-1"
                  onKeyDown={e => { if (e.key === 'Enter') void saveEdit(); if (e.key === 'Escape') setEditingId(null) }}
                  autoFocus
                />
                <button onClick={() => void saveEdit()} className="text-xs text-blue-500 hover:underline">Save</button>
                <button onClick={() => setEditingId(null)} className="text-xs text-slate-400 hover:underline">Cancel</button>
              </>
            ) : (
              <>
                <span className="w-3 h-3 rounded-full" style={{ backgroundColor: c.color }} />
                <span className="flex-1">{c.name}</span>
                <button onClick={() => startEdit(c)} className="text-xs text-blue-500 hover:underline">Edit</button>
                <button onClick={() => void confirmDelete(c)} className="text-xs text-red-400 hover:underline">Delete</button>
              </>
            )}
          </div>
        ))}
      </div>
      {deleting && (
        <div className="mt-3 rounded border border-red-400/50 bg-red-500/10 p-3 text-sm">
          <div className="flex items-center gap-2 mb-2">
            <span className="text-red-400">⚠</span>
            <span>Deleting this member will affect <strong>{deleting.splits}</strong> transaction split(s) across <strong>{deleting.projects}</strong> project(s).</span>
          </div>
          <div className="flex gap-2">
            <button onClick={() => void executeDelete()} className="px-2 py-1 text-xs bg-red-500 text-white rounded hover:bg-red-600">Delete anyway</button>
            <button onClick={() => setDeleting(null)} className="px-2 py-1 text-xs app-btn-secondary rounded">Cancel</button>
          </div>
        </div>
      )}
    </div>
  )
}

function SplitwiseManager() {
  const [status, setStatus] = useState<SplitwiseStatus | null>(null)
  const [links, setLinks] = useState<SplitwiseContactLink[]>([])
  const [friends, setFriends] = useState<SplitwiseFriend[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = () => {
    api.getSplitwiseStatus().then(setStatus).catch(() => setStatus(null))
    api.getSplitwiseLinks().then(r => setLinks(r.contacts)).catch(() => setLinks([]))
  }

  useEffect(load, [])

  // The OAuth popup posts back when it finishes, same as the Google Drive flow.
  useEffect(() => {
    const listener = (event: MessageEvent) => {
      if (event.data?.type === 'splitwise-connected') load()
    }
    window.addEventListener('message', listener)
    return () => window.removeEventListener('message', listener)
  }, [])

  useEffect(() => {
    if (!status?.connected) { setFriends([]); return }
    api.getSplitwiseFriends().then(r => setFriends(r.friends)).catch(() => setFriends([]))
  }, [status?.connected])

  const connect = () => {
    const popup = window.open(api.startSplitwiseConnect(), 'splitwise-connect', 'width=560,height=720')
    if (!popup) return
    const timer = window.setInterval(() => {
      if (popup.closed) { window.clearInterval(timer); load() }
    }, 750)
  }

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
      load()
    } catch (err: any) {
      setError(err?.message || 'Splitwise request failed')
    } finally {
      setBusy(false)
    }
  }

  if (!status) return null

  const unlinked = links.filter(c => !c.splitwise_user_id)

  return (
    <div className="border-t dark:border-gray-700 pt-4">
      <div className="mb-2 flex items-center justify-between gap-3">
        <h3 className="font-medium">Splitwise</h3>
        {status.connected ? (
          <button onClick={() => void run(api.disconnectSplitwise)} disabled={busy}
            className="text-xs text-red-400 hover:underline disabled:opacity-60">Disconnect</button>
        ) : (
          <button onClick={connect} disabled={!status.configured}
            title={status.configured ? 'Sign in to Splitwise' : 'Add your Client ID and Secret first'}
            className="text-xs text-blue-500 hover:underline disabled:opacity-50">Connect</button>
        )}
      </div>

      <SplitwiseCredentialsForm
        info={status.credentials}
        onSaved={load}
        startOpen={!status.configured}
      />

      {status.connected ? (
        <p className="mb-3 text-xs text-gray-500 dark:text-gray-400">
          Connected as {status.account_name || status.account_email} · commits {status.batch_size} at a time
        </p>
      ) : (
        <p className="mb-3 text-xs text-gray-500 dark:text-gray-400">
          Not connected. Projects still work; only pushing to Splitwise is unavailable.
        </p>
      )}

      {status.connected ? (
        <>
          {!status.self_contact ? (
            <div className="mb-3 rounded border border-amber-400/50 bg-amber-500/10 p-2 text-xs">
              Pick which person is you — Splitwise records you as the payer on every expense.
            </div>
          ) : null}
          {unlinked.length > 0 ? (
            <div className="mb-3 rounded border border-amber-400/50 bg-amber-500/10 p-2 text-xs">
              {unlinked.length} {unlinked.length === 1 ? 'person is' : 'people are'} not linked yet.
              A commit is blocked until everyone on that project is linked.
            </div>
          ) : null}

          <div className="space-y-2">
            {links.map(c => (
              <div key={c.id} className="flex items-center gap-2 text-sm">
                <span className="h-3 w-3 shrink-0 rounded-full" style={{ backgroundColor: c.color }} />
                <span className="min-w-0 flex-1 truncate">
                  {c.name}
                  {c.is_self ? <span className="ml-1 text-[10px] text-blue-400">(you)</span> : null}
                </span>
                <select
                  className="app-input max-w-[190px] px-2 py-1 text-xs rounded"
                  value={c.splitwise_user_id ?? ''}
                  disabled={busy}
                  onChange={(e) => {
                    const value = e.target.value
                    if (!value) return void run(() => api.unlinkSplitwiseContact(c.id))
                    const friend = friends.find(f => f.id === value)
                    void run(() => api.linkSplitwiseContact(c.id, value, friend?.name))
                  }}
                >
                  <option value="">Not linked</option>
                  {/* Keep the current link selectable even if it is not in the friends list. */}
                  {c.splitwise_user_id && !friends.some(f => f.id === c.splitwise_user_id) ? (
                    <option value={c.splitwise_user_id}>{c.splitwise_name || c.splitwise_user_id}</option>
                  ) : null}
                  {friends.map(f => (
                    <option key={f.id} value={f.id}>{f.name}</option>
                  ))}
                </select>
                {!c.is_self ? (
                  <button
                    onClick={() => void run(() => api.setSelfContact(c.id))}
                    disabled={busy}
                    className="shrink-0 text-[10px] text-slate-400 hover:text-blue-400 disabled:opacity-60"
                    title="Mark as yourself"
                  >
                    set as me
                  </button>
                ) : null}
              </div>
            ))}
          </div>
        </>
      ) : null}

      {error ? <div className="mt-2 text-xs text-red-400">{error}</div> : null}
    </div>
  )
}

function SplitwiseCredentialsForm({
  info,
  onSaved,
  startOpen,
}: {
  info: SplitwiseCredentialsInfo
  onSaved: () => void
  startOpen: boolean
}) {
  const [open, setOpen] = useState(startOpen)
  // Re-open if the credentials get cleared, so the section never sits collapsed with
  // nothing behind it. `useState(startOpen)` alone only applies on first mount.
  useEffect(() => { if (!info.usable) setOpen(true) }, [info.usable])
  const [clientId, setClientId] = useState('')
  const [clientSecret, setClientSecret] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      // Only send what was typed, so blanks don't wipe an already-stored secret.
      const body: Record<string, string> = {}
      if (clientId.trim()) body.client_id = clientId.trim()
      if (clientSecret.trim()) body.client_secret = clientSecret.trim()
      if (apiKey.trim()) body.api_key = apiKey.trim()
      if (Object.keys(body).length === 0) { setOpen(false); return }
      await api.saveSplitwiseCredentials(body)
      setClientId(''); setClientSecret(''); setApiKey('')
      setOpen(false)
      onSaved()
    } catch (err: any) {
      setError(err?.message || 'Could not save credentials')
    } finally {
      setBusy(false)
    }
  }

  const forget = async () => {
    if (!confirm('Remove the stored Splitwise credentials?')) return
    setBusy(true)
    try {
      await api.clearSplitwiseCredentials()
      onSaved()
    } finally {
      setBusy(false)
    }
  }

  const stored = info.client_id_present || info.api_key_present

  if (!open) {
    return (
      <div className="mb-3 flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
        <span>
          {info.auth_mode === 'api_key'
            ? 'Using a personal API key'
            : info.client_id_present
              ? `App credentials set${info.client_id_hint ? ` (…${info.client_id_hint})` : ''}`
              : 'No credentials yet'}
          {info.client_id_source === 'config' ? ' from config.yaml' : ''}
        </span>
        <button onClick={() => setOpen(true)} className="text-blue-500 hover:underline">Edit</button>
        {info.client_id_source === 'app' || info.api_key_source === 'app' ? (
          <button onClick={() => void forget()} disabled={busy} className="text-red-400 hover:underline disabled:opacity-60">
            Forget
          </button>
        ) : null}
      </div>
    )
  }

  return (
    <div className="mb-3 rounded border border-[var(--app-border-soft)] p-3 dark:border-slate-700">
      <p className="mb-2 text-xs text-gray-500 dark:text-gray-400">
        Register an app on Splitwise to get these. Stored in the local database, not in
        config.yaml, so they never end up in the repo.
      </p>

      <label className="mb-2 block text-xs">
        <span className="text-gray-500 dark:text-gray-400">Personal API key — simplest for one user</span>
        <input
          type="password"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder={info.api_key_present ? '•••••••• (stored)' : 'paste API key'}
          className="app-input mt-1 w-full rounded px-2 py-1 text-sm"
          autoComplete="off"
        />
      </label>

      <div className="my-2 text-[10px] uppercase tracking-wide text-slate-400">or OAuth app</div>

      <label className="mb-2 block text-xs">
        <span className="text-gray-500 dark:text-gray-400">Client ID</span>
        <input
          value={clientId}
          onChange={(e) => setClientId(e.target.value)}
          placeholder={info.client_id_present ? `•••••${info.client_id_hint ?? ''} (stored)` : 'Client ID'}
          className="app-input mt-1 w-full rounded px-2 py-1 text-sm"
          autoComplete="off"
        />
      </label>
      <label className="mb-2 block text-xs">
        <span className="text-gray-500 dark:text-gray-400">Client Secret</span>
        <input
          type="password"
          value={clientSecret}
          onChange={(e) => setClientSecret(e.target.value)}
          placeholder={info.client_secret_present ? '•••••••• (stored)' : 'Client Secret'}
          className="app-input mt-1 w-full rounded px-2 py-1 text-sm"
          autoComplete="off"
        />
      </label>
      <p className="mb-2 text-[11px] text-gray-500 dark:text-gray-400">
        Set the app's redirect URI to <code>{info.redirect_uri}</code>
      </p>

      <div className="flex items-center gap-2">
        <button onClick={() => void save()} disabled={busy}
          className="app-btn-primary text-xs disabled:opacity-60">
          {busy ? 'Saving…' : 'Save'}
        </button>
        {stored ? (
          <button onClick={() => setOpen(false)} disabled={busy} className="app-btn-secondary text-xs">Cancel</button>
        ) : null}
      </div>
      {error ? <div className="mt-2 text-xs text-red-400">{error}</div> : null}
    </div>
  )
}
