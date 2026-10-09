-- Database invariants that must always hold (I1 to I4 since M4.1, I5 to I7 since M4.3, I8 to I11 since M5.1, I12 since M5.2). Read-only. One SELECT per invariant, one row per offender:
-- an empty result means the invariant holds. Used by simulator/stress.py, and runnable by hand:
--   docker compose exec -T db psql -U uber -d uber -At -F '|' < simulator/invariants.sql
-- Statuses are stored as the enum names in upper case (VARCHAR, no CHECK constraint).

\echo '== driver_pending_offers'
-- I1: no driver has more than one live offer (PENDING and not past its deadline).
SELECT driver_id, count(*), string_agg(id::text, ',' ORDER BY id) AS offer_ids, string_agg(ride_id::text, ',' ORDER BY id) AS ride_ids
FROM ride_offers
WHERE status = 'PENDING' AND expires_at > now()
GROUP BY driver_id
HAVING count(*) > 1
ORDER BY driver_id;

\echo '== driver_active_rides'
-- I2: no driver has more than one ride in DRIVER_ASSIGNED, DRIVER_ARRIVED, or IN_PROGRESS (a double assignment).
SELECT driver_id, count(*), string_agg(id::text, ',' ORDER BY id) AS ride_ids, string_agg(status, ',' ORDER BY id) AS statuses
FROM rides
WHERE status IN ('DRIVER_ASSIGNED', 'DRIVER_ARRIVED', 'IN_PROGRESS')
GROUP BY driver_id
HAVING count(*) > 1
ORDER BY driver_id;

\echo '== rider_active_rides'
-- I3: no rider has more than one ride in REQUESTED, DRIVER_ASSIGNED, DRIVER_ARRIVED, or IN_PROGRESS.
SELECT rider_id, count(*), string_agg(id::text, ',' ORDER BY id) AS ride_ids, string_agg(status, ',' ORDER BY id) AS statuses
FROM rides
WHERE status IN ('REQUESTED', 'DRIVER_ASSIGNED', 'DRIVER_ARRIVED', 'IN_PROGRESS')
GROUP BY rider_id
HAVING count(*) > 1
ORDER BY rider_id;

\echo '== stuck_requested'
-- I4: no REQUESTED ride without a PENDING offer (a ride that nobody is deciding on and nothing will move on).
SELECT r.id AS ride_id, r.created_at
FROM rides r
WHERE r.status = 'REQUESTED'
  AND NOT EXISTS (SELECT 1 FROM ride_offers o WHERE o.ride_id = r.id AND o.status = 'PENDING')
ORDER BY r.id;

\echo '== overdue_pending_offers'
-- I5: no PENDING offer more than 10 seconds past its deadline (the sweeper is not keeping up, or is dead).
SELECT id AS offer_id, ride_id, driver_id, expires_at
FROM ride_offers
WHERE status = 'PENDING' AND expires_at < now() - interval '10 seconds'
ORDER BY id;

\echo '== orphan_pending_offers'
-- I6: no PENDING offer on a ride that is not REQUESTED (an offer must be closed in the same transaction that moves the ride on).
SELECT o.id AS offer_id, o.ride_id, o.driver_id, r.status AS ride_status
FROM ride_offers o
JOIN rides r ON r.id = o.ride_id
WHERE o.status = 'PENDING' AND r.status <> 'REQUESTED'
ORDER BY o.id;

\echo '== assigned_without_accepted_offer'
-- I7: every ride in DRIVER_ASSIGNED, DRIVER_ARRIVED, or IN_PROGRESS has an ACCEPTED offer for the ride's own driver
-- (accepting the offer and assigning the ride are one transaction).
SELECT r.id AS ride_id, r.driver_id, r.status
FROM rides r
WHERE r.status IN ('DRIVER_ASSIGNED', 'DRIVER_ARRIVED', 'IN_PROGRESS')
  AND NOT EXISTS (
    SELECT 1 FROM ride_offers o WHERE o.ride_id = r.id AND o.driver_id = r.driver_id AND o.status = 'ACCEPTED'
  )
