import type { Mode, Weekend } from '../types';
import { MODES, joinClassNames } from '../utils';

interface FiltersProps {
  weekends: Weekend[];
  weekend: string;
  mode: Mode;
  onWeekendChange: (weekend: string) => void;
  onModeChange: (mode: Mode) => void;
  allowCollection?: boolean;
  compact?: boolean;
}

export const TravelFilters = ({
  weekends,
  weekend,
  mode,
  onWeekendChange,
  onModeChange,
  allowCollection = false,
  compact = false,
}: FiltersProps) => {
  const activeWeekend = weekends.find((item) => item.friday === weekend);

  return (
    <section className={joinClassNames('filter-panel', compact && 'is-compact')} aria-label="Trip filters">
      <div className="filter-field">
        <label htmlFor="weekend-select">Weekend</label>
        <div className="select-wrap">
          <select
            id="weekend-select"
            value={weekend}
            onChange={(event) => onWeekendChange(event.target.value)}
          >
            {allowCollection && <option value="">Collection overview</option>}
            {weekends.map((item) => (
              <option value={item.friday} key={item.friday}>{item.label}</option>
            ))}
          </select>
        </div>
        {!compact && (
          <span className="field-help">
            {activeWeekend
              ? 'Friday after 19:00 → Monday by 07:00 · minimum 12h stay'
              : 'Choose a weekend to reveal observed round-trip prices.'}
          </span>
        )}
      </div>
      <fieldset className="mode-field">
        <legend>Transport</legend>
        <div className="segmented-control">
          {MODES.map((item) => (
            <button
              type="button"
              key={item.value}
              className={joinClassNames('segment-button', mode === item.value && 'is-active')}
              aria-pressed={mode === item.value}
              onClick={() => onModeChange(item.value)}
            >
              <span aria-hidden="true">{item.icon}</span>
              {item.label}
            </button>
          ))}
        </div>
      </fieldset>
    </section>
  );
};
