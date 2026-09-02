import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type { ContestContext } from '../App'
import type { Item } from '../types'
import Poster from '../components/Poster'
import TierPicker from '../components/TierPicker'

const SORTS = [
  { key: 'score', label: 'Score' },
  { key: 'title', label: 'Title' },
  { key: 'year', label: 'Year' },
  { key: 'recent', label: 'Last watched' },
  { key: 'added', label: 'Added' },
  { key: 'rounds', label: 'Least ranked' },
]

export default function LibraryPage({ ctx }: { ctx: ContestContext }) {
  const [items, setItems] = useState<Item[]>([])
  const [total, setTotal] = useState(0)
  const [query, setQuery] = useState('')
  const [sort, setSort] = useState('score')
  const [showArchived, setShowArchived] = useState(false)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    setBusy(true)
    const data = await api.items({
      type: ctx.active.key, q: query, sort,
      archived: showArchived ? 1 : undefined, ownership: 1,
    })
    setItems(data.items)
    setTotal(data.total)
    setBusy(false)
  }, [ctx.active.key, query, sort, showArchived])

  useEffect(() => {
    const timer = window.setTimeout(() => { void load() }, query ? 250 : 0)
    return () => window.clearTimeout(timer)
  }, [load, query])

  const patch = async (item: Item, changes: Partial<Item>) => {
    await api.updateItem(item.id, changes)
    await Promise.all([load(), ctx.refreshTypes()])
  }

  return (
    <div className="page">
      <div className="page-head">
        <h1>{ctx.active.icon} {ctx.active.label} library</h1>
        <p className="muted">
          {total} titles. Metadata refreshes on every import; your tier, notes and
          which contest a title belongs to never do.
        </p>
      </div>

      <div className="toolbar">
        <input
          className="search"
          placeholder="Filter by title or studio…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <div className="seg">
          {SORTS.map((option) => (
            <button
              key={option.key}
              className={`seg-btn${sort === option.key ? ' is-active' : ''}`}
              onClick={() => setSort(option.key)}
            >{option.label}</button>
          ))}
        </div>
        <label className="check">
          <input
            type="checkbox"
            checked={showArchived}
            onChange={(event) => setShowArchived(event.target.checked)}
          />
          Show archived
        </label>
      </div>

      {busy && <div className="muted">Loading…</div>}

      <div className="library-grid">
        {items.map((item) => (
          <article key={item.id} className={`lib-card${item.archived ? ' is-archived' : ''}`}>
            <Poster item={item} size="card" />
            <div className="lib-body">
              <h3 className="lib-title">
                {item.title} <span className="muted">{item.year ?? ''}</span>
              </h3>
              <div className="lib-meta muted">
                {Math.round(item.elo_score ?? 0)} ±{Math.round(item.elo_sigma ?? 0)} ·
                {' '}{item.elo_rounds} rounds
                {item.episodes_watched > 0 && ` · ${item.episodes_watched} eps`}
                {item.ownership?.owned && (
                  <span className="pill pill-ok" title={`on disk (${item.ownership.app})`}>
                    on disk
                  </span>
                )}
              </div>

              <TierPicker value={item.tier} compact
                onChange={(tier) => void patch(item, { tier })} />

              <div className="lib-actions">
                {/* Re-filing a title is a real move: its comparisons belonged to
                    the old contest and would become cross-contest judgements, so
                    the server drops them rather than letting them leak. */}
                <select
                  value={item.media_type}
                  onChange={(event) => {
                    if (!window.confirm(
                      `Move "${item.title}" to ${event.target.value}?\n\n` +
                      'Its comparison history belongs to the current contest and will be discarded.',
                    )) return
                    void patch(item, { media_type: event.target.value })
                  }}
                >
                  {ctx.types.map((type) => (
                    <option key={type.key} value={type.key}>{type.icon} {type.label}</option>
                  ))}
                </select>
                <button className="ghost sm"
                  onClick={() => void patch(item, { archived: !item.archived })}>
                  {item.archived ? 'Restore' : 'Archive'}
                </button>
              </div>
            </div>
          </article>
        ))}
      </div>

      {!busy && !items.length && (
        <div className="empty-state">
          <p className="muted">Nothing matches.</p>
        </div>
      )}
    </div>
  )
}
