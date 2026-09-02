import type { ContestStats } from '../types'

/**
 * Three distinct progress signals — don't collapse them.
 *
 * - **covered** is COVERAGE ("has it been looked at properly"), the gauge.
 * - **contested** is the audit's actual FINDING, and the confident count is the
 *   stopping signal.
 * - **settled** is shown as a number and deliberately NOT as a bar: it is not
 *   reachable for every title, so a settled-percentage bar crawls toward a low
 *   ceiling and never fills, implying permanent failure when the work is done.
 *   A title sitting on a tier line genuinely IS borderline.
 */
export default function StatsBar({ stats }: { stats: ContestStats }) {
  return (
    <div className="stats-bar">
      <Stat label="titles" value={stats.total} />
      <Stat label="rounds" value={stats.rounds} />

      <div className="stat stat-gauge" title={`At least ${stats.audit_rounds_target} rounds each — coverage, not precision`}>
        <div className="stat-value">{stats.covered_pct}%</div>
        <div className="stat-label">covered</div>
        <div className="gauge"><span style={{ width: `${stats.covered_pct}%` }} /></div>
      </div>

      <Stat label="settled" value={stats.settled} muted />
      <Stat
        label="contested"
        value={stats.contested_confident}
        tone={stats.contested_confident > 0 ? 'warn' : undefined}
        title={`${stats.contested} over the line in total; ${stats.contested_confident} confidently so. A review queue, not a verdict.`}
      />
      {stats.untiered > 0 && <Stat label="untiered" value={stats.untiered} muted />}
    </div>
  )
}

function Stat({
  label, value, muted, tone, title,
}: {
  label: string; value: number; muted?: boolean; tone?: 'warn'; title?: string
}) {
  return (
    <div className={`stat${muted ? ' is-muted' : ''}${tone ? ` is-${tone}` : ''}`} title={title}>
      <div className="stat-value">{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  )
}
