import React, { useEffect, useState } from 'react'

export default function Instrument({ instrumentId, apiBase = '' }) {
  // instrumentId may be encoded as "ID::bond_file.json" from the table link
  let bondFile = null
  let isin = instrumentId
  if (instrumentId && instrumentId.includes('::')) {
    const parts = instrumentId.split('::')
    isin = parts[0]
    bondFile = parts.slice(1).join('::')
  }
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [priceEntry, setPriceEntry] = useState(null)
  const [editMode, setEditMode] = useState(false)
  const [draftValues, setDraftValues] = useState({})
  const [saving, setSaving] = useState(false)
  const [snack, setSnack] = useState({ visible: false, message: '', type: 'info' })
  const [snackHiding, setSnackHiding] = useState(false)

  useEffect(() => {
    let mounted = true
    async function fetchJson() {
      try {
        // Primary source: backend endpoint /fetch_asset?instrument_id=...
        if (isin) {
          const base = String(apiBase || '').replace(/\/$/, '')
          const endpoint = `${base}/fetch_asset?instrument_id=${encodeURIComponent(isin)}`
          try {
            const r = await fetch(endpoint)
            if (r.ok) {
              const j = await r.json()
              if (!mounted) return
              setData(j)
              return
            }
          } catch {
            // continue to local fallback
          }
        }

        // Fallback source: local public/assets JSON files
        const paths = []
        if (bondFile) {
          // try explicit bond file first (both absolute and relative)
          paths.push(`/assets/${bondFile}`)
          paths.push(`assets/${bondFile}`)
          paths.push(`./assets/${bondFile}`)
          paths.push(`/assets/${bondFile.replace(/\\s+/g, '')}`)
        }
        // fallbacks based on isin (try .json and no-leading-slash variants)
        if (isin) {
          paths.push(`/assets/${isin}.json`)
          paths.push(`assets/${isin}.json`)
          paths.push(`./assets/${isin}.json`)
          paths.push(`/assets/${isin.toUpperCase()}.json`)
          paths.push(`/assets/${isin}.JSON`)
        }
        for (const p of paths) {
          try {
            const r = await fetch(p)
            if (!r.ok) continue
            const j = await r.json()
            if (!mounted) return
            setData(j)
            return
          } catch {
            continue
          }
        }
        if (mounted) setError('Could not fetch asset JSON for ' + (bondFile || isin))
      } catch (e) {
        if (mounted) setError(String(e))
      }
    }
    fetchJson()
    return () => { mounted = false }
  }, [instrumentId, apiBase, bondFile, isin])

  useEffect(() => {
    let mounted = true
    async function fetchPrices() {
      try {
        // Try backend /prices endpoint first, then fall back to local prices
        const tryPaths = ['/prices', '/src/prices.json', '/prices.json', 'prices.json']
        let j = null
        for (const p of tryPaths) {
          try {
            const r = await fetch(p)
            if (!r.ok) continue
            j = await r.json()
            break
          } catch {
            continue
          }
        }
        if (!j) return
        if (!mounted) return
        // find by instrument id (match either instrument_id or bond_file)
        const isinKey = isin || instrumentId
        const found = j.find(e => e.instrument_id === isinKey || e.bond_file === (bondFile || `${isinKey}.json`))
        if (found) setPriceEntry(found)
      } catch {
        // ignore silently
      }
    }
    fetchPrices()
    return () => { mounted = false }
  }, [instrumentId, data, bondFile, isin])

  useEffect(() => {
    let hideTimer = null
    let removeTimer = null
    if (snack.visible) {
      hideTimer = setTimeout(() => setSnackHiding(true), 4000)
    }
    if (snackHiding) {
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

  if (error) return (
    <div>
      <a href="#" onClick={(e)=>{e.preventDefault(); window.location.hash=''}}>&larr; Back</a>
      <div className="error">{error}</div>
    </div>
  )
  if (!data) return (
    <div>
      <a href="#" onClick={(e)=>{e.preventDefault(); window.location.hash=''}}>&larr; Back</a>
      <div>Loading {instrumentId}...</div>
    </div>
  )

  // prepare values for three-column display
  const nestedKeys = new Set(['collateral', 'swap', 'csa', 'valuation_adjustments'])
  // priceResult/ytm used in fallbacks must be defined before columns
  const priceResult = priceEntry && priceEntry.result ? priceEntry.result : null
  const ytm = priceResult && (priceResult.ytm || priceResult.ytm_expected || priceResult.model_ytm_to_maturity || priceResult.yield_to_maturity || priceResult.model_ytm_to_maturity)
  const pp = priceResult && priceResult.price_pct ? priceResult.price_pct : {}
  const colPV = pp.pv_note ?? (priceResult && (priceResult.pv_note ?? priceResult.selected_npv))
  const colWorst = pp.pv_note_to_worst ?? pp.pv_note_to_worst_call ?? (priceResult && (priceResult.npv_to_worst_call || priceResult.npv_to_worst))
  const colMat = pp.pv_note_to_maturity ?? (priceResult && (priceResult.npv_to_maturity || priceResult.npv_to_maturity))
  const firstCol = {
    isin: isin,
    model: data.model || data.pricing_model || null,
    denomination: data.denomination || data.face_amount || data.nominal || null,
    reference_cds_curve_name: data.reference_cds_curve_name || data['reference_cds_curve_name'] || data['reference_cds:curve:name'] || (data.reference_cds && data.reference_cds.curve && data.reference_cds.curve.name) || (data.reference && data.reference.cds && data.reference.cds.curve && data.reference.cds.curve.name) || null,
    reference_entity: data.reference_entity || (data.terms && data.terms.reference_entity) || null,
    reference_entity_seniority: data.reference_entity_seniority || (data.terms && data.terms.reference_entity_seniority) || null,
    issuer: data.issuer || (data.terms && data.terms.issuer) || data.issuer_name || null,
    callable_type: data.callable_type || data.callableType || (data.terms && data.terms.callable_type) || null,
    ['call:price']: data['call:price'] || data.call_price || data.callPrice || (data.terms && (data.terms['call:price'] || data.terms.call_price)) || null,
    valuationmode: data.valuationmode || data.valuation_mode || (priceResult && priceResult.valuation_mode) || null,
    coupon_structure: data.coupon_structure || data.couponStructure || (data.terms && data.terms.coupon_structure) || null,
    typology: data.typology || data.instrument_type || (data.terms && data.terms.underlying_type) || data.type || null,
    bond_structure: data.bond_structure || (data.terms && data.terms.pricing_formula_type) || data.structure || null,
    trading_type: data.trading_type || (data.terms && data.terms.trading_type) || null,
    currency: data.currency || (data.terms && data.terms.payoff_currency) || data.payoff_currency || null,
    clearing_settlement: data.clearing_settlement || data.clearingSettlement || (data.terms && data.terms.clearing_settlement) || null,
    description: data.description || data.name || null
  }
  const secondCol = {
    evaluation_date: data.evaluation_date || data.eval_date || null,
    issue_date: data.issue_date || data.issuance_date || (data.terms && data.terms.issue_date) || null,
    maturity_date: data.maturity_date || (data.terms && data.terms.maturity_date) || (data.terms && data.terms.observation_dates && data.terms.observation_dates.slice(-1)[0]) || null,
    first_coupon_date: data.first_coupon_date || (data.terms && data.terms.first_coupon_date) || null,
    coupon_frequency: data.coupon_frequency || (data.terms && data.terms.coupon_frequency) || null,
    discount_curve_name: data.discount_curve_name || data.dicount_curve_name || (priceResult && (priceResult.discount_curve_name || priceResult.discount_curve)) || (data.terms && data.terms.discount_curve_name) || null,
    settlement_currency: data.settlement_currency || (data.terms && data.terms.settlement_currency) || null,
    negotiation_currency: data.negotiation_currency || (data.terms && data.terms.negotiation_currency) || null,
    first_day_of_trading: data.first_day_of_trading || (data.terms && data.terms.first_day_of_trading) || null,
    date_generation: data.date_generation || (data.terms && data.terms.date_generation) || null,
    calendar: data.calendar || (data.terms && data.terms.calendar) || null,
    day_count_convention: data.day_count_convention || data.day_count || (data.terms && data.terms.day_count_convention) || null,
    accrual_day_count: data.accrual_day_count || (data.terms && data.terms.accrual_day_count) || null
  }
  // YTM/PV come from prices.json entry when available
  const thirdCol = {
    amount_issued: data.amount_issued || data.issuance_amount || data.issue_amount || (data.terms && data.terms.amount_issued) || null,
    lot_size: data.lot_size || data.lot || (data.terms && data.terms.lot_size) || null,
    note_notional: data.note_notional || data.notional || (data.terms && data.terms.note_notional) || null,
    credit_spread_bp: data.credit_spread_bp || data.credit_spread || data.spread_bp || (data.terms && data.terms.credit_spread_bp) || null,
    par: data.par || data.face_value || (data.terms && data.terms.par) || null,
    period_coupon_rate: data.period_coupon_rate || data.period_rate || data['period:coupon:rate'] || null,
    ["recovery:Rate"]: data['recovery:Rate'] || (data.terms && data.terms['recovery:Rate']) || data.recovery_rate || null,
    annual_coupon_rate: data.annual_coupon_rate || data.coupon_rate || (data.terms && data.terms.coupon_rate) || null,
    market: data.market || (data.terms && data.terms.market) || null,
    outstanding: data.outstanding || data.outstanding_amount || (data.terms && data.terms.outstanding) || null,
    redemption: data.redemption || (data.terms && data.terms.redemption) || data.redemption_formula_type || null,
    fixed_coupon_rate: data.fixed_coupon_rate || data.coupon_rate || (data.terms && data.terms.fixed_coupon_rate) || null,
    PV: colPV || null,
    PV_to_worst: colWorst || null,
    PV_to_maturity: colMat || null,
    ytm: ytm,
    pv_note: priceResult && (priceResult.pv_note || (priceResult.note_leg && priceResult.note_leg.pv_note) || null),
    pv_note_coupons: priceResult && (priceResult.pv_note_coupons || (priceResult.note_leg && priceResult.note_leg.pv_note_coupons) || (priceResult.price_pct && priceResult.price_pct.pv_note_coupons) || null)
  }

  const renderValue = (k, v) => {
    const truncate = (s, n = 50) => (typeof s === 'string' && s.length > n) ? s.slice(0, n - 1) + '…' : s
    // format numeric fields: percentages (YTM/YTD/coupon/any *rate* keys) and 3-decimal numbers (PV)
    const lowerKey = String(k).toLowerCase()
    const percentageKeys = new Set(['ytm', 'ytd', 'yield_to_maturity', 'model_ytm_to_maturity', 'ytm_expected', 'fixed_coupon_rate', 'coupon_rate'])
    const threeDpKeys = new Set(['pv_note', 'pv_note_coupons', 'pv_to_worst', 'pv_to_maturity', 'pv_note_to_worst', 'pv_note_to_maturity', 'pv_note_to_worst_call'])
    if (typeof v === 'number') {
      // treat known percentage keys or any key containing 'rate' as percentage
      if (percentageKeys.has(k) || percentageKeys.has(lowerKey) || lowerKey.includes('rate')) {
        return String((v * 100).toFixed(3)) + '%'
      }
      if (threeDpKeys.has(k) || threeDpKeys.has(lowerKey)) return String(Number(v).toFixed(3))
    }
    if (v && typeof v === 'object' && nestedKeys.has(k)) {
      return (
        <table className="nested-table">
          <tbody>
            {Object.entries(v).map(([nk, nv]) => (
              <tr key={nk}>
                <td className="nested-key">{nk}</td>
                <td className="nested-value"><pre>{typeof nv === 'object' ? JSON.stringify(nv, null, 2) : String(nv)}</pre></td>
              </tr>
            ))}
          </tbody>
        </table>
      )
    }
    if (k === 'call_dates' && Array.isArray(v)) {
      return (
        <table className="call-dates-table">
          <thead>
            <tr><th>Call Date</th></tr>
          </thead>
          <tbody>
            {v.map((item, ri) => (
              Array.isArray(item) ? (
                <tr key={ri}>{item.map((c, ci) => <td key={ci}>{String(c)}</td>)}</tr>
              ) : (
                <tr key={ri}><td>{String(item)}</td></tr>
              )
            ))}
          </tbody>
        </table>
      )
    }
    if (typeof v === 'string' && lowerKey === 'description') return <pre>{truncate(v, 50)}</pre>
    return <pre>{typeof v === 'object' ? JSON.stringify(v, null, 2) : String(v)}</pre>
  }

  const isDateKey = (k) => {
    const s = String(k || '').toLowerCase()
    return s.includes('date') || s.includes('maturity') || s.includes('issue') || s.includes('expiry')
  }

  const toDateInputValue = (v) => {
    if (v == null) return ''
    const s = String(v).trim()
    if (!s) return ''
    // already ISO-like
    if (/^\d{4}-\d{2}-\d{2}/.test(s)) return s.slice(0, 10)
    // common day-first formats (DD-MM-YYYY / DD/MM/YYYY)
    let m = s.match(/^(\d{2})[-/](\d{2})[-/](\d{4})$/)
    if (m) {
      const dd = m[1]
      const mm = m[2]
      const yyyy = m[3]
      return `${yyyy}-${mm}-${dd}`
    }
    // compact yyyymmdd
    m = s.match(/^(\d{4})(\d{2})(\d{2})$/)
    if (m) return `${m[1]}-${m[2]}-${m[3]}`
    const d = new Date(s)
    if (Number.isNaN(d.getTime())) return ''
    const yyyy = d.getFullYear()
    const mm = String(d.getMonth() + 1).padStart(2, '0')
    const dd = String(d.getDate()).padStart(2, '0')
    return `${yyyy}-${mm}-${dd}`
  }

  const onDraftChange = (k, nextValue) => {
    setDraftValues(prev => ({ ...prev, [k]: nextValue }))
  }

  const isDayCountKey = (k) => {
    const s = String(k || '').toLowerCase()
    return s === 'day_count_convention' || s === 'accrual_day_count' || s === 'day_count' || s === 'float_reference_day_count' || s === 'cms_day_count'
  }

  const renderEditor = (k, fallbackValue) => {
    const value = Object.prototype.hasOwnProperty.call(draftValues, k) ? draftValues[k] : fallbackValue
    if (isDayCountKey(k)) {
      const options = ['Actual360', 'Actual365Fixed', 'Thirty360', '30/360', 'ActualActual', 'ACT/ACT (PERIODIC BASIS)', 'ACT/ACT (ICMA)']
      const selected = value == null ? '' : String(value)
      const values = options.includes(selected) ? options : [...options, selected]
      return (
        <select value={selected} onChange={(e) => onDraftChange(k, e.target.value)}>
          <option value="">(empty)</option>
          {values.map((opt) => <option key={opt} value={opt}>{opt}</option>)}
        </select>
      )
    }
    if (value != null && typeof value === 'object') {
      return (
        <textarea
          value={typeof value === 'string' ? value : JSON.stringify(value, null, 2)}
          onChange={(e) => onDraftChange(k, e.target.value)}
          rows={4}
          style={{ width: '100%' }}
        />
      )
    }
    if (isDateKey(k)) {
      return (
        <input
          type="date"
          value={toDateInputValue(value)}
          onChange={(e) => onDraftChange(k, e.target.value)}
        />
      )
    }
    if (typeof value === 'boolean') {
      return (
        <input
          type="checkbox"
          checked={Boolean(value)}
          onChange={(e) => onDraftChange(k, e.target.checked)}
        />
      )
    }
    if (typeof value === 'number') {
      return (
        <input
          type="number"
          step="any"
          value={Number.isFinite(value) ? String(value) : ''}
          onChange={(e) => onDraftChange(k, e.target.value)}
        />
      )
    }
    return (
      <input
        type="text"
        value={value == null ? '' : String(value)}
        onChange={(e) => onDraftChange(k, e.target.value)}
      />
    )
  }

  const coerceEditedValue = (key, rawValue, originalValue) => {
    if (rawValue == null) return rawValue
    if (typeof rawValue === 'boolean') return rawValue
    if (typeof rawValue === 'string') {
      if (typeof originalValue === 'number') {
        const n = Number(rawValue)
        return Number.isNaN(n) ? originalValue : n
      }
      if (typeof originalValue === 'boolean') {
        return rawValue.toLowerCase() === 'true'
      }
      const trimmed = rawValue.trim()
      if ((typeof originalValue === 'object' && originalValue !== null) || trimmed.startsWith('{') || trimmed.startsWith('[')) {
        try {
          return JSON.parse(trimmed)
        } catch {
          return rawValue
        }
      }
      if (isDateKey(key)) return trimmed || null
      return rawValue
    }
    return rawValue
  }
  // build table rows: each row contains up to three field/value pairs (col pairs: 1-2, 3-4, 5-6)
  const entries1 = Object.entries(firstCol)
  const entries2 = Object.entries(secondCol)
  const entries3 = Object.entries(thirdCol)
  const usedKeys = new Set([...Object.keys(firstCol), ...Object.keys(secondCol), ...Object.keys(thirdCol)])
  const otherEntries = Object.entries(data).filter(([k]) => !usedKeys.has(k))
  const maxLen = Math.max(entries1.length, entries2.length, entries3.length)
  const rows = []
  for (let i = 0; i < maxLen; i++) {
    rows.push([
      entries1[i] || [null, null],
      entries2[i] || [null, null],
      entries3[i] || [null, null]
    ])
  }

  const allDisplayedEntries = [...entries1, ...entries2, ...entries3, ...otherEntries]

  const startEdit = () => {
    const initial = {}
    for (const [k, v] of allDisplayedEntries) initial[k] = v
    setDraftValues(initial)
    setEditMode(true)
  }

  const cancelEdit = () => {
    setEditMode(false)
    setDraftValues({})
  }

  const saveEdit = async () => {
    if (saving) return
    setSaving(true)
    try {
      const updated = JSON.parse(JSON.stringify(data))
      for (const [k, v] of Object.entries(draftValues)) {
        const original = Object.prototype.hasOwnProperty.call(updated, k) ? updated[k] : undefined
        updated[k] = coerceEditedValue(k, v, original)
      }

      const fileNameRaw = bondFile || `${isin}.json`
      const fileName = String(fileNameRaw).split('/').pop() || `${isin}.json`
      const jsonBlob = new Blob([JSON.stringify(updated, null, 2)], { type: 'application/json' })
      const form = new FormData()
      form.append('file', jsonBlob, fileName)

      const base = String(apiBase || '').replace(/\/$/, '')
      const endpoint = `${base}/update_asset`
      const resp = await fetch(endpoint, { method: 'POST', body: form })
      if (!resp.ok) {
        const msg = await resp.text().catch(() => 'Unknown error')
        setSnack({ visible: true, message: `Save failed: ${msg}`, type: 'error' })
        return
      }

      setData(updated)
      setEditMode(false)
      setDraftValues({})
      setSnack({ visible: true, message: 'Asset updated successfully', type: 'success' })
    } catch (e) {
      setSnack({ visible: true, message: `Save failed: ${String(e)}`, type: 'error' })
    } finally {
      setSaving(false)
    }
  }

  const openTermsheet = async () => {
    try {
      const base = String(apiBase || '').replace(/\/$/, '')
      const url = `${base}/fetch_termsheet?instrument_id=${encodeURIComponent(isin)}&_ts=${Date.now()}`
      const resp = await fetch(url)
      if (!resp.ok) {
        const msg = await resp.text().catch(() => 'Could not load termsheet')
        setSnack({ visible: true, message: `Termsheet not available: ${msg}`, type: 'error' })
        return
      }
      const blob = await resp.blob()
      const blobUrl = URL.createObjectURL(new Blob([blob], { type: 'application/pdf' }))
      window.open(blobUrl, '_blank', 'noopener,noreferrer')
      // Revoke after a delay to avoid closing the opened resource too early.
      setTimeout(() => URL.revokeObjectURL(blobUrl), 60000)
    } catch (e) {
      setSnack({ visible: true, message: `Termsheet open failed: ${String(e)}`, type: 'error' })
    }
  }

  const openReport = async () => {
    try {
      const base = String(apiBase || '').replace(/\/$/, '')
      const url = `${base}/fetch_report?instrument_id=${encodeURIComponent(isin)}&_ts=${Date.now()}`
      const resp = await fetch(url)
      if (!resp.ok) {
        const msg = await resp.text().catch(() => 'Could not load report')
        setSnack({ visible: true, message: `Report not available: ${msg}`, type: 'error' })
        return
      }
      const blob = await resp.blob()
      const blobUrl = URL.createObjectURL(new Blob([blob], { type: 'application/pdf' }))
      window.open(blobUrl, '_blank', 'noopener,noreferrer')
      // Revoke after a delay to avoid closing the opened resource too early.
      setTimeout(() => URL.revokeObjectURL(blobUrl), 60000)
    } catch (e) {
      setSnack({ visible: true, message: `Report open failed: ${String(e)}`, type: 'error' })
    }
  }

  return (
    <div>
      <a href="#" onClick={(e)=>{e.preventDefault(); window.location.hash=''}}>&larr; Back</a>
      <div style={{ marginTop: 10, marginBottom: 8, display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
        <button className="clear-btn clear-btn--termsheet" onClick={openTermsheet}>Termsheet</button>
        <button className="clear-btn clear-btn--report" onClick={openReport}>Report</button>
        {!editMode ? (
          <button className="clear-btn" onClick={startEdit}>Edit</button>
        ) : (
          <>
            <button className="clear-btn" onClick={saveEdit} disabled={saving}>{saving ? 'Saving...' : 'Save'}</button>
            <button className="clear-btn" onClick={cancelEdit} disabled={saving}>Cancel</button>
          </>
        )}
      </div>
      <h2>{(data.description ? (data.description.length > 100 ? data.description.slice(0, 99) + '…' : data.description) : instrumentId)}</h2>
      <table className="instrument-table" style={{width: '100%', border: 'none'}}>
        <tbody>
          {rows.map((row, ri) => (
            <tr key={ri}>
              <td className="detail-key" style={{border: 'none', padding: '6px 8px'}}>{row[0][0] || ''}</td>
              <td className="detail-value" style={{border: 'none', padding: '6px 8px'}}>{editMode ? renderEditor(row[0][0], row[0][1]) : (row[0][1] == null ? '-' : renderValue(row[0][0], row[0][1]))}</td>

              <td className="detail-key" style={{border: 'none', padding: '6px 8px', paddingLeft: '24px', borderLeft: '4px solid rgba(158,167,173,0.5)'}}>{row[1][0] || ''}</td>
              <td className="detail-value" style={{border: 'none', padding: '6px 8px'}}>{editMode ? renderEditor(row[1][0], row[1][1]) : (row[1][1] == null ? '-' : renderValue(row[1][0], row[1][1]))}</td>

              <td className="detail-key" style={{border: 'none', padding: '6px 8px', paddingLeft: '24px', borderLeft: '4px solid rgba(158,167,173,0.5)'}}>{row[2][0] || ''}</td>
              <td className="detail-value" style={{border: 'none', padding: '6px 8px'}}>{editMode ? renderEditor(row[2][0], row[2][1]) : (row[2][1] == null ? '-' : renderValue(row[2][0], row[2][1]))}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="other-fields" style={{marginTop: '24px'}}>
        <h3>Other Fields</h3>
        <dl>
          {otherEntries.map(([k, v]) => (
            <div key={k} className="detail-row"><dt className="detail-key">{k}</dt><dd className="detail-value">{editMode ? renderEditor(k, v) : (v == null ? '-' : renderValue(k, v))}</dd></div>
          ))}
        </dl>
      </div>
      {(snack.visible || snackHiding) && (
        <div className={`snackbar snackbar--${snack.type || 'info'} ${snackHiding ? 'hide' : 'show'}`}>{snack.message}</div>
      )}
    </div>
  )
}
