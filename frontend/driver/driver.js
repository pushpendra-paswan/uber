import { api, clearSession, getSession, saveSession } from "/shared/api.js";
import { connect, disconnect, reconnectNow } from "/shared/ws.js";

const STATUS_TEXT = {
  REQUESTED: "Waiting for a driver to be assigned",
  DRIVER_ASSIGNED: "Drive to the pickup point",
  DRIVER_ARRIVED: "Ask the rider for their trip code",
  IN_PROGRESS: "Take the rider to the drop-off",
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
const COUNTDOWN_MS = 250;
// Shown when an offer closes without the driver answering it. Accepted and rejected need no message.
const CLOSED_NOTICE = {
  expired: "The offer expired.",
  ride_cancelled: "The rider cancelled the request.",
  driver_offline: "The offer was withdrawn because you went offline.",
};
const CLOSE_REPLACED = 4409; // ws.js reports it as "closed": this account has too many tabs
const STILL_ARRIVE = "New offers still arrive within a few seconds.";
const LIVE_TEXT = {
  connecting: "Live updates: connecting...",
  open: "Live updates: connected",
  paused: `Live updates paused: this account is open in too many tabs. ${STILL_ARRIVE}`,
};
const TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
const MAX_ZOOM = 19;
const MIN_ZOOM = 10;
const MARKER_COLOR = "#1a56db";
const RIDE_KINDS = ["pickup", "dropoff"];
// Leaflet renders tooltips as HTML, so they only ever get these fixed strings, never an address.
const RIDE_MARKER_LABEL = { pickup: "Pickup", dropoff: "Drop-off" };
const RIDE_MARKER_COLOR = { pickup: "#1a7f37", dropoff: "#b42318" };
const FIT_PADDING = [40, 40];
const ENTRIES_PAGE = 10; // earning entries shown at first, and added by each "Show more"
const money = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" }); // money.format(paise / 100)

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
  rideMarkers: { pickup: null, dropoff: null },
  fittedKey: null, // "ride:7" or "offer:12": the map is fitted to each once, never on every poll
  ride: null,
  rideId: null, // remembered so a finished ride can still be shown after /rides/active returns 404
  events: [],
  offer: null, // from GET /drivers/me/offer
  deadline: 0, // performance.now() value at which the offer ends, worked out once when the offer is first seen
  offerTotal: 0, // whole seconds the offer had when first seen, the maximum of the progress bar
  secondsLeft: 0,
  notice: "", // why an offer went away, cleared by the next action or the next offer
  earnings: null, // {summary, entries, hasMore} from GET /drivers/me/earnings and /entries; null hides the section
  earningsPeriod: "today", // "today", "week" or "all", written only by the period buttons' handlers
  earningsSince: null, // the since (ISO) that the shown earnings were asked for, so "Show more" pages the same window
  earningsKey: null, // "start" or "ride:<id>": what the earnings were last loaded for; set before the call so a failure is not retried every poll
  socketStatus: "connecting", // "connecting", "open", "reconnecting", "closed", as reported by ws.js
  socketInfo: null, // the info that came with the status
  error: "",
  busy: false,
};
let refreshing = false;
let countdownTimer = null; // setInterval id while an offer is showing

