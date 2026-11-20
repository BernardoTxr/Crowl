# crowl-core

`crowl-core` owns Crowl's persistence model and canonical trip rules. Source
adapters should stay outside this package.

## Composing independently sold one-way offers

Normalize each source response to the shared `Segment` and `Itinerary` types,
then wrap independently bookable directional prices as `OneWayCandidate`:

```python
from crowl_core.composition import OneWayCandidate, compose_round_trips
from crowl_core.enums import TransportMode

result = compose_round_trips(
    outbound_offers,
    return_offers,
    destination_city_id="vienna-at",
    validation_context=context,
)

cheapest_any = result.winner()
cheapest_train = result.winner(TransportMode.TRAIN)
```

The returned `ComposedRoundTrip` retains both source offers for snapshot booking
rows. The composer deduplicates schedule identities, permits different sources,
modes, and Paris terminals, and calls `validate_trip` for every published
candidate. `CompositionLimits` bounds directional candidates, pair evaluations,
output size, and cross-source observation skew; inspect `result.truncated` before
treating the returned set as exhaustive.
