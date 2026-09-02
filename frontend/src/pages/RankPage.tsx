import { useCallback, useEffect, useRef, useState } from 'react'
import type { PointerEvent as ReactPointerEvent } from 'react'
import { api } from '../api'
import type { ContestContext } from '../App'
import { usePointerDrag } from '../dnd'
import type { Item, RankSet } from '../types'
import Poster from '../components/Poster'
import StatsBar from '../components/StatsBar'

const SET_SIZES = [4, 6, 9]
const SIZE_KEY = 'curator.setSize'

/** What a title is being dragged onto. `rank` inserts a new tier *before* tier
 *  `index`; `tie` joins tier `index`; `pool` sends a placed title back. */
type DropTarget =
  | { kind: 'rank'; index: number }
  | { kind: 'tie'; index: number }
  | { kind: 'pool' }

/** Where the drag started — the pool, or the index of the tier it sits in. */
type DragPayload = { id: number; from: 'pool' | number }

/**
 * Move one title to `target`, returning the new tier list.
 *
 * Emptied tiers are kept in place until the very end: `target.index` was
 * measured against the tiers the pointer was actually over, so dropping the
 * indices early would shift every position below the title being moved.
 */
export function applyDrop(tiers: number[][], itemId: number, target: DropTarget): number[][] {
  const stripped = tiers.map((tier) => tier.filter((id) => id !== itemId))
  if (target.kind === 'tie') {
    const tier = stripped[target.index]
    if (tier) stripped[target.index] = [...tier, itemId]
    else stripped.push([itemId])
  } else if (target.kind === 'rank') {
    stripped.splice(target.index, 0, [itemId])
  }
  return stripped.filter((tier) => tier.length > 0)
}

/**
 * The elicitation surface: order N titles into tiers, ties allowed.
 *
 * **Why ranking rather than "pick the best" (don't silently revert).** Ordering a
 * set pins down every relation in it rather than just naming a winner. Total
 * Fisher information at equal scores: a duel is 0.500, top-1 of 6 is 0.833, a
 * full ranking of 6 is 3.550 — 7.1x the duel. Measured, ordering 6 reached in
 * ~110 rounds what top-1 needed ~700 for.
 *
 * **Ties are load-bearing, not a convenience.** Forcing a choice between two
 * titles you read as equal feeds a coin flip into the model as though it were
 * signal. One tier holding everything ("these are all the same to me") is a
 * legitimate, accepted answer.
 *
 * Two ways in, and both must keep working. Clicking places at the *end* of the
 * order, which is the fast path once you have read the set. Dragging places at a
 * *chosen* position — including back into an order you have already started —
 * which is what you want when the sixth title turns out to belong second.
 */
