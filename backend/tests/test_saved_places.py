import asyncio

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.models import Ride, RideStatus, SavedPlace
from app.repositories import saved_places as saved_places_repo
from conftest import widen

PLACE_KEYS = {"id", "label", "address", "lat", "lng", "created_at"}
LIMIT_MESSAGE = "You can save up to 10 places. Delete one first."
NAME_TAKEN = "You already have a place with that name."
OUTSIDE = "Location is outside the service area"


# --- helpers ---


def place_body(label: str = "Home", **changes) -> dict:
    body = {"label": label, "address": "12 MG Road", "lat": settings.city_center_lat, "lng": settings.city_center_lng}
    return {**body, **changes}


async def create(client, who: dict, label: str = "Home", **changes):
    return await client.post("/saved-places", json=place_body(label, **changes), headers=who["headers"])


async def rows_of(db, user_id: int | None = None):
    query = select(SavedPlace.id, SavedPlace.user_id, SavedPlace.label, SavedPlace.address).order_by(SavedPlace.id)
    if user_id is not None:
        query = query.where(SavedPlace.user_id == user_id)
    return (await db.execute(query)).all()


async def fill(client, who: dict, number: int) -> list[int]:
    """`number` places with distinct labels, one after the other. Returns their ids."""
    ids = []
    for index in range(number):
        response = await create(client, who, f"Place {index}")
        assert response.status_code == 201, response.text
        ids.append(response.json()["id"])
    return ids


# --- 1. create and list ---


async def test_create_returns_exactly_the_documented_fields(client, db, rider):
    response = await create(client, rider, "Home", address="12 MG Road", lat=12.9716, lng=77.5946)

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == PLACE_KEYS
    assert (body["label"], body["address"], body["lat"], body["lng"]) == ("Home", "12 MG Road", 12.9716, 77.5946)
    assert [(row.id, row.user_id, row.label) for row in await rows_of(db)] == [(body["id"], rider["user"].id, "Home")]


async def test_the_list_shows_only_the_callers_places_in_id_order(client, rider, make_user):
    other = await make_user("rider")
    ids = [(await create(client, rider, label)).json()["id"] for label in ("Work", "Gym", "Home")]
    await create(client, other, "Mine")

    listed = await client.get("/saved-places", headers=rider["headers"])
    nothing = await client.get("/saved-places", headers=(await make_user("rider"))["headers"])

    assert listed.status_code == 200
    assert [place["id"] for place in listed.json()] == ids  # id order, not label order
    assert [place["label"] for place in listed.json()] == ["Work", "Gym", "Home"]
    assert all(set(place) == PLACE_KEYS for place in listed.json())
    assert nothing.json() == []


# --- 2. validation ---


@pytest.mark.parametrize(
    "changes",
    [
        {"label": ""}, {"label": "   "}, {"label": "a" * 31}, {"label": "bad\nlabel"}, {"label": "bad\tlabel"}, {"label": "bad\x7flabel"},
        {"address": ""}, {"address": "   "}, {"address": "a" * 201}, {"address": "two\nlines"}, {"address": "bell\x07"},
        {"lat": 91}, {"lat": -91}, {"lng": 181}, {"lng": -181},
        {"lat": "12.9"}, {"lat": True}, {"lng": "77.5"}, {"lng": False}, {"lat": None}, {"label": 5}, {"address": ["x"]},
        {"id": 7}, {"user_id": 1}, {"created_at": "2026-01-01T00:00:00Z"},
    ],
    ids=str,
)
async def test_a_bad_body_is_refused_and_nothing_is_stored(client, db, rider, changes):
    response = await client.post("/saved-places", json=place_body(**changes), headers=rider["headers"])

    assert response.status_code == 422, response.text
    assert await rows_of(db) == []