const message = document.getElementById("message");
const noticeText = document.getElementById("notice");
const liveStatus = document.getElementById("live-status");
const reconnectButton = document.getElementById("reconnect-button");
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
const offerSection = document.getElementById("offer-section");
const offerPickup = document.getElementById("offer-pickup");
const offerDropoff = document.getElementById("offer-dropoff");
const offerPickupDistance = document.getElementById("offer-pickup-distance");
const offerTrip = document.getElementById("offer-trip");
const offerFare = document.getElementById("offer-fare");
const offerCountdown = document.getElementById("offer-countdown");
const offerProgress = document.getElementById("offer-progress");
const acceptButton = document.getElementById("accept-button");
const rejectButton = document.getElementById("reject-button");
const noRideSection = document.getElementById("no-ride-section");
const rideSection = document.getElementById("ride-section");
const rideId = document.getElementById("ride-id");
const rideStatus = document.getElementById("ride-status");
const rideStatusText = document.getElementById("ride-status-text");
const rideNote = document.getElementById("ride-note");
const rideFare = document.getElementById("ride-fare");
const rideEarning = document.getElementById("ride-earning");
const ridePickup = document.getElementById("ride-pickup");
const rideDropoff = document.getElementById("ride-dropoff");
const arriveButton = document.getElementById("arrive-button");
const startForm = document.getElementById("start-form");
const codeInput = document.getElementById("trip-code-input");
const completeButton = document.getElementById("complete-button");
const cancelButton = document.getElementById("cancel-button");
const doneButton = document.getElementById("done-button");
const eventsBody = document.getElementById("events");
const earningsSection = document.getElementById("earnings-section");
const periodButtons = document.querySelectorAll("[data-period]");
const earningsRefreshButton = document.getElementById("earnings-refresh-button");
const earningsTrips = document.getElementById("earnings-trips");
const earningsFees = document.getElementById("earnings-fees");
const earningsGross = document.getElementById("earnings-gross");
const earningsPlatformFee = document.getElementById("earnings-platform-fee");
const earningsDriverEarning = document.getElementById("earnings-driver-earning");
const earningsCash = document.getElementById("earnings-cash");
const earningsWallet = document.getElementById("earnings-wallet");
const earningsBalance = document.getElementById("earnings-balance");
const earningsList = document.getElementById("earnings-list");
const earningsMoreButton = document.getElementById("earnings-more-button");

async function login(email, password) {
  const data = await api("POST", "/auth/login", { email, password });
  saveSession(data.access_token, data.user);
  state.user = data.user;
  if (state.user.role === "driver") connect(socketHandlers);
}

// Events only say "something changed": the page reacts by asking the REST API, which is the source of truth.
// A missed event is harmless, because the poll below catches up within 3 seconds.
const socketHandlers = {
  onEvent: (type, data) => {
    if (type === "offer_closed") {
      // An event about an older offer must not put its message on top of a newer one.
      if (state.offer === null || state.offer.id === data.offer_id) state.notice = CLOSED_NOTICE[data.reason] || "";
      refresh().then(render);
    } else if (type === "offer_created") {
      refresh().then(render);
    } else if (type === "ride_updated") {
      // A driver only receives these for their own ride.
      refresh().then(render);
    }
  },
  onStatus: (status, info) => {
    state.socketStatus = status;
    state.socketInfo = info;
    // Events published while the socket was down are lost: an offer or ride that arrived meanwhile shows now.
    if (status === "open" && info.reconnected) refresh().then(render);
    render();
  },
};

// GET that returns null on 404 (no profile yet, no active ride) and throws on anything else.
async function getOrNull(path) {
  try {
    return await api("GET", path);
  } catch (err) {
    if (err.status === 404) return null;
    throw err;
  }
}

// The summary and the latest entries of the selected period. The window comes from the browser's local midnight, so the
// server needs no timezone. null from the server (no driver profile yet) hides the section.
async function loadEarnings() {
  let since = null;
  if (state.earningsPeriod !== "all") {
    const start = new Date();
    start.setHours(0, 0, 0, 0);
    if (state.earningsPeriod === "week") start.setDate(start.getDate() - 6);
    since = start.toISOString();
  }
  const sinceQuery = since === null ? "" : `since=${encodeURIComponent(since)}`;
  const summary = await getOrNull(`/drivers/me/earnings?${sinceQuery}`);
  if (summary === null) {
    state.earnings = null;
    return;
  }
  const entries = await api("GET", `/drivers/me/earnings/entries?limit=${ENTRIES_PAGE}&${sinceQuery}`);
  state.earningsSince = since;
  state.earnings = { summary, entries, hasMore: entries.length === ENTRIES_PAGE };
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

      // The safety net behind the socket: an offer is also found by polling. A driver on a ride has none.
      const offer = active ? null : await getOrNull("/drivers/me/offer");
      if (offer === null) {
        state.offer = null;
      } else if (state.offer === null || state.offer.id !== offer.id) {
        // The deadline is set once, from the first sighting, so later polls of the same offer cannot restart the clock.
        state.offer = offer;
        state.deadline = performance.now() + offer.expires_in * 1000;
        state.offerTotal = Math.ceil(offer.expires_in);
        state.secondsLeft = state.offerTotal;
        state.notice = "";
      }
    } else {
      state.presence = null;
      state.offer = null;
    }
    state.loaded = true;

    // Earnings change only when a ride is settled, so they are loaded once at the start and once when a ride finishes,
    // never on every poll. Last, so a failure here cannot stop the offer and ride checks above.
    if (state.driver !== null) {
      const key = state.ride !== null && FINISHED.includes(state.ride.status) ? `ride:${state.ride.id}` : state.earningsKey || "start";
      if (state.earningsKey !== key) {
        state.earningsKey = key;
        await loadEarnings();
      }
    }
  } catch (err) {
    state.error = err.message;
  }

  if (state.offer !== null && countdownTimer === null) countdownTimer = setInterval(tickCountdown, COUNTDOWN_MS);
  if (state.offer === null && countdownTimer !== null) {
    clearInterval(countdownTimer);
    countdownTimer = null;
  }
}

