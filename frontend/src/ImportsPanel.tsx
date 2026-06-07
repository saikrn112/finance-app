import { Upload, X } from 'lucide-react'
import { ImportWorkflow } from './ImportWorkflow'

export function ImportsPanel({
  open,
  onClose,
  onSuccess,
}: {
  open: boolean
  onClose: () => void
  onSuccess?: () => void
}) {
  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 p-4" onClick={onClose}>
      <div
        className="app-surface-strong max-h-[90vh] w-full max-w-6xl overflow-y-auto rounded-3xl p-6 shadow-2xl dark:border-slate-700 dark:bg-slate-950"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="mb-5 flex items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <div className="rounded-2xl bg-blue-500/10 p-2 text-blue-600 dark:text-blue-300">
                <Upload size={18} />
              </div>
              <h2 className="text-2xl font-semibold tracking-tight text-slate-900 dark:text-slate-100">Imports</h2>
            </div>
            <p className="mt-2 max-w-3xl text-sm text-slate-500 dark:text-slate-400">
              Pick the account first, upload one or more files, preview duplicates, then commit explicitly. Statements,
              payroll PDFs, and retirement CSVs all use the same flow.
            </p>
          </div>
          <button
            onClick={onClose}
            className="app-surface rounded-full p-2 text-slate-600 transition-colors hover:border-slate-400 hover:text-slate-900 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-300 dark:hover:border-slate-500"
          >
            <X size={18} />
          </button>
        </div>

        <ImportWorkflow onSuccess={onSuccess} />
      </div>
    </div>
  )
}
