import { api, clearSession, getSession, saveSession } from "/shared/api.js";

const STATUS_TEXT = {
  REQUESTED: "Waiting for a driver to be assigned",
  DRIVER_ASSIGNED: "Go to the pickup point",
  DRIVER_ARRIVED: "Waiting at pickup. Start the trip when the rider is in",
  IN_PROGRESS: "Trip in progress",
  COMPLETED: "Trip completed",
  CANCELLED: "Ride cancelled",
  NO_DRIVER_FOUND: "No driver was found",
};
const VERIFICATION_TEXT = {
  pending: "Waiting for admin approval",
  rejected: "Your application was rejected",
  approved: "You are approved",
};
const FINISHED = ["COMPLETED", "CANCELLED", "NO_DRIVER_FOUND"];
const POLL_MS = 3000;
const TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
const MAX_ZOOM = 19;
const MIN_ZOOM = 10;
const MARKER_COLOR = "#1a56db";

const session = getSession();
const state = {
  user: session ? session.user : null,
  loaded: false, // true after the first successful refresh, so the forms do not flash before the profile shows
  driver: null,
  config: null, // from GET /places/map-config
  position: null, // {lat, lng}, chosen by clicking the map
  presence: null, // from GET /drivers/me/presence or the last location ping
  map: null,
  marker: null,
  ride: null,
  rideId: null, // remembered so a finished ride can still be shown after /rides/active returns 404
  events: [],
  error: "",
  busy: false,
};
let refreshing = false;

const message = document.getElementById("message");
const userBar = document.getElementById("user-bar");
const userName = document.getElementById("user-name");
const logoutButton = document.getElementById("logout-button");
const loginSection = document.getElementById("login-section");
const loginForm = document.getElementById("login-form");
const registerForm = document.getElementById("register-form");
const wrongRoleSection = document.getElementById("wrong-role-section");
const wrongRoleText = document.getElementById("wrong-role-text");
const driverIdSection = document.getElementById("driver-id-section");
const driverId = document.getElementById("driver-id");
const verificationText = document.getElementById("verification-text");
const profileSection = document.getElementById("profile-section");
const profileForm = document.getElementById("profile-form");
const vehicleSection = document.getElementById("vehicle-section");
const vehicleForm = document.getElementById("vehicle-form");
const presenceSection = document.getElementById("presence-section");
const presenceStatus = document.getElementById("presence-status");
const onlineButton = document.getElementById("online-button");
const offlineButton = document.getElementById("offline-button");
const offlineNote = document.getElementById("offline-note");
const noRideSection = document.getElementById("no-ride-section");
const rideSection = document.getElementById("ride-section");
const rideId = document.getElementById("ride-id");
const rideStatus = document.getElementById("ride-status");
const rideStatusText = document.getElementById("ride-status-text");
const ridePickup = document.getElementById("ride-pickup");
const rideDropoff = document.getElementById("ride-dropoff");
const arriveButton = document.getElementById("arrive-button");
const startButton = document.getElementById("start-button");
const completeButton = document.getElementById("complete-button");
const cancelButton = document.getElementById("cancel-button");
const doneButton = document.getElementById("done-button");
const eventsBody = document.getElementById("events");

async function login(email, password) {
  const data = await api("POST", "/auth/login", { email, password });
  saveSession(data.access_token, data.user);
  state.user = data.user;
}

// GET that returns null on 404 (no profile yet, no active ride) and throws on anything else.
async function getOrNull(path) {
  try {
    return await api("GET", path);
  } catch (err) {
    if (err.status === 404) return null;
    throw err;
  }
}

