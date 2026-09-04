import { useState, useEffect } from 'react'
import { api } from './api.js'
import QueuePanel from './components/QueuePanel.jsx'
import PositionsPanel from './components/PositionsPanel.jsx'
import TradeLog from './components/TradeLog.jsx'
import ConfigPanel from './components/ConfigPanel.jsx'
import StatsBar from './components/StatsBar.jsx'

const TABS = ['Queue', 'Positions', 'Trade Log', 'Config']

export default function App() {
  const [tab, setTab] = useState('Queue')
  const [stats, setStats] = useState(null)

  useEffect(() => {
    api.getStats().then(setStats).catch(() => {})
    const t = setInterval(() => api.getStats().then(setStats).catch(() => {}), 60000)
    return () => clearInterval(t)
  }, [])

  return (
    <div style={{ minHeight: '100vh', display: 'flex', flexDirection: 'column' }}>
      {/* Header */}
      <header style={{
        background: 'var(--surface)',
        borderBottom: '1px solid var(--border)',
        padding: '12px 24px',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        position: 'sticky', top: 0, zIndex: 100,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ color: 'var(--accent)', fontSize: 18, fontWeight: 700 }}>◈</span>
          <span style={{ color: 'var(--text)', fontWeight: 700, fontSize: 15, letterSpacing: 1 }}>
            CUP STRATEGY
          </span>
          <span style={{ color: 'var(--sub)', fontSize: 11 }}>· Malkansview ATH Breakout</span>
        </div>
        <div style={{ display: 'flex', gap: 6 }}>
          {TABS.map(t => (
            <button key={t} onClick={() => setTab(t)} style={{
              padding: '5px 14px',
              background: tab === t ? 'var(--accent)' : 'transparent',
              color: tab === t ? '#000' : 'var(--sub)',
              border: `1px solid ${tab === t ? 'var(--accent)' : 'var(--border)'}`,
              borderRadius: 4,
              fontWeight: tab === t ? 700 : 400,
              transition: 'all 0.15s',
            }}>{t}</button>
          ))}
        </div>
      </header>

      {/* Stats bar */}
      {stats && <StatsBar stats={stats} />}

      {/* Content */}
      <main style={{ flex: 1, padding: '20px 24px', maxWidth: 1400, width: '100%', margin: '0 auto' }}>
        {tab === 'Queue'      && <QueuePanel />}
        {tab === 'Positions'  && <PositionsPanel />}
        {tab === 'Trade Log'  && <TradeLog />}
        {tab === 'Config'     && <ConfigPanel />}
      </main>
    </div>
  )
}
