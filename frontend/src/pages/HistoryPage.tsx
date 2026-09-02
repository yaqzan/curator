import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type { ContestContext } from '../App'
import type { RoundSummary } from '../types'
import Poster from '../components/Poster'

/**
 * Every recorded round, newest first, with its ordering rebuilt from the stored
 * pairs rather than a cached tier column — so a correction made here flows
 * straight back into scoring instead of silently disagreeing with a cache.
 *
 * **Upsets are not findings (don't re-present them as such).** A lower-tier title
 * beating a higher-tier one is mostly judgement noise on any single round; the
 * accumulated posterior is the high-precision version of the same signal, and it
 * lives on the Review tab. This page exists so a mis-click can be corrected.
 */
export default function HistoryPage({ ctx }: { ctx: ContestContext }) {
  const [rounds, setRounds] = useState<RoundSummary[]>([])
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    setRounds((await api.history(ctx.active.key)).rounds)
    setLoading(false)
  }, [ctx.active.key])

  useEffect(() => { void load() }, [load])

  const correct = async (matchId: number, body: { winner_id?: number; tie?: boolean }) => {
    await api.correctMatch(matchId, body)
    await Promise.all([load(), ctx.refreshTypes()])
  }

  if (loading) return <div className="boot">Loading…</div>

  if (!rounds.length) {
    return (
      <div className="page"><div className="empty-state">
        <h2>No rounds yet</h2>
        <p className="muted">Rank a set and it will show up here.</p>
      </div></div>
    )
  }

  return (
    <div className="page">
      <div className="page-head">
        <h1>History — {ctx.active.label}</h1>
        <p className="muted">
          {rounds.length} rounds. Each is stored as every pair in the set, so the
          ordering below is reconstructed from the data the scorer actually reads.
        </p>
      </div>

      <ul className="history-list">
        {rounds.map((round) => (
          <li key={round.set_id} className="history-round">
            <header className="history-head">
              <span className="muted">
                #{round.set_id} · {new Date(round.created_at * 1000).toLocaleString()} ·
                {' '}{round.size} titles
              </span>
              {round.upsets.length > 0 && (
                <span className="pill" title="a lower-tier title placed above a higher-tier one">
                  {round.upsets.length} cross-tier
                </span>
              )}
            </header>

            <ol className="history-order">
              {round.ordering.map((tier, index) => (
                <li key={index} className="history-tier">
                  <span className="rail-rank">{index + 1}</span>
                  <div className="rail-chips">
                    {tier.map((entry) => entry && (
                      <span className="rail-chip" key={entry.id}>
                        <Poster item={entry} size="chip" />
                        <span className="rail-chip-title">{entry.title}</span>
                      </span>
                    ))}
                  </div>
                  {tier.length > 1 && <span className="rail-tie">tied</span>}
                </li>
              ))}
            </ol>

            {round.upsets.length > 0 && (
              <div className="history-upsets">
                {round.upsets.map((upset) => (
                  <div className="upset-row" key={upset.match_id}>
                    <span>
                      <strong>{upset.winner?.title}</strong>
                      <span className="muted"> (tier {upset.winner?.tier})</span>
                      {upset.is_tie ? ' tied ' : ' beat '}
                      <strong>{upset.loser?.title}</strong>
                      <span className="muted"> (tier {upset.loser?.tier})</span>
                    </span>
                    <span className="upset-actions">
                      <button className="ghost sm"
                        onClick={() => void correct(upset.match_id, { winner_id: upset.loser?.id })}>
                        Flip
                      </button>
                      <button className="ghost sm"
                        onClick={() => void correct(upset.match_id, { tie: !upset.is_tie })}>
                        {upset.is_tie ? 'Untie' : 'Tie'}
                      </button>
                    </span>
                  </div>
                ))}
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}
