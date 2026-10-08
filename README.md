# Uber Clone

A ride-hailing web app built for learning: riders request trips, nearby drivers accept them, and both sides see live location, fare, and trip status. Three apps (rider, driver, admin) share one FastAPI backend with PostgreSQL/PostGIS and Redis. Frontend is plain HTML, CSS, and JavaScript.

See `CLAUDE.md` for the architecture and milestones, and `PROJECT_CONTEXT.md` for current status.

## Start

```bash
cp .env.example .env        # first time only
docker compose up --build
docker compose exec backend alembic upgrade head    # create the tables (first time, and after a reset)
```

- API: http://localhost:8000
- Health check: http://localhost:8000/health
- Postgres: `localhost:5432`, Redis: `localhost:6379`

## Stop

```bash
docker compose down
```

## Reset (deletes all database data)

```bash
docker compose down -v
docker compose up --build
docker compose exec backend alembic upgrade head
```
