import React, { useEffect, useState } from 'react'
import logo from '../logo_q.png'
import Instrument from './Instrument'
import Sidebar from './Sidebar'

function fmt(v) {
  if (v == null) return ''
  if (typeof v === 'number') return v.toFixed(3)
  return String(v)
}

function fmtPct(v) {
  if (v == null) return ''
  if (typeof v === 'number') return (v * 100).toFixed(3) + '%'
  // try parse
  const n = Number(v)
  if (!isNaN(n)) return (n * 100).toFixed(3) + '%'
  return String(v)
}

export default function App() {
  const [rows, setRows] = useState(null)
  const [error, setError] = useState(null)
  const [filterInstrument, setFilterInstrument] = useState('')
  const [filterModel, setFilterModel] = useState('')
  const [filterCurrency, setFilterCurrency] = useState('')
  const [route, setRoute] = useState(() => {
    const h = window.location.hash || ''
    if (h.startsWith('#/instrument/')) return h.replace('#/instrument/', '')
    return null
  })

  useEffect(() => {
    const tryPaths = ['/prices.json', 'prices.json']
    let mounted = true

    async function fetchOne() {
      for (const p of tryPaths) {
        try {
          const r = await fetch(p)
          if (!r.ok) continue
          const data = await r.json()
          if (!mounted) return
          setRows(data)
          return
        } catch (e) {
          continue
        }
      }
      if (mounted) setError('Could not fetch output/prices.json from server')
    }

    fetchOne()
    return () => { mounted = false }
  }, [])

  useEffect(() => {
    function onHash() {
      const h = window.location.hash || ''
      if (h.startsWith('#/instrument/')) setRoute(h.replace('#/instrument/', ''))
      else setRoute(null)
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  if (error) return <div className="error">Error: {error}</div>
  if (!rows) return <div>Loading data...</div>

  if (route) {
    return <Instrument instrumentId={route} />
  }

  return (
    <div>
      <h1>
        <img src={logo} alt="Quantyx" style={{ height: 32, verticalAlign: 'middle', marginRight: 8 }} />
        Quantyx Pricer
      </h1>
      <p style={{ marginTop: 4 }}>Click an Instrument ID to view its details.</p>
      <div className="app-layout">
        <Sidebar
          models={Array.from(new Set(rows.map(r => r.model || (r.result && r.result.model) || '').filter(Boolean)))}
          currencies={Array.from(new Set(rows.map(r => r.currency || (r.result && r.result.currency) || '').filter(Boolean)))}
          filterInstrument={filterInstrument}
          setFilterInstrument={setFilterInstrument}
          filterModel={filterModel}
          setFilterModel={setFilterModel}
          filterCurrency={filterCurrency}
          setFilterCurrency={setFilterCurrency}
          clearAll={() => { setFilterInstrument(''); setFilterModel(''); setFilterCurrency('') }}
        />
        <div className="main-panel">
          <datalist id="instrument-ids">
            {rows && rows.map((r, i) => {
              const id = r.instrument_id || (r.result && r.result.instrument_id) || r.bond_file || ''
              return id ? <option key={i} value={id} /> : null
            })}
          </datalist>

          
      
      
      <table>
        <thead>
          <tr>
            <th>Instrument ID</th>
            <th>Currency</th>
            <th className="center">PV </th>
            <th className="center">PV to worst</th>
            <th className="center">PV to maturity</th>
            <th className="center">YTM</th>
            <th>Model</th>
          </tr>
        </thead>
        <tbody>
            {rows
              .filter(r => {
                if (filterInstrument) {
                  const id = r.instrument_id || (r.result && r.result.instrument_id) || r.bond_file || ''
                  if (id !== filterInstrument) return false
                }
                if (filterModel) {
                  const model = r.model || (r.result && r.result.model) || ''
                  if (model !== filterModel) return false
                }
                if (filterCurrency) {
                  const cur = r.currency || (r.result && r.result.currency) || ''
                  if (cur !== filterCurrency) return false
                }
                return true
              })
              .map((r, i) => {
            const res = r.result || {}
            const pp = res.price_pct || {}
            const colPV = pp.pv_note ?? res.pv_note ?? res.selected_npv
            const colWorst = pp.pv_note_to_worst ?? pp.pv_note_to_worst_call ?? res.npv_to_worst_call ?? ''
            const colMat = pp.pv_note_to_maturity ?? res.npv_to_maturity ?? ''
            return (
              <tr key={i}>
                <td className="mono">
                  <a href={`#/instrument/${r.instrument_id || res.instrument_id || r.bond_file || ''}${r.bond_file ? '::' + r.bond_file : ''}`}>{r.instrument_id || res.instrument_id || r.bond_file || ''}</a>
                </td>
                <td>{r.currency || res.currency || ''}</td>
                <td className="center">{fmt(colPV)}</td>
                <td className="center">{fmt(colWorst)}</td>
                <td className="center">{fmt(colMat)}</td>
                <td className="center">{fmtPct(res.ytm ?? res.ytm_expected ?? res.model_ytm_to_maturity ?? res.yield_to_maturity)}</td>
                <td>{r.model || res.model || ''}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
        </div>
      </div>
    </div>
  )
}
