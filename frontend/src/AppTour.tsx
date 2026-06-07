import { useEffect, useState, useCallback, useRef } from 'react'
import { createPortal } from 'react-dom'

export const GETTING_STARTED_DONE_KEY = 'finance-app-getting-started-done'
export const GETTING_STARTED_DASHBOARD_KEY = 'finance-app-getting-started-dashboard-read'

interface Props {
  open: boolean
  onClose: () => void
  onComplete: () => void
}

interface TourStop {
  selector: string
  title: string
  description: string
  /** If set, replace "Next" with instructional text and auto-advance when this selector appears in DOM */
  waitForSelector?: string
  /** Describes the action the user should perform (shown in tooltip when waiting) */
  waitForAction?: string
}

const TOUR_STOPS: TourStop[] = [
  // Phase 1: Overview
  {
    selector: '[data-tour="tour-dashboard"]',
    title: 'Your Financial Dashboard',
    description: 'Spending trends, categories, merchants, and transaction ledger all live here.',
  },
  {
    selector: '[data-tour="tour-main-chart"]',
    title: 'Spending Over Time',
    description: 'Your spending over time. Hover for details, drag to zoom into a range.',
  },
  {
    selector: '[data-tour="sidebar"]',
    title: 'Sidebar Navigation',
    description: 'Your navigation lives here. Click it to expand.',
    waitForSelector: '[data-tour-expanded="true"]',
    waitForAction: 'Click the sidebar to expand it',
  },
  // Phase 2: Sidebar Deep Dive
  {
    selector: '[data-tour="tour-accounts-section"]',
    title: 'Your Accounts',
    description: 'Your bank accounts and credit cards. Click any account to filter transactions to just that source.',
  },
  {
    selector: '[data-tour="tour-investments-section"]',
    title: 'Investments',
    description: 'Investment and retirement accounts appear here once connected.',
  },
  {
    selector: '[data-tour="tour-sync"]',
    title: 'Sync Data',
    description: 'Pull latest data from all connected banks with one click.',
  },
  // Phase 3: Settings
  {
    selector: '[data-tour="tour-settings"]',
    title: 'Settings',
    description: 'Your workspace settings. Click to open.',
    waitForSelector: '[data-tour="tour-settings-panel"]',
    waitForAction: 'Click the settings button to open it',
  },
  // Phase 4: Inside Settings
  {
    selector: '[data-tour="tour-vault-section"]',
    title: 'Vault Backup',
    description: 'Your data backs up to Google Drive. Restore on any device.',
  },
  {
    selector: '[data-tour="tour-plaid-section"]',
    title: 'Connected Institutions',
    description: 'Connect bank accounts here via Plaid for automatic transaction sync.',
  },
  // Phase 5: Imports
  {
    selector: '[data-tour="tour-imports"]',
    title: 'Imports',
    description: 'For banks not on Plaid, upload statements and CSVs here. Click to open.',
    waitForSelector: '[data-tour="tour-import-upload"]',
    waitForAction: 'Click the imports button to open it',
  },
  // Phase 6: Inside Imports
  {
    selector: '[data-tour="tour-import-upload"]',
    title: 'Upload Area',
    description: 'Drag a PDF statement or CSV export here. The app auto-detects the format.',
  },
  // Phase 7: Wrap-up
  {
    selector: '__none__',
    title: 'You\'re All Set!',
    description: 'Explore at your own pace. You can reopen this tour from the sidebar anytime.',
  },
]

const TOTAL_STEPS = TOUR_STOPS.length

type Placement = 'bottom' | 'left' | 'top' | 'right'

