import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models import Rating, RatingSummary, RideStatus
from app.repositories import ratings as ratings_repo
from app.services import ratings as ratings_service
from app.utils.ratings import PUBLIC_MIN_RATINGS, average

RATING_KEYS = {"id", "ride_id", "score", "comment", "created_at"}
NOT_COMPLETED = "You can only rate completed trips"
ALREADY_RATED = "You have already rated this trip"
WINDOW_ENDED = "The rating period for this trip has ended"


# --- helpers ---


async def rate(client, who: dict, ride_id: int, score=5, comment=None):
    body = {"score": score}
    if comment is not None:
        body["comment"] = comment
    return await client.post(f"/rides/{ride_id}/rating", json=body, headers=who["headers"])


async def summary_of(db, user_id: int) -> tuple[int, int] | None:
    result = await db.execute(
        select(RatingSummary.rating_count, RatingSummary.rating_total).where(RatingSummary.user_id == user_id)
    )
    row = result.first()
    return (row.rating_count, row.rating_total) if row is not None else None


async def rating_rows(db, ride_id: int | None = None):
    query = select(Rating.id, Rating.ride_id, Rating.from_user_id, Rating.to_user_id, Rating.score, Rating.comment).order_by(Rating.id)
    if ride_id is not None:
        query = query.where(Rating.ride_id == ride_id)
    return (await db.execute(query)).all()


async def rate_many(client, make_user, completed_ride, driver: dict, scores: list[int]) -> list[dict]:
    """One new rider and one new completed ride with `driver` for each score; each rider rates the driver."""
    riders = []
    for score in scores:
        rider = await make_user("rider")
        ride = await completed_ride(rider, driver)
        assert (await rate(client, rider, ride.id, score)).status_code == 201
        riders.append({"rider": rider, "ride": ride})
    return riders


# --- A. creating ratings ---


async def test_a_rider_rates_the_driver(client, db, rider, driver, completed_ride):
    ride = await completed_ride(rider, driver)
    assert driver["user"].id != driver["driver"].id  # the rating must go to the user id, so the two must differ here

    response = await rate(client, rider, ride.id, 4)

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == RATING_KEYS
    assert (body["ride_id"], body["score"], body["comment"]) == (ride.id, 4, None)
    rows = await rating_rows(db)
    assert len(rows) == 1
    assert (rows[0].from_user_id, rows[0].to_user_id) == (rider["user"].id, driver["user"].id)
    assert rows[0].comment is None
    assert await summary_of(db, driver["user"].id) == (1, 4)


async def test_the_driver_rates_the_rider_on_the_same_ride(client, db, rider, driver, completed_ride):
    ride = await completed_ride(rider, driver)
    await rate(client, rider, ride.id, 5)

    response = await rate(client, driver, ride.id, 3, "Was late")

    assert response.status_code == 201, response.text
    assert set(response.json()) == RATING_KEYS  # no user ids
    rows = await rating_rows(db, ride.id)
    assert [(row.from_user_id, row.to_user_id, row.score) for row in rows] == [
        (rider["user"].id, driver["user"].id, 5),
        (driver["user"].id, rider["user"].id, 3),
    ]
    assert await summary_of(db, rider["user"].id) == (1, 3)
    assert await summary_of(db, driver["user"].id) == (1, 5)


@pytest.mark.parametrize("score", [0, 6, -1, "5", 4.5, True, None])
async def test_a_bad_score_is_refused_and_nothing_changes(client, db, rider, driver, completed_ride, score):
    ride = await completed_ride(rider, driver)

    response = await client.post(f"/rides/{ride.id}/rating", json={"score": score}, headers=rider["headers"])

    assert response.status_code == 422
    assert await rating_rows(db) == []
    assert await summary_of(db, driver["user"].id) is None


async def test_a_missing_score_and_a_too_long_comment_are_refused(client, db, rider, driver, completed_ride):
    ride = await completed_ride(rider, driver)

    missing = await client.post(f"/rides/{ride.id}/rating", json={}, headers=rider["headers"])
    too_long = await rate(client, rider, ride.id, 4, "a" * 301)
    not_text = await client.post(f"/rides/{ride.id}/rating", json={"score": 4, "comment": 5}, headers=rider["headers"])

    assert (missing.status_code, too_long.status_code, not_text.status_code) == (422, 422, 422)
    assert await rating_rows(db) == []
    assert await summary_of(db, driver["user"].id) is None


