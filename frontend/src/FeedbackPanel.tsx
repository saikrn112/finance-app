import { useCallback, useEffect, useRef, useState } from 'react'
import { Check, Circle, ImagePlus, MessageSquare, Pencil, Trash2, X } from 'lucide-react'
import { api, type FeedbackAttachment, type FeedbackNote } from './api'

/**
 * Notes written while using the app, and the screenshots attached to them.
 *
 * Modelled on Timeslice's feedback sheet, minus the platform tags and the platform filter — this
 * app has one platform, so a tag whose only value is "macOS" carries no information.
 */

const stamp = new Intl.DateTimeFormat('en-US', {
  day: 'numeric',
  month: 'short',
  hour: '2-digit',
  minute: '2-digit',
})

/**
 * Image files from a clipboard or drag payload.
 *
 * Two functions rather than one taking a union: a `DataTransferItemList` yields items that have
 * to be asked for their file, a `FileList` is already files, and discriminating between them at
 * runtime was both unpleasant to read and not expressible to the type checker.
 */
function imageFilesFromItems(items: DataTransferItemList | null): File[] {
  if (!items) return []
  const files: File[] = []
  for (const item of Array.from(items)) {
    if (item.kind !== 'file') continue
    const file = item.getAsFile()
    if (file && file.type.startsWith('image/')) files.push(file)
  }
  return files
}

function imageFilesFromList(list: FileList | null): File[] {
  if (!list) return []
  return Array.from(list).filter((file) => file.type.startsWith('image/'))
}

function Thumbnail({
  src,
  alt,
  onRemove,
  onOpen,
}: {
  src: string
  alt: string
  onRemove?: () => void
  onOpen?: () => void
}) {
  const [broken, setBroken] = useState(false)
  return (
    <div className="relative shrink-0">
      {broken ? (
        // A row can outlive its file — a restored database whose images were not restored. That
        // is expected, not a failure, so it gets a placeholder rather than a broken-image icon.
        <div className="flex h-10 w-14 items-center justify-center rounded border border-slate-300 text-[9px] text-slate-500 dark:border-slate-600">
          missing
        </div>
      ) : (
        <img
          src={src}
          alt={alt}
          onError={() => setBroken(true)}
          onClick={onOpen}
          className="h-10 w-14 cursor-pointer rounded border border-slate-300 object-cover dark:border-slate-600"
        />
      )}
      {onRemove && (
        <button
          // onMouseDown with preventDefault, not onClick.
          //
          // This button is only shown while a note is being edited, and the editor commits on
          // blur. On click the sequence was: mousedown → textarea blurs → the edit commits →
          // editing ends → this button unmounts → the click never dispatches. Nothing happened,
          // with no error and no request. preventDefault stops focus moving, so there is no
          // blur, no commit, and no unmount.
          onMouseDown={(event) => {
            event.preventDefault()
            onRemove()
          }}
          title="Remove this image"
          className="absolute -right-1.5 -top-1.5 rounded-full bg-slate-900 p-0.5 text-white shadow dark:bg-slate-100 dark:text-slate-900"
        >
          <X size={9} />
        </button>
      )}
    </div>
  )
}

