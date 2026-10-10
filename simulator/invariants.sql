-- Database invariants that must always hold (I1 to I4 since M4.1, I5 to I7 since M4.3, I8 to I11 since M5.1, I12 since M5.2,
-- I13 to I18 since M5.3, I19 and I20 since M5.4, I21 and I22 since M6.1). Read-only. One SELECT per invariant, one row per offender:
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

\echo '== wallet_balance_mismatch'
-- I13: a wallet's balance equals the sum of its entries and the balance_after of its latest entry, and every entry's user has
-- a wallet row. Every ledger write is in the same transaction as its cause, under the wallet lock, so this can never be
-- violated even for an instant.
SELECT w.user_id, w.balance, COALESCE(s.total, 0) AS entries_sum, l.balance_after AS last_balance_after
FROM wallets w
LEFT JOIN (SELECT user_id, sum(amount) AS total FROM wallet_entries GROUP BY user_id) s ON s.user_id = w.user_id
LEFT JOIN (SELECT DISTINCT ON (user_id) user_id, balance_after FROM wallet_entries ORDER BY user_id, id DESC) l ON l.user_id = w.user_id
WHERE w.balance <> COALESCE(s.total, 0) OR w.balance <> COALESCE(l.balance_after, 0)
UNION ALL
SELECT e.user_id, NULL, sum(e.amount), NULL
FROM wallet_entries e
LEFT JOIN wallets w ON w.user_id = e.user_id
WHERE w.user_id IS NULL
GROUP BY e.user_id
ORDER BY user_id;

\echo '== ledger_running_balance_mismatch'
-- I14: every entry's balance_after equals the running sum of that user's amounts in id order (entry ids follow the wallet
-- lock order, because the insert happens after the lock).
SELECT entry_id, user_id, balance_after, running_sum
FROM (
  SELECT id AS entry_id, user_id, balance_after, sum(amount) OVER (PARTITION BY user_id ORDER BY id) AS running_sum
  FROM wallet_entries
) running
WHERE balance_after <> running_sum
ORDER BY entry_id;