async def test_comments_are_stripped_and_limited_to_300_characters(client, db, rider, driver, completed_ride):
    blank, padded, longest = [await completed_ride(rider, driver) for _ in range(3)]

    assert (await rate(client, rider, blank.id, 3, "   \n ")).status_code == 201
    assert (await rate(client, rider, padded.id, 3, "  Nice ride  ")).json()["comment"] == "Nice ride"
    assert (await rate(client, rider, longest.id, 3, "a" * 300)).status_code == 201

    comments = {row.ride_id: row.comment for row in await rating_rows(db)}
    assert comments == {blank.id: None, padded.id: "Nice ride", longest.id: "a" * 300}


@pytest.mark.parametrize(
    "status", [RideStatus.REQUESTED, RideStatus.DRIVER_ASSIGNED, RideStatus.DRIVER_ARRIVED, RideStatus.IN_PROGRESS,
               RideStatus.CANCELLED, RideStatus.NO_DRIVER_FOUND],
)
async def test_only_completed_rides_can_be_rated(client, db, make_user, insert_ride, status):
    rider = await make_user("rider")
    driver = await make_user("driver")
    has_driver = status not in (RideStatus.REQUESTED, RideStatus.NO_DRIVER_FOUND)
    ride = await insert_ride(rider, status, driver if has_driver else None)

    answers = [await rate(client, rider, ride.id)]
    if has_driver:
        answers.append(await rate(client, driver, ride.id))

    for answer in answers:
        assert answer.status_code == 409, answer.text
        assert answer.json()["detail"] == NOT_COMPLETED
    assert await rating_rows(db) == []
    assert await summary_of(db, driver["user"].id) is None


async def test_a_cancelled_ride_with_a_fee_cannot_be_rated(client, db, rider, driver, settled_ride):
    cancelled = await settled_ride(rider, driver, amount=3000, kind="cancellation")

    answers = [await rate(client, rider, cancelled["ride_id"]), await rate(client, driver, cancelled["ride_id"])]

    assert [(answer.status_code, answer.json()["detail"]) for answer in answers] == [(409, NOT_COMPLETED)] * 2
    assert await rating_rows(db) == []


async def test_access_rules(client, make_user, rider, driver, admin, completed_ride):
    ride = await completed_ride(rider, driver)
    other_rider = await make_user("rider")
    other_driver = await make_user("driver")

    assert (await rate(client, other_rider, ride.id)).status_code == 404
    assert (await rate(client, other_driver, ride.id)).status_code == 404
    assert (await rate(client, rider, ride.id + 1000)).status_code == 404
    assert (await rate(client, admin, ride.id)).status_code == 403
    assert (await client.post(f"/rides/{ride.id}/rating", json={"score": 5})).status_code == 401


async def test_a_driver_who_is_now_rejected_can_still_rate(client, db, rider, driver, completed_ride):
    ride = await completed_ride(rider, driver)
    await db.execute(text("UPDATE drivers SET verification_status = 'rejected'"))
    await db.commit()

    assert (await rate(client, driver, ride.id, 4)).status_code == 201


async def test_a_second_rating_is_refused_and_the_first_stays(client, db, rider, driver, completed_ride):
    ride = await completed_ride(rider, driver)
    await rate(client, rider, ride.id, 2, "first")

    same = await rate(client, rider, ride.id, 2, "first")
    different = await rate(client, rider, ride.id, 5, "second")

    assert [(answer.status_code, answer.json()["detail"]) for answer in (same, different)] == [(409, ALREADY_RATED)] * 2
    rows = await rating_rows(db)
    assert [(row.score, row.comment) for row in rows] == [(2, "first")]
    assert await summary_of(db, driver["user"].id) == (1, 2)


