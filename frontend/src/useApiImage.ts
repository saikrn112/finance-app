import { useEffect, useState } from 'react'

/**
 * An image URL that works in both apps, for images served by our own API.
 *
 * In the macOS app every API request must carry the session token. The shell attaches it by
 * wrapping `fetch` and `XMLHttpRequest`; a cookie was tried and WKWebView never sends one to
 * `127.0.0.1` (see AppWebView.swift). An `<img src>` goes through neither, so every bank logo and
 * feedback screenshot came back 401 and fell back to a placeholder -- letters in the sidebar where
 * the web app showed logos. Fetching the bytes and handing `<img>` a blob URL routes the image
 * through the authenticated path, without exempting image routes from the token gate (which would
 * let any local process learn which banks are connected).
 *
 * In a browser this costs one fetch per distinct URL, shared across every component that shows it.
 */
const cache = new Map<string, Promise<string>>()

function isApiPath(src: string): boolean {
  try {
    const url = new URL(src, window.location.href)
    return url.origin === window.location.origin && url.pathname.startsWith('/api/')
  } catch {
    return false
  }
}

function load(src: string): Promise<string> {
  let pending = cache.get(src)
  if (!pending) {
    pending = fetch(src).then(async (response) => {
      if (!response.ok) throw new Error(`image ${response.status}`)
      return URL.createObjectURL(await response.blob())
    })
    // A failure must not be cached: a transient error would otherwise stick for the session.
    pending.catch(() => cache.delete(src))
    cache.set(src, pending)
  }
  return pending
}

/** `{ url, failed }`. `url` is null while loading; non-API URLs pass straight through. */
export function useApiImage(src: string | null | undefined): { url: string | null; failed: boolean } {
  const direct = !src || !isApiPath(src)
  const [state, setState] = useState<{ url: string | null; failed: boolean }>({
    url: direct ? src ?? null : null,
    failed: false,
  })

  useEffect(() => {
    if (!src) {
      setState({ url: null, failed: false })
      return
    }
    if (!isApiPath(src)) {
      setState({ url: src, failed: false })
      return
    }
    let active = true
    setState({ url: null, failed: false })
    load(src).then(
      (url) => { if (active) setState({ url, failed: false }) },
      () => { if (active) setState({ url: null, failed: true }) },
    )
    return () => { active = false }
  }, [src])

  return state
}