ORDER BY r.id;

\echo '== completed_without_fare'
-- I8: every COMPLETED ride has its fare, distance, duration, and a trip breakdown (settlement is in the same transaction as
-- the status change). Rides completed before M5.1 carry the marker {"kind": "legacy"} and are left out.
SELECT id AS ride_id, final_fare, actual_distance_m, actual_duration_s, fare_breakdown->>'kind' AS kind
FROM rides
WHERE status = 'COMPLETED'
  AND (fare_breakdown->>'kind') IS DISTINCT FROM 'legacy'
  AND (final_fare IS NULL OR actual_distance_m IS NULL OR actual_duration_s IS NULL OR fare_breakdown IS NULL
       OR (fare_breakdown->>'kind') IS DISTINCT FROM 'trip' OR final_fare < 0)
ORDER BY id;

\echo '== cancelled_without_settlement'
-- I9: every CANCELLED ride has a cancellation fee (which may be 0) that equals the fee in its breakdown. Legacy rides are left out.
SELECT id AS ride_id, final_fare, fare_breakdown->>'kind' AS kind, fare_breakdown->>'fee' AS fee
FROM rides
WHERE status = 'CANCELLED'
  AND (fare_breakdown->>'kind') IS DISTINCT FROM 'legacy'
  AND (final_fare IS NULL OR final_fare < 0 OR (fare_breakdown->>'kind') IS DISTINCT FROM 'cancellation'
       OR final_fare IS DISTINCT FROM (fare_breakdown->>'fee')::int)
ORDER BY id;

\echo '== fare_on_unsettled_ride'
-- I10: a ride that is neither COMPLETED nor CANCELLED (active, or NO_DRIVER_FOUND) has no fare, no billed distance or
-- duration, and no breakdown.
SELECT id AS ride_id, status, final_fare, fare_breakdown->>'kind' AS kind
FROM rides
WHERE status NOT IN ('COMPLETED', 'CANCELLED')
  AND (final_fare IS NOT NULL OR actual_distance_m IS NOT NULL OR actual_duration_s IS NOT NULL OR fare_breakdown IS NOT NULL)
ORDER BY id;

\echo '== fare_over_cap'
-- I11: a trip fare is never above 150 percent of the estimate (integer division, like the code).
SELECT id AS ride_id, final_fare, fare_estimate
FROM rides
WHERE status = 'COMPLETED'
  AND (fare_breakdown->>'kind') = 'trip'
  AND final_fare > fare_estimate * 150 / 100
ORDER BY id;

\echo '== surge_settlement_mismatch'
-- I12: a settled trip used the multiplier locked on the ride, and its surge arithmetic adds up. Only trips whose breakdown
-- HAS a surge_percent key are checked (rides settled before M5.2 have none). Settlement is in the ride's own transaction,
-- so this can never be violated even for an instant. The 100 to 200 range is enforced by a check constraint instead.
SELECT id AS ride_id, surge_percent AS ride_surge, (fare_breakdown->>'surge_percent')::int AS breakdown_surge,
       (fare_breakdown->>'normal_fare')::int AS normal_fare, (fare_breakdown->>'surge_amount')::int AS surge_amount,
       (fare_breakdown->>'computed_fare')::int AS computed_fare
FROM rides
WHERE status = 'COMPLETED'
  AND (fare_breakdown->>'kind') = 'trip'
  AND fare_breakdown ? 'surge_percent'
  AND ((fare_breakdown->>'surge_percent')::int <> surge_percent
       OR (fare_breakdown->>'normal_fare')::int + (fare_breakdown->>'surge_amount')::int <> (fare_breakdown->>'computed_fare')::int
       OR ((fare_breakdown->>'normal_fare')::int * surge_percent + 50) / 100 <> (fare_breakdown->>'computed_fare')::int)
ORDER BY id;
