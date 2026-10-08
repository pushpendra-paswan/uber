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

const session = getSession();
const state = {
  user: session ? session.user : null,
  loaded: false, // true after the first successful refresh, so the request form does not flash before the ride shows
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
const requestSection = document.getElementById("request-section");
const requestForm = document.getElementById("request-form");
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

async function login(email, password) {
  const data = await api("POST", "/auth/login", { email, password });
  saveSession(data.access_token, data.user);
  state.user = data.user;
}

async function refresh() {
  if (!state.user || state.user.role !== "rider") return;
  try {
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
  const isRider = loggedIn && state.user.role === "rider";
  const showRide = isRider && state.ride !== null;

  message.hidden = state.error === "";
  message.textContent = state.error;

  loginSection.hidden = loggedIn;
  userBar.hidden = !loggedIn;
  userName.textContent = loggedIn ? state.user.name : "";
  wrongRoleSection.hidden = !loggedIn || isRider;
  if (loggedIn) wrongRoleText.textContent = `This account is a ${state.user.role}. Open /${state.user.role}/ instead.`;

  requestSection.hidden = !isRider || !state.loaded || showRide;
  rideSection.hidden = !showRide;

  if (showRide) {
    rideId.textContent = state.ride.id;
    rideStatus.textContent = state.ride.status;
    rideStatusText.textContent = STATUS_TEXT[state.ride.status];
    ridePickup.textContent = state.ride.pickup_address;
    rideDropoff.textContent = state.ride.dropoff_address;
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

requestForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const form = new FormData(requestForm);
  act(() =>
    api("POST", "/rides", {
      pickup_address: form.get("pickup_address"),
      pickup_lat: Number(form.get("pickup_lat")),
      pickup_lng: Number(form.get("pickup_lng")),
      dropoff_address: form.get("dropoff_address"),
      dropoff_lat: Number(form.get("dropoff_lat")),
      dropoff_lng: Number(form.get("dropoff_lng")),
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
