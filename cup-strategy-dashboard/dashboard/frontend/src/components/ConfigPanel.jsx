import { useState, useEffect } from 'react'
import { api } from '../api.js'

const FIELDS = [
  { key: 'CAPITAL_PER_TRADE',         label: 'Capital per Trade (₹)',       type: 'number', hint: 'Max capital deployed per position' },
  { key: 'FIXED_LOSS',                label: 'Fixed Loss per Trade (₹)',     type: 'number', hint: 'Max rupee loss per trade (drives qty sizing)' },
  { key: 'SL_PCT',                    label: 'Stop Loss %',                  type: 'number', hint: 'Default: 6% below buy price' },
  { key: 'BREAKEVEN_TRIGGER_PCT',     label: 'Breakeven Trigger %',          type: 'number', hint: 'Move SL to buy price when stock is up this much' },
  { key: 'PARTIAL_BOOK_TRIGGER_PCT',  label: 'Partial Book Trigger %',       type: 'number', hint: 'Book 50% when stock is up this much' },
  { key: 'TRAIL_PCT',                 label: 'Trailing SL %',                type: 'number', hint: 'Trail SL this % below highest price' },
  { key: 'QUEUE_THRESHOLD_PCT',       label: 'Queue Threshold %',            type: 'number', hint: 'Add to queue when within this % of ATH' },
  { key: 'INTRADAY_SCAN_INTERVAL_MIN',label: 'Intraday Scan Interval (min)', type: 'number', hint: 'How often to check prices during market hours' },
  { key: 'TELEGRAM_BOT_TOKEN',        label: 'Telegram Bot Token',           type: 'password', hint: 'From @BotFather on Telegram' },
  { key: 'TELEGRAM_CHAT_ID',          label: 'Telegram Chat ID',             type: 'text', hint: 'Your chat ID to receive alerts' },
]

export default function ConfigPanel() {
  const [cfg, setCfg] = useState({})
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')

  useEffect(() => {
    api.getConfig().then(d => { setCfg(d); setLoading(false) })
  }, [])

  const handleSave = async () => {
    setSaving(true)
    try {
      await api.saveConfig(cfg)
      setMsg('✓ Config saved — restart scanner to apply scan interval changes')
    } catch (e) {
      setMsg(`✗ ${e.message}`)
    }
    setSaving(false)
    setTimeout(() => setMsg(''), 5000)
  }

  if (loading) return <div style={{ color: 'var(--sub)', padding: 40, textAlign: 'center' }}>Loading…</div>

  return (
    <div style={{ maxWidth: 600 }}>
      <h2 style={{ color: 'var(--text)', marginBottom: 20, fontSize: 14, fontWeight: 700,
        textTransform: 'uppercase', letterSpacing: 1 }}>Strategy Configuration</h2>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        {FIELDS.map(f => (
          <div key={f.key} style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <label style={{ color: 'var(--sub)', fontSize: 11, textTransform: 'uppercase', letterSpacing: 0.5 }}>
              {f.label}
            </label>
            <input
              type={f.type || 'text'}
              value={cfg[f.key] || ''}
              onChange={e => setCfg({ ...cfg, [f.key]: e.target.value })}
              style={{ width: '100%' }}
            />
            <span style={{ color: 'var(--muted)', fontSize: 11 }}>{f.hint}</span>
          </div>
        ))}
      </div>

      <div style={{ marginTop: 24, display: 'flex', gap: 12, alignItems: 'center' }}>
        <button onClick={handleSave} disabled={saving} style={{
          background: 'var(--accent)', color: '#000',
          padding: '8px 24px', borderRadius: 4, fontWeight: 700, fontSize: 13
        }}>{saving ? 'Saving…' : 'Save Config'}</button>
        {msg && <span style={{ color: msg.startsWith('✓') ? 'var(--green)' : 'var(--red)', fontSize: 12 }}>{msg}</span>}
      </div>

      {/* Sizing calculator */}
      <div style={{ marginTop: 32, background: 'var(--surface)', border: '1px solid var(--border)',
        borderRadius: 6, padding: 16 }}>
        <div style={{ color: 'var(--accent)', fontWeight: 700, marginBottom: 12, fontSize: 12,
          textTransform: 'uppercase', letterSpacing: 1 }}>Position Sizing Preview</div>
        <SizingCalc cfg={cfg} />
      </div>
    </div>
  )
}

function SizingCalc({ cfg }) {
  const [price, setPrice] = useState('')
  const ath = price ? parseFloat(price) : null
  const buy = ath ? +(ath * 1.04).toFixed(2) : null
  const sl_pct = parseFloat(cfg.FIXED_LOSS ? cfg.SL_PCT : 6) / 100
  const fixed_loss = parseFloat(cfg.FIXED_LOSS || 750)
  const qty = buy ? Math.max(1, Math.floor(fixed_loss / (buy * sl_pct))) : null
  const capital = buy && qty ? +(buy * qty).toFixed(2) : null
  const sl = buy ? +(buy * (1 - sl_pct)).toFixed(2) : null

  return (
    <div>
      <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginBottom: 12 }}>
        <input type="number" placeholder="Enter ATH price" value={price}
          onChange={e => setPrice(e.target.value)} style={{ width: 160 }} />
      </div>
      {buy && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
          {[
            ['ATH', `₹${ath}`],
            ['Buy @ (ATH+4%)', `₹${buy}`],
            ['SL @ (−${Math.round(sl_pct*100)}%)', `₹${sl}`],
            ['Qty (Fixed Loss / Risk/share)', `${qty} shares`],
            ['Capital Required', `₹${capital?.toLocaleString('en-IN')}`],
            ['Max Loss', `₹${fixed_loss}`],
          ].map(([k,v]) => (
            <div key={k} style={{ background: 'var(--bg)', borderRadius: 4, padding: '6px 10px' }}>
              <div style={{ color: 'var(--sub)', fontSize: 10, textTransform: 'uppercase' }}>{k}</div>
              <div style={{ color: 'var(--text)', fontWeight: 700, marginTop: 2 }}>{v}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