export function AppTour({ open, onClose, onComplete }: Props) {
  const [step, setStep] = useState(0)
  const [targetRect, setTargetRect] = useState<DOMRect | null>(null)
  const [tooltipPos, setTooltipPos] = useState<{ top: number; left: number }>({ top: 0, left: 0 })
  const [placement, setPlacement] = useState<Placement>('bottom')
  const [arrowOffset, setArrowOffset] = useState(0)
  const [waitingForAction, setWaitingForAction] = useState(false)
  const tooltipRef = useRef<HTMLDivElement>(null)
  const observerRef = useRef<MutationObserver | null>(null)

  const currentStop = TOUR_STOPS[step]
  const isLastStep = step === TOTAL_STEPS - 1
  const isFinalNoTarget = currentStop?.selector === '__none__'

  // Measure current target element position
  const measureTarget = useCallback(() => {
    if (!open) return
    const stop = TOUR_STOPS[step]
    if (stop.selector === '__none__') {
      setTargetRect(null)
      return
    }
    const el = document.querySelector(stop.selector)
    if (el) {
      setTargetRect(el.getBoundingClientRect())
    } else {
      setTargetRect(null)
    }
  }, [open, step])

  // Setup resize/scroll listeners and measure on step change
  useEffect(() => {
    if (!open) {
      setStep(0)
      setWaitingForAction(false)
      return
    }
    measureTarget()
    const interval = setInterval(measureTarget, 200)
    window.addEventListener('resize', measureTarget)
    window.addEventListener('scroll', measureTarget, true)
    return () => {
      clearInterval(interval)
      window.removeEventListener('resize', measureTarget)
      window.removeEventListener('scroll', measureTarget, true)
    }
  }, [open, step, measureTarget])

  // Handle "waitForSelector" logic using MutationObserver
  useEffect(() => {
    if (!open) return
    const stop = TOUR_STOPS[step]
    if (!stop.waitForSelector) {
      setWaitingForAction(false)
      return
    }

    // Check if the target already exists
    const existing = document.querySelector(stop.waitForSelector)
    if (existing) {
      // Auto-advance immediately
      setWaitingForAction(false)
      setStep((s) => Math.min(s + 1, TOTAL_STEPS - 1))
      return
    }

    // Otherwise, start waiting
    setWaitingForAction(true)

    const observer = new MutationObserver(() => {
      const el = document.querySelector(stop.waitForSelector!)
      if (el) {
        observer.disconnect()
        setWaitingForAction(false)
        setStep((s) => Math.min(s + 1, TOTAL_STEPS - 1))
      }
    })

    observer.observe(document.body, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['data-tour', 'data-tour-expanded'],
    })

    observerRef.current = observer
    return () => {
      observer.disconnect()
      observerRef.current = null
    }
  }, [open, step])

  // Position tooltip relative to target
  useEffect(() => {
    if (!tooltipRef.current) return

    // For no-target (final) step, center the tooltip
    if (isFinalNoTarget) {
      const tooltip = tooltipRef.current
      const tRect = tooltip.getBoundingClientRect()
      setTooltipPos({
        top: window.innerHeight / 2 - tRect.height / 2,
        left: window.innerWidth / 2 - tRect.width / 2,
      })
      setPlacement('bottom')
      setArrowOffset(0)
      return
    }

    if (!targetRect) return

    const tooltip = tooltipRef.current
    const tRect = tooltip.getBoundingClientRect()
    const pad = 14
    const arrow = 10

    // Try below
    let top = targetRect.bottom + pad + arrow
    let left = targetRect.left + targetRect.width / 2 - tRect.width / 2
    let place: Placement = 'bottom'

    // Clamp horizontal
    left = Math.max(pad, Math.min(left, window.innerWidth - pad - tRect.width))

    if (top + tRect.height > window.innerHeight - pad) {
      // Try left of target
      top = targetRect.top + targetRect.height / 2 - tRect.height / 2
      left = targetRect.left - tRect.width - pad - arrow
      place = 'left'

      if (left < pad) {
        // Try right
        left = targetRect.right + pad + arrow
        place = 'right'
        if (left + tRect.width > window.innerWidth - pad) {
          // Fall back to above
          left = targetRect.left + targetRect.width / 2 - tRect.width / 2
          top = targetRect.top - tRect.height - pad - arrow
          place = 'top'
        }
      }

      // Clamp
      top = Math.max(pad, Math.min(top, window.innerHeight - pad - tRect.height))
      left = Math.max(pad, Math.min(left, window.innerWidth - pad - tRect.width))
    }

    // Compute arrow offset
    let offset = 0
    if (place === 'bottom' || place === 'top') {
      offset = targetRect.left + targetRect.width / 2 - left
      offset = Math.max(16, Math.min(offset, tRect.width - 16))
    } else {
      offset = targetRect.top + targetRect.height / 2 - top
      offset = Math.max(16, Math.min(offset, tRect.height - 16))
    }

    setTooltipPos({ top, left })
    setPlacement(place)
    setArrowOffset(offset)
  }, [targetRect, isFinalNoTarget])

  const handleNext = () => {
    if (isLastStep) {
      onComplete()
      return
    }
    // If current step has a waitForSelector, don't advance via button
    if (currentStop.waitForSelector && waitingForAction) {
      return
    }
    setStep(step + 1)
  }

  const handleSkip = () => {
    onClose()
  }

  if (!open) return null

  const overlay = (
    <div className="fixed inset-0" style={{ zIndex: 10000, pointerEvents: waitingForAction ? 'none' : undefined }}>
      {/* Overlay with spotlight cutout — pointer-events:none allows clicks through */}
      {targetRect && !isFinalNoTarget && (
        <div
          className="fixed inset-0 transition-all duration-300 ease-in-out"
          style={{
            pointerEvents: 'none',
            zIndex: 10000,
          }}
        >
          <div
            className="transition-all duration-300 ease-in-out"
            style={{
              position: 'fixed',
              top: targetRect.top - 6,
              left: targetRect.left - 6,
              width: targetRect.width + 12,
              height: targetRect.height + 12,
              borderRadius: 10,
              boxShadow: '0 0 0 9999px rgba(0, 0, 0, 0.75)',
              pointerEvents: 'none',
            }}
          />
        </div>
      )}

      {/* Dark overlay for no-target final step */}
      {isFinalNoTarget && (
        <div
          className="fixed inset-0 bg-black/75 transition-opacity duration-300"
          style={{ zIndex: 10000, pointerEvents: 'none' }}
        />
      )}

      {/* Click catcher: only active when NOT waiting for user action */}
      {!waitingForAction && (
        <div
          className="fixed inset-0"
          style={{ zIndex: 10001 }}
          onClick={onClose}
        />
      )}

      {/* Tooltip */}
      <div
        ref={tooltipRef}
        className="app-surface-strong dark:bg-slate-800 rounded-xl dark:border-slate-700 shadow-2xl p-5 max-w-sm transition-all duration-300 ease-in-out"
        style={{
          position: 'fixed',
          top: tooltipPos.top,
          left: tooltipPos.left,
          zIndex: 10003,
          pointerEvents: 'auto',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Arrow */}
        {!isFinalNoTarget && targetRect && (
          <>
            {placement === 'bottom' && (
              <div
                className="absolute border-8 border-transparent border-b-white dark:border-b-slate-800"
                style={{ top: -16, left: arrowOffset - 8 }}
              />
            )}
            {placement === 'top' && (
              <div
                className="absolute border-8 border-transparent border-t-white dark:border-t-slate-800"
                style={{ bottom: -16, left: arrowOffset - 8 }}
              />
            )}
            {placement === 'left' && (
              <div
                className="absolute border-8 border-transparent border-l-white dark:border-l-slate-800"
                style={{ right: -16, top: arrowOffset - 8 }}
              />
            )}
            {placement === 'right' && (
              <div
                className="absolute border-8 border-transparent border-r-white dark:border-r-slate-800"
                style={{ left: -16, top: arrowOffset - 8 }}
              />
            )}
          </>
        )}

        {/* Step counter */}
        <div className="mb-2">
          <span className="text-xs font-medium text-slate-400 dark:text-slate-500">
            Step {step + 1} of {TOTAL_STEPS}
          </span>
        </div>

        {/* Title with optional checkmark on final step */}
        <h3 className="text-sm font-semibold text-slate-900 dark:text-slate-100 mb-1.5 flex items-center gap-2">
          {isLastStep && (
            <svg className="w-5 h-5 text-emerald-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
          )}
          {currentStop.title}
        </h3>

        <p className="text-sm text-slate-600 dark:text-slate-300 mb-4 leading-relaxed">
          {currentStop.description}
        </p>

        {/* Action area */}
        <div className="flex items-center justify-between">
          <button
            onClick={handleSkip}
            className="text-xs text-slate-400 dark:text-slate-500 hover:text-slate-700 dark:hover:text-slate-200 transition-colors"
          >
            Skip tour
          </button>

          {waitingForAction && currentStop.waitForAction ? (
            <span className="text-xs italic text-blue-500 dark:text-blue-400 font-medium animate-pulse">
              {currentStop.waitForAction} →
            </span>
          ) : (
            <button
              onClick={handleNext}
              className={`rounded-lg px-4 py-2 text-xs font-medium transition-colors ${
                isLastStep
                  ? 'bg-emerald-600 text-white hover:bg-emerald-500'
                  : 'bg-blue-600 text-white hover:bg-blue-500'
              }`}
            >
              {isLastStep ? 'Finish' : 'Next'}
            </button>
          )}
        </div>
      </div>
    </div>
  )

  return createPortal(overlay, document.body)
}
