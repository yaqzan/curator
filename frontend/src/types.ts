export interface MediaTypeInfo {
  key: string
  label: string
  singular: string
  icon: string
  question: string
  subject: string
  order: number
  stats: ContestStats
}

export interface ContestStats {
  media_type: string
  total: number
  untiered: number
  rounds: number
  covered: number
  covered_pct: number
  settled: number
  contested: number
  contested_confident: number
  audit_rounds_target: number
}

export interface Item {
  id: number
  media_type: string
  title: string
  year: number | null
  summary: string | null
  tagline: string | null
  studio: string | null
  duration_ms: number | null
  content_rating: string | null
  genres: string[]
  countries: string[]
  poster_url: string | null
  art_url: string | null
  critic_rating: number | null
  audience_rating: number | null
  tmdb_id: string | null
  imdb_id: string | null
  plex_slug: string | null

  source: string
  watched: boolean
  watch_count: number
  episodes_watched: number
  last_watched_at: number | null

  tier: number | null
  tier_label: string | null
  elo_score: number | null
  elo_sigma: number | null
  elo_rounds: number

  // derived server-side so no page reimplements the scorer's thresholds
  implied_tier: number | null
  boundary_margin: number | null
  contested: boolean
  contested_raw: boolean
  settled: boolean
  provisional: boolean

  notes: string | null
  archived: boolean
  ownership?: { owned: boolean; app: string; quality?: string } | null
  history?: MatchRow[]
  drift?: number
}

export interface MatchRow {
  id: number
  set_id: number | null
  is_tie: number
  created_at: number
  winner_id: number
  loser_id: number
  winner_title: string
  loser_title: string
}

export interface RankSet {
  type: string
  question: string
  subject: string
  size: number
  pool: number
  total?: number
  items: Item[]
}

export interface SearchResult {
  guid: string
  rating_key: string
  title: string
  year: number | null
  type: string
  media_type: string
  summary: string | null
  poster_url: string | null
  studio: string | null
  rating: number | null
  existing_id?: number | null
}

export interface RoundSummary {
  set_id: number
  created_at: number
  size: number
  ordering: (Pick<Item, 'id' | 'title' | 'year' | 'poster_url' | 'tier' | 'elo_score'> | null)[][]
  upsets: {
    match_id: number
    winner: { id: number; title: string; tier: number | null } | null
    loser: { id: number; title: string; tier: number | null } | null
    is_tie: boolean
  }[]
}