export function FeedbackPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [notes, setNotes] = useState<FeedbackNote[]>([])
  const [openCount, setOpenCount] = useState(0)
  const [showDone, setShowDone] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const [draft, setDraft] = useState('')
  /** Images chosen before the note exists; uploaded once it does. */
  const [pending, setPending] = useState<File[]>([])
  const [dragActive, setDragActive] = useState(false)

  /**
   * Object URLs for the pending thumbnails, kept so they can be revoked.
   *
   * `URL.createObjectURL` in the render body leaks a URL per render, and the browser holds the
   * whole image alive for each one. Created once per file and released when it goes.
   */
  const [pendingUrls, setPendingUrls] = useState<string[]>([])

  const [editingId, setEditingId] = useState<string | null>(null)
  const [editDraft, setEditDraft] = useState('')
  const composeRef = useRef<HTMLTextAreaElement | null>(null)

  const reload = useCallback(async () => {
    try {
      const list = await api.listFeedback()
      setNotes(list.items)
      setOpenCount(list.open_count)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load feedback')
    }
  }, [])

  useEffect(() => {
    if (!open) return
    void reload()
    // Focus the composer on open: the reason for opening this panel is almost always to write
    // something, and making that require a click is a small tax paid every single time.
    const timer = window.setTimeout(() => composeRef.current?.focus(), 50)
    return () => window.clearTimeout(timer)
  }, [open, reload])

  // Esc closes, unless an edit is in progress — then it cancels the edit, which is the more
  // local action and the one the user means.
  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      if (editingId) {
        setEditingId(null)
        event.stopPropagation()
      } else {
        onClose()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, editingId, onClose])

  if (!open) return null

  const visible = notes.filter((note) => showDone || note.resolved_at === null)

  function addPending(files: File[]) {
    if (!files.length) return
    setPending((current) => [...current, ...files])
    setPendingUrls((current) => [...current, ...files.map((file) => URL.createObjectURL(file))])
  }

  function dropPending(index: number) {
    setPendingUrls((current) => {
      URL.revokeObjectURL(current[index])
      return current.filter((_, i) => i !== index)
    })
    setPending((current) => current.filter((_, i) => i !== index))
  }

  function clearPending() {
    pendingUrls.forEach(URL.revokeObjectURL)
    setPendingUrls([])
    setPending([])
  }

  async function submit() {
    const body = draft.trim()
    if (!body) return
    setBusy(true)
    try {
      const note = await api.addFeedback(body)
      // Sequentially, not Promise.all: the attachment rows are ordered by created_at, and
      // concurrent uploads would land in an arbitrary order.
      for (const file of pending) {
        await api.addFeedbackAttachment(note.id, file)
      }
      setDraft('')
      clearPending()
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save the note')
    } finally {
      setBusy(false)
    }
  }

  async function attachTo(noteId: string, files: File[]) {
    setBusy(true)
    try {
      for (const file of files) await api.addFeedbackAttachment(noteId, file)
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not attach the image')
    } finally {
      setBusy(false)
    }
  }

  async function commitEdit(note: FeedbackNote) {
    const body = editDraft.trim()
    setEditingId(null)
    // An unchanged or emptied draft is not a write. The backend also treats empty as "no
    // change", but not sending the request keeps updated_at honest.
    if (!body || body === note.body) return
    try {
      await api.updateFeedback(note.id, { body })
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save the edit')
    }
  }

  async function toggleResolved(note: FeedbackNote) {
    try {
      await api.updateFeedback(note.id, { resolved: note.resolved_at === null })
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not update the note')
    }
  }

  async function remove(note: FeedbackNote) {
    try {
      await api.deleteFeedback(note.id)
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not delete the note')
    }
  }

  async function removeAttachment(attachment: FeedbackAttachment) {
    try {
      await api.deleteFeedbackAttachment(attachment.id)
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not remove the image')
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 p-4"
      onClick={onClose}
    >
      <div
        className="app-surface-strong flex max-h-[88vh] w-full max-w-3xl flex-col overflow-hidden rounded-2xl"
        onClick={(event) => event.stopPropagation()}
        // Dropping a screenshot from the desktop is the other half of pasting one.
        onDragOver={(event) => {
          event.preventDefault()
          setDragActive(true)
        }}
        onDragLeave={() => setDragActive(false)}
        onDrop={(event) => {
          event.preventDefault()
          setDragActive(false)
          addPending(imageFilesFromList(event.dataTransfer.files))
        }}
      >
        <div className="flex items-center justify-between gap-3 border-b px-4 py-3" style={{ borderColor: 'var(--border-soft)' }}>
          <div className="flex items-center gap-2">
            <MessageSquare size={16} />
            <h2 className="text-base font-semibold">Feedback</h2>
            <span className="text-xs text-slate-500">{openCount} open</span>
          </div>
          <div className="flex items-center gap-3">
            <label className="flex cursor-pointer items-center gap-1.5 text-xs text-slate-500">
              <input
                type="checkbox"
                checked={showDone}
                onChange={(event) => setShowDone(event.target.checked)}
              />
              Show done
            </label>
            <button onClick={onClose} className="app-surface rounded-full p-1.5" title="Close">
              <X size={14} />
            </button>
          </div>
        </div>

        {/* Compose. Writing a note while reading the list of notes is common enough that sending
            the user elsewhere to do it would be silly. */}
        <div className="border-b px-4 py-3" style={{ borderColor: 'var(--border-soft)' }}>
          <textarea
            ref={composeRef}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            // ⌘V lands in this textarea, so images are caught here rather than needing a button.
            onPaste={(event) => {
              const files = imageFilesFromItems(event.clipboardData.items)
              if (!files.length) return
              // Only swallow the paste when it actually carried an image; otherwise pasted text
              // would vanish.
              event.preventDefault()
              setPending((current) => [...current, ...files])
            }}
            onKeyDown={(event) => {
              // ⌘↩ submits. Plain Return inserts a newline, because notes are often more than
              // one line and losing the rest to an accidental submit is worse than one extra key.
              if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
                event.preventDefault()
                void submit()
              }
            }}
            rows={2}
            placeholder="What's wrong? Paste or drop a screenshot to attach it. ⌘↩ to add."
            className="app-input w-full resize-y"
          />
          <div className="mt-2 flex items-center gap-2">
            <label className="app-surface flex cursor-pointer items-center gap-1.5 rounded px-2 py-1 text-xs">
              <ImagePlus size={12} />
              Add image
              <input
                type="file"
                accept="image/*"
                multiple
                className="hidden"
                onChange={(event) => {
                  const files = imageFilesFromList(event.target.files)
                  addPending(files)
                  event.target.value = ''
                }}
              />
            </label>
            {pending.map((file, index) => (
              <Thumbnail
                key={`${file.name}-${index}`}
                src={pendingUrls[index]}
                // Distinguished from a saved note's thumbnail, which is fetched over http. Also
                // what lets a test tell "queued" from "stored" apart.
                alt={`pending ${file.name}`}
                onRemove={() => dropPending(index)}
              />
            ))}
            {pending.length > 0 && (
              <span className="text-[10px] text-slate-500">attached on add</span>
            )}
            <div className="flex-1" />
            <button
              onClick={() => void submit()}
              disabled={busy || !draft.trim()}
              className="app-surface rounded px-3 py-1 text-xs font-medium disabled:opacity-50"
            >
              Add
            </button>
          </div>
          {dragActive && (
            <p className="mt-2 text-xs text-slate-500">Drop the image to attach it…</p>
          )}
        </div>

        {error && (
          <div className="border-b px-4 py-2 text-xs text-rose-500" style={{ borderColor: 'var(--border-soft)' }} role="alert">
            {error}
          </div>
        )}

        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-3">
          {visible.length === 0 ? (
            <p className="py-8 text-center text-sm text-slate-500">
              {showDone ? 'Nothing yet.' : 'Nothing open.'}
            </p>
          ) : (
            <ul className="space-y-2">
              {visible.map((note) => {
                const isOpen = note.resolved_at === null
                const editing = editingId === note.id
                return (
                  <li key={note.id} className="app-card rounded-lg p-2.5">
                    <div className="flex items-start gap-2">
                      <button
                        onClick={() => void toggleResolved(note)}
                        title={isOpen ? 'Mark done' : 'Reopen'}
                        className="mt-0.5 shrink-0"
                      >
                        {isOpen ? (
                          <Circle size={14} className="text-slate-400" />
                        ) : (
                          <Check size={14} className="text-emerald-500" />
                        )}
                      </button>

                      {/* Fixed width and tabular, so the text column still lines up down the
                          list as the numbers grow. */}
                      <span className="mt-0.5 w-8 shrink-0 text-right font-mono text-[10px] text-slate-500">
                        #{note.seq}
                      </span>

                      <div className="min-w-0 flex-1">
                        {editing ? (
                          <textarea
                            autoFocus
                            value={editDraft}
                            onChange={(event) => setEditDraft(event.target.value)}
                            // Blur saves rather than silently discarding what was typed.
                            onBlur={() => void commitEdit(note)}
                            onKeyDown={(event) => {
                              if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
                                event.preventDefault()
                                void commitEdit(note)
                              }
                            }}
                            rows={Math.min(6, editDraft.split('\n').length + 1)}
                            className="app-input w-full resize-y text-sm"
                          />
                        ) : (
                          <p
                            onDoubleClick={() => {
                              setEditDraft(note.body)
                              setEditingId(note.id)
                            }}
                            className={`whitespace-pre-wrap break-words text-sm ${
                              isOpen ? '' : 'text-slate-500 line-through'
                            }`}
                          >
                            {note.body}
                          </p>
                        )}

                        <p className="mt-1 text-[10px] text-slate-500">
                          {stamp.format(new Date(note.created_at))}
                          {note.updated_at !== note.created_at && ' · edited'}
                        </p>

                        {(
                          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                            {note.attachments.map((attachment) => (
                              <Thumbnail
                                key={attachment.id}
                                src={api.feedbackAttachmentUrl(attachment.id)}
                                alt={attachment.filename}
                                onOpen={() =>
                                  window.open(api.feedbackAttachmentUrl(attachment.id), '_blank')
                                }
                                // Removable only while editing, so a stray click on a thumbnail
                                // in the list cannot delete an image.
                                onRemove={
                                  editing ? () => void removeAttachment(attachment) : undefined
                                }
                              />
                            ))}
                            {/* Always available, not only while editing. A <label> wrapping a
                                file input cannot suppress the blur the way the remove button
                                does — preventDefault would stop the picker opening — and
                                requiring an edit first bought nothing. */}
                            {(
                              <label
                                className="app-surface flex h-10 w-10 cursor-pointer items-center justify-center rounded"
                                title="Attach another image"
                              >
                                <ImagePlus size={12} />
                                <input
                                  type="file"
                                  accept="image/*"
                                  multiple
                                  className="hidden"
                                  onChange={(event) => {
                                    const files = imageFilesFromList(event.target.files)
                                    if (files.length) void attachTo(note.id, files)
                                    event.target.value = ''
                                  }}
                                />
                              </label>
                            )}
                          </div>
                        )}
                      </div>

                      {/* A visible button, not only a double-click: double-click competes with
                          selecting a word in the same text, and a near-miss reads as the app
                          ignoring you. */}
                      <button
                        onClick={() => {
                          if (editing) {
                            void commitEdit(note)
                          } else {
                            setEditDraft(note.body)
                            setEditingId(note.id)
                          }
                        }}
                        title={editing ? 'Save' : 'Edit'}
                        className="shrink-0 text-slate-500 hover:text-slate-900 dark:hover:text-slate-100"
                      >
                        {editing ? <Check size={13} /> : <Pencil size={12} />}
                      </button>
                      <button
                        onClick={() => void remove(note)}
                        title="Delete — for something written by mistake"
                        className="shrink-0 text-slate-500 hover:text-rose-500"
                      >
                        <Trash2 size={12} />
                      </button>
                    </div>
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      </div>
    </div>
  )
}