@pytest.mark.parametrize("missing", ["label", "address", "lat", "lng"])
async def test_a_missing_field_is_refused(client, db, rider, missing):
    body = place_body()
    del body[missing]

    response = await client.post("/saved-places", json=body, headers=rider["headers"])

    assert response.status_code == 422
    assert await rows_of(db) == []


async def test_the_longest_label_and_address_are_accepted_and_text_is_stripped(client, rider):
    longest = await create(client, rider, "a" * 30, address="b" * 200)
    padded = await create(client, rider, "  Padded  ", address="  Near the park  ")

    assert longest.status_code == 201
    assert (longest.json()["label"], len(longest.json()["address"])) == ("a" * 30, 200)
    assert (padded.json()["label"], padded.json()["address"]) == ("Padded", "Near the park")  # the case typed, the spaces gone


async def test_a_point_outside_the_city_is_refused_before_any_lock(client, db, rider, monkeypatch):
    locked = []

    async def lock_owner(db, user_id):
        locked.append(user_id)

    monkeypatch.setattr(saved_places_repo, "lock_owner", lock_owner)
    outside = [
        {"lat": settings.city_north + 0.5}, {"lat": settings.city_south - 0.5},
        {"lng": settings.city_east + 0.5}, {"lng": settings.city_west - 0.5},
    ]

    for changes in outside:
        response = await create(client, rider, **changes)
        assert response.status_code == 422
        assert response.json()["detail"] == OUTSIDE

    assert locked == []
    assert await rows_of(db) == []


# --- 3. the limit ---


async def test_ten_places_are_accepted_and_the_eleventh_is_refused(client, db, rider):
    ids = await fill(client, rider, 10)

    refused = await create(client, rider, "One too many")

    assert refused.status_code == 409
    assert refused.json()["detail"] == LIMIT_MESSAGE
    assert len(await rows_of(db)) == 10

    assert (await client.delete(f"/saved-places/{ids[3]}", headers=rider["headers"])).status_code == 204
    assert (await create(client, rider, "One too many")).status_code == 201
    assert len(await rows_of(db)) == 10


async def test_two_riders_have_separate_limits(client, db, rider, make_user):
    other = await make_user("rider")
    await fill(client, rider, 10)

    response = await create(client, other, "Place 0")

    assert response.status_code == 201
    assert len(await rows_of(db, other["user"].id)) == 1


# --- 4. labels ---


async def test_a_label_is_unique_per_rider_ignoring_case(client, db, rider, make_user):
    other = await make_user("rider")
    assert (await create(client, rider, "Home")).status_code == 201

    for label in ("Home", "home", "HOME", "hOmE"):
        response = await create(client, rider, label)
        assert response.status_code == 409
        assert response.json()["detail"] == NAME_TAKEN

    assert (await create(client, other, "home")).status_code == 201  # another rider may reuse it
    assert [row.label for row in await rows_of(db, rider["user"].id)] == ["Home"]  # the case that was typed first


# --- 5. rename ---


async def rename(client, who: dict, place_id: int, label: str):
    return await client.patch(f"/saved-places/{place_id}", json={"label": label}, headers=who["headers"])


async def test_rename_changes_the_label_and_nothing_else(client, db, rider):
    placed = (await create(client, rider, "Home", address="Elm Street", lat=12.95, lng=77.6)).json()

    response = await rename(client, rider, placed["id"], "  Flat  ")

    assert response.status_code == 200, response.text
    assert set(response.json()) == PLACE_KEYS
    assert response.json() == {**placed, "label": "Flat"}
    assert [(row.label, row.address) for row in await rows_of(db)] == [("Flat", "Elm Street")]


async def test_renaming_to_the_identical_label_is_a_200_that_writes_nothing(client, db, rider, monkeypatch):
    placed = (await create(client, rider, "Home")).json()
    written = []
    real = saved_places_repo.rename

    async def spy(*args, **kwargs):
        written.append(args)
        return await real(*args, **kwargs)

    monkeypatch.setattr(saved_places_repo, "rename", spy)

    response = await rename(client, rider, placed["id"], "Home")

    assert response.status_code == 200
    assert response.json() == placed
    assert written == []


