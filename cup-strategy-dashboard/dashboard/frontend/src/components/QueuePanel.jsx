import { useState, useEffect, useRef } from 'react'
import { api } from '../api.js'

const INR = v => `₹${Number(v).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
const PCT = v => {
  const n = Number(v)
  return <span style={{ color: n >= 0 ? 'var(--green)' : n >= -3 ? 'var(--amber)' : 'var(--red)' }}>
    {n >= 0 ? '+' : ''}{n.toFixed(2)}%
  </span>
}

export default function QueuePanel() {
  const [queue, setQueue] = useState([])
  const [loading, setLoading] = useState(true)
  const [sym, setSym] = useState('')
  const [ath, setAth] = useState('')
  const [adding, setAdding] = useState(false)
  const [importing, setImporting] = useState(false)
  const [msg, setMsg] = useState('')
  const fileRef = useRef()
  const [buyRow, setBuyRow] = useState(null)   // row being bought (opens dialog)
  const [buyPrice, setBuyPrice] = useState('')
  const [buyQty, setBuyQty] = useState('')
  const [buying, setBuying] = useState(false)

  const load = () => {
    api.getQueue().then(d => { setQueue(d); setLoading(false) }).catch(() => setLoading(false))
  }

  useEffect(() => { load(); const t = setInterval(load, 30000); return () => clearInterval(t) }, [])

  const flash = m => { setMsg(m); setTimeout(() => setMsg(''), 3000) }

  const handleAdd = async () => {
    if (!sym.trim()) return
    setAdding(true)
    try {
      await api.addStock(sym.trim().toUpperCase(), ath ? parseFloat(ath) : null)
      setSym(''); setAth('')
      flash(`✓ ${sym.toUpperCase()} added to queue`)
      load()
    } catch (e) { flash(`✗ ${e.message}`) }
    setAdding(false)
  }

  const handleImport = async (e) => {
    const file = e.target.files[0]
    if (!file) return
    setImporting(true)
    try {
      const res = await api.importFile(file)
      flash(`✓ Imported ${res.imported} stocks`)
      load()
    } catch (e) { flash(`✗ Import failed`) }
    setImporting(false)
    e.target.value = ''
  }

  const handleRemove = async (symbol) => {
    await api.removeQueue(symbol)
    load()
  }

  const openBuy = (r) => {
    setBuyRow(r)
    setBuyPrice(String(r.buy_price))   // pre-calculated buy trigger (rim × 1.04)
    setBuyQty(String(r.qty))
  }

  const confirmBuy = async () => {
    if (!buyRow) return
    const price = parseFloat(buyPrice)
    const qty = parseInt(buyQty, 10)
    if (!price || !qty) { flash('✗ Enter a valid price and qty'); return }
    setBuying(true)
    try {
      await api.addPosition({ symbol: buyRow.symbol, buy_price: price, qty, ath: buyRow.ath, notes: 'Manual buy' })
      flash(`✓ Bought ${buyRow.symbol} @ ₹${price} × ${qty}`)
      setBuyRow(null)
      load()
    } catch (e) { flash(`✗ ${e.message}`) }
    setBuying(false)
  }

  return (
    <div>
      {/* Fyers / auto-order status — honest state so nothing is assumed to be live */}
      <div style={{
        background: 'rgba(255,170,0,0.08)', border: '1px solid var(--amber)',
        borderRadius: 6, padding: '10px 14px', marginBottom: 14, fontSize: 12, color: 'var(--text)'
      }}>
        <b style={{ color: 'var(--amber)' }}>⚠️ Auto-GTT / Fyers: NOT CONNECTED.</b>{' '}
        No broker is linked, so no real orders are placed. Every “Buy @” below is the GTT
        trigger that <i>would</i> be sent once Fyers is connected — for now buys are app-tracking only.
      </div>

      {/* Add / Import bar */}
      <div style={{ display: 'flex', gap: 10, marginBottom: 16, flexWrap: 'wrap', alignItems: 'center' }}>
        <input value={sym} onChange={e => setSym(e.target.value.toUpperCase())}
          placeholder="Symbol e.g. FEDERALBNK"
          style={{ width: 180 }}
          onKeyDown={e => e.key === 'Enter' && handleAdd()} />
        <input value={ath} onChange={e => setAth(e.target.value)}
          placeholder="ATH (optional)"
          type="number" style={{ width: 130 }} />
        <button onClick={handleAdd} disabled={adding} style={{
          background: 'var(--accent)', color: '#000', padding: '6px 16px',
          borderRadius: 4, fontWeight: 700
        }}>{adding ? 'Adding…' : '+ Add Stock'}</button>

        <button onClick={() => fileRef.current.click()} disabled={importing} style={{
          background: 'var(--surface)', color: 'var(--text)',
          border: '1px solid var(--border)', padding: '6px 14px', borderRadius: 4
        }}>{importing ? 'Importing…' : '↑ Import CSV/Excel'}</button>
        <input ref={fileRef} type="file" accept=".csv,.xlsx,.xls" onChange={handleImport} style={{ display: 'none' }} />

        {msg && <span style={{ color: msg.startsWith('✓') ? 'var(--green)' : 'var(--red)', fontSize: 12 }}>{msg}</span>}
        <span style={{ marginLeft: 'auto', color: 'var(--sub)', fontSize: 11 }}>
          {queue.length} stocks in queue · Auto-refreshes every 30s
        </span>
      </div>

      {/* Table */}
      {loading ? (
        <div style={{ color: 'var(--sub)', padding: 40, textAlign: 'center' }}>Loading queue…</div>
      ) : queue.length === 0 ? (
        <div style={{ color: 'var(--sub)', padding: 40, textAlign: 'center' }}>
          No stocks in queue.<br />Add manually or wait for nightly scan.
        </div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ background: 'var(--surface)', borderBottom: '1px solid var(--border)' }}>
                {['Symbol','ATH','ATH Date','Current','% from ATH','Buy @','SL @','Qty','Capital','Action'].map(h => (
                  <th key={h} style={{ padding: '8px 12px', textAlign: 'left', color: 'var(--sub)',
                    fontSize: 11, fontWeight: 600, textTransform: 'uppercase', whiteSpace: 'nowrap' }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {queue.map((r, i) => (
                <tr key={r.symbol} style={{
                  borderBottom: '1px solid var(--border)',
                  background: i % 2 === 0 ? 'transparent' : 'rgba(255,255,255,0.01)',
                  transition: 'background 0.1s',
                }}>
                  <td style={{ padding: '8px 12px', fontWeight: 700, color: 'var(--accent)' }}>{r.symbol}</td>
                  <td style={{ padding: '8px 12px' }}>{INR(r.ath)}</td>
                  <td style={{ padding: '8px 12px', color: 'var(--sub)' }}>{r.ath_date}</td>
                  <td style={{ padding: '8px 12px' }}>{INR(r.current_price)}</td>
                  <td style={{ padding: '8px 12px' }}>{PCT(r.pct_from_ath)}</td>
                  <td style={{ padding: '8px 12px', color: 'var(--green)', fontWeight: 600 }}>{INR(r.buy_price)}</td>
                  <td style={{ padding: '8px 12px', color: 'var(--red)' }}>{INR(r.sl_price)}</td>
                  <td style={{ padding: '8px 12px' }}>{r.qty}</td>
                  <td style={{ padding: '8px 12px' }}>{INR(r.capital)}</td>
                  <td style={{ padding: '8px 12px', whiteSpace: 'nowrap' }}>
                    <button onClick={() => openBuy(r)} style={{
                      background: 'var(--green)', color: '#000', fontWeight: 700,
                      border: 'none', borderRadius: 3, padding: '3px 12px', fontSize: 11, marginRight: 6
                    }}>Buy</button>
                    <button onClick={() => handleRemove(r.symbol)} style={{
                      background: 'transparent', color: 'var(--red)',
                      border: '1px solid var(--red)', borderRadius: 3, padding: '3px 8px', fontSize: 11
                    }}>Remove</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Buy dialog */}
      {buyRow && (
        <div onClick={() => !buying && setBuyRow(null)} style={{
          position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 50
        }}>
          <div onClick={e => e.stopPropagation()} style={{
            background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 8,
            padding: 24, width: 340, maxWidth: '90vw'
          }}>
            <div style={{ fontWeight: 700, fontSize: 16, marginBottom: 4, color: 'var(--accent)' }}>
              Buy {buyRow.symbol}
            </div>
            <div style={{ color: 'var(--sub)', fontSize: 12, marginBottom: 16 }}>
              Rim {INR(buyRow.ath)} · trigger {INR(buyRow.buy_price)} · live {INR(buyRow.current_price)}
            </div>

            <label style={{ fontSize: 12, color: 'var(--sub)' }}>Buy price</label>
            <input type="number" value={buyPrice} onChange={e => setBuyPrice(e.target.value)}
              style={{ width: '100%', marginBottom: 4, boxSizing: 'border-box' }} />
            <div style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
              <button onClick={() => setBuyPrice(String(buyRow.buy_price))} style={{
                background: 'var(--bg)', color: 'var(--sub)', border: '1px solid var(--border)',
                borderRadius: 3, padding: '2px 8px', fontSize: 11 }}>Use trigger {INR(buyRow.buy_price)}</button>
              <button onClick={() => setBuyPrice(String(buyRow.current_price))} style={{
                background: 'var(--bg)', color: 'var(--sub)', border: '1px solid var(--border)',
                borderRadius: 3, padding: '2px 8px', fontSize: 11 }}>Use live {INR(buyRow.current_price)}</button>
            </div>

            <label style={{ fontSize: 12, color: 'var(--sub)' }}>Quantity</label>
            <input type="number" value={buyQty} onChange={e => setBuyQty(e.target.value)}
              style={{ width: '100%', marginBottom: 16, boxSizing: 'border-box' }} />

            <div style={{ color: 'var(--sub)', fontSize: 11, marginBottom: 16 }}>
              Est. capital ₹{(parseFloat(buyPrice || 0) * parseInt(buyQty || 0, 10)).toLocaleString('en-IN')} ·
              tracked in app only, no real order placed
            </div>

            <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
              <button onClick={() => setBuyRow(null)} disabled={buying} style={{
                background: 'transparent', color: 'var(--text)', border: '1px solid var(--border)',
                borderRadius: 4, padding: '6px 14px' }}>Cancel</button>
              <button onClick={confirmBuy} disabled={buying} style={{
                background: 'var(--green)', color: '#000', fontWeight: 700,
                border: 'none', borderRadius: 4, padding: '6px 18px' }}>
                {buying ? 'Buying…' : 'Confirm Buy'}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