async def test_five_identical_requests_at_once_give_one_rating(client, db, rider, driver, completed_ride):
    ride = await completed_ride(rider, driver)

    answers = await asyncio.gather(*[rate(client, rider, ride.id, 4) for _ in range(5)])

    assert sorted(answer.status_code for answer in answers) == [201, 409, 409, 409, 409]
    assert len(await rating_rows(db)) == 1
    assert await summary_of(db, driver["user"].id) == (1, 4)


async def test_the_rating_window(client, db, rider, driver, completed_ride, monkeypatch):
    now = datetime.now(timezone.utc)
    inside = await completed_ride(rider, driver, now - timedelta(days=6, hours=23))
    outside = await completed_ride(rider, driver, now - timedelta(days=7, hours=1))

    assert (await rate(client, rider, inside.id, 4)).status_code == 201
    late = await rate(client, rider, outside.id, 4)
    assert (late.status_code, late.json()["detail"]) == (409, WINDOW_ENDED)
    assert [row.ride_id for row in await rating_rows(db)] == [inside.id]
    assert await summary_of(db, driver["user"].id) == (1, 4)

    # The constant is read when the rating is made, not when the module is loaded.
    monkeypatch.setattr(ratings_service, "RATING_WINDOW_DAYS", 1)
    day_old = await completed_ride(rider, driver, now - timedelta(hours=25))
    assert (await rate(client, rider, day_old.id)).status_code == 409


# --- B. running averages ---


@pytest.mark.parametrize(
    "scores, expected",
    [([5], 5.0), ([5, 4], 4.5), ([5, 5, 4], 4.67), ([4, 4, 5], 4.33), ([1, 2], 1.5), ([3, 3, 3, 4], 3.25), ([1, 1, 2], 1.33),
     ([5, 5, 5, 5, 5, 4, 4, 4], 4.63)],  # 37 / 8 = 4.625: half up gives 4.63, Python's round() gives 4.62
)
async def test_the_average_after_the_last_score_is_worked_by_hand(client, make_user, driver, completed_ride, scores, expected):
    await rate_many(client, make_user, completed_ride, driver, scores)

    response = await client.get("/ratings/me", headers=driver["headers"])

    assert response.json() == {"count": len(scores), "average": expected}


@pytest.mark.parametrize(
    "count, total, expected",
    [(0, 0, None), (1, 5, 5.0), (2, 9, 4.5), (3, 14, 4.67), (3, 13, 4.33), (3, 4, 1.33), (4, 13, 3.25), (8, 37, 4.63), (7, 20, 2.86), (200, 999, 5.0), (200, 998, 4.99)],
)
def test_the_average_function(count, total, expected):
    assert average(count, total) == expected


def test_the_public_minimum_is_three():
    assert PUBLIC_MIN_RATINGS == 3


async def test_the_summary_equals_the_ratings_after_every_insert_and_ratings_by_a_user_do_not_change_it(
    client, db, make_user, driver, completed_ride
):
    for number, score in enumerate([5, 2, 4, 4, 1], start=1):
        rider = await make_user("rider")
        ride = await completed_ride(rider, driver)
        await rate(client, rider, ride.id, score)
        # The rider rated: the rider's own summary has no row; the driver's equals the sum over the ratings table.
        assert await summary_of(db, rider["user"].id) is None
        totals = (await db.execute(
            text("SELECT count(*), sum(score) FROM ratings WHERE to_user_id = :id"), {"id": driver["user"].id}
        )).one()
        assert await summary_of(db, driver["user"].id) == tuple(totals)
        assert totals[0] == number

    before = await summary_of(db, driver["user"].id)
    last_rider, last_ride = rider, ride
    await rate(client, driver, last_ride.id, 3)  # the driver rates a rider: the driver's own summary stays the same
    assert await summary_of(db, driver["user"].id) == before
    assert await summary_of(db, last_rider["user"].id) == (1, 3)


