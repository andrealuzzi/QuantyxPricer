import React from 'react'

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
}) {
  return (
    <aside className="sidebar">
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
        <button className="clear-btn" onClick={clearAll}>Clear filters</button>
      </div>
    </aside>
  )
}