async def test_a_change_of_letter_case_only_is_allowed(client, db, rider):
    placed = (await create(client, rider, "home")).json()

    response = await rename(client, rider, placed["id"], "Home")

    assert response.status_code == 200
    assert response.json()["label"] == "Home"
    assert [row.label for row in await rows_of(db)] == ["Home"]


async def test_renaming_to_another_of_the_callers_labels_is_refused(client, db, rider):
    await create(client, rider, "Work")
    placed = (await create(client, rider, "Home")).json()

    for label in ("Work", "work", "WORK"):
        response = await rename(client, rider, placed["id"], label)
        assert response.status_code == 409
        assert response.json()["detail"] == NAME_TAKEN

    assert [row.label for row in await rows_of(db)] == ["Work", "Home"]


async def test_renaming_an_unknown_place_or_someone_elses_is_a_404(client, db, rider, make_user):
    other = await make_user("rider")
    theirs = (await create(client, other, "Theirs")).json()

    unknown = await rename(client, rider, 999999, "Anything")
    stranger = await rename(client, rider, theirs["id"], "Taken over")

    assert (unknown.status_code, stranger.status_code) == (404, 404)
    assert unknown.json() == stranger.json()
    assert [row.label for row in await rows_of(db)] == ["Theirs"]


@pytest.mark.parametrize(
    "body", [{}, {"address": "New address"}, {"label": "Fine", "address": "New address"}, {"label": "Fine", "id": 3}, {"label": ""}, {"label": "a" * 31}]
)
async def test_a_bad_rename_body_is_a_422(client, db, rider, body):
    placed = (await create(client, rider, "Home")).json()

    response = await client.patch(f"/saved-places/{placed['id']}", json=body, headers=rider["headers"])

    assert response.status_code == 422
    assert [(row.label, row.address) for row in await rows_of(db)] == [("Home", "12 MG Road")]


# --- 6. delete ---


async def test_delete_removes_the_place_and_a_second_delete_is_a_404(client, db, rider):
    placed = (await create(client, rider, "Home")).json()

    first = await client.delete(f"/saved-places/{placed['id']}", headers=rider["headers"])
    second = await client.delete(f"/saved-places/{placed['id']}", headers=rider["headers"])

    assert (first.status_code, first.content) == (204, b"")
    assert second.status_code == 404
    assert await rows_of(db) == []


async def test_deleting_someone_elses_place_is_a_404_and_the_place_stays(client, db, rider, make_user):
    other = await make_user("rider")
    theirs = (await create(client, other, "Theirs")).json()

    response = await client.delete(f"/saved-places/{theirs['id']}", headers=rider["headers"])

    assert response.status_code == 404
    assert [row.label for row in await rows_of(db)] == ["Theirs"]


async def test_deleting_a_place_changes_no_ride(client, db, rider, insert_ride):
    ride = await insert_ride(rider, RideStatus.NO_DRIVER_FOUND)
    placed = (await create(client, rider, "Home", address=ride.pickup_address)).json()
    before = (await db.execute(select(Ride.__table__).order_by(Ride.id))).all()

    assert (await client.delete(f"/saved-places/{placed['id']}", headers=rider["headers"])).status_code == 204

    assert (await db.execute(select(Ride.__table__).order_by(Ride.id))).all() == before


# --- 7. roles ---


@pytest.mark.parametrize(
    "method, path, body",
    [("get", "/saved-places", None), ("post", "/saved-places", place_body()), ("patch", "/saved-places/1", {"label": "X"}), ("delete", "/saved-places/1", None)],
)
async def test_only_riders_may_use_saved_places(client, driver, admin, method, path, body):
    for who in (driver, admin):
        response = await client.request(method, path, json=body, headers=who["headers"])
        assert response.status_code == 403
    assert (await client.request(method, path, json=body)).status_code == 401


