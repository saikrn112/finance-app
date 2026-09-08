import { useEffect, useRef, useState } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Dashboard } from './Dashboard'
import { api } from './api'
import './index.css'
// Scoped to html.platform-macos, which only the desktop shell adds. Inert in a browser.
import './macos.css'

const queryClient = new QueryClient()

function GoogleDriveCallback() {
  const params = new URLSearchParams(window.location.search)
  const code = params.get('code')
  const state = params.get('state')
  const [message, setMessage] = useState('Completing Google Drive connection...')
  const startedRef = useRef(false)

  useEffect(() => {
    if (!code || !state) return
    if (startedRef.current) return
    startedRef.current = true

    api.completeGoogleDriveConnect(code, state)
      .then(() => {
        try {
          window.opener?.postMessage({ type: 'vault-google-connected' }, '*')
        } catch {
          // noop
        }
        setMessage('Google Drive connected! You can close this tab.')
        window.setTimeout(() => window.close(), 300)
      })
      .catch((error) => {
        setMessage(error instanceof Error ? error.message : 'Google Drive connection failed')
      })
  }, [code, state])

  return (
    <div className="app-shell min-h-screen dark:bg-gray-900 text-gray-900 dark:text-gray-100 flex items-center justify-center px-6">
      <div className="app-surface max-w-md rounded-xl dark:border-gray-700 dark:bg-gray-800 p-6">
        <h1 className="text-lg font-semibold mb-2">Google Drive</h1>
        <p className="text-sm text-gray-600 dark:text-gray-300">{message}</p>
      </div>
    </div>
  )
}

export default function App() {
  const params = new URLSearchParams(window.location.search)
  const isGoogleCallback = Boolean(params.get('code') && params.get('state'))

  return (
    <QueryClientProvider client={queryClient}>
      <div className="min-h-screen dark:bg-gray-900">
        {isGoogleCallback ? <GoogleDriveCallback /> : <Dashboard />}
      </div>
    </QueryClientProvider>
  )
}
