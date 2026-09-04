const BASE = '/api'

async function req(path, opts = {}) {
  const r = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  })
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }))
    throw new Error(err.detail || 'Request failed')
  }
  return r.json()
}

export const api = {
  // Config
  getConfig:    ()       => req('/config'),
  saveConfig:   updates  => req('/config', { method: 'POST', body: JSON.stringify({ updates }) }),

  // Queue
  getQueue:     ()       => req('/queue'),
  addStock:     (symbol, ath) => req('/queue/add', { method: 'POST', body: JSON.stringify({ symbol, ath: ath || null }) }),
  removeQueue:  symbol   => req(`/queue/${symbol}`, { method: 'DELETE' }),
  importFile:   file     => {
    const fd = new FormData(); fd.append('file', file)
    return fetch(BASE + '/queue/import', { method: 'POST', body: fd }).then(r => r.json())
  },

  // Positions
  getPositions:   ()       => req('/positions'),
  addPosition:    body     => req('/positions/add', { method: 'POST', body: JSON.stringify(body) }),
  updatePosition: (id, b)  => req(`/positions/${id}`, { method: 'PATCH', body: JSON.stringify(b) }),
  closePosition:  (id, price, reason) =>
    req(`/positions/${id}?exit_price=${price}&reason=${reason}`, { method: 'DELETE' }),

  // Trades & Stats
  getTrades: () => req('/trades'),
  getStats:  () => req('/stats'),
}
