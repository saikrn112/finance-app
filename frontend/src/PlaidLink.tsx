import { useCallback, useEffect, useState } from 'react'
import { usePlaidLink } from 'react-plaid-link'
import { Plus } from 'lucide-react'
import { api } from './api'

export function PlaidLinkButton({ onSuccess, compact, products }: { onSuccess?: () => void; compact?: boolean; products?: string }) {
  const [linkToken, setLinkToken] = useState<string>('')
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')

  useEffect(() => {
    api.getLinkToken(products)
      .then(data => {
        setLinkToken(data.link_token || '')
        setStatus(data.link_token ? 'ready' : 'error')
      })
      .catch(() => setStatus('error'))
  }, [])

  const handleSuccess = useCallback(async (public_token: string, metadata: any) => {
    const institutionName = metadata?.institution?.name || ""
    await api.exchangeToken(public_token, institutionName)
    await api.syncPlaid()
    onSuccess?.()
  }, [onSuccess])

  const { open, ready } = usePlaidLink({ token: linkToken, onSuccess: handleSuccess })

  if (status === 'loading') {
    return compact ? null : <button disabled className="px-3 py-2 bg-gray-300 rounded text-sm">Loading...</button>
  }

  if (status === 'error') {
    return compact ? null : (
      <button 
        onClick={() => alert('Add Plaid credentials to config.yaml')}
        className="px-3 py-2 bg-gray-400 text-white rounded text-sm"
      >
        Setup Plaid
      </button>
    )
  }

  if (compact) {
    return (
      <button
        onClick={() => open()}
        disabled={!ready}
        className="p-1.5 bg-blue-500 text-white rounded hover:bg-blue-600 disabled:opacity-50"
        title="Connect another bank"
      >
        <Plus size={16} />
      </button>
    )
  }

  return (
    <button
      onClick={() => open()}
      disabled={!ready}
      className="px-3 py-2 bg-blue-500 text-white rounded text-sm hover:bg-blue-600 disabled:opacity-50"
    >
      + Connect Bank
    </button>
  )
}
