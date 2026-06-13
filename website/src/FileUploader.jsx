import React, { useState, useCallback } from 'react'

export default function FileUploader({ onClose, onSaved }) {
  const [dragOver, setDragOver] = useState(false)
  const [error, setError] = useState(null)
  const [saving, setSaving] = useState(false)
  const [mode, setMode] = useState('json')

  const apiBase = import.meta.env.VITE_API_URL || ''

  const uploadJson = useCallback(async (obj) => {
    setSaving(true)
    setError(null)
    try {
      const res = await fetch(`${apiBase}/assets`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(obj)
      })
      if (!res.ok) {
        const txt = await res.text()
        throw new Error(txt || 'Upload failed')
      }
      const j = await res.json()
      setSaving(false)
      onSaved && onSaved(j)
      onClose()
    } catch (e) {
      setSaving(false)
      setError(String(e))
    }
  }, [apiBase, onClose, onSaved])

  const uploadTermsheet = useCallback(async (file) => {
    setSaving(true)
    setError(null)
    try {
      const form = new FormData()
      form.append('file', file)
      const res = await fetch(`${apiBase}/termsheet_asset`, {
        method: 'POST',
        body: form,
      })
      if (!res.ok) {
        const txt = await res.text()
        throw new Error(txt || 'Termsheet upload failed')
      }
      const j = await res.json()
      setSaving(false)
      onSaved && onSaved(j)
      onClose()
    } catch (e) {
      setSaving(false)
      setError(String(e))
    }
  }, [apiBase, onClose, onSaved])

  const handleFile = async (file) => {
    setError(null)
    if (mode === 'termsheet') {
      if (!file.name.toLowerCase().endsWith('.pdf')) {
        setError('Invalid file type: termsheet mode expects a PDF file')
        return
      }
      await uploadTermsheet(file)
      return
    }
    try {
      const text = await file.text()
      const obj = JSON.parse(text)
      await uploadJson(obj)
    } catch (e) {
      setError('Invalid JSON: ' + e.message)
    }
  }

  const onDrop = (e) => {
    e.preventDefault()
    setDragOver(false)
    const f = e.dataTransfer.files && e.dataTransfer.files[0]
    if (f) handleFile(f)
  }

  return (
    <div className="uploader-backdrop" onClick={onClose}>
      <div className="uploader-modal" onClick={(e) => e.stopPropagation()}>
        <h3>Upload Asset</h3>
        <div style={{ marginBottom: 10, display: 'flex', gap: 14 }}>
          <label>
            <input
              type="radio"
              name="upload-mode"
              value="json"
              checked={mode === 'json'}
              onChange={() => setMode('json')}
            />{' '}
            JSON
          </label>
          <label>
            <input
              type="radio"
              name="upload-mode"
              value="termsheet"
              checked={mode === 'termsheet'}
              onChange={() => setMode('termsheet')}
            />{' '}
            Termsheet (PDF)
          </label>
        </div>
        <div
          className={`dropzone ${dragOver ? 'dragover' : ''}`}
          onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
          onDragLeave={() => setDragOver(false)}
          onDrop={onDrop}
        >
          <p>{mode === 'json' ? 'Drop a bond file here, or' : 'Drop a termsheet PDF file here, or'}</p>
          <input
            type="file"
            accept={mode === 'json' ? 'application/json,.json' : 'application/pdf,.pdf'}
            onChange={(e) => { if (e.target.files[0]) handleFile(e.target.files[0]) }}
          />
        </div>
        {error && <div className="upload-error">{error}</div>}
        <div style={{ marginTop: 12, display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button onClick={onClose} className="clear-btn">Cancel</button>
          <button onClick={() => { onClose() }} disabled={saving} className="clear-btn">Close</button>
        </div>
      </div>
    </div>
  )
}
