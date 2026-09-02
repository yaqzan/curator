import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type { ContestContext } from '../App'
import type { Item } from '../types'
import Poster from '../components/Poster'
import TierPicker from '../components/TierPicker'

/**
 * The audit's OUTPUT — titles whose accumulated score has left the tier they are
 * filed under.
 *
 * **A review queue, not a verdict.** At these settings roughly half of true
 * one-tier misfilings surface and about one in six flagged titles is a false
 * alarm — a fine ratio for something a human looks at, and a terrible one for
 * anything automatic. Nothing here moves a tier on its own.
 */
export default function ReviewPage({ ctx }: { ctx: ContestContext }) {
  const [items, setItems] = useState<Item[]>([])
  const [gate, setGate] = useState(true)
  const [loading, setLoading] = useState(true)
  const [dismissed, setDismissed] = useState<Set<number>>(new Set())

  const load = useCallback(async () => {
    setLoading(true)
    setItems((await api.review(ctx.active.key, gate)).items)
    setLoading(false)
  }, [ctx.active.key, gate])

  useEffect(() => { void load() }, [load])

  const accept = async (item: Item) => {
    await api.updateItem(item.id, { tier: item.implied_tier })
    await Promise.all([load(), ctx.refreshTypes()])
  }

  const visible = items.filter((item) => !dismissed.has(item.id))

  if (loading) return <div className="boot">Loading…</div>

  return (
    <div className="page">
      <div className="page-head">
        <h1>Review — {ctx.active.label}</h1>
        <p className="muted">
          These titles score outside the tier you filed them under. Look at them, then
          accept the implied tier, pick another, or keep what you had.
        </p>
      </div>

      <label className="check">
        <input type="checkbox" checked={!gate} onChange={(e) => setGate(!e.target.checked)} />
        Include boundary drift
        <span className="muted">
          {' '}— titles grazing a tier line. Mostly noise: they cross and come back as
          evidence trickles in.
        </span>
      </label>

      {!visible.length && (
        <div className="empty-state">
          <h2>Nothing contested</h2>
          <p className="muted">
            {ctx.active.stats.rounds === 0
              ? 'No rounds recorded yet — rank a few sets and findings will appear here.'
              : 'Every filed tier is holding up. The honest stopping rule is "sweeping ' +
                'turns up nothing new" — not "everything settled".'}
          </p>
        </div>
      )}

      <ul className="review-list">
        {visible.map((item) => (
          <li key={item.id} className={`review-row${item.provisional ? ' is-provisional' : ''}`}>
            <Poster item={item} size="row" />
            <div className="review-main">
              <div className="review-title">
                {item.title} <span className="muted">{item.year ?? ''}</span>
                {item.provisional && (
                  <span className="pill" title={`under ${ctx.active.stats.audit_rounds_target} rounds — not enough evidence yet`}>
                    provisional
                  </span>
                )}
              </div>
              <div className="review-move">
                <span className={`tier-dot tier-${item.tier}`}>{item.tier}</span>
                <span className="arrow">→</span>
                <span className={`tier-dot tier-${item.implied_tier} is-active`}>
                  {item.implied_tier}
                </span>
                <span className="muted">
                  {Math.round(item.elo_score ?? 0)} ±{Math.round(item.elo_sigma ?? 0)} ·
                  {' '}{Math.round(item.drift ?? 0)} ELO out of tier · {item.elo_rounds} rounds
                </span>
              </div>
            </div>
            <div className="review-actions">
              <button className="primary sm" onClick={() => void accept(item)}>
                Accept {item.implied_tier}
              </button>
              <TierPicker value={item.tier} compact onChange={async (tier) => {
                await api.updateItem(item.id, { tier })
                await Promise.all([load(), ctx.refreshTypes()])
              }} />
              {/* Session-only, deliberately not persisted: a dismissal that
                  survived would quietly hide a real finding forever. */}
              <button className="ghost sm"
                onClick={() => setDismissed(new Set(dismissed).add(item.id))}>
                Keep
              </button>
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}