// Works from the clock, not from counting ticks, so a background tab shows the right value when you return.
function tickCountdown() {
  if (state.offer === null) return;
  state.secondsLeft = Math.max(0, Math.ceil((state.deadline - performance.now()) / 1000));
  if (state.secondsLeft === 0) {
    state.offer = null;
    state.notice = "The offer expired.";
    clearInterval(countdownTimer);
    countdownTimer = null;
    refresh().then(render);
  }
  render();
}

async function act(fn) {
  state.error = "";
  state.notice = "";
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
  const showOffer = approved && state.offer !== null && !hasActiveRide;

  message.hidden = state.error === "";
  message.textContent = state.error;
  noticeText.hidden = state.notice === "";
  noticeText.textContent = state.notice;
  liveStatus.hidden = !isDriver;
  const { socketStatus: status, socketInfo: info } = state;
  if (status === "reconnecting") {
    liveStatus.textContent = `Live updates: connection lost, reconnecting (attempt ${info.attempt}). ${STILL_ARRIVE}`;
  } else if (status === "closed") {
    liveStatus.textContent = info.code === CLOSE_REPLACED ? LIVE_TEXT.paused : `Live updates stopped (code ${info.code}). ${STILL_ARRIVE}`;
  } else {
    liveStatus.textContent = LIVE_TEXT[status];
  }
  reconnectButton.hidden = !isDriver || status !== "closed";

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
  noRideSection.hidden = !approved || showRide || showOffer;
  rideSection.hidden = !showRide;
  offerSection.hidden = !showOffer;

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

    // Pickup and drop-off of the active ride, or of the offer being considered (the field names are the same).
    // Not interactive, so a click on them still moves the driver.
    const shown = hasActiveRide ? state.ride : state.offer;
    const wanted = shown
      ? {
          pickup: { lat: shown.pickup_lat, lng: shown.pickup_lng },
          dropoff: { lat: shown.dropoff_lat, lng: shown.dropoff_lng },
        }
      : { pickup: null, dropoff: null };
    for (const kind of RIDE_KINDS) {
      const point = wanted[kind];
      const rideMarker = state.rideMarkers[kind];
      if (point === null && rideMarker !== null) {
        rideMarker.remove();
        state.rideMarkers[kind] = null;
      } else if (point !== null && rideMarker === null) {
        state.rideMarkers[kind] = L.circleMarker([point.lat, point.lng], {
          radius: 9,
          color: RIDE_MARKER_COLOR[kind],
          fillColor: RIDE_MARKER_COLOR[kind],
          fillOpacity: 1,
          interactive: false,
        })
          .bindTooltip(RIDE_MARKER_LABEL[kind], { permanent: true, direction: "top", offset: [0, -9] })
          .addTo(state.map)
          .bringToBack(); // below the driver's own marker
      } else if (point !== null) {
        rideMarker.setLatLng([point.lat, point.lng]);
      }
    }
    const fitKey = shown ? `${hasActiveRide ? "ride" : "offer"}:${shown.id}` : null;
    if (fitKey !== null && state.fittedKey !== fitKey) {
      const points = [[shown.pickup_lat, shown.pickup_lng], [shown.dropoff_lat, shown.dropoff_lng]];
      if (state.position !== null) points.push([state.position.lat, state.position.lng]);
      state.map.fitBounds(points, { padding: FIT_PADDING, animate: false });
      state.fittedKey = fitKey;
    }

    presenceStatus.textContent = online
      ? `Online, last update ${new Date(state.presence.updated_at * 1000).toLocaleTimeString()}`
      : "Offline";
    onlineButton.hidden = online;
    offlineButton.hidden = !online;
    offlineNote.hidden = !online || !hasActiveRide;
  }

  if (showOffer) {
    const offer = state.offer;
    offerPickup.textContent = offer.pickup_address;
    offerDropoff.textContent = offer.dropoff_address;
    offerPickupDistance.textContent = `Pickup is ${(offer.pickup_distance_m / 1000).toFixed(1)} km from you`;
    offerTrip.textContent =
      offer.trip_distance_m === null
        ? "Trip: -"
        : `Trip: ${(offer.trip_distance_m / 1000).toFixed(1)} km, ${Math.max(1, Math.round(offer.trip_duration_s / 60))} min`;
    offerFare.textContent = offer.fare_estimate === null ? "Fare: -" : `Fare: ${money.format(offer.fare_estimate / 100)}`;
    offerCountdown.textContent = `Respond within ${state.secondsLeft} second${state.secondsLeft === 1 ? "" : "s"}`;
    offerProgress.max = state.offerTotal;
    offerProgress.value = state.secondsLeft;
  }

  if (showRide) {
    const status = state.ride.status;
    rideId.textContent = state.ride.id;
    rideStatus.textContent = status;
    rideStatusText.textContent = STATUS_TEXT[status];

    // Who cancelled comes from the last CANCELLED event: the actor is the driver (this user), the rider, or the system.
    const cancelled = status === "CANCELLED" ? state.events.findLast((event) => event.to_status === "CANCELLED") : null;
    // Rides finished before the fare existed have no breakdown, or only a legacy marker.
    const breakdown = state.ride.fare_breakdown;
    const settled = state.ride.final_fare !== null && breakdown !== null && breakdown.kind !== "legacy";
    // Who pays the driver: cash is collected from the rider, a wallet ride was already paid from the rider's wallet.
    const paidText = !settled ? "" : state.ride.payment_method === "wallet" ? "Paid from the rider's wallet." : `Collect ${money.format(state.ride.final_fare / 100)} in cash from the rider.`;
    let note = "";
    if (cancelled) {
      if (cancelled.actor_user_id === getSession().user.id) note = settled ? "You cancelled this ride. The rider was not charged." : "You cancelled this ride.";
      else if (cancelled.actor_user_id === null) note = "Ride cancelled.";
      else if (!settled) note = "The rider cancelled this ride.";
      else if (breakdown.fee > 0) note = `The rider cancelled and was charged a cancellation fee of ${money.format(breakdown.fee / 100)}. ${paidText}`;
      else note = "The rider cancelled. No fee was charged.";
    }
    rideNote.hidden = note === "";
    rideNote.textContent = note;
    const showFare = settled && status === "COMPLETED";
    rideFare.hidden = !showFare;
    if (showFare) {
      rideFare.textContent = `Trip fare: ${money.format(state.ride.final_fare / 100)} (${(breakdown.distance_m / 1000).toFixed(1)} km, ${Math.max(1, Math.round(breakdown.duration_s / 60))} min). ${paidText}`;
    }
    // The earning row of this ride, once the earnings list has it (a ride with nothing to pay has none).
    const earning = state.earnings === null ? undefined : state.earnings.entries.find((entry) => entry.ride_id === state.ride.id);
    rideEarning.hidden = earning === undefined;
    if (earning !== undefined) {
      rideEarning.textContent = `You earn ${money.format(earning.driver_earning / 100)} (${money.format(earning.gross_amount / 100)} fare minus ${money.format(earning.platform_fee / 100)} platform fee).`;
    }
    ridePickup.textContent = state.ride.pickup_address;
    rideDropoff.textContent = state.ride.dropoff_address;
    arriveButton.hidden = status !== "DRIVER_ASSIGNED";
    startForm.hidden = status !== "DRIVER_ARRIVED"; // only shown or hidden: the typed code is never touched here
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

  // Only textContent below: addresses are plain text.
  earningsSection.hidden = !isDriver || state.earnings === null;
  for (const button of periodButtons) button.setAttribute("aria-pressed", String(button.dataset.period === state.earningsPeriod));
  if (!earningsSection.hidden) {
    const { summary, entries, hasMore } = state.earnings;
    earningsTrips.textContent = summary.trips;
    earningsFees.textContent = summary.cancellation_fees;
    earningsGross.textContent = money.format(summary.total.gross / 100);
    earningsPlatformFee.textContent = money.format(summary.total.platform_fee / 100);
    earningsDriverEarning.textContent = money.format(summary.total.driver_earning / 100);
    earningsCash.textContent = `Cash rides (${summary.cash.rides}): you collected ${money.format(summary.cash.gross / 100)} in cash and owe the platform ${money.format(summary.cash.platform_fee / 100)} commission.`;
    earningsWallet.textContent = `Wallet rides (${summary.wallet.rides}): ${money.format(summary.wallet.driver_earning / 100)} will be paid to you.`;
    const net = summary.settlement.net;
    const owed = net > 0 ? `the platform owes you ${money.format(net / 100)}` : net < 0 ? `you owe the platform ${money.format(-net / 100)}` : "nothing owed";
    earningsBalance.textContent = `Balance with the platform: ${owed}`;
    earningsList.replaceChildren(
      ...entries.map((entry) => {
        const row = document.createElement("li");
        const what = entry.kind === "trip" ? "Trip" : "Cancellation fee";
        row.textContent =
          `${new Date(entry.created_at).toLocaleString()}: ${what}, ${entry.pickup_address} \u2192 ${entry.dropoff_address}. ` +
          `Fare ${money.format(entry.gross_amount / 100)}, platform fee ${money.format(entry.platform_fee / 100)} (${entry.commission_percent}%), ` +
          `you earn ${money.format(entry.driver_earning / 100)} (${entry.payment_method}).`;
        return row;
      })
    );
    earningsMoreButton.hidden = !hasMore;
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
  disconnect();
  clearInterval(countdownTimer);
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

reconnectButton.addEventListener("click", () => act(() => reconnectNow()));
acceptButton.addEventListener("click", () => {
  codeInput.value = ""; // nothing typed for an earlier ride is left over for this one
  act(() => api("POST", `/offers/${state.offer.id}/accept`));
});
rejectButton.addEventListener("click", () => act(() => api("POST", `/offers/${state.offer.id}/reject`)));

arriveButton.addEventListener("click", () => act(() => api("POST", `/rides/${state.ride.id}/arrive`)));

// The code input is written only by handlers: cleared after a successful start, left alone after an error so the
// driver can fix it. Polling calls render(), which never touches it.
startForm.addEventListener("submit", (event) => {
  event.preventDefault();
  act(async () => {
    await api("POST", `/rides/${state.ride.id}/start`, { otp: codeInput.value });
    codeInput.value = "";
  });
});

completeButton.addEventListener("click", () => act(() => api("POST", `/rides/${state.ride.id}/complete`)));

cancelButton.addEventListener("click", () => {
  if (!confirm("Cancel this ride?")) return;
  codeInput.value = "";
  act(() => api("POST", `/rides/${state.ride.id}/cancel`));
});

doneButton.addEventListener("click", () => {
  codeInput.value = "";
  act(async () => {
    state.ride = null;
    state.rideId = null;
    state.events = [];
  });
});

// The period and the entries are written only here: render() shows them and never changes them.
for (const button of periodButtons) {
  button.addEventListener("click", () => {
    state.earningsPeriod = button.dataset.period;
    act(loadEarnings);
  });
}

earningsRefreshButton.addEventListener("click", () => act(loadEarnings));

earningsMoreButton.addEventListener("click", () => {
  act(async () => {
    const entries = state.earnings.entries;
    const sinceQuery = state.earningsSince === null ? "" : `&since=${encodeURIComponent(state.earningsSince)}`;
    const more = await api("GET", `/drivers/me/earnings/entries?limit=${ENTRIES_PAGE}&before_id=${entries[entries.length - 1].id}${sinceQuery}`);
    state.earnings = { ...state.earnings, entries: [...entries, ...more], hasMore: more.length === ENTRIES_PAGE };
  });
});

// The poll stays even when the socket works: it is the safety net for events that were missed.
setInterval(async () => {
  if (refreshing || state.busy) return;
  refreshing = true;
  await refresh();
  refreshing = false;
  render();
}, POLL_MS);

if (state.user !== null && state.user.role === "driver") connect(socketHandlers);
render();
refresh().then(render);
