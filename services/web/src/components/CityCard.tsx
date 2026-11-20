import { Link } from 'react-router-dom';
import type { City, MapFare, Mode } from '../types';
import { availabilityLabel, categoryLabel, formatCurrency, joinClassNames, modeLabel, priceBand } from '../utils';

interface CityCardProps {
  city: City;
  fare?: MapFare;
  weekend: string;
  mode: Mode;
  categoryLabels?: Record<string, string>;
}

export const CityCard = ({ city, fare, weekend, mode, categoryLabels = {} }: CityCardProps) => {
  const query = new URLSearchParams();
  if (weekend) query.set('weekend', weekend);
  if (mode !== 'all') query.set('mode', mode);

  return (
    <article className="city-card">
      <div className="city-card-topline">
        <div>
          <p className="eyebrow">{city.country.name}</p>
          <h3><Link to={`/cities/${city.slug}${query.size ? `?${query}` : ''}`}>{city.name}</Link></h3>
        </div>
        <div className={joinClassNames('city-card-price', fare && priceBand(fare.priceCents))}>
          {weekend
            ? <><strong>{formatCurrency(fare?.priceCents)}</strong><span>{fare?.winningMode ? modeLabel(fare.winningMode) : availabilityLabel[fare?.availability ?? 'unknown_due_failure']}</span></>
            : <><strong>{city.status.visited ? 'Visited' : 'Wishlist'}</strong><span>{city.status.visited ? 'Saved memory' : 'Still to discover'}</span></>}
        </div>
      </div>
      <p>{city.summary}</p>
      <div className="chip-row" aria-label={`${city.name} tags`}>
        {city.status.visited && <span className="chip chip-visited">✓ Visited</span>}
        {city.status.wishlist && <span className="chip chip-wishlist">☆ Wishlist</span>}
        {city.categories.slice(0, 3).map((category) => <span className="chip" key={category}>{categoryLabels[category] ?? categoryLabel(category)}</span>)}
      </div>
      {fare?.originTerminal && fare.destinationTerminal && (
        <div className="terminal-line">
          <span aria-hidden="true">⌁</span> {fare.originTerminal} <span aria-hidden="true">→</span> {fare.destinationTerminal}
        </div>
      )}
    </article>
  );
};
