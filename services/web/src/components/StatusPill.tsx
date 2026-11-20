import type { Availability, Coverage, Freshness } from '../types';
import { availabilityLabel, coverageLabel, freshnessLabel, joinClassNames } from '../utils';

type Tone = 'success' | 'warning' | 'danger' | 'neutral' | 'blue';

export const StatusPill = ({ label, tone = 'neutral' }: { label: string; tone?: Tone }) => (
  <span className={joinClassNames('status-pill', `tone-${tone}`)}>{label}</span>
);

export const AvailabilityPill = ({ value }: { value: Availability }) => (
  <StatusPill
    label={availabilityLabel[value]}
    tone={value === 'available' ? 'success' : value === 'not_on_sale' ? 'neutral' : value === 'unknown_due_failure' ? 'danger' : 'warning'}
  />
);

export const CoveragePill = ({ value }: { value: Coverage }) => (
  <StatusPill
    label={coverageLabel[value]}
    tone={value === 'complete' ? 'success' : value === 'partial' ? 'warning' : value === 'failed' ? 'danger' : 'neutral'}
  />
);

export const FreshnessPill = ({ value }: { value: Freshness }) => (
  <StatusPill label={freshnessLabel[value]} tone={value === 'fresh' ? 'blue' : value === 'never_observed' ? 'neutral' : 'warning'} />
);
