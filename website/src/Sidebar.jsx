import React, { useState } from 'react'
import FileUploader from './FileUploader'

export default function Sidebar({
  models = [],
  currencies = [],
  filterInstrument,
  setFilterInstrument,
  filterModel,
  setFilterModel,
  filterCurrency,
  setFilterCurrency,
  clearAll,
  onPriceAll,
  pricingAll,
  onUpdateCurves,
  updatingCurves,
  apiBase,
}) {
  const [showUploader, setShowUploader] = useState(false)
  return (
    <aside className="sidebar">
      <div style={{ marginBottom: 12 }}>
        <div style={{ display: 'flex', gap: 8 }}>
          <button
            className="clear-btn"
            onClick={onPriceAll}
            disabled={pricingAll}
            title="Price all instruments"
          >
            {pricingAll ? '⏳ Pricing...' : '⏱️ Price All'}
          </button>
          <button
            className="clear-btn clear-btn--update-curves"
            onClick={onUpdateCurves}
            disabled={updatingCurves}
            title="Update swap curves (ECB)"
          >
            {updatingCurves ? '⏳ Updating...' : '💾 Update Curves'}
          </button>
        </div>
      </div>
      <h3 className="sidebar-title">Filters</h3>

      <div className="filter-group">
        <label>Instrument ID</label>
        <input
          list="instrument-ids"
          value={filterInstrument}
          onChange={(e) => setFilterInstrument(e.target.value)}
          placeholder="Type or pick..."
        />
      </div>

      <div className="filter-group">
        <label>Model</label>
        <select value={filterModel} onChange={(e) => setFilterModel(e.target.value)}>
          <option value="">(all)</option>
          {models.map((m, i) => <option key={i} value={m}>{m}</option>)}
        </select>
      </div>

      <div className="filter-group">
        <label>Currency</label>
        <select value={filterCurrency} onChange={(e) => setFilterCurrency(e.target.value)}>
          <option value="">(all)</option>
          {currencies.map((c, i) => <option key={i} value={c}>{c}</option>)}
        </select>
      </div>

      <div style={{ marginTop: 12 }}>
        <div style={{ display: 'flex', gap: 8 }}>
          <button className="clear-btn" onClick={() => setShowUploader(true)}>Add</button>
          <button className="clear-btn" onClick={clearAll}>Clear filters</button>
        </div>
        {showUploader && <FileUploader onClose={() => setShowUploader(false)} onSaved={(r) => { console.log('Saved asset:', r) }} />}
      </div>

      <div style={{ marginTop: 16 }}>
        <a
          className="clear-btn clear-btn--api"
          href={`${(apiBase || '').replace(/\/$/, '')}/docs`}
          target="_blank"
          rel="noreferrer"
          style={{ display: 'inline-block', textDecoration: 'none' }}
          title="Open API documentation"
        >
          API
        </a>
      </div>
    </aside>
  )
}
