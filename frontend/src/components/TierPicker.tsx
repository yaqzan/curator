const TIERS = [
  { value: 1, label: 'Bad' },
  { value: 2, label: 'Weak' },
  { value: 3, label: 'Good' },
  { value: 4, label: 'Great' },
  { value: 5, label: 'All-time' },
]

/**
 * The filed 1-5 tier. It seeds the fit's PRIOR, so changing one moves that
 * title's starting point and therefore what every opponent's result implies —
 * which is why the server refits the whole contest on a tier change rather than
 * nudging one row.
 *
 * Leaving it unset is fine and common: an untiered title seeds neutral at 1000
 * and the ranking rounds alone decide where it lands.
 */
export default function TierPicker({
  value, onChange, compact,
}: {
  value: number | null
  onChange: (tier: number | null) => void
  compact?: boolean
}) {
  return (
    <div className={`tier-picker${compact ? ' is-compact' : ''}`} role="group" aria-label="Tier">
      {TIERS.map((tier) => (
        <button
          key={tier.value}
          className={`tier-dot tier-${tier.value}${value === tier.value ? ' is-active' : ''}`}
          title={`${tier.label} (${tier.value})`}
          onClick={() => onChange(value === tier.value ? null : tier.value)}
        >
          {tier.value}
        </button>
      ))}
    </div>
  )
}
