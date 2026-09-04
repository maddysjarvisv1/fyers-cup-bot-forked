export default function StatsBar({ stats }) {
  const items = [
    { label: 'Queue',     value: stats.queue_count,                  color: 'var(--blue)' },
    { label: 'Open',      value: stats.open_positions,               color: 'var(--amber)' },
    { label: 'Trades',    value: stats.total_trades,                 color: 'var(--sub)' },
    { label: 'Win Rate',  value: `${stats.win_rate}%`,               color: stats.win_rate >= 50 ? 'var(--green)' : 'var(--red)' },
    { label: 'Realised P&L', value: `₹${stats.total_pnl.toLocaleString('en-IN')}`,
      color: stats.total_pnl >= 0 ? 'var(--green)' : 'var(--red)' },
    { label: 'Open P&L',  value: `₹${(stats.open_pnl||0).toLocaleString('en-IN')}`,
      color: (stats.open_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' },
    { label: 'Avg Win',   value: `₹${stats.avg_win.toLocaleString('en-IN')}`, color: 'var(--green)' },
    { label: 'Avg Loss',  value: `₹${stats.avg_loss.toLocaleString('en-IN')}`, color: 'var(--red)' },
  ]

  return (
    <div style={{
      background: 'var(--surface)',
      borderBottom: '1px solid var(--border)',
      display: 'flex',
      gap: 0,
      overflowX: 'auto',
    }}>
      {items.map(({ label, value, color }) => (
        <div key={label} style={{
          padding: '8px 20px',
          borderRight: '1px solid var(--border)',
          minWidth: 110,
          flexShrink: 0,
        }}>
          <div style={{ color: 'var(--sub)', fontSize: 10, textTransform: 'uppercase', letterSpacing: 1 }}>
            {label}
          </div>
          <div style={{ color, fontWeight: 700, fontSize: 15, marginTop: 2 }}>
            {value}
          </div>
        </div>
      ))}
    </div>
  )
}
