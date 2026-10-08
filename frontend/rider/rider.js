import { api, clearSession, getSession, saveSession } from "/shared/api.js";

const STATUS_TEXT = {
  REQUESTED: "Waiting for a driver to be assigned",
  DRIVER_ASSIGNED: "A driver is assigned and on the way",
  DRIVER_ARRIVED: "Your driver has arrived",
  IN_PROGRESS: "Trip in progress",
  COMPLETED: "Trip completed",
  CANCELLED: "Ride cancelled",
  NO_DRIVER_FOUND: "No driver was found",
};
const CANCELLABLE = ["REQUESTED", "DRIVER_ASSIGNED", "DRIVER_ARRIVED"];
const FINISHED = ["COMPLETED", "CANCELLED", "NO_DRIVER_FOUND"];
const POLL_MS = 3000;
const TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
const MAX_ZOOM = 19;
const MIN_ZOOM = 10;
const PLACE_ZOOM = 16;
const ADDRESS_MAX_LENGTH = 255; // the limit on pickup_address and dropoff_address in POST /rides
const KINDS = ["pickup", "dropoff"];
// Leaflet renders tooltips as HTML, so they only ever get these fixed strings, never an address.
const MARKER_LABEL = { pickup: "Pickup", dropoff: "Drop-off" };
const MARKER_COLOR = { pickup: "#1a7f37", dropoff: "#b42318" };
const ROUTE_WEIGHT = 5;
const FIT_PADDING = [40, 40];
const money = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" }); // money.format(paise / 100)

const session = getSession();
const state = {
  user: session ? session.user : null,
  loaded: false, // true after the first successful refresh, so the request form does not flash before the ride shows
  ride: null,
  rideId: null, // remembered so a finished ride can still be shown after /rides/active returns 404
  events: [],
  config: null, // from GET /places/map-config
  points: { pickup: null, dropoff: null }, // each {lat, lng, address} once chosen
  results: { pickup: null, dropoff: null }, // each {items, notice}; a new object every time it changes
  map: null,
  markers: { pickup: null, dropoff: null },
  fittedRideId: null, // the map is fitted to a ride's markers once, never on every poll
  estimate: null, // answer of POST /rides/estimate for the chosen points; null while there is none
  estimating: false,
  estimateRequest: 0, // counts estimate calls, so a slow old answer can never replace a newer one
  ridePath: null, // route of the current ride, asked for once per ride
  routeRideId: null, // the ride whose route was asked for; set before the call so a failure is not retried every poll
  routeLine: null, // Leaflet polyline
  drawnPath: null, // the path routeLine shows, so polling neither redraws nor refits it
  error: "",
  busy: false,
};
let refreshing = false;
const shownResults = { pickup: null, dropoff: null }; // which results object is in the DOM, so polling does not rebuild it

const message = document.getElementById("message");
const userBar = document.getElementById("user-bar");
const userName = document.getElementById("user-name");
const logoutButton = document.getElementById("logout-button");
const loginSection = document.getElementById("login-section");
const loginForm = document.getElementById("login-form");
const registerForm = document.getElementById("register-form");
const wrongRoleSection = document.getElementById("wrong-role-section");
const wrongRoleText = document.getElementById("wrong-role-text");
const mapSection = document.getElementById("map-section");
const requestSection = document.getElementById("request-section");
const forms = { pickup: document.getElementById("pickup-form"), dropoff: document.getElementById("dropoff-form") };
const inputs = { pickup: document.getElementById("pickup-input"), dropoff: document.getElementById("dropoff-input") };
const resultLists = { pickup: document.getElementById("pickup-results"), dropoff: document.getElementById("dropoff-results") };
const pickRadios = { pickup: document.querySelector('input[name="pick"][value="pickup"]'), dropoff: document.querySelector('input[name="pick"][value="dropoff"]') };
const requestButton = document.getElementById("request-button");
const rideSection = document.getElementById("ride-section");
const rideId = document.getElementById("ride-id");
const rideStatus = document.getElementById("ride-status");
const rideStatusText = document.getElementById("ride-status-text");
const ridePickup = document.getElementById("ride-pickup");
const rideDropoff = document.getElementById("ride-dropoff");
const rideDriverRow = document.getElementById("ride-driver-row");
const rideDriver = document.getElementById("ride-driver");
const cancelButton = document.getElementById("cancel-button");
const newRideButton = document.getElementById("new-ride-button");
const eventsBody = document.getElementById("events");
const estimateLoading = document.getElementById("estimate-loading");
const estimateSection = document.getElementById("estimate-section");
const estimateTrip = document.getElementById("estimate-trip");
const estimateFare = document.getElementById("estimate-fare");
const estimateBase = document.getElementById("estimate-base");
const estimateDistanceFare = document.getElementById("estimate-distance-fare");
const estimateTimeFare = document.getElementById("estimate-time-fare");
const estimateMinimum = document.getElementById("estimate-minimum");
const rideTrip = document.getElementById("ride-trip");
const rideFare = document.getElementById("ride-fare");

