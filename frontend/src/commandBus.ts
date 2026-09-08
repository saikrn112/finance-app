/**
 * A named-command registry, so a host shell has exactly one way to drive the app.
 *
 * The macOS app's menu bar needs to trigger in-app actions — navigate, focus the search
 * field, open Settings, run a sync. Without a bus the shell would have to synthesise
 * clicks on CSS selectors, which breaks silently the first time a class name changes and
 * gives no way to know whether the action ran.
 *
 * This is intentionally generic: nothing here knows about macOS, and in a browser it is
 * inert (the shell simply never dispatches). One dispatch path, one place to look when a
 * shortcut stops working.
 */

export type CommandHandler = (argument?: unknown) => void | Promise<void>

const handlers = new Map<string, CommandHandler>()

/** Register a command. Returns an unregister function, for use in an effect's cleanup. */
export function registerCommand(name: string, handler: CommandHandler): () => void {
  handlers.set(name, handler)
  return () => {
    // Only delete if it is still ours: in React StrictMode an effect runs twice, and a
    // blind delete would unregister the *second* registration during the first cleanup.
    if (handlers.get(name) === handler) handlers.delete(name)
  }
}

export function registerCommands(map: Record<string, CommandHandler>): () => void {
  const cleanups = Object.entries(map).map(([name, handler]) => registerCommand(name, handler))
  return () => cleanups.forEach((cleanup) => cleanup())
}

/** Returns whether a handler existed, so a host can tell "did nothing" from "not wired". */
export async function dispatchCommand(name: string, argument?: unknown): Promise<boolean> {
  const handler = handlers.get(name)
  if (!handler) return false
  await handler(argument)
  return true
}

export function commandNames(): string[] {
  return [...handlers.keys()].sort()
}

declare global {
  interface Window {
    __financeCommandBus?: {
      dispatch: (name: string, argument?: unknown) => Promise<boolean>
      list: () => string[]
    }
  }
}

/**
 * Expose the bus on `window` for the host shell.
 *
 * Idempotent, and safe to call from a module top level: in a plain browser nothing ever
 * calls it, so the only cost is two properties on `window`.
 */
export function installCommandBus(): void {
  window.__financeCommandBus = {
    dispatch: dispatchCommand,
    list: commandNames,
  }
}
