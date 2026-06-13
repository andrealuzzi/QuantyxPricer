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
  // Prefer VITE_API_URL. In production mode, default to Azure backend.
  const apiBase = (() => {
    if (typeof import.meta === 'undefined' || !import.meta.env) return ''
    if (import.meta.env.VITE_API_URL) return String(import.meta.env.VITE_API_URL).replace(/\/$/, '')
    if (import.meta.env.MODE === 'production') {
      return 'https://lux-pricer-eta2cxamh7evctdv.switzerlandnorth-01.azurewebsites.net'
    }
    return ''
  })()
  const [rows, setRows] = useState(null)
  const [missingInstrumentIds, setMissingInstrumentIds] = useState([])
  const [error, setError] = useState(null)
  const [pricingIds, setPricingIds] = useState([])
  const [snack, setSnack] = useState({ visible: false, message: '', type: 'info' })
  const [snackHiding, setSnackHiding] = useState(false)
  const [pricingAll, setPricingAll] = useState(false)
  const [updatingCurves, setUpdatingCurves] = useState(false)
  const [filterInstrument, setFilterInstrument] = useState('')
  const [filterModel, setFilterModel] = useState('')
  const [filterCurrency, setFilterCurrency] = useState('')
  const [route, setRoute] = useState(() => {
    const h = window.location.hash || ''
    if (h.startsWith('#/instrument/')) return h.replace('#/instrument/', '')
    return null
  })

  useEffect(() => {
    const tryPaths = [
      apiBase ? `${apiBase}/prices` : '/prices',
      apiBase ? `${apiBase}/prices.json` : '/prices.json',
      'prices.json'
    ]
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
        } catch {
          continue
        }
      }
      if (mounted) setError('Could not fetch output/prices.json from server')
    }

    fetchOne()
    return () => { mounted = false }
  }, [apiBase])

  useEffect(() => {
    let mounted = true

    async function fetchMissing() {
      const endpoint = apiBase ? `${apiBase}/fetch_noprice_assets` : '/fetch_noprice_assets'
      try {
        const resp = await fetch(endpoint)
        if (!resp.ok) return
        const data = await resp.json()
        if (!mounted) return
        setMissingInstrumentIds(Array.isArray(data.missing_instrument_ids) ? data.missing_instrument_ids : [])
      } catch {
        if (mounted) setMissingInstrumentIds([])
      }
    }

    fetchMissing()
    return () => { mounted = false }
  }, [apiBase, rows])

  useEffect(() => {
    function onHash() {
      const h = window.location.hash || ''
      if (h.startsWith('#/instrument/')) setRoute(h.replace('#/instrument/', ''))
      else setRoute(null)
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  // auto-hide snackbar with hide animation
  useEffect(() => {
    let hideTimer = null
    let removeTimer = null
    if (snack.visible) {
      // schedule start hiding after 4s
      hideTimer = setTimeout(() => setSnackHiding(true), 4000)
    }
    if (snackHiding) {
      // after hide animation duration, actually remove the snackbar
      removeTimer = setTimeout(() => {
        setSnack({ visible: false, message: '', type: 'info' })
        setSnackHiding(false)
      }, 240)
    }
    return () => {
      if (hideTimer) clearTimeout(hideTimer)
      if (removeTimer) clearTimeout(removeTimer)
    }
  }, [snack.visible, snackHiding])

  const refreshPrices = async () => {
    for (const p2 of [apiBase ? `${apiBase}/prices` : '/prices', apiBase ? `${apiBase}/prices.json` : '/prices.json', 'prices.json']) {
      try {
        const r2 = await fetch(p2)
        if (!r2.ok) continue
        const data2 = await r2.json()
        setRows(data2)
        break
      } catch { continue }
    }
  }

  const priceOne = async (id) => {
    if (!id) {
      setSnack({ visible: true, message: 'No instrument id available', type: 'error' })
      return
    }
    setPricingIds(prev => [...prev, id])
    const payload = { InstrumentId: id }
    console.debug('[UI] pricing request', payload)
    try {
      const resp = await fetch((apiBase ? `${apiBase}` : '') + '/price', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
      console.debug('[UI] pricing response status', resp.status)
      if (!resp.ok) {
        let txt = ''
        try { txt = await resp.text() } catch { txt = '<no body>' }
        console.error('[UI] pricing failed', resp.status, txt)
        setSnack({ visible: true, message: `Pricing failed: ${txt}`, type: 'error' })
        setPricingIds(prev => prev.filter(x => x !== id))
        return
      }
      const j = await resp.json()
      console.debug('[UI] pricing result', j)
      setPricingIds(prev => prev.filter(x => x !== id))
      setSnack({ visible: true, message: `Pricing succeeded for ${id}`, type: 'success' })
      await refreshPrices()
    } catch (err) {
      setPricingIds(prev => prev.filter(x => x !== id))
      setSnack({ visible: true, message: `Error calling price API: ${String(err)}`, type: 'error' })
    }
  }

  if (error) return <div className="error">Error: {error}</div>
  if (!rows) return <div>Loading data...</div>

  if (route) {
    return <Instrument instrumentId={route} apiBase={apiBase} />
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
          apiBase={apiBase}
          onPriceAll={async () => {
            if (pricingAll) return
            setPricingAll(true)
            setSnack({ visible: true, message: 'Starting price all...', type: 'info' })
            try {
              const resp = await fetch((apiBase ? `${apiBase}` : '') + '/price_all', { method: 'POST' })
              if (!resp.ok) {
                const txt = await resp.text().catch(() => '<no body>')
                setSnack({ visible: true, message: `Price all failed: ${txt}`, type: 'error' })
                setPricingAll(false)
                return
              }
              const jobObj = await resp.json().catch(() => null)
              const jobId = jobObj && jobObj.job_id
              if (!jobId) {
                // fallback: maybe server returned immediate data
                if (Array.isArray(jobObj)) setRows(jobObj)
                setSnack({ visible: true, message: 'Price all completed', type: 'success' })
                setPricingAll(false)
                return
              }

              // poll job status
              const statusUrl = (apiBase ? `${apiBase}` : '') + `/jobs/${jobId}`
              let done = false
              while (!done) {
                await new Promise(r => setTimeout(r, 2000))
                try {
                  const sresp = await fetch(statusUrl)
                  if (!sresp.ok) continue
                  const s = await sresp.json()
                  if (s.status === 'pending' || s.status === 'running') continue
                  done = true
                  if (s.status === 'succeeded') {
                    setSnack({ visible: true, message: 'Price all succeeded', type: 'success' })
                    // refresh prices
                    for (const p2 of [apiBase ? `${apiBase}/prices` : '/prices', apiBase ? `${apiBase}/prices.json` : '/prices.json', 'prices.json']) {
                      try {
                        const r2 = await fetch(p2)
                        if (!r2.ok) continue
                        const data2 = await r2.json()
                        setRows(data2)
                        break
                      } catch { continue }
                    }
                  } else {
                    setSnack({ visible: true, message: `Price all failed: ${s.error || 'unknown'}`, type: 'error' })
                  }
                } catch (e) {
                  console.error('Polling job status error', e)
                }
              }
            } catch (err) {
              setSnack({ visible: true, message: `Error calling price_all: ${String(err)}`, type: 'error' })
            }
            setPricingAll(false)
          }}
          pricingAll={pricingAll}
          onUpdateCurves={async () => {
            if (updatingCurves) return
            setUpdatingCurves(true)
            setSnack({ visible: true, message: 'Starting curve update...', type: 'info' })
            try {
              const resp = await fetch((apiBase ? `${apiBase}` : '') + '/update_curve', { method: 'POST' })
              if (!resp.ok) {
                const txt = await resp.text().catch(() => '<no body>')
                setSnack({ visible: true, message: `Update curves failed: ${txt}`, type: 'error' })
                setUpdatingCurves(false)
                return
              }
              const jobObj = await resp.json().catch(() => null)
              const jobId = jobObj && jobObj.job_id
              if (!jobId) {
                setSnack({ visible: true, message: 'Update curves completed', type: 'success' })
                setUpdatingCurves(false)
                return
              }

              const statusUrl = (apiBase ? `${apiBase}` : '') + `/jobs/${jobId}`
              let done = false
              while (!done) {
                await new Promise(r => setTimeout(r, 2000))
                try {
                  const sresp = await fetch(statusUrl)
                  if (!sresp.ok) continue
                  const s = await sresp.json()
                  if (s.status === 'pending' || s.status === 'running') continue
                  done = true
                  if (s.status === 'succeeded') {
                    setSnack({ visible: true, message: 'Update curves succeeded', type: 'success' })
                  } else {
                    setSnack({ visible: true, message: `Update curves failed: ${s.error || 'unknown'}`, type: 'error' })
                  }
                } catch (e) {
                  console.error('Polling update_curve job status error', e)
                }
              }
            } catch (err) {
              setSnack({ visible: true, message: `Error calling update_curve: ${String(err)}`, type: 'error' })
            }
            setUpdatingCurves(false)
          }}
          updatingCurves={updatingCurves}
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
            <th title="Price">⏱️</th>
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
                <td style={{textAlign: 'center'}}>
                  {(() => {
                    const id = r.instrument_id || res.instrument_id || ''
                    const busy = id && pricingIds.includes(id)
                    return (
                      <button
                        title="pricing"
                        disabled={busy}
                        onClick={async (e) => {
                          e.preventDefault()
                          await priceOne(id)
                        }}
                      >
                        {busy ? '⏳' : '⏱️'}
                      </button>
                    )
                  })()}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
          <div style={{ marginTop: 24 }}>
            <h2 style={{ marginBottom: 8 }}>Not Priced Instruments</h2>
            <table>
              <thead>
                <tr>
                  <th>Instrument ID</th>
                  <th title="Price">⏱️</th>
                </tr>
              </thead>
              <tbody>
                {missingInstrumentIds.length > 0 ? missingInstrumentIds.map((instrumentId) => (
                  <tr key={instrumentId}>
                    <td className="mono">
                      <a href={`#/instrument/${instrumentId}::${instrumentId}.json`}>{instrumentId}</a>
                    </td>
                    <td style={{ textAlign: 'center' }}>
                      <button
                        title="pricing"
                        disabled={pricingIds.includes(instrumentId)}
                        onClick={async (e) => {
                          e.preventDefault()
                          await priceOne(instrumentId)
                        }}
                      >
                        {pricingIds.includes(instrumentId) ? '⏳' : '⏱️'}
                      </button>
                    </td>
                  </tr>
                )) : (
                  <tr>
                    <td colSpan={2}>No missing instruments.</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
      {/* Snackbar */}
      {(snack.visible || snackHiding) && (
        <div className={`snackbar snackbar--${snack.type || 'info'} ${snackHiding ? 'hide' : 'show'}`}>{snack.message}</div>
      )}
    </div>
  )
}

// Snackbar styles: simple inline component can be used in App
