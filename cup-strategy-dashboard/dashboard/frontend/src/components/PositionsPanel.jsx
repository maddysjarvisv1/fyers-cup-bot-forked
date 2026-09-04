import { useState, useEffect } from 'react'
import { api } from '../api.js'

const INR = v => `₹${Number(v).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

const STATUS_BADGE = {
  'ACTIVE':         { label: 'Active',       bg: 'var(--blue)',   color: '#fff' },
  'BE_SET':         { label: 'Risk Free ✓',  bg: 'var(--green)',  color: '#000' },
  'PARTIAL_BOOKED': { label: 'Partial Out',  bg: 'var(--purple)', color: '#fff' },
  'TRAILING':       { label: 'Trailing 🚀',  bg: 'var(--amber)',  color: '#000' },
}

function Badge({ status }) {
  const s = STATUS_BADGE[status] || { label: status, bg: 'var(--muted)', color: '#fff' }
  return (
    <span style={{ background: s.bg, color: s.color, padding: '2px 8px',
      borderRadius: 3, fontSize: 11, fontWeight: 700, whiteSpace: 'nowrap' }}>
      {s.label}
    </span>
  )
}

export default function PositionsPanel() {
  const [positions, setPositions] = useState([])
  const [loading, setLoading] = useState(true)
  const [showAdd, setShowAdd] = useState(false)
  const [form, setForm] = useState({ symbol: '', buy_price: '', qty: '', ath: '', notes: '' })
  const [msg, setMsg] = useState('')

  const load = () => {
    api.getPositions().then(d => { setPositions(d); setLoading(false) }).catch(() => setLoading(false))
  }

  useEffect(() => { load(); const t = setInterval(load, 30000); return () => clearInterval(t) }, [])

  const flash = m => { setMsg(m); setTimeout(() => setMsg(''), 3000) }

  const handleAdd = async () => {
    try {
      await api.addPosition({
        symbol: form.symbol.toUpperCase(),
        buy_price: parseFloat(form.buy_price),
        qty: parseInt(form.qty),
        ath: parseFloat(form.ath),
        notes: form.notes || null,
      })
      setForm({ symbol: '', buy_price: '', qty: '', ath: '', notes: '' })
      setShowAdd(false)
      flash('✓ Position added')
      load()
    } catch (e) { flash(`✗ ${e.message}`) }
  }

  const handleClose = async (pos) => {
    const price = prompt(`Exit price for ${pos.symbol}?`)
    if (!price) return
    await api.closePosition(pos.id, parseFloat(price), 'MANUAL')
    load()
  }

  return (
    <div>
      <div style={{ display: 'flex', gap: 10, marginBottom: 16, alignItems: 'center' }}>
        <button onClick={() => setShowAdd(!showAdd)} style={{
          background: 'var(--accent)', color: '#000', padding: '6px 16px',
          borderRadius: 4, fontWeight: 700
        }}>+ Manual Entry</button>
        {msg && <span style={{ color: msg.startsWith('✓') ? 'var(--green)' : 'var(--red)', fontSize: 12 }}>{msg}</span>}
        <span style={{ marginLeft: 'auto', color: 'var(--sub)', fontSize: 11 }}>
          {positions.length} open positions
        </span>
      </div>

      {/* Manual add form */}
      {showAdd && (
        <div style={{ background: 'var(--surface)', border: '1px solid var(--border)',
          borderRadius: 6, padding: 16, marginBottom: 16, display: 'flex', gap: 10, flexWrap: 'wrap' }}>
          <input placeholder="Symbol" value={form.symbol} onChange={e => setForm({...form, symbol: e.target.value.toUpperCase()})} style={{ width: 140 }} />
          <input placeholder="Buy Price" type="number" value={form.buy_price} onChange={e => setForm({...form, buy_price: e.target.value})} style={{ width: 120 }} />
          <input placeholder="Qty" type="number" value={form.qty} onChange={e => setForm({...form, qty: e.target.value})} style={{ width: 80 }} />
          <input placeholder="ATH" type="number" value={form.ath} onChange={e => setForm({...form, ath: e.target.value})} style={{ width: 120 }} />
          <input placeholder="Notes (optional)" value={form.notes} onChange={e => setForm({...form, notes: e.target.value})} style={{ width: 180 }} />
          <button onClick={handleAdd} style={{ background: 'var(--green)', color: '#000', padding: '6px 14px', borderRadius: 4, fontWeight: 700 }}>Add</button>
          <button onClick={() => setShowAdd(false)} style={{ background: 'transparent', color: 'var(--sub)', border: '1px solid var(--border)', padding: '6px 14px', borderRadius: 4 }}>Cancel</button>
        </div>
      )}

      {loading ? (
        <div style={{ color: 'var(--sub)', padding: 40, textAlign: 'center' }}>Loading…</div>
      ) : positions.length === 0 ? (
        <div style={{ color: 'var(--sub)', padding: 40, textAlign: 'center' }}>
          No open positions. Positions open automatically when scanner fires a buy signal.
        </div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ background: 'var(--surface)', borderBottom: '1px solid var(--border)' }}>
                {['Symbol','Status','Buy @','Current SL','Trail SL','Qty Rem','Capital','ATH','Highest','P&L','Entry','Action'].map(h => (
                  <th key={h} style={{ padding: '8px 12px', textAlign: 'left', color: 'var(--sub)',
                    fontSize: 11, fontWeight: 600, textTransform: 'uppercase', whiteSpace: 'nowrap' }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {positions.map((p, i) => (
                <tr key={p.id} style={{ borderBottom: '1px solid var(--border)',
                  background: i % 2 === 0 ? 'transparent' : 'rgba(255,255,255,0.01)' }}>
                  <td style={{ padding: '8px 12px', fontWeight: 700, color: 'var(--accent)' }}>{p.symbol}</td>
                  <td style={{ padding: '8px 12px' }}><Badge status={p.status} /></td>
                  <td style={{ padding: '8px 12px' }}>{INR(p.buy_price)}</td>
                  <td style={{ padding: '8px 12px', color: 'var(--red)' }}>{INR(p.sl_price)}</td>
                  <td style={{ padding: '8px 12px', color: p.trail_sl ? 'var(--amber)' : 'var(--muted)' }}>
                    {p.trail_sl ? INR(p.trail_sl) : '—'}
                  </td>
                  <td style={{ padding: '8px 12px' }}>{p.qty_remaining} / {p.qty_total}</td>
                  <td style={{ padding: '8px 12px' }}>{INR(p.capital)}</td>
                  <td style={{ padding: '8px 12px', color: 'var(--sub)' }}>{INR(p.ath)}</td>
                  <td style={{ padding: '8px 12px', color: 'var(--green)' }}>
                    {p.highest_price ? INR(p.highest_price) : '—'}
                  </td>
                  <td style={{ padding: '8px 12px', color: (p.pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>
                    {INR(p.pnl || 0)}
                  </td>
                  <td style={{ padding: '8px 12px', color: 'var(--sub)', fontSize: 11 }}>
                    {p.entry_date?.substring(0, 10)}
                  </td>
                  <td style={{ padding: '8px 12px' }}>
                    <button onClick={() => handleClose(p)} style={{
                      background: 'transparent', color: 'var(--red)',
                      border: '1px solid var(--red)', borderRadius: 3, padding: '3px 8px', fontSize: 11
                    }}>Close</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
