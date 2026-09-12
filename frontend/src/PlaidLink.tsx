import { useCallback, useEffect, useState } from 'react'
import { usePlaidLink } from 'react-plaid-link'
import { Plus, RefreshCw } from 'lucide-react'
import { api } from './api'

export function PlaidLinkButton({ onSuccess, compact, products, accountId, label, onUnrecoverable }: { onSuccess?: () => void; compact?: boolean; products?: string; accountId?: string; label?: string; onUnrecoverable?: (message: string) => void }) {
  const [linkToken, setLinkToken] = useState<string>('')
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [connectionError, setConnectionError] = useState('')
  // Why the token could not be created. Previously every failure was reported as "add your
  // Plaid credentials to config.yaml", which sent people editing a config file that was
  // already correct — the real cause is usually a specific Plaid error about this one Item.
  const [tokenError, setTokenError] = useState('')

  useEffect(() => {
    let cancelled = false
    setStatus('loading')
    setTokenError('')
    api.getLinkToken(products, accountId)
      .then(data => {
        if (cancelled) return
        setLinkToken(data.link_token || '')
        setStatus(data.link_token ? 'ready' : 'error')
        if (!data.link_token) setTokenError('Plaid did not return a link token.')
      })
      .catch((error: unknown) => {
        if (cancelled) return
        const message = error instanceof Error ? error.message : 'Could not reach Plaid.'
        setTokenError(message)
        setStatus('error')
        // A dead Item cannot be repaired by retrying, so let the parent say so in context.
        if (/no longer exists at Plaid/i.test(message)) onUnrecoverable?.(message)
      })
    return () => { cancelled = true }
  }, [products, accountId, onUnrecoverable])

  const handleSuccess = useCallback(async (public_token: string, metadata: any) => {
    const institutionName = metadata?.institution?.name || ""
    const fingerprints = (metadata?.accounts || []).map((account: any) => ({
      mask: account.mask || '',
      name: account.name || '',
      type: account.type || '',
      subtype: account.subtype || '',
    }))
    try {
      setConnectionError('')
      await api.exchangeToken(public_token, institutionName, metadata?.institution?.institution_id, fingerprints, accountId, products)
      await api.syncPlaid()
      onSuccess?.()
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Unable to connect this institution.'
      setConnectionError(message)
      if (compact) window.alert(message)
    }
  }, [onSuccess, accountId, products, compact])

  const { open, ready } = usePlaidLink({ token: linkToken, onSuccess: handleSuccess })

  if (status === 'loading') {
    return compact ? null : <button disabled className="px-3 py-2 bg-gray-300 rounded text-sm">Loading...</button>
  }

  if (status === 'error') {
    // Keep the reason visible instead of hiding it behind an alert, and title-attribute it
    // so it is readable even in the narrow row layout.
    return compact ? null : (
      <div>
        <button
          onClick={() => window.alert(tokenError || 'Plaid could not start a connection.')}
          title={tokenError}
          className="px-3 py-2 rounded text-sm text-white bg-rose-500 hover:bg-rose-600"
        >
          {label ? `${label} unavailable` : 'Plaid unavailable'}
        </button>
        {tokenError ? <p className="mt-2 max-w-xs text-xs text-rose-500">{tokenError}</p> : null}
      </div>
    )
  }

  if (compact) {
    return (
      <button
        onClick={() => open()}
        disabled={!ready}
        className="p-1.5 bg-blue-500 text-white rounded hover:bg-blue-600 disabled:opacity-50"
        title={accountId ? 'Reconnect bank' : 'Connect another bank'}
      >
        {accountId ? <RefreshCw size={16} /> : <Plus size={16} />}
      </button>
    )
  }

  return (
    <div>
      <button
        onClick={() => open()}
        disabled={!ready}
        className="px-3 py-2 bg-blue-500 text-white rounded text-sm hover:bg-blue-600 disabled:opacity-50"
      >
        {label || '+ Connect Bank'}
      </button>
      {connectionError ? <p className="mt-2 text-sm text-red-500">{connectionError}</p> : null}
    </div>
  )
}