async def test_a_driver_cannot_create_a_place(client, db, driver):
    assert (await create(client, driver, "Home")).status_code == 403
    assert await rows_of(db) == []


# --- 8. concurrency (repeated: a missing lock does not always lose the race) ---


@pytest.mark.parametrize("attempt", range(5))
async def test_25_simultaneous_creates_give_exactly_ten_places(client, db, rider, monkeypatch, attempt):
    widen(monkeypatch, saved_places_repo, "count", 0.05)

    answers = await asyncio.gather(*[create(client, rider, f"Place {index}") for index in range(25)])

    codes = sorted(answer.status_code for answer in answers)
    assert codes == [201] * 10 + [409] * 15
    assert {answer.json()["detail"] for answer in answers if answer.status_code == 409} == {LIMIT_MESSAGE}
    assert len({answer.json()["id"] for answer in answers if answer.status_code == 201}) == 10
    assert len(await rows_of(db)) == 10


@pytest.mark.parametrize("attempt", range(5))
async def test_6_simultaneous_creates_with_one_label_in_different_cases_give_one_place(client, db, rider, monkeypatch, attempt):
    widen(monkeypatch, saved_places_repo, "label_exists", 0.05)

    answers = await asyncio.gather(*[create(client, rider, label) for label in ("Gym", "gym", "GYM", "gYm", "Gym", "GyM")])

    assert sorted(answer.status_code for answer in answers) == [201] + [409] * 5
    assert {answer.json()["detail"] for answer in answers if answer.status_code == 409} == {NAME_TAKEN}
    assert len(await rows_of(db)) == 1


@pytest.mark.parametrize("attempt", range(5))
async def test_two_simultaneous_renames_to_one_label_give_one_200_and_one_409(client, db, rider, monkeypatch, attempt):
    first = (await create(client, rider, "A")).json()
    second = (await create(client, rider, "B")).json()
    widen(monkeypatch, saved_places_repo, "label_exists", 0.05)

    answers = await asyncio.gather(rename(client, rider, first["id"], "Z"), rename(client, rider, second["id"], "z"))

    assert sorted(answer.status_code for answer in answers) == [200, 409]
    labels = sorted(row.label.lower() for row in await rows_of(db))
    assert labels in (["a", "z"], ["b", "z"])  # one place is called Z, the other kept its name


@pytest.mark.parametrize("attempt", range(5))
async def test_a_delete_and_a_create_at_the_same_moment_never_pass_ten(client, db, rider, monkeypatch, attempt):
    ids = await fill(client, rider, 10)
    widen(monkeypatch, saved_places_repo, "count", 0.05)
    requests = [client.delete(f"/saved-places/{ids[0]}", headers=rider["headers"]), create(client, rider, "Newcomer")]
    if attempt % 2 == 1:  # alternate which request is sent first
        requests.reverse()

    answers = await asyncio.gather(*requests)

    deleted, created = answers if attempt % 2 == 0 else answers[::-1]
    assert deleted.status_code == 204
    assert created.status_code in (201, 409)
    assert len(await rows_of(db)) == 10 - 1 + (created.status_code == 201)
    assert len(await rows_of(db)) <= 10


# --- 9. lock hygiene ---


async def assert_user_row_is_free(test_engine, user_id: int) -> None:
    """A separate session can take the row with NOWAIT (it raises if a lock is still held)."""
    async with async_sessionmaker(test_engine)() as other:
        await other.execute(text("SELECT id FROM users WHERE id = :id FOR UPDATE NOWAIT"), {"id": user_id})
        await other.rollback()


