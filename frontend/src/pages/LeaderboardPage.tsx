import { useEffect, useState } from 'react'
import { api } from '../api'
import type { ContestContext } from '../App'
import type { Item } from '../types'
import Poster from '../components/Poster'
import TierPicker from '../components/TierPicker'

export default function LeaderboardPage({ ctx }: { ctx: ContestContext }) {
  const [items, setItems] = useState<Item[]>([])
  const [loading, setLoading] = useState(true)

  const load = async () => {
    setLoading(true)
    setItems((await api.leaderboard(ctx.active.key)).items)
    setLoading(false)
  }
  useEffect(() => { void load() }, [ctx.active.key])

  const onTier = async (item: Item, tier: number | null) => {
    await api.updateItem(item.id, { tier })
    await Promise.all([load(), ctx.refreshTypes()])
  }

  if (loading) return <div className="boot">Loading…</div>
  if (!items.length) {
    return (
      <div className="page"><div className="empty-state">
        <h2>{ctx.active.icon} {ctx.active.label}</h2>
        <p className="muted">Nothing here yet — import from Plex or add titles.</p>
      </div></div>
    )
  }

  return (
    <div className="page">
      <div className="page-head">
        <h1>{ctx.active.icon} {ctx.active.label} — the standings</h1>
        <p className="muted">
          Score is a posterior, not a transcript of the last round. A title you ranked
          below another can still outscore it — one round has not overturned the prior.
        </p>
      </div>

      <ol className="board">
        {items.map((item, index) => (
          <li key={item.id} className={`board-row${item.provisional ? ' is-provisional' : ''}`}>
            <span className="board-rank">{index + 1}</span>
            <Poster item={item} size="row" />
            <div className="board-main">
              <div className="board-title">
                {item.title}
                <span className="muted"> {item.year ?? ''}</span>
                {item.contested && (
                  <span className="pill pill-warn" title="score has left its filed tier">
                    contested
                  </span>
                )}
                {item.provisional && (
                  <span className="pill" title={`under ${ctx.active.stats.audit_rounds_target} rounds`}>
                    provisional
                  </span>
                )}
              </div>
              <div className="board-sub muted">
                {item.studio ?? '—'}
                {item.episodes_watched > 0 && ` · ${item.episodes_watched} episodes watched`}
              </div>
            </div>
            <div className="board-score">
              <strong>{Math.round(item.elo_score ?? 0)}</strong>
              <span className="muted"> ±{Math.round(item.elo_sigma ?? 0)}</span>
              <div className="muted board-rounds">{item.elo_rounds} rounds</div>
            </div>
            <TierPicker value={item.tier} onChange={(tier) => void onTier(item, tier)} />
          </li>
        ))}
      </ol>
    </div>
  )
}