// "5.2 km, 14 min". Used by the estimate panel and the ride view. Old rides have null values.
function formatTrip(distanceM, durationS) {
  if (distanceM === null || durationS === null) return "-";
  return `${(distanceM / 1000).toFixed(1)} km, ${Math.max(1, Math.round(durationS / 60))} min`;
}

async function login(email, password) {
  const data = await api("POST", "/auth/login", { email, password });
  saveSession(data.access_token, data.user);
  state.user = data.user;
}

async function refresh() {
  if (!state.user || state.user.role !== "rider") return;
  try {
    if (state.config === null) state.config = await api("GET", "/places/map-config");
    const active = await api("GET", "/rides/active").catch((err) => {
      if (err.status === 404) return null;
      throw err;
    });
    if (active) {
      state.ride = active;
      state.rideId = active.id;
    } else if (state.rideId && !FINISHED.includes(state.ride.status)) {
      // /rides/active is 404 once a ride ends, so fetch the remembered ride once to show how it ended.
      state.ride = await api("GET", `/rides/${state.rideId}`);
    }
    state.events = state.ride ? await api("GET", `/rides/${state.ride.id}/events`) : [];
    state.loaded = true;

    // The path is not stored on the ride, so ask for it once per ride.
    if (state.ride && state.routeRideId !== state.ride.id) {
      state.routeRideId = state.ride.id;
      state.ridePath = null;
      const route = await api("POST", "/rides/estimate", {
        pickup_lat: state.ride.pickup_lat,
        pickup_lng: state.ride.pickup_lng,
        dropoff_lat: state.ride.dropoff_lat,
        dropoff_lng: state.ride.dropoff_lng,
      });
      state.ridePath = route.path;
    }
  } catch (err) {
    state.error = err.message;
  }
}

async function act(fn) {
  state.error = "";
  state.busy = true;
  render();
  try {
    await fn();
  } catch (err) {
    state.error = err.message;
  }
  await refresh();
  state.busy = false;
  render();
}

// Used by a search result click and a map click. Writing the input here, not in render(), keeps polling away from the user's typing.
function setPoint(kind, lat, lng, address) {
  const other = kind === "pickup" ? "dropoff" : "pickup";
  state.points[kind] = { lat, lng, address: address.slice(0, ADDRESS_MAX_LENGTH) };
  inputs[kind].value = state.points[kind].address;
  state.results[kind] = null;
  if (state.points[other] === null) pickRadios[other].checked = true;

  // A new point makes the old route and estimate wrong, so drop them now. Only the latest answer is applied.
  state.estimate = null;
  const requestNumber = ++state.estimateRequest;
  const { pickup, dropoff } = state.points;
  state.estimating = pickup !== null && dropoff !== null;
  if (state.estimating) {
    api("POST", "/rides/estimate", {
      pickup_lat: pickup.lat,
      pickup_lng: pickup.lng,
      dropoff_lat: dropoff.lat,
      dropoff_lng: dropoff.lng,
    })
      .then((estimate) => {
        if (requestNumber === state.estimateRequest) state.estimate = estimate;
      })
      .catch((err) => {
        if (requestNumber === state.estimateRequest) state.error = err.message;
      })
      .finally(() => {
        if (requestNumber === state.estimateRequest) state.estimating = false;
        render();
      });
  }
  render();
}

function onMapClick(event) {
  if (state.ride !== null || state.busy) return; // view-only with a ride, and one click at a time
  act(async () => {
    const { lat, lng } = event.latlng;
    const c = state.config;
    if (lat < c.south || lat > c.north || lng < c.west || lng > c.east) {
      state.error = `That location is outside ${c.city_name}`;
      return;
    }
    const kind = document.querySelector('input[name="pick"]:checked').value;
    const place = await api("GET", `/places/reverse?lat=${lat}&lng=${lng}`).catch((err) => {
      if (err.status === 404) return null;
      throw err;
    });
    setPoint(kind, lat, lng, place ? place.display_name : `${lat.toFixed(5)}, ${lng.toFixed(5)}`);
  });
}