async function refresh() {
  if (!state.user || state.user.role !== "driver") return;
  try {
    state.driver = await getOrNull("/drivers/me");
    if (state.driver && state.driver.verification_status === "approved") {
      if (state.config === null) state.config = await api("GET", "/places/map-config");

      // The heartbeat: a location ping every poll keeps the 30 second presence key alive.
      state.presence = await api("GET", "/drivers/me/presence");
      if (state.presence.online) {
        // After a reload the position is still in Redis, so take it from there.
        if (state.position === null) state.position = { lat: state.presence.lat, lng: state.presence.lng };
        state.presence = await api("POST", "/drivers/me/location", state.position);
      }

      const active = await getOrNull("/rides/active");
      if (active) {
        state.ride = active;
        state.rideId = active.id;
      } else if (state.rideId && !FINISHED.includes(state.ride.status)) {
        // /rides/active is 404 once a ride ends, so fetch the remembered ride once to show how it ended.
        state.ride = await api("GET", `/rides/${state.rideId}`);
      }
      state.events = state.ride ? await api("GET", `/rides/${state.ride.id}/events`) : [];
    } else {
      state.presence = null;
    }
    state.loaded = true;
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

// Never touches form inputs, because polling calls this every few seconds while the user types.
function render() {
  const loggedIn = state.user !== null;
  const isDriver = loggedIn && state.user.role === "driver";
  const hasProfile = isDriver && state.driver !== null;
  const hasVehicle = hasProfile && state.driver.vehicle !== null;
  const approved = hasVehicle && state.driver.verification_status === "approved";
  const showRide = approved && state.ride !== null;
  const showMap = approved && state.config !== null;
  const online = state.presence !== null && state.presence.online;
  const hasActiveRide = state.ride !== null && !FINISHED.includes(state.ride.status);

  message.hidden = state.error === "";
  message.textContent = state.error;

  loginSection.hidden = loggedIn;
  userBar.hidden = !loggedIn;
  userName.textContent = loggedIn ? state.user.name : "";
  wrongRoleSection.hidden = !loggedIn || isDriver;
  if (loggedIn) wrongRoleText.textContent = `This account is a ${state.user.role}. Open /${state.user.role}/ instead.`;

  driverIdSection.hidden = !hasProfile;
  driverId.textContent = hasProfile ? state.driver.id : "";
  verificationText.hidden = !hasVehicle;
  if (hasVehicle) verificationText.textContent = VERIFICATION_TEXT[state.driver.verification_status];

  profileSection.hidden = !isDriver || !state.loaded || hasProfile;
  vehicleSection.hidden = !hasProfile || hasVehicle;
  presenceSection.hidden = !showMap;
  noRideSection.hidden = !approved || showRide;
  rideSection.hidden = !showRide;

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
      state.map.on("click", (event) => {
        if (state.busy) return;
        const { lat, lng } = event.latlng;
        act(async () => {
          if (lat < c.south || lat > c.north || lng < c.west || lng > c.east) {
            throw new Error(`That location is outside ${c.city_name}`);
          }
          state.position = { lat, lng };
          // While online, a click is also how the driver moves.
          if (state.presence && state.presence.online) {
            state.presence = await api("POST", "/drivers/me/location", state.position);
          }
        });
      });
      state.map.invalidateSize();
    }

    // Created, moved, and removed here from state.position. The tooltip is a fixed string, never API data.
    if (state.position === null && state.marker !== null) {
      state.marker.remove();
      state.marker = null;
    } else if (state.position !== null && state.marker === null) {
      state.marker = L.circleMarker([state.position.lat, state.position.lng], {
        radius: 9,
        color: MARKER_COLOR,
        fillColor: MARKER_COLOR,
        fillOpacity: 1,
      })
        .bindTooltip("You", { permanent: true, direction: "top", offset: [0, -9] })
        .addTo(state.map);
    } else if (state.position !== null) {
      state.marker.setLatLng([state.position.lat, state.position.lng]);
    }

    presenceStatus.textContent = online
      ? `Online, last update ${new Date(state.presence.updated_at * 1000).toLocaleTimeString()}`
      : "Offline";
    onlineButton.hidden = online;
    offlineButton.hidden = !online;
    offlineNote.hidden = !online || !hasActiveRide;
  }

  if (showRide) {
    const status = state.ride.status;
    rideId.textContent = state.ride.id;
    rideStatus.textContent = status;
    rideStatusText.textContent = STATUS_TEXT[status];
    ridePickup.textContent = state.ride.pickup_address;
    rideDropoff.textContent = state.ride.dropoff_address;
    arriveButton.hidden = status !== "DRIVER_ASSIGNED";
    startButton.hidden = status !== "DRIVER_ARRIVED";
    completeButton.hidden = status !== "IN_PROGRESS";
    cancelButton.hidden = status !== "DRIVER_ASSIGNED" && status !== "DRIVER_ARRIVED";
    doneButton.hidden = !FINISHED.includes(status);

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
  onlineButton.disabled = state.busy || state.position === null;
  offlineButton.disabled = state.busy || hasActiveRide;
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
  const body = { name: form.get("name"), email: form.get("email"), password: form.get("password"), role: "driver" };
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

profileForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const form = new FormData(profileForm);
  act(() => api("POST", "/drivers/me/profile", { license_number: form.get("license_number") }));
});

vehicleForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const form = new FormData(vehicleForm);
  act(() =>
    api("POST", "/drivers/me/vehicle", {
      plate_number: form.get("plate_number"),
      model: form.get("model"),
      color: form.get("color"),
    })
  );
});

onlineButton.addEventListener("click", () => {
  act(async () => {
    state.presence = await api("POST", "/drivers/me/online", state.position);
  });
});

offlineButton.addEventListener("click", () => {
  act(async () => {
    state.presence = await api("POST", "/drivers/me/offline");
  });
});

arriveButton.addEventListener("click", () => act(() => api("POST", `/rides/${state.ride.id}/arrive`)));
startButton.addEventListener("click", () => act(() => api("POST", `/rides/${state.ride.id}/start`)));
completeButton.addEventListener("click", () => act(() => api("POST", `/rides/${state.ride.id}/complete`)));

cancelButton.addEventListener("click", () => {
  if (confirm("Cancel this ride?")) act(() => api("POST", `/rides/${state.ride.id}/cancel`));
});

doneButton.addEventListener("click", () => {
  act(async () => {
    state.ride = null;
    state.rideId = null;
    state.events = [];
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
