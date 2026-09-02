import { useEffect, useState } from 'react'
import { api } from '../api'
import type { ContestContext } from '../App'
import type { SearchResult } from '../types'
import Poster from '../components/Poster'

interface ImportEvent { stage: string; done?: number; total?: number; result?: any; error?: string }

export default function AddPage({ ctx }: { ctx: ContestContext }) {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<SearchResult[]>([])
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)

  const [status, setStatus] = useState<Awaited<ReturnType<typeof api.importStatus>> | null>(null)
  const [progress, setProgress] = useState<ImportEvent | null>(null)
  const [importResult, setImportResult] = useState<any>(null)

  useEffect(() => { void api.importStatus().then(setStatus).catch(() => {}) }, [])

  const runSearch = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!query.trim()) return
    setSearching(true)
    setSearchError(null)
    try {
      setResults((await api.search(query)).results)
    } catch (err) {
      setSearchError(String(err))
    } finally {
      setSearching(false)
    }
  }

  const add = async (result: SearchResult, watched: boolean) => {
    const { item } = await api.addItem({
      guid: result.guid, media_type: result.media_type, watched,
    })
    setResults((current) => current.map((r) =>
      r.guid === result.guid ? { ...r, existing_id: item.id } : r))
    await ctx.refreshTypes()
  }

  const runImport = (dryRun: boolean) => {
    setImportResult(null)
    setProgress({ stage: 'starting' })
    api.importPlex((event) => {
      if (event.stage === 'done') {
        setProgress(null)
        setImportResult(event.result)
        void ctx.refreshTypes()
        void api.importStatus().then(setStatus).catch(() => {})
      } else if (event.stage === 'error') {
        setProgress({ stage: 'error', error: event.error })
      } else {
        setProgress(event)
      }
    }, dryRun)
  }

  return (
    <div className="page">
      <div className="page-head">
        <h1>Add titles</h1>
        <p className="muted">
          Two ways in: pull everything you've watched out of Plex, or search for
          anything else — the cinema, a friend's account, years ago.
        </p>
      </div>

      <section className="panel">
        <h2>Import from Plex</h2>
        <p className="muted">
          Reads Plex's own watch ledger. It is keyed by global GUID rather than by
          library id, so it survives a library being removed or a machine migration —
          which is why this works even when Plex reports no libraries at all.
          Episodes roll up to their show.
        </p>

        {status && (
          <ul className="facts">
            <li>Plex database: <strong>{status.plex_db_present ? 'found' : 'missing'}</strong></li>
            <li>Plex token: <strong>{status.plex_token ? 'ok' : 'missing'}</strong></li>
            <li>Last import: <strong>
              {status.last_plex_import
                ? new Date(status.last_plex_import * 1000).toLocaleString()
                : 'never'}
            </strong></li>
            <li>MediaStack: <strong>
              {Object.entries(status.mediastack)
                .map(([k, v]) => `${k} ${v ? 'up' : 'down'}`).join(', ')}
            </strong> <span className="muted">(optional — adds an "on disk" badge)</span></li>
          </ul>
        )}

        <div className="row-actions">
          <button className="primary" disabled={!!progress} onClick={() => runImport(false)}>
            {progress ? 'Importing…' : 'Import watched titles'}
          </button>
          <button className="ghost" disabled={!!progress} onClick={() => runImport(true)}>
            Dry run
          </button>
        </div>

        {progress && (
          <div className="progress">
            <div className="progress-label">
              {progress.stage}
              {progress.total ? ` — ${progress.done}/${progress.total}` : '…'}
            </div>
            {progress.total ? (
              <div className="gauge">
                <span style={{ width: `${(100 * (progress.done ?? 0)) / progress.total}%` }} />
              </div>
            ) : null}
            {progress.error && <div className="flash is-error">{progress.error}</div>}
          </div>
        )}

        {importResult && (
          <div className="import-result">
            <p>
              <strong>{importResult.ledger_rows}</strong> watch rows →
              {' '}<strong>{importResult.resolved_titles}</strong> titles
              {importResult.unresolved > 0 && ` (${importResult.unresolved} unresolved)`}
            </p>
            <p className="muted">
              {importResult.movies} watched films · {importResult.episodes} episodes
              rolled up to {importResult.shows} shows
            </p>
            {importResult.dry_run ? (
              <p><em>Dry run — nothing was written.</em></p>
            ) : (
              <p>
                created <strong>{importResult.created}</strong> ·
                {' '}updated <strong>{importResult.updated}</strong> ·
                {' '}unchanged <strong>{importResult.unchanged}</strong>
              </p>
            )}
            <div className="chips">
              {Object.entries(importResult.by_type ?? {}).map(([key, count]) => (
                <span className="pill" key={key}>{key}: {String(count)}</span>
              ))}
            </div>
          </div>
        )}
      </section>

      <section className="panel">
        <h2>Search</h2>
        <form className="row-actions" onSubmit={runSearch}>
          <input
            className="search"
            placeholder="Title…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <button className="primary" type="submit" disabled={searching}>
            {searching ? 'Searching…' : 'Search'}
          </button>
        </form>
        {searchError && <div className="flash is-error">{searchError}</div>}

        <ul className="search-results">
          {results.map((result) => (
            <li key={result.guid} className="search-row">
              <Poster item={{ title: result.title, poster_url: result.poster_url }} size="row" />
              <div className="search-main">
                <div className="search-title">
                  {result.title} <span className="muted">{result.year ?? ''}</span>
                  <span className="pill">{result.media_type}</span>
                </div>
                <div className="muted search-summary">{result.summary}</div>
              </div>
              <div className="row-actions">
                {result.existing_id ? (
                  <span className="pill pill-ok">in library</span>
                ) : (
                  <>
                    <button className="primary sm" onClick={() => void add(result, true)}>
                      Add as watched
                    </button>
                    <button className="ghost sm" onClick={() => void add(result, false)}>
                      Add
                    </button>
                  </>
                )}
              </div>
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}
