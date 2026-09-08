import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertCircle, CheckCircle2, Cloud, LoaderCircle } from 'lucide-react'
import { ApiError, api, type SettingsResponse } from './api'
import { registerCommand } from './commandBus'

interface Props {
  open: boolean
  settings: SettingsResponse | null
  onRefreshSettings: () => Promise<SettingsResponse | null | undefined>
  onCompleteFreshStart: () => void
}

type Phase = 'connect' | 'waiting' | 'checking' | 'restoring' | 'error'

function describeBackupTimestamp(timestamp: string | null) {
  if (!timestamp) return 'latest available backup'
  return new Date(timestamp).toLocaleString('en-US')
}

export function OnboardingGate({
  open,
  settings,
  onRefreshSettings,
  onCompleteFreshStart,
}: Props) {
  const [phase, setPhase] = useState<Phase>('connect')
  const [message, setMessage] = useState('Connect your Google account to open this workspace.')
  const [error, setError] = useState<string | null>(null)
  const [restoreProgress, setRestoreProgress] = useState(0)
  const popupRef = useRef<Window | null>(null)
  const connectPollRef = useRef<number | null>(null)
  const postConnectStartedRef = useRef(false)

  const clearPopupPoll = useCallback(() => {
    if (connectPollRef.current !== null) {
      window.clearInterval(connectPollRef.current)
      connectPollRef.current = null
    }
  }, [])

  const resetToConnect = useCallback(() => {
    clearPopupPoll()
    popupRef.current = null
    postConnectStartedRef.current = false
    setPhase('connect')
    setError(null)
    setMessage('Connect your Google account to open this workspace.')
  }, [clearPopupPoll])

  const completeFreshStart = useCallback(() => {
    clearPopupPoll()
    popupRef.current = null
    postConnectStartedRef.current = false
    setPhase('checking')
    setError(null)
    setMessage('No backup found. Opening a fresh workspace...')
    window.setTimeout(() => {
      onCompleteFreshStart()
    }, 250)
  }, [clearPopupPoll, onCompleteFreshStart])

  const runPostConnectFlow = useCallback(async (currentSettings: SettingsResponse) => {
    if (!currentSettings.vault.connected) return
    setError(null)
    setPhase('restoring')
    setRestoreProgress(2)
    setMessage('Checking for an existing backup...')
    try {
      const discovered = await api.discoverGoogleVaults({ fresh: true })
      setRestoreProgress(5)
      const localVaultId = currentSettings.vault.vault_id
      const localLatest = localVaultId
        ? discovered.vaults.find((vault) => vault.vault_id === localVaultId && vault.latest_backup_id)
        : null
      const restoreTarget =
        localLatest ??
        discovered.vaults.find((vault) => vault.latest_backup_id) ??
        null

      if (!restoreTarget) {
        completeFreshStart()
        return
      }

      setMessage(`Restoring backup from ${describeBackupTimestamp(restoreTarget.latest_backup_created_at)}...`)
      const job = await api.startRestore(restoreTarget.vault_id)
      await new Promise<void>((resolve, reject) => {
        const timer = window.setInterval(async () => {
          try {
            const next = await api.getRestoreJob(job.job_id)
            if (next.progress !== undefined) setRestoreProgress(5 + Math.round(next.progress * 0.95))
            if (next.message) setMessage(next.message)
            if (next.status === 'success') { window.clearInterval(timer); resolve() }
            else if (next.status === 'error') { window.clearInterval(timer); reject(new Error(next.error || 'Restore failed')) }
          } catch (err) { window.clearInterval(timer); reject(err) }
        }, 1000)
      })
      window.localStorage.setItem('finance-app-getting-started-done', 'true')
      window.localStorage.setItem('finance-app-getting-started-dashboard-read', 'true')
      window.location.replace('/')
    } catch (err) {
      const detail = err instanceof ApiError ? err.message : 'Unable to complete account setup.'
      setPhase('error')
      setError(detail)
      setMessage('Account connected, but restore could not be completed.')
    }
  }, [completeFreshStart])

  useEffect(() => {
    if (open) return
    clearPopupPoll()
    popupRef.current = null
    postConnectStartedRef.current = false
  }, [clearPopupPoll, open])

  /** The provider flow finished, however we found out. */
  const handleProviderReturned = useCallback(async () => {
    clearPopupPoll()
    popupRef.current = null
    if (postConnectStartedRef.current) return
    postConnectStartedRef.current = true
    const latestSettings = await onRefreshSettings()
    if (!latestSettings?.vault.connected) {
      postConnectStartedRef.current = false
      setPhase('connect')
      setMessage('Connect your Google account to open this workspace.')
      return
    }
    await runPostConnectFlow(latestSettings)
  }, [clearPopupPoll, onRefreshSettings, runPostConnectFlow])

  useEffect(() => {
    if (!open) return
    const listener = (event: MessageEvent) => {
      if (event.data?.type === 'vault-google-connected') void handleProviderReturned()
    }
    window.addEventListener('message', listener)
    return () => window.removeEventListener('message', listener)
  }, [open, handleProviderReturned])

  // The same completion, for a host that carried the flow out to a real browser.
  //
  // The postMessage path above cannot work there: the popup opened in another
  // application, so there is no `window.opener` to post back through and the
  // "did the popup close?" poll never fires. The macOS shell watches the backend
  // instead and dispatches this once the vault reports connected.
  useEffect(() => {
    if (!open) return
    return registerCommand('onboarding:provider-returned', () => handleProviderReturned())
  }, [open, handleProviderReturned])

  useEffect(() => () => clearPopupPoll(), [clearPopupPoll])

  if (!open) return null

  const handleConnect = () => {
    setError(null)
    const popup = window.open(api.startGoogleDriveConnect(), 'google-drive-connect', 'width=560,height=720')
    if (!popup) {
      setPhase('error')
      setError('Popup was blocked. Allow popups and try again.')
      return
    }
    popupRef.current = popup
    setPhase('waiting')
    setMessage('Finish the Google sign-in flow in the popup.')
    clearPopupPoll()
    connectPollRef.current = window.setInterval(() => {
      if (popupRef.current?.closed) {
        clearPopupPoll()
        popupRef.current = null
        if (postConnectStartedRef.current) return
        postConnectStartedRef.current = true
        void onRefreshSettings().then((latestSettings) => {
          if (!latestSettings?.vault.connected) {
            postConnectStartedRef.current = false
            setPhase('connect')
            setMessage('Connect your Google account to open this workspace.')
            return
          }
          void runPostConnectFlow(latestSettings)
        })
      }
    }, 750)
  }

  const handleRetry = () => {
    if (settings?.vault.connected) {
      postConnectStartedRef.current = false
      void runPostConnectFlow(settings)
      return
    }
    resetToConnect()
  }

  const isBusy = phase === 'waiting' || phase === 'checking' || phase === 'restoring'

  return (
    <div className="fixed inset-0 z-[70] flex items-center justify-center bg-slate-950/85 px-4">
      <div className="w-full max-w-2xl rounded-2xl border border-slate-700 bg-slate-900 shadow-2xl">
        <div className="border-b border-slate-800 px-8 py-7">
          <div className="mb-3 inline-flex items-center gap-2 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-3 py-1 text-xs font-medium text-emerald-300">
            <Cloud size={14} />
            First steps
          </div>
          <h1 className="text-2xl font-semibold text-white">Connect your Google account</h1>
          <p className="mt-2 max-w-xl text-sm text-slate-300">
            This workspace is account-bound. After you connect Google, the app will automatically restore an existing backup if one exists. Otherwise it will continue as a fresh install.
          </p>
        </div>

        <div className="px-8 py-7">
          <div className="rounded-2xl border border-slate-800 bg-slate-950/70 p-5">
            <div className="flex items-start gap-4">
              <div className="mt-0.5 rounded-full bg-slate-800 p-2 text-slate-200">
                {phase === 'error' ? (
                  <AlertCircle size={18} className="text-rose-400" />
                ) : isBusy ? (
                  <LoaderCircle size={18} className="animate-spin text-emerald-300" />
                ) : settings?.vault.connected ? (
                  <CheckCircle2 size={18} className="text-emerald-300" />
                ) : (
                  <Cloud size={18} className="text-emerald-300" />
                )}
              </div>
              <div className="min-w-0 flex-1">
                <h2 className="text-base font-medium text-white">
                  {phase === 'restoring'
                    ? 'Restoring your workspace'
                    : phase === 'waiting'
                      ? 'Waiting for account connection'
                      : phase === 'error'
                        ? 'Setup needs attention'
                        : 'Google account required'}
                </h2>
                <p className="mt-1 text-sm text-slate-300">{message}</p>
                {phase === 'restoring' && (
                  <div className="mt-3">
                    <div className="flex items-center justify-between text-xs text-slate-400 mb-1">
                      <span>{restoreProgress <= 5 ? 'Discovering backup...' : restoreProgress < 75 ? 'Downloading...' : 'Restoring...'}</span>
                      <span>{restoreProgress}%</span>
                    </div>
                    <div className="h-2 rounded-full bg-slate-800 overflow-hidden">
                      <div
                        className="h-full rounded-full bg-emerald-500 transition-all duration-500"
                        style={{ width: `${restoreProgress}%` }}
                      />
                    </div>
                  </div>
                )}
                {settings?.vault.connected && settings.vault.provider_email ? (
                  <p className="mt-3 text-xs text-slate-400">
                    Connected as <span className="font-medium text-slate-200">{settings.vault.provider_email}</span>
                  </p>
                ) : null}
                {error ? (
                  <p className="mt-3 text-xs text-rose-300">{error}</p>
                ) : null}
              </div>
            </div>
          </div>

          <div className="mt-6 flex flex-wrap items-center gap-3">
            {!settings?.vault.connected ? (
              <button
                onClick={handleConnect}
                disabled={isBusy}
                className="rounded-md bg-emerald-500 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-emerald-600 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {phase === 'waiting' ? 'Waiting for Google...' : 'Connect Google'}
              </button>
            ) : null}
            {phase === 'error' ? (
              <button
                onClick={handleRetry}
                className="rounded-md border border-slate-600 px-4 py-2 text-sm text-slate-200 transition-colors hover:bg-slate-800"
              >
                Try again
              </button>
            ) : null}
          </div>

          {!settings?.vault.connected && phase === 'connect' && (
            <div className="mt-6 rounded-xl border border-slate-800 bg-slate-950/50 p-5">
              <h3 className="text-sm font-medium text-slate-200 mb-3">What this connection does</h3>
              <ul className="space-y-2 text-xs text-slate-400">
                <li className="flex gap-2">
                  <span className="text-emerald-400 shrink-0">✓</span>
                  <span>Backs up your financial data to <strong className="text-slate-300">your own Google Drive</strong> — no one else can access it, including the project maintainers</span>
                </li>
                <li className="flex gap-2">
                  <span className="text-emerald-400 shrink-0">✓</span>
                  <span>Uses the <a href="https://developers.google.com/drive/api/guides/about-sdk" target="_blank" rel="noopener" className="text-blue-400 underline underline-offset-2 hover:text-blue-300">Google Drive API</a> with scope limited to files created by your instance (<a href="https://developers.google.com/identity/protocols/oauth2/scopes#drive" target="_blank" rel="noopener" className="text-blue-400 underline underline-offset-2 hover:text-blue-300">drive.file scope</a>)</span>
                </li>
                <li className="flex gap-2">
                  <span className="text-emerald-400 shrink-0">✓</span>
                  <span>Your instance <strong className="text-slate-300">cannot read</strong> your other Drive files, emails, or any Google data outside its own folder</span>
                </li>
                <li className="flex gap-2">
                  <span className="text-emerald-400 shrink-0">✓</span>
                  <span>Authentication uses <a href="https://developers.google.com/identity/protocols/oauth2" target="_blank" rel="noopener" className="text-blue-400 underline underline-offset-2 hover:text-blue-300">OAuth 2.0</a> — your Google credentials are exchanged directly with Google, never stored or visible to anyone else</span>
                </li>
                <li className="flex gap-2">
                  <span className="text-emerald-400 shrink-0">✓</span>
                  <span>You can <a href="https://myaccount.google.com/permissions" target="_blank" rel="noopener" className="text-blue-400 underline underline-offset-2 hover:text-blue-300">revoke access</a> anytime from your Google account settings</span>
                </li>
              </ul>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