async def test_twenty_ratings_of_one_driver_at_once_lose_nothing(client, db, make_user, driver, completed_ride):
    scores = [(number % 5) + 1 for number in range(20)]
    riders = []
    for score in scores:
        rider = await make_user("rider")
        riders.append((rider, await completed_ride(rider, driver), score))

    answers = await asyncio.gather(*[rate(client, rider, ride.id, score) for rider, ride, score in riders for _ in range(2)])

    codes = [answer.status_code for answer in answers]
    assert codes.count(201) == 20 and codes.count(409) == 20, codes
    assert len(await rating_rows(db)) == 20
    assert await summary_of(db, driver["user"].id) == (20, sum(scores))


async def test_a_failing_summary_update_undoes_the_rating(client, db, rider, driver, completed_ride, monkeypatch):
    ride = await completed_ride(rider, driver)

    async def broken(db, user_id, score):
        raise RuntimeError("summary update failed")

    with monkeypatch.context() as patch:
        patch.setattr(ratings_repo, "add_to_summary", broken)
        with pytest.raises(RuntimeError):
            await rate(client, rider, ride.id, 4)
    assert await rating_rows(db) == []
    assert await summary_of(db, driver["user"].id) is None

    assert (await rate(client, rider, ride.id, 4)).status_code == 201
    assert await summary_of(db, driver["user"].id) == (1, 4)


async def violates(db, sql: str, constraint: str, **params):
    with pytest.raises(IntegrityError) as error:
        async with db.begin_nested():
            await db.execute(text(sql), params)
    assert constraint in str(error.value)


INSERT_RATING = "INSERT INTO ratings (ride_id, from_user_id, to_user_id, score, comment) VALUES (:ride, :from_id, :to_id, :score, :comment)"
INSERT_SUMMARY = "INSERT INTO rating_summaries (user_id, rating_count, rating_total) VALUES (:user, :count, :total)"


async def test_the_constraints_of_ratings_and_summaries(db, rider, driver, completed_ride):
    ride = await completed_ride(rider, driver)
    rider_id, driver_id = rider["user"].id, driver["user"].id
    good = {"ride": ride.id, "from_id": rider_id, "to_id": driver_id, "score": 3, "comment": None}

    await violates(db, INSERT_RATING, "ck_ratings_no_self_rating", **{**good, "to_id": rider_id})
    await violates(db, INSERT_RATING, "ck_ratings_score_range", **{**good, "score": 0})
    await violates(db, INSERT_RATING, "ck_ratings_score_range", **{**good, "score": 6})
    await violates(db, INSERT_RATING, "ck_ratings_comment_length", **{**good, "comment": "a" * 301})
    await db.execute(text(INSERT_RATING), {**good, "comment": "a" * 300})
    await violates(db, INSERT_RATING, "uq_ratings_ride_id_from_user_id", **{**good, "score": 5})

    await violates(db, INSERT_SUMMARY, "ck_rating_summaries_total_within_range", user=rider_id, count=2, total=1)
    await violates(db, INSERT_SUMMARY, "ck_rating_summaries_total_within_range", user=rider_id, count=2, total=11)
    await violates(db, INSERT_SUMMARY, "ck_rating_summaries_count_not_negative", user=rider_id, count=-1, total=0)

    # The edges that are allowed: a score of 1 and of 5 by the other person, an empty summary, all ones, all fives.
    await db.execute(text(INSERT_RATING), {**good, "from_id": driver_id, "to_id": rider_id, "score": 1})
    for user, count, total in [(rider_id, 0, 0), (driver_id, 2, 10)]:
        await db.execute(text(INSERT_SUMMARY), {"user": user, "count": count, "total": total})
    await db.execute(text("UPDATE rating_summaries SET rating_total = 2 WHERE user_id = :id"), {"id": driver_id})
    await db.rollback()


# --- C. views and privacy ---


async def test_my_summary(client, make_user, rider, driver, admin, completed_ride):
    assert (await client.get("/ratings/me", headers=rider["headers"])).json() == {"count": 0, "average": None}
    assert (await client.get("/ratings/me", headers=driver["headers"])).json() == {"count": 0, "average": None}

    first = await completed_ride(rider, driver)
    await rate(client, rider, first.id, 5)
    assert (await client.get("/ratings/me", headers=driver["headers"])).json() == {"count": 1, "average": 5.0}  # the real value
    second_rider = await make_user("rider")
    await rate(client, second_rider, (await completed_ride(second_rider, driver)).id, 4)
    assert (await client.get("/ratings/me", headers=driver["headers"])).json() == {"count": 2, "average": 4.5}

    await rate(client, driver, first.id, 2)
    assert (await client.get("/ratings/me", headers=rider["headers"])).json() == {"count": 1, "average": 2.0}

    assert (await client.get("/ratings/me", headers=admin["headers"])).status_code == 403
    assert (await client.get("/ratings/me")).status_code == 401


