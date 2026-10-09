-- Database invariants that must always hold (M4.1). Read-only. One SELECT per invariant, one row per offender:
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