export default function RankPage({ ctx }: { ctx: ContestContext }) {
  const [set, setSet] = useState<RankSet | null>(null)
  const [tiers, setTiers] = useState<number[][]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [flash, setFlash] = useState<string | null>(null)
  const [size, setSize] = useState(() => {
    const stored = Number(localStorage.getItem(SIZE_KEY))
    return SET_SIZES.includes(stored) ? stored : 6
  })

  // Tracks whether a tie group is open. Grouping applies to the clicks you make,
  // NOT to the previously placed title — see `place()`.
  const groupOpen = useRef(false)
  // Checked SYNCHRONOUSLY, and released only after the NEXT set has landed. A
  // React state flag is too late: there is a window after submitting where the
  // finished ranking is still in state and the next set has not arrived.
  const submitLock = useRef(false)

  const loadSet = useCallback(async (nextSize = size) => {
    setLoading(true)
    setError(null)
    try {
      const data = await api.rankSet(ctx.active.key, nextSize)
      setSet(data)
      setTiers([])
      groupOpen.current = false
    } catch (err) {
      setError(String(err))
    } finally {
      setLoading(false)
      submitLock.current = false
    }
  }, [ctx.active.key, size])

  useEffect(() => { void loadSet() }, [loadSet])

  const placed = tiers.flat()
  const remaining = (set?.items ?? []).filter((item) => !placed.includes(item.id))

  const submit = useCallback(async (finalTiers: number[][]) => {
    try {
      const result = await api.submitRanking(ctx.active.key, finalTiers)
      setFlash(`Round recorded — ${result.pairs} pairs`)
      window.setTimeout(() => setFlash(null), 1800)
      await ctx.refreshTypes()
      await loadSet()
    } catch (err) {
      setError(String(err))
      submitLock.current = false
    }
  }, [ctx, loadSet])

  /**
   * Submit as soon as the last title lands.
   *
   * Called from inside a state updater, which React re-runs in StrictMode, so
   * the lock is re-checked here rather than only at the top of the click — a
   * second run must not fire a second round.
   */
  const commit = useCallback((next: number[][], current: RankSet) => {
    if (submitLock.current) return
    if (next.flat().length !== current.items.length) return
    submitLock.current = true
    void submit(next)
  }, [submit])

  /**
   * Place one title.
   *
   * **Submission is an EVENT consequence, never a render effect (don't regress).**
   * An earlier version of this system auto-submitted from an effect watching "all
   * placed"; because fetching the next set awaits the network, the effect fired
   * twice and *every round was recorded twice* — double-counting the evidence and
   * manufacturing false findings. The lock is a ref so it is visible
   * synchronously within the same click.
   */
  const place = useCallback((itemId: number, grouped: boolean) => {
    if (submitLock.current || !set) return

    setTiers((current) => {
      let next: number[][]
      if (grouped && groupOpen.current && current.length > 0) {
        // Join the tier this run of grouped clicks opened.
        next = current.map((tier, index) =>
          index === current.length - 1 ? [...tier, itemId] : tier)
      } else {
        next = [...current, [itemId]]
        // A grouped click after a plain one STARTS a new tier and opens it, so
        // "these next two are tied" never requires mis-tying one of them first.
        groupOpen.current = grouped
      }

      commit(next, set)
      return next
    })
  }, [set, commit])

  /**
   * Place — or move — one title by dropping it.
   *
   * A drag closes any open run of tie-clicks: `groupOpen` means "the tier at the
   * END of the order is still taking members", and after an arbitrary-position
   * drop that is no longer the tier the user last touched.
   */
  const drop = useCallback((payload: DragPayload, target: DropTarget | null) => {
    if (submitLock.current || !set || !target) return
    groupOpen.current = false
    setTiers((current) => {
      const next = applyDrop(current, payload.id, target)
      commit(next, set)
      return next
    })
  }, [set, commit])

  /**
   * Resolve viewport coordinates to a drop target.
   *
   * The rail row is divided top/middle/bottom: the middle of a row means *tie
   * with it*, its edges mean *rank above / below it*. Reading the geometry here
   * rather than rendering a placeholder into the list keeps the layout still —
   * an inserted gap would shove the row out from under the pointer and the
   * target would oscillate.
   */
  const resolveDrop = useCallback(
    (x: number, y: number, payload: DragPayload): DropTarget | null => {
      const under = document.elementFromPoint(x, y)
      if (!under) return null

      const row = under.closest<HTMLElement>('[data-rail-row]')
      if (row) {
        const index = Number(row.dataset.railRow)
        const box = row.getBoundingClientRect()
        const fraction = (y - box.top) / box.height
        if (fraction < 0.3) return { kind: 'rank', index }
        if (fraction > 0.7) return { kind: 'rank', index: index + 1 }
        return { kind: 'tie', index }
      }

      const rail = under.closest<HTMLElement>('[data-rail]')
      if (rail) {
        // Empty rail space: rank by how many rows the pointer is already past.
        const rows = Array.from(rail.querySelectorAll<HTMLElement>('[data-rail-row]'))
        return {
          kind: 'rank',
          index: rows.filter((other) => {
            const box = other.getBoundingClientRect()
            return box.top + box.height / 2 < y
          }).length,
        }
      }

      if (under.closest('[data-pool]')) {
        return payload.from === 'pool' ? null : { kind: 'pool' }
      }
      return null
    }, [])

  const { drag, start: startDrag, consumeClick } =
    usePointerDrag<DragPayload, DropTarget>({ resolve: resolveDrop, onDrop: drop })

  const takeBack = useCallback(() => {
    if (submitLock.current) return
    setTiers((current) => {
      if (!current.length) return current
      const last = current[current.length - 1]
      if (last.length > 1) {
        return [...current.slice(0, -1), last.slice(0, -1)]
      }
      groupOpen.current = false
      return current.slice(0, -1)
    })
  }, [])

  const undoRound = useCallback(async () => {
    try {
      await api.undoRound(ctx.active.key)
      setFlash('Last round undone')
      window.setTimeout(() => setFlash(null), 1800)
      await ctx.refreshTypes()
      await loadSet()
    } catch (err) {
      setError(String(err))
    }
  }, [ctx, loadSet])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.target instanceof HTMLInputElement) return
      if (event.key === 'Backspace') { event.preventDefault(); takeBack() }
      else if (event.key === 's' || event.key === 'S') { void loadSet() }
      else if (event.key === 'z' || event.key === 'Z') { void undoRound() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [takeBack, loadSet, undoRound])

  const changeSize = (next: number) => {
    setSize(next)
    localStorage.setItem(SIZE_KEY, String(next))
    void loadSet(next)
  }

  if (loading && !set) return <div className="boot">Loading a set…</div>

  const notEnough = set && set.items.length < 2
  if (notEnough) {
    return (
      <div className="page">
        <EmptyContest ctx={ctx} total={set?.total ?? 0} />
      </div>
    )
  }

  return (
    <div className="page rank-page">
      {/* The two contests look near-identical by design, so the question is
          stated verbatim rather than inferred from the tab. */}
      <div className="dim-banner">
        <span className="dim-icon">{ctx.active.icon}</span>
        <div>
          <strong>{set?.question ?? ctx.active.question}</strong>
          <span className="muted"> — judging {set?.subject ?? ctx.active.subject}</span>
        </div>
        <div className="dim-tools">
          <div className="seg">
            {SET_SIZES.map((option) => (
              <button
                key={option}
                className={`seg-btn${option === size ? ' is-active' : ''}`}
                onClick={() => changeSize(option)}
              >{option}</button>
            ))}
          </div>
          <button className="ghost" onClick={() => void loadSet()} title="Skip this set (S)">
            Skip
          </button>
          <button className="ghost" onClick={() => void undoRound()} title="Undo last round (Z)">
            Undo round
          </button>
        </div>
      </div>

      <StatsBar stats={ctx.active.stats} />
      {flash && <div className="flash">{flash}</div>}
      {error && <div className="flash is-error">{error}</div>}

      {/* Rail on the LEFT, pool on the right: a strip *under* the grid pushed
          the posters up the page as the ranking grew. */}
      <div className={`rank-layout${drag ? ' is-dragging' : ''}`}>
        <aside
          className={`rank-rail${drag && drag.target?.kind === 'rank' ? ' is-target' : ''}`}
          data-rail=""
        >
          <div className="rail-head">
            <span>Your order</span>
            <button className="ghost sm" onClick={takeBack} disabled={!tiers.length}>
              ⌫ Back
            </button>
          </div>

          {tiers.length === 0 && (
            <p className="rail-hint">
              Click a title to place it, or drag it here to choose where it goes.<br />
              <kbd>Shift</kbd> or <kbd>Ctrl</kbd> + click to tie it with your next picks —
              or drop one title onto another.
            </p>
          )}

          <ol className="rail-list">
            {tiers.map((tier, index) => (
              <li
                key={index}
                className={`rail-row${dropClass(drag?.target ?? null, index, tiers.length)}`}
                data-rail-row={index}
              >
                <span className="rail-rank">{index + 1}</span>
                <div className="rail-chips">
                  {tier.map((id) => {
                    const item = set?.items.find((candidate) => candidate.id === id)
                    return (
                      <span
                        className={`rail-chip${drag?.payload.id === id ? ' is-lifted' : ''}`}
                        key={id}
                        onPointerDown={(event) => startDrag(event, { id, from: index })}
                      >
                        <Poster item={item} size="chip" />
                        <span className="rail-chip-title">{item?.title}</span>
                      </span>
                    )
                  })}
                </div>
                {tier.length > 1 && <span className="rail-tie">tied</span>}
              </li>
            ))}
          </ol>

          <div className="rail-foot muted">
            {placed.length} of {set?.items.length ?? 0} placed
            {placed.length === (set?.items.length ?? 0) && placed.length > 0 && ' — submitting…'}
          </div>
        </aside>

        <section
          className={`rank-grid${drag?.target?.kind === 'pool' ? ' is-target' : ''}`}
          data-pool=""
        >
          {remaining.map((item) => (
            <RankCard
              key={item.id}
              item={item}
              lifted={drag?.payload.id === item.id}
              onPointerDown={(event) => startDrag(event, { id: item.id, from: 'pool' })}
              onPlace={(grouped) => {
                if (consumeClick()) return
                place(item.id, grouped)
              }}
            />
          ))}
          {remaining.length === 0 && (
            <div className="grid-done muted">All placed — recording…</div>
          )}
        </section>
      </div>

      {/* Follows the pointer. `pointer-events: none` keeps it out of
          `elementFromPoint`, which is what resolves the drop target. */}
      {drag && (
        <div className="drag-ghost" style={{ left: drag.x, top: drag.y }}>
          <Poster item={set?.items.find((item) => item.id === drag.payload.id)} size="chip" />
          <span className="rail-chip-title">
            {set?.items.find((item) => item.id === drag.payload.id)?.title}
          </span>
        </div>
      )}

      <footer className="rank-keys muted">
        <kbd>Click</kbd> place · <kbd>Drag</kbd> place or reorder · drop <kbd>onto</kbd> a row to
        tie · <kbd>Shift</kbd>/<kbd>Ctrl</kbd>+click tie · <kbd>⌫</kbd> take back ·
        <kbd>S</kbd> skip set · <kbd>Z</kbd> undo round
      </footer>
    </div>
  )
}

