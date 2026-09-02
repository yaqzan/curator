import { useCallback, useEffect, useMemo, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useSearchParams } from 'react-router-dom'
import { api } from './api'
import type { MediaTypeInfo } from './types'
import RankPage from './pages/RankPage'
import LeaderboardPage from './pages/LeaderboardPage'
import LibraryPage from './pages/LibraryPage'
import ReviewPage from './pages/ReviewPage'
import AddPage from './pages/AddPage'
import HistoryPage from './pages/HistoryPage'

export interface ContestContext {
  types: MediaTypeInfo[]
  active: MediaTypeInfo
  setActive: (key: string) => void
  refreshTypes: () => Promise<void>
}

const NAV = [
  { to: '/rank', label: 'Rank' },
  { to: '/leaderboard', label: 'Leaderboard' },
  { to: '/review', label: 'Review' },
  { to: '/history', label: 'History' },
  { to: '/library', label: 'Library' },
  { to: '/add', label: 'Add' },
]

export default function App() {
  const [types, setTypes] = useState<MediaTypeInfo[]>([])
  const [error, setError] = useState<string | null>(null)
  const [params, setParams] = useSearchParams()

  const refreshTypes = useCallback(async () => {
    try {
      setTypes((await api.types()).types)
      setError(null)
    } catch (err) {
      setError(String(err))
    }
  }, [])

  useEffect(() => { void refreshTypes() }, [refreshTypes])

  const activeKey = params.get('type') ?? types[0]?.key ?? 'movie'
  const active = useMemo(
    () => types.find((t) => t.key === activeKey) ?? types[0],
    [types, activeKey],
  )

  const setActive = useCallback((key: string) => {
    const next = new URLSearchParams(params)
    next.set('type', key)
    setParams(next, { replace: true })
  }, [params, setParams])

  if (error) {
    return (
      <div className="boot-error">
        <h1>Curator</h1>
        <p>Can't reach the API.</p>
        <code>{error}</code>
        <p className="muted">Start it with <code>python -m curator serve</code>.</p>
      </div>
    )
  }
  if (!active) return <div className="boot">Loading…</div>

  const context: ContestContext = { types, active, setActive, refreshTypes }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">◆</span>
          <span className="brand-name">Curator</span>
        </div>

        {/* The contest strip. Two titles in different contests never meet, so
            which tab you are on is the single most load-bearing piece of state
            on the page — it stays visible everywhere. */}
        <nav className="type-strip" aria-label="Media type">
          {types.map((type) => (
            <button
              key={type.key}
              className={`type-tab${type.key === active.key ? ' is-active' : ''}`}
              onClick={() => setActive(type.key)}
              title={`${type.stats.total} titles · ${type.stats.rounds} rounds`}
            >
              <span className="type-icon">{type.icon}</span>
              <span>{type.label}</span>
              <span className="type-count">{type.stats.total}</span>
              {type.stats.contested_confident > 0 && (
                <span className="type-flag" title="contested — score has left its filed tier">
                  {type.stats.contested_confident}
                </span>
              )}
            </button>
          ))}
        </nav>

        <nav className="page-nav" aria-label="Sections">
          {NAV.map((entry) => (
            <NavLink
              key={entry.to}
              to={`${entry.to}?type=${active.key}`}
              className={({ isActive }) => `page-link${isActive ? ' is-active' : ''}`}
            >
              {entry.label}
            </NavLink>
          ))}
        </nav>
      </header>

      <main className="content">
        <Routes>
          <Route path="/" element={<Navigate to={`/rank?type=${active.key}`} replace />} />
          <Route path="/rank" element={<RankPage ctx={context} />} />
          <Route path="/leaderboard" element={<LeaderboardPage ctx={context} />} />
          <Route path="/review" element={<ReviewPage ctx={context} />} />
          <Route path="/history" element={<HistoryPage ctx={context} />} />
          <Route path="/library" element={<LibraryPage ctx={context} />} />
          <Route path="/add" element={<AddPage ctx={context} />} />
          <Route path="*" element={<Navigate to={`/rank?type=${active.key}`} replace />} />
        </Routes>
      </main>
    </div>
  )
}