async def test_the_driver_details_hide_the_average_below_three_ratings(client, make_user, rider, driver, completed_ride):
    first = await completed_ride(rider, driver)
    details = lambda: client.get(f"/rides/{first.id}/driver", headers=rider["headers"])  # noqa: E731

    assert (await details()).json()["rating"] == {"count": 0, "average": None}
    await rate(client, rider, first.id, 5)
    assert (await details()).json()["rating"] == {"count": 1, "average": None}
    other = await make_user("rider")
    await rate(client, other, (await completed_ride(other, driver)).id, 4)
    assert (await details()).json()["rating"] == {"count": 2, "average": None}
    third = await make_user("rider")
    await rate(client, third, (await completed_ride(third, driver)).id, 4)
    assert (await details()).json()["rating"] == {"count": 3, "average": 4.33}


async def test_the_rating_status(client, make_user, rider, driver, admin, completed_ride):
    now = datetime.now(timezone.utc)
    ride = await completed_ride(rider, driver, now - timedelta(days=1))
    path = f"/rides/{ride.id}/rating"

    before = (await client.get(path, headers=rider["headers"])).json()
    assert set(before) == {"can_rate", "reason", "expires_at", "mine"}
    assert (before["can_rate"], before["reason"], before["mine"]) == (True, None, None)
    expected = ride.completed_at + timedelta(days=7)
    assert abs(datetime.fromisoformat(before["expires_at"]) - expected) < timedelta(seconds=1)

    await rate(client, rider, ride.id, 4, "good")
    after = (await client.get(path, headers=rider["headers"])).json()
    assert (after["can_rate"], after["reason"]) == (False, "already_rated")
    assert (after["mine"]["score"], after["mine"]["comment"]) == (4, "good")
    # The driver has not rated: the rider's rating is not theirs to see.
    theirs = (await client.get(path, headers=driver["headers"])).json()
    assert (theirs["can_rate"], theirs["mine"]) == (True, None)

    old = await completed_ride(rider, driver, now - timedelta(days=8))
    closed = (await client.get(f"/rides/{old.id}/rating", headers=rider["headers"])).json()
    assert (closed["can_rate"], closed["reason"], closed["mine"]) == (False, "window_closed", None)

    stranger = await make_user("rider")
    assert (await client.get(path, headers=stranger["headers"])).status_code == 404
    assert (await client.get(path, headers=admin["headers"])).status_code == 403
    assert (await client.get(f"/rides/{ride.id + 1000}/rating", headers=rider["headers"])).status_code == 404
    assert (await client.get(path)).status_code == 401


async def test_the_rating_status_of_unfinished_rides(client, make_user, insert_ride):
    for status in (RideStatus.DRIVER_ASSIGNED, RideStatus.IN_PROGRESS, RideStatus.CANCELLED, RideStatus.NO_DRIVER_FOUND):
        rider = await make_user("rider")
        driver = await make_user("driver")
        has_driver = status != RideStatus.NO_DRIVER_FOUND
        ride = await insert_ride(rider, status, driver if has_driver else None)

        body = (await client.get(f"/rides/{ride.id}/rating", headers=rider["headers"])).json()

        assert body == {"can_rate": False, "reason": "not_completed", "expires_at": None, "mine": None}


