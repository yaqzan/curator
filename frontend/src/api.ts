import type {
  ContestStats, Item, MediaTypeInfo, RankSet, RoundSummary, SearchResult,
} from './types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!response.ok) {
    let detail = response.statusText
    try {
      detail = (await response.json()).error ?? detail
    } catch { /* non-JSON error body */ }
    throw new Error(detail)
  }
  return response.json() as Promise<T>
}

export const api = {
  health: () => request<{ status: string; items: number; rounds: number }>('/api/health'),

  types: () => request<{
    types: MediaTypeInfo[]
    tier_labels: Record<string, string>
    set_sizes: number[]
    default_set_size: number
  }>('/api/types'),

  items: (params: Record<string, string | number | undefined>) => {
    const query = new URLSearchParams()
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined && value !== '') query.set(key, String(value))
    })
    return request<{ items: Item[]; total: number }>(`/api/items?${query}`)
  },

  item: (id: number) => request<Item>(`/api/items/${id}`),

  updateItem: (id: number, patch: Partial<Item>) =>
    request<Item>(`/api/items/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    }),

  deleteItem: (id: number) =>
    request<{ deleted: number }>(`/api/items/${id}`, { method: 'DELETE' }),

  search: (query: string) =>
    request<{ results: SearchResult[] }>(`/api/search?q=${encodeURIComponent(query)}`),

  addItem: (body: { guid: string; media_type?: string; tier?: number; watched?: boolean }) =>
    request<{ action: string; item: Item }>('/api/items', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  importStatus: () => request<{
    last_plex_import: number | null
    plex_db_present: boolean
    plex_token: boolean
    mediastack: Record<string, boolean>
  }>('/api/import/status'),

  rankSet: (type: string, size: number) =>
    request<RankSet>(`/api/rank/set?type=${type}&size=${size}`),

  submitRanking: (type: string, tiers: number[][]) =>
    request<{ set_id: number; pairs: number; stats: ContestStats }>('/api/rank/set-result', {
      method: 'POST',
      body: JSON.stringify({ type, tiers }),
    }),

  undoRound: (type: string) =>
    request<{ undone_set: number; stats: ContestStats }>('/api/rank/undo', {
      method: 'POST',
      body: JSON.stringify({ type }),
    }),

  stats: (type: string) => request<ContestStats>(`/api/rank/stats?type=${type}`),

  leaderboard: (type: string) =>
    request<{ items: Item[] }>(`/api/rank/leaderboard?type=${type}`),

  review: (type: string, gate = true) =>
    request<{ items: Item[] }>(`/api/rank/review?type=${type}${gate ? '' : '&gate=0'}`),

  history: (type: string) =>
    request<{ rounds: RoundSummary[] }>(`/api/rank/history?type=${type}`),

  correctMatch: (matchId: number, body: { winner_id?: number; tie?: boolean }) =>
    request(`/api/matches/${matchId}/result`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  refit: (type?: string) =>
    request(`/api/rank/refit${type ? `?type=${type}` : ''}`, { method: 'POST' }),

  /** The Plex import streams SSE — several hundred GUID lookups is long enough
   *  that a silent spinner reads as a hang. */
  importPlex(
    onEvent: (event: { stage: string; done?: number; total?: number; result?: unknown; error?: string }) => void,
    dryRun = false,
  ) {
    const controller = new AbortController()
    fetch(`/api/import/plex${dryRun ? '?dry_run=1' : ''}`, {
      method: 'POST',
      signal: controller.signal,
    }).then(async (response) => {
      const reader = response.body?.getReader()
      if (!reader) return
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const chunks = buffer.split('\n\n')
        buffer = chunks.pop() ?? ''
        for (const chunk of chunks) {
          const line = chunk.split('\n').find((l) => l.startsWith('data: '))
          if (!line) continue
          try { onEvent(JSON.parse(line.slice(6))) } catch { /* keepalive */ }
        }
      }
    }).catch((error) => {
      if (error.name !== 'AbortError') onEvent({ stage: 'error', error: String(error) })
    })
    return () => controller.abort()
  },
}