async def test_a_refused_create_leaves_no_lock_behind(client, test_engine, rider):
    await create(client, rider, "Home")

    assert (await create(client, rider, "home")).status_code == 409  # refused after the lock was taken
    await assert_user_row_is_free(test_engine, rider["user"].id)

    ids = await fill(client, rider, 9)
    assert (await create(client, rider, "Eleventh")).status_code == 409  # the limit
    await assert_user_row_is_free(test_engine, rider["user"].id)
    await client.delete(f"/saved-places/{ids[0]}", headers=rider["headers"])
    assert (await asyncio.wait_for(create(client, rider, "Fits now"), 5)).status_code == 201


async def test_a_request_that_fails_halfway_leaves_no_lock_behind(client, db, test_engine, rider, monkeypatch):
    async def broken_insert(*args, **kwargs):
        raise RuntimeError("the insert failed")

    with monkeypatch.context() as patch:
        patch.setattr(saved_places_repo, "insert", broken_insert)
        assert (await create(client, rider, "Home")).status_code == 500

    await assert_user_row_is_free(test_engine, rider["user"].id)
    assert await rows_of(db) == []
    assert (await asyncio.wait_for(create(client, rider, "Home"), 5)).status_code == 201


# --- 10. the constraints, with direct SQL (each in its own savepoint) ---


async def insert_place(db, user_id: int, label: str = "Home", address: str = "Somewhere", lat: float = 12.9, lng: float = 77.5) -> None:
    await db.execute(
        text("INSERT INTO saved_places (user_id, label, address, lat, lng) VALUES (:user_id, :label, :address, :lat, :lng)"),
        {"user_id": user_id, "label": label, "address": address, "lat": lat, "lng": lng},
    )


@pytest.mark.parametrize(
    "changes, constraint",
    [
        ({"label": ""}, "ck_saved_places_label_length"),
        ({"address": ""}, "ck_saved_places_address_length"),
        ({"lat": 91}, "ck_saved_places_lat_range"),
        ({"lat": -91}, "ck_saved_places_lat_range"),
        ({"lng": 181}, "ck_saved_places_lng_range"),
        ({"lng": -181}, "ck_saved_places_lng_range"),
    ],
    ids=str,
)
async def test_the_database_refuses_a_bad_row(db, rider, changes, constraint):
    with pytest.raises(IntegrityError) as error:
        async with db.begin_nested():
            await insert_place(db, rider["user"].id, **changes)

    assert constraint in str(error.value)


@pytest.mark.parametrize("changes, column_type", [({"label": "a" * 31}, "character varying(30)"), ({"address": "a" * 201}, "character varying(200)")], ids=str)
async def test_the_column_type_refuses_a_too_long_text_before_the_check_constraint_runs(db, rider, changes, column_type):
    # VARCHAR(30) and VARCHAR(200) fire first, so the upper bound of the two length checks is never the one that is reported
    # (the lower bound, an empty text, is: see the test above).
    with pytest.raises(DBAPIError) as error:
        async with db.begin_nested():
            await insert_place(db, rider["user"].id, **changes)

    assert f"value too long for type {column_type}" in str(error.value)


async def test_the_database_refuses_a_second_label_that_differs_only_in_case(db, rider):
    await insert_place(db, rider["user"].id, label="Home")

    for label in ("Home", "home", "HOME"):
        with pytest.raises(IntegrityError) as error:
            async with db.begin_nested():
                await insert_place(db, rider["user"].id, label=label)
        assert "uq_saved_places_user_id_lower_label" in str(error.value)


async def test_the_allowed_variations_succeed(db, rider, make_user):
    other = await make_user("rider")
    await insert_place(db, rider["user"].id, label="Home", address="Same address")

    await insert_place(db, other["user"].id, label="Home", address="Same address")  # the same label for another user
    await insert_place(db, rider["user"].id, label="Work", address="Same address")  # the same address twice
    await insert_place(db, rider["user"].id, label="a" * 30, address="b" * 200, lat=-90, lng=180)  # the limits themselves

    assert (await db.execute(select(func.count()).select_from(SavedPlace))).scalar_one() == 4