\echo '== negative_wallet'
-- I15: no wallet is below zero (a wallet ride needs the cap in the wallet, and an adjustment never overdraws; the one
-- legitimate way is a cancellation fee rule raised above a rider's balance, which a stress run does not do).
SELECT user_id, balance
FROM wallets
WHERE balance < 0
ORDER BY user_id;

\echo '== ride_payment_mismatch'
-- I16: a COMPLETED or CANCELLED ride (not legacy) with a fare has exactly one SUCCEEDED payment of that amount by its own
-- method; one without a fare (NULL or 0) has none; a ride that is not settled has none.
SELECT r.id AS ride_id, r.final_fare, count(p.id) AS payment_count, sum(p.amount) AS payment_amount,
       min(p.method) AS payment_method, r.payment_method AS ride_method
FROM rides r
LEFT JOIN payments p ON p.ride_id = r.id
WHERE r.status IN ('COMPLETED', 'CANCELLED') AND (r.fare_breakdown->>'kind') IS DISTINCT FROM 'legacy'
GROUP BY r.id
HAVING (r.final_fare > 0 AND (count(p.id) <> 1 OR bool_or(p.amount <> r.final_fare) OR bool_or(p.method <> r.payment_method)
                              OR bool_or(p.status <> 'succeeded')))
    OR (COALESCE(r.final_fare, 0) = 0 AND count(p.id) > 0)
UNION ALL
SELECT r.id, r.final_fare, 1, p.amount, p.method, r.payment_method
FROM payments p
JOIN rides r ON r.id = p.ride_id
WHERE r.status NOT IN ('COMPLETED', 'CANCELLED')
ORDER BY ride_id;

\echo '== wallet_charge_mismatch'
-- I17: a wallet payment has exactly one RIDE_CHARGE entry for its ride, of minus its amount, on the rider's wallet; a
-- RIDE_CHARGE entry has a wallet payment for its ride (never a cash one).
SELECT p.ride_id, p.amount AS payment_amount, count(e.id) AS entry_count, COALESCE(sum(e.amount), 0) AS entry_amount
FROM payments p
JOIN rides r ON r.id = p.ride_id
LEFT JOIN wallet_entries e ON e.ride_id = p.ride_id AND e.kind = 'RIDE_CHARGE'
WHERE p.method = 'wallet'
GROUP BY p.ride_id, p.amount, r.rider_id
HAVING count(e.id) <> 1 OR COALESCE(sum(e.amount), 0) <> -p.amount OR bool_or(e.user_id <> r.rider_id)
UNION ALL
SELECT e.ride_id, p.amount, 1, e.amount
FROM wallet_entries e
LEFT JOIN payments p ON p.ride_id = e.ride_id
WHERE e.kind = 'RIDE_CHARGE' AND (p.id IS NULL OR p.method <> 'wallet')
ORDER BY ride_id;

\echo '== topup_credit_mismatch'
-- I18: a SUCCEEDED top-up has exactly one TOPUP entry, of its amount, on its user's wallet; a top-up that is not SUCCEEDED
-- has no entry at all.
SELECT t.id AS topup_id, t.status, t.amount, count(e.id) AS entry_count, COALESCE(sum(e.amount), 0) AS entry_amount
FROM wallet_topups t
LEFT JOIN wallet_entries e ON e.topup_id = t.id
GROUP BY t.id
HAVING (t.status = 'SUCCEEDED' AND (count(e.id) <> 1 OR sum(e.amount) <> t.amount OR bool_or(e.user_id <> t.user_id OR e.kind <> 'TOPUP')))
    OR (t.status <> 'SUCCEEDED' AND count(e.id) > 0)
ORDER BY t.id;

\echo '== earning_payment_mismatch'
-- I19: every payment has its earning row (the two are written in one transaction), and the row agrees with its payment and
-- its ride: same ride, gross_amount equal to the payment amount, the ride's own driver, and the kind of the ride's breakdown.
SELECT p.ride_id, p.id AS payment_id, p.amount AS payment_amount, e.id AS earning_id, e.gross_amount,
       r.driver_id AS ride_driver_id, e.driver_id AS earning_driver_id, e.kind, r.fare_breakdown->>'kind' AS breakdown_kind
FROM payments p
JOIN rides r ON r.id = p.ride_id
LEFT JOIN ride_earnings e ON e.payment_id = p.id
WHERE e.id IS NULL
   OR e.ride_id <> p.ride_id
   OR e.gross_amount <> p.amount
   OR r.driver_id IS NULL
   OR e.driver_id <> r.driver_id
   OR e.kind IS DISTINCT FROM (r.fare_breakdown->>'kind')
ORDER BY p.ride_id;

\echo '== earning_math_mismatch'
-- I20: the split adds up and follows the rule: fee + earning = gross, the fee is the percent of the gross rounded half up,
-- nothing is negative, the gross is positive, and the percent is between 0 and 100.
SELECT id AS earning_id, ride_id, gross_amount, commission_percent, platform_fee, driver_earning
FROM ride_earnings
WHERE platform_fee + driver_earning <> gross_amount
   OR platform_fee <> (gross_amount * commission_percent + 50) / 100
   OR platform_fee < 0 OR driver_earning < 0 OR gross_amount <= 0
   OR commission_percent NOT BETWEEN 0 AND 100
ORDER BY id;

\echo '== rating_summary_mismatch'
-- I21: every user's rating_summaries row equals the count and the sum of the ratings made about them (a missing row counts
-- as 0). The summary changes in the same transaction as the rating, so this cannot be violated even for an instant.
SELECT COALESCE(s.user_id, r.to_user_id) AS user_id, COALESCE(s.rating_count, 0) AS summary_count,
       COALESCE(s.rating_total, 0) AS summary_total, COALESCE(r.n, 0) AS ratings_count, COALESCE(r.total, 0) AS ratings_sum
FROM rating_summaries s
FULL JOIN (SELECT to_user_id, count(*) AS n, sum(score) AS total FROM ratings GROUP BY to_user_id) r ON r.to_user_id = s.user_id
WHERE COALESCE(s.rating_count, 0) <> COALESCE(r.n, 0) OR COALESCE(s.rating_total, 0) <> COALESCE(r.total, 0)
ORDER BY 1;

\echo '== rating_participants_mismatch'
-- I22: a rating is about a COMPLETED ride and goes between its two people, in either direction: the rider rates the driver's
-- USER id, the driver's user rates the rider. Nobody rates themselves. (IS NOT TRUE also catches a ride with no driver.)
SELECT g.id AS rating_id, g.ride_id, r.status AS ride_status, g.from_user_id, g.to_user_id, r.rider_id, d.user_id AS driver_user_id
FROM ratings g
JOIN rides r ON r.id = g.ride_id
LEFT JOIN drivers d ON d.id = r.driver_id
WHERE r.status <> 'COMPLETED'
   OR g.from_user_id = g.to_user_id
   OR ((g.from_user_id = r.rider_id AND g.to_user_id = d.user_id) OR (g.from_user_id = d.user_id AND g.to_user_id = r.rider_id)) IS NOT TRUE
ORDER BY g.id;