async def test_comments_and_the_other_sides_score_never_reach_the_rated_person(client, rider, driver, settled_ride):
    paid = await settled_ride(rider, driver, amount=9000, method="cash")
    ride_id = paid["ride_id"]
    await rate(client, rider, ride_id, 2, "RIDER-SECRET-COMMENT-8f3a")
    await rate(client, driver, ride_id, 1, "DRIVER-SECRET-COMMENT-77c1")

    # What the driver (rated by the rider) can read, and what the rider (rated by the driver) can read.
    for who, other_comment, calls in [
        (driver, "RIDER-SECRET-COMMENT-8f3a", [
            f"/rides/{ride_id}", f"/rides/{ride_id}/driver", "/ratings/me", "/drivers/me/earnings", "/drivers/me/earnings/entries",
            f"/rides/{ride_id}/rating",
        ]),
        (rider, "DRIVER-SECRET-COMMENT-77c1", [
            f"/rides/{ride_id}", f"/rides/{ride_id}/driver", "/ratings/me", f"/rides/{ride_id}/receipt", "/wallet",
            "/wallet/entries", f"/rides/{ride_id}/rating", f"/rides/{ride_id}/events",
        ]),
    ]:
        for path in calls:
            response = await client.get(path, headers=who["headers"])
            assert response.status_code == 200, f"{path}: {response.text}"
            assert other_comment not in response.text, path
            if not path.endswith("/rating"):
                assert '"score"' not in response.text and '"comment"' not in response.text, path
    offer = await client.get("/drivers/me/offer", headers=driver["headers"])
    assert "RIDER-SECRET" not in offer.text

    # The rater sees only their own rating, and "/ratings/me" holds nothing but the count and the average.
    mine = (await client.get(f"/rides/{ride_id}/rating", headers=driver["headers"])).json()["mine"]
    assert (mine["score"], mine["comment"]) == (1, "DRIVER-SECRET-COMMENT-77c1")
    assert set((await client.get("/ratings/me", headers=driver["headers"])).json()) == {"count", "average"}


async def test_the_admin_list(client, db, make_user, rider, driver, admin, completed_ride):
    second_rider = await make_user("rider")
    rides = [await completed_ride(rider, driver), await completed_ride(second_rider, driver), await completed_ride(rider, driver)]
    await rate(client, rider, rides[0].id, 5, "great")
    await rate(client, second_rider, rides[1].id, 2, "slow")
    await rate(client, rider, rides[2].id, 1, "rude")
    await rate(client, driver, rides[0].id, 4, "polite")
    ids = [row.id for row in await rating_rows(db)]

    everything = (await client.get("/admin/ratings", headers=admin["headers"])).json()
    assert [row["id"] for row in everything] == sorted(ids, reverse=True)
    assert set(everything[0]) == {"id", "ride_id", "from_user_id", "to_user_id", "score", "comment", "created_at"}
    assert everything[0]["comment"] == "polite"
    assert (everything[0]["from_user_id"], everything[0]["to_user_id"]) == (driver["user"].id, rider["user"].id)

    about_driver = (await client.get("/admin/ratings", params={"user_id": driver["user"].id}, headers=admin["headers"])).json()
    assert [row["comment"] for row in about_driver] == ["rude", "slow", "great"]
    low = (await client.get("/admin/ratings", params={"user_id": driver["user"].id, "max_score": 2}, headers=admin["headers"])).json()
    assert [row["score"] for row in low] == [1, 2]
    assert [row["comment"] for row in (await client.get("/admin/ratings", params={"max_score": 1}, headers=admin["headers"])).json()] == ["rude"]

    seen = []
    before_id = None
    while True:
        params = {"limit": 3, **({"before_id": before_id} if before_id else {})}
        page = (await client.get("/admin/ratings", params=params, headers=admin["headers"])).json()
        if not page:
            break
        seen += [row["id"] for row in page]
        before_id = page[-1]["id"]
    assert seen == sorted(ids, reverse=True)

    for params in ({"limit": 0}, {"limit": 101}, {"max_score": 0}, {"max_score": 6}):
        assert (await client.get("/admin/ratings", params=params, headers=admin["headers"])).status_code == 422, params
    assert (await client.get("/admin/ratings", params={"limit": 100}, headers=admin["headers"])).status_code == 200
    for who in (rider, driver):
        assert (await client.get("/admin/ratings", headers=who["headers"])).status_code == 403
    assert (await client.get("/admin/ratings")).status_code == 401
