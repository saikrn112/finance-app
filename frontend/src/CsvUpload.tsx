import { useState, useCallback, useEffect } from 'react'
import { Upload } from 'lucide-react'
import { api } from './api'

interface SourceOption {
  value: string
  label: string
  group: string
}

export function CsvUpload({ onSuccess }: { onSuccess?: () => void }) {
  const [dragging, setDragging] = useState(false)
  const [source, setSource] = useState('')
  const [sources, setSources] = useState<SourceOption[]>([])
  const [status, setStatus] = useState<string | null>(null)

  useEffect(() => {
    fetch('/api/imports/sources')
      .then((r) => r.json())
      .then((data: SourceOption[]) => {
        setSources(data)
        if (data.length > 0 && !source) setSource(data[0].value)
      })
      .catch(() => {})
  }, [])

  const handleFile = useCallback(async (file: File) => {
    setStatus('Uploading...')
    try {
      const result = await api.uploadCsv(file, source)
      setStatus(`Imported ${result.imported}, skipped ${result.skipped}`)
      onSuccess?.()
    } catch {
      setStatus('Upload failed')
    }
  }, [source, onSuccess])

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    setDragging(false)
    const file = e.dataTransfer.files[0]
    if (file?.name.endsWith('.csv')) handleFile(file)
  }, [handleFile])

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file) handleFile(file)
  }

  return (
    <div className="space-y-3">
      <div className="flex gap-2 items-center">
        <label className="text-sm">Source:</label>
        <select
          value={source}
          onChange={(e) => setSource(e.target.value)}
          className="px-2 py-1 rounded border text-sm dark:bg-gray-700 dark:border-gray-600"
        >
          {sources.map((s) => (
            <option key={s.value} value={s.value}>{s.label}</option>
          ))}
        </select>
      </div>
      <div
        onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
        className={`border-2 border-dashed rounded-lg p-6 text-center cursor-pointer transition-colors
          ${dragging ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20' : 'border-[var(--app-border)] dark:border-gray-600'}`}
      >
        <input type="file" accept=".csv" onChange={handleChange} className="hidden" id="csv-input" />
        <label htmlFor="csv-input" className="cursor-pointer">
          <Upload className="mx-auto mb-2 text-gray-400" size={24} />
          <p className="text-sm text-gray-500">Drop CSV or click to upload</p>
        </label>
      </div>
      {status && <p className="text-sm text-center text-gray-500">{status}</p>}
    </div>
  )
}