// Never touches the search inputs, because polling calls this every few seconds while the user types.
function render() {
  const loggedIn = state.user !== null;
  const isRider = loggedIn && state.user.role === "rider";
  const showRide = isRider && state.ride !== null;
  const showMap = isRider && state.loaded && state.config !== null;

  message.hidden = state.error === "";
  message.textContent = state.error;

  loginSection.hidden = loggedIn;
  userBar.hidden = !loggedIn;
  userName.textContent = loggedIn ? state.user.name : "";
  wrongRoleSection.hidden = !loggedIn || isRider;
  if (loggedIn) wrongRoleText.textContent = `This account is a ${state.user.role}. Open /${state.user.role}/ instead.`;

  requestSection.hidden = !isRider || !state.loaded || showRide;
  rideSection.hidden = !showRide;
  mapSection.hidden = !showMap;

  if (showMap) {
    const c = state.config;
    // Created the first time the section is visible: Leaflet cannot measure a hidden container.
    if (state.map === null) {
      state.map = L.map("map", {
        center: [c.center_lat, c.center_lng],
        zoom: c.zoom,
        minZoom: MIN_ZOOM,
        maxZoom: MAX_ZOOM,
        maxBounds: [[c.south, c.west], [c.north, c.east]],
        maxBoundsViscosity: 1.0,
      });
      L.tileLayer(TILE_URL, { attribution: ATTRIBUTION, maxZoom: MAX_ZOOM }).addTo(state.map);
      state.map.on("click", onMapClick);
      state.map.invalidateSize();
    }

    // The ride's points when a ride exists, otherwise the rider's selection.
    const wanted = state.ride
      ? {
          pickup: { lat: state.ride.pickup_lat, lng: state.ride.pickup_lng },
          dropoff: { lat: state.ride.dropoff_lat, lng: state.ride.dropoff_lng },
        }
      : state.points;
    for (const kind of KINDS) {
      const point = wanted[kind];
      const marker = state.markers[kind];
      if (point === null && marker !== null) {
        marker.remove();
        state.markers[kind] = null;
      } else if (point !== null && marker === null) {
        state.markers[kind] = L.circleMarker([point.lat, point.lng], {
          radius: 9,
          color: MARKER_COLOR[kind],
          fillColor: MARKER_COLOR[kind],
          fillOpacity: 1,
        })
          .bindTooltip(MARKER_LABEL[kind], { permanent: true, direction: "top", offset: [0, -9] })
          .addTo(state.map);
      } else if (point !== null && (marker.getLatLng().lat !== point.lat || marker.getLatLng().lng !== point.lng)) {
        marker.setLatLng([point.lat, point.lng]);
      }
    }

    if (state.ride && state.fittedRideId !== state.ride.id) {
      state.map.fitBounds(
        [[state.ride.pickup_lat, state.ride.pickup_lng], [state.ride.dropoff_lat, state.ride.dropoff_lng]],
        { padding: FIT_PADDING, animate: false }
      );
      state.fittedRideId = state.ride.id;
    }

    // The ride's route when there is a ride, otherwise the estimate for the chosen points.
    let path = state.ridePath;
    if (!state.ride) path = state.estimate === null ? null : state.estimate.path;
    if (path === null && state.routeLine !== null) {
      state.routeLine.remove();
      state.routeLine = null;
      state.drawnPath = null;
    } else if (path !== null && path !== state.drawnPath) {
      if (state.routeLine === null) state.routeLine = L.polyline(path, { weight: ROUTE_WEIGHT }).addTo(state.map);
      else state.routeLine.setLatLngs(path);
      state.routeLine.bringToBack(); // below the pickup and drop-off markers
      state.drawnPath = path;
      // Once per new path, never on a poll. Not animated, here and in the other view changes from code:
      // Leaflet silently ignores a view change that arrives during a zoom animation, so an animated
      // search-result zoom or ride fit would swallow this one.
      state.map.fitBounds(state.routeLine.getBounds(), { padding: FIT_PADDING, animate: false });
    }
  }

  for (const kind of KINDS) {
    const result = state.results[kind];
    if (result === shownResults[kind]) continue; // unchanged: rebuilding would swallow a click made during a poll
    shownResults[kind] = result;
    const rows = [];
    if (result !== null) {
      if (result.notice !== "") {
        const row = document.createElement("li");
        row.className = "note";
        row.textContent = result.notice;
        rows.push(row);
      }
      for (const place of result.items) {
        const row = document.createElement("li");
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = place.display_name;
        button.addEventListener("click", () =>
          act(async () => {
            setPoint(kind, place.lat, place.lng, place.display_name);
            state.map.setView([place.lat, place.lng], PLACE_ZOOM, { animate: false });
          })
        );
        row.append(button);
        rows.push(row);
      }
    }
    resultLists[kind].replaceChildren(...rows);
  }

  estimateLoading.hidden = !state.estimating;
  estimateSection.hidden = state.estimate === null;
  if (state.estimate !== null) {
    const estimate = state.estimate;
    estimateTrip.textContent = formatTrip(estimate.distance_m, estimate.duration_s);
    estimateFare.textContent = money.format(estimate.fare_estimate / 100);
    estimateBase.textContent = money.format(estimate.base_fare / 100);
    estimateDistanceFare.textContent = money.format(estimate.distance_fare / 100);
    estimateTimeFare.textContent = money.format(estimate.time_fare / 100);
    estimateMinimum.hidden = !estimate.minimum_fare_applied;
  }

  if (showRide) {
    rideId.textContent = state.ride.id;
    rideStatus.textContent = state.ride.status;
    rideStatusText.textContent = STATUS_TEXT[state.ride.status];
    ridePickup.textContent = state.ride.pickup_address;
    rideDropoff.textContent = state.ride.dropoff_address;
    rideTrip.textContent = formatTrip(state.ride.distance_m, state.ride.duration_s);
    rideFare.textContent = state.ride.fare_estimate === null ? "-" : money.format(state.ride.fare_estimate / 100);
    rideDriverRow.hidden = state.ride.driver_id === null;
    rideDriver.textContent = state.ride.driver_id;
    cancelButton.hidden = !CANCELLABLE.includes(state.ride.status);
    newRideButton.hidden = !FINISHED.includes(state.ride.status);

    const rows = state.events.map((event) => {
      const row = document.createElement("tr");
      for (const text of [event.from_status || "-", event.to_status, new Date(event.created_at).toLocaleString()]) {
        const cell = document.createElement("td");
        cell.textContent = text;
        row.append(cell);
      }
      return row;
    });
    eventsBody.replaceChildren(...rows);
  }

  for (const button of document.querySelectorAll("button")) button.disabled = state.busy;
  requestButton.disabled = state.busy || state.points.pickup === null || state.points.dropoff === null || state.estimate === null;
}

loginForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const form = new FormData(loginForm);
  act(async () => {
    await login(form.get("email"), form.get("password"));
    loginForm.reset();
  });
});

registerForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const form = new FormData(registerForm);
  const body = { name: form.get("name"), email: form.get("email"), password: form.get("password"), role: "rider" };
  const phone = form.get("phone").trim();
  if (phone !== "") body.phone = phone;
  act(async () => {
    await api("POST", "/auth/register", body);
    await login(body.email, body.password);
    registerForm.reset();
  });
});

logoutButton.addEventListener("click", () => {
  clearSession();
  location.reload();
});

// Pressing Enter in an input submits its form, so Enter and the Search button do the same thing.
for (const kind of KINDS) {
  forms[kind].addEventListener("submit", (event) => {
    event.preventDefault();
    const query = inputs[kind].value.trim();
    act(async () => {
      if (query.length < 3) {
        state.results[kind] = { items: [], notice: "Type at least 3 characters to search" };
        return;
      }
      const items = await api("GET", `/places/search?q=${encodeURIComponent(query)}`);
      state.results[kind] = { items, notice: items.length === 0 ? `No places found in ${state.config.city_name}` : "" };
    });
  });
}

requestButton.addEventListener("click", () => {
  const { pickup, dropoff } = state.points;
  act(() =>
    api("POST", "/rides", {
      pickup_address: pickup.address,
      pickup_lat: pickup.lat,
      pickup_lng: pickup.lng,
      dropoff_address: dropoff.address,
      dropoff_lat: dropoff.lat,
      dropoff_lng: dropoff.lng,
    })
  );
});

cancelButton.addEventListener("click", () => {
  if (confirm("Cancel this ride?")) act(() => api("POST", `/rides/${state.ride.id}/cancel`));
});

newRideButton.addEventListener("click", () => {
  act(async () => {
    state.ride = null;
    state.rideId = null;
    state.events = [];
    state.points = { pickup: null, dropoff: null };
    state.results = { pickup: null, dropoff: null };
    state.estimate = null;
    state.estimating = false;
    state.estimateRequest++; // an answer still on its way must not show up
    state.ridePath = null;
    state.routeRideId = null;
    for (const kind of KINDS) inputs[kind].value = "";
    pickRadios.pickup.checked = true;
  });
});

// Polling keeps the page current for now; WebSockets replace this in M3.
setInterval(async () => {
  if (refreshing || state.busy) return;
  refreshing = true;
  await refresh();
  refreshing = false;
  render();
}, POLL_MS);

render();
refresh().then(render);