/** Which edge (or middle) of rail row `index` the drag is currently aiming at. */
function dropClass(target: DropTarget | null, index: number, rows: number): string {
  if (!target) return ''
  if (target.kind === 'tie') return target.index === index ? ' drop-tie' : ''
  if (target.kind !== 'rank') return ''
  if (target.index === index) return ' drop-above'
  // The insertion point past the last row has no row of its own to mark.
  if (target.index === rows && index === rows - 1) return ' drop-below'
  return ''
}

function RankCard({ item, lifted, onPlace, onPointerDown }: {
  item: Item
  lifted: boolean
  onPlace: (grouped: boolean) => void
  onPointerDown: (event: ReactPointerEvent) => void
}) {
  return (
    <button
      className={`rank-card${lifted ? ' is-lifted' : ''}`}
      onPointerDown={onPointerDown}
      onClick={(event) => onPlace(event.shiftKey || event.ctrlKey || event.metaKey)}
      title={item.summary ?? undefined}
    >
      <Poster item={item} size="card" />
      {/* Title sits ON the poster over a scrim — a caption bar underneath
          inserted a strip of chrome between every poster. */}
      <span className="card-caption">
        <span className="card-title">{item.title}</span>
        <span className="card-meta">
          {item.year ?? '—'}
          {item.episodes_watched > 0 && ` · ${item.episodes_watched} eps`}
        </span>
      </span>
      {item.tier && <span className="card-tier">{item.tier}</span>}
    </button>
  )
}

function EmptyContest({ ctx, total }: { ctx: ContestContext; total: number }) {
  return (
    <div className="empty-state">
      <h2>{ctx.active.icon} {ctx.active.label}</h2>
      {total < 2 ? (
        <>
          <p>
            There {total === 1 ? 'is 1 title' : `are ${total} titles`} in this contest —
            ranking needs at least two.
          </p>
          <p className="muted">
            Import what you've watched from Plex, or search for titles on the
            <strong> Add</strong> page.
          </p>
        </>
      ) : (
        <p className="muted">
          Everything here has been audited and sits comfortably inside its tier.
          Nothing left to ask.
        </p>
      )}
    </div>
  )
}
