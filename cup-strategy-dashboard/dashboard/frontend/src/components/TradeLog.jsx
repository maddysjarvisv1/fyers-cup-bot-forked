import { useState, useEffect } from 'react'
import { api } from '../api.js'

const INR = v => `₹${Number(v).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

const REASON_COLOR = {
  'SL_HIT':       'var(--red)',
  'BE_EXIT':      'var(--sub)',
  'PARTIAL_BOOK': 'var(--blue)',
  'TRAIL_EXIT':   'var(--green)',
  'MANUAL':       'var(--sub)',
}

export default function TradeLog() {
  const [trades, setTrades] = useState([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api.getTrades().then(d => { setTrades(d); setLoading(false) })
  }, [])

  const totalPnl = trades.reduce((s, t) => s + t.pnl, 0)

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', marginBottom: 16, gap: 16 }}>
        <span style={{ color: 'var(--sub)', fontSize: 12 }}>{trades.length} closed trades</span>
        <span style={{ color: totalPnl >= 0 ? 'var(--green)' : 'var(--red)', fontWeight: 700 }}>
          Total P&L: {INR(totalPnl)}
        </span>
      </div>

      {loading ? (
        <div style={{ color: 'var(--sub)', padding: 40, textAlign: 'center' }}>Loading…</div>
      ) : trades.length === 0 ? (
        <div style={{ color: 'var(--sub)', padding: 40, textAlign: 'center' }}>No closed trades yet.</div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ background: 'var(--surface)', borderBottom: '1px solid var(--border)' }}>
                {['Symbol','Buy @','Exit @','Qty','P&L','Exit Reason','Entry','Exit'].map(h => (
                  <th key={h} style={{ padding: '8px 12px', textAlign: 'left', color: 'var(--sub)',
                    fontSize: 11, fontWeight: 600, textTransform: 'uppercase', whiteSpace: 'nowrap' }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {trades.map((t, i) => (
                <tr key={t.id} style={{ borderBottom: '1px solid var(--border)',
                  background: i % 2 === 0 ? 'transparent' : 'rgba(255,255,255,0.01)' }}>
                  <td style={{ padding: '8px 12px', fontWeight: 700, color: 'var(--accent)' }}>{t.symbol}</td>
                  <td style={{ padding: '8px 12px' }}>{INR(t.buy_price)}</td>
                  <td style={{ padding: '8px 12px' }}>{INR(t.exit_price)}</td>
                  <td style={{ padding: '8px 12px' }}>{t.qty}</td>
                  <td style={{ padding: '8px 12px', fontWeight: 700,
                    color: t.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>
                    {t.pnl >= 0 ? '+' : ''}{INR(t.pnl)}
                  </td>
                  <td style={{ padding: '8px 12px' }}>
                    <span style={{ color: REASON_COLOR[t.exit_reason] || 'var(--sub)', fontSize: 11, fontWeight: 600 }}>
                      {t.exit_reason}
                    </span>
                  </td>
                  <td style={{ padding: '8px 12px', color: 'var(--sub)', fontSize: 11 }}>
                    {t.entry_date?.substring(0,10)}
                  </td>
                  <td style={{ padding: '8px 12px', color: 'var(--sub)', fontSize: 11 }}>
                    {t.exit_date?.substring(0,10)}
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
