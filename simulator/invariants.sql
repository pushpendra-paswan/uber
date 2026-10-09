-- Database invariants that must always hold (I1 to I4 since M4.1, I5 to I7 since M4.3). Read-only. One SELECT per invariant, one row per offender:
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
