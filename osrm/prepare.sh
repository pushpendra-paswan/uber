#!/usr/bin/env bash
# One-time OSRM data preparation: download a regional map, clip it to the city, build the routing data.
# Safe to run twice: the download is skipped if the file exists; the clip and OSRM steps are redone.
# Delete osrm/data/ (or change CITY_* in .env and run this again) to rebuild for another city.
set -e

# Must be the same tag as the osrm service in docker-compose.yml.
OSRM_IMAGE=ghcr.io/project-osrm/osrm-backend:v26.10.0-debian
PADDING=0.05 # degrees added on every side: a route near the edge may use roads just outside the box

cd "$(dirname "$0")/.."
DATA_DIR="$PWD/osrm/data"

# Not `source .env`: NOMINATIM_USER_AGENT contains spaces and parentheses.
read_env() {
  grep "^$1=" .env | tail -1 | cut -d= -f2-
}
SOUTH=$(read_env CITY_SOUTH)
WEST=$(read_env CITY_WEST)
NORTH=$(read_env CITY_NORTH)
EAST=$(read_env CITY_EAST)
EXTRACT_URL=$(read_env OSM_EXTRACT_URL)
if [ -z "$SOUTH" ] || [ -z "$WEST" ] || [ -z "$NORTH" ] || [ -z "$EAST" ] || [ -z "$EXTRACT_URL" ]; then
  echo "ERROR: CITY_SOUTH, CITY_WEST, CITY_NORTH, CITY_EAST and OSM_EXTRACT_URL must all be set in .env"
  exit 1
fi

mkdir -p "$DATA_DIR"

echo "==> Step 1/4: download the regional extract"
if [ -f "$DATA_DIR/region.osm.pbf" ]; then
  echo "    $DATA_DIR/region.osm.pbf already exists, skipping the download"
else
  STATUS=$(curl -sIL -o /dev/null -w '%{http_code}' "$EXTRACT_URL")
  if [ "$STATUS" != "200" ]; then
    echo "ERROR: $EXTRACT_URL answered HTTP $STATUS. Fix OSM_EXTRACT_URL in .env, I will not guess another URL."
    exit 1
  fi
  echo "    downloading $EXTRACT_URL"
  # Download to a .part file, so an interrupted download is never mistaken for a finished one.
  curl -fL --progress-bar -C - -o "$DATA_DIR/region.osm.pbf.part" "$EXTRACT_URL"
  mv "$DATA_DIR/region.osm.pbf.part" "$DATA_DIR/region.osm.pbf"
fi

# The running osrm container has these files open, so stop it before they are rewritten.
docker compose stop osrm > /dev/null 2>&1 || true

echo "==> Step 2/4: clip the region to the city box plus $PADDING degrees (osmium, in a throwaway container)"
BBOX=$(awk -v s="$SOUTH" -v w="$WEST" -v n="$NORTH" -v e="$EAST" -v p="$PADDING" \
  'BEGIN { printf "%.4f,%.4f,%.4f,%.4f", w - p, s - p, e + p, n + p }')
echo "    bbox (west,south,east,north) = $BBOX"
docker run --rm -v "$DATA_DIR:/data" -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" debian:12-slim bash -c "
  set -e
  apt-get update -qq
  apt-get install -y -qq osmium-tool > /dev/null
  osmium extract --overwrite --strategy=complete_ways --bbox $BBOX -o /data/city.osm.pbf /data/region.osm.pbf
  chown \$HOST_UID:\$HOST_GID /data/city.osm.pbf
"

echo "==> Step 3/4: osrm-extract (car profile)"
docker run --rm --user "$(id -u):$(id -g)" -v "$DATA_DIR:/data" "$OSRM_IMAGE" osrm-extract -p /opt/car.lua /data/city.osm.pbf

echo "==> Step 4/4: osrm-partition and osrm-customize (MLD)"
docker run --rm --user "$(id -u):$(id -g)" -v "$DATA_DIR:/data" "$OSRM_IMAGE" osrm-partition /data/city.osrm
docker run --rm --user "$(id -u):$(id -g)" -v "$DATA_DIR:/data" "$OSRM_IMAGE" osrm-customize /data/city.osrm

echo "==> Done. Routing data is in $DATA_DIR (city.osrm.*)."
echo "    Start it with: docker compose up -d osrm"
