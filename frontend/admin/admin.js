import { api, clearSession, getSession, saveSession } from "/shared/api.js";

// This file stays ONE long file on purpose (approved for M6.2): it is grouped by tab with the section comments below.
// Everything the API sends is shown with textContent, so nothing from the API is ever parsed as markup. Leaflet never receives an API string:
// tooltips are fixed words plus an integer id. The URL hash is parsed with one strict regex.

// ---------------------------------------------------------------- constants

const POLL_MS = 3000;
const HASH_PATTERN = /^#(overview|live|drivers|rides|pricing)(?:\/(\d{1,9}))?$/;
const NO_DATA = "no data";
const SVG_NS = "http://www.w3.org/2000/svg";

const DRIVER_STATE_TEXT = { offline: "offline", free: "free", offered: "deciding on an offer", on_ride: "on a ride" };
const DRIVER_COLOR = { free: "#1a7f37", offered: "#d97706", on_ride: "#1a56db" };
const RIDE_STATUS_TEXT = {
  REQUESTED: "looking for a driver",
  DRIVER_ASSIGNED: "driver assigned",
  DRIVER_ARRIVED: "driver arrived",
  IN_PROGRESS: "trip in progress",
  COMPLETED: "completed",
  CANCELLED: "cancelled",
  NO_DRIVER_FOUND: "no driver found",
};
const RIDE_COLOR = { REQUESTED: "#ea580c", DRIVER_ASSIGNED: "#7c3aed", DRIVER_ARRIVED: "#7c3aed", IN_PROGRESS: "#dc2626" };
const FINISHED = ["COMPLETED", "CANCELLED", "NO_DRIVER_FOUND"];
const OFFER_STATUS_TEXT = { PENDING: "waiting", ACCEPTED: "accepted", REJECTED: "rejected", EXPIRED: "ran out of time", CANCELLED: "withdrawn" };
const ACTOR_TEXT = { system: "the system", rider: "the rider", driver: "the driver", other: "someone else (an admin)" };
const REASON_TEXT = {
  no_driver_yet: "no driver had been assigned yet",
  within_free_window: "within the free cancellation window",
  late_cancellation: "after the free cancellation window",
  driver_arrived: "the driver had already arrived",
  driver_cancelled: "the driver cancelled",
};
const BREAKDOWN_LABEL = {
  kind: "Kind", distance_m: "Distance billed (m)", duration_s: "Duration billed (s)", distance_source: "Distance source",
  fallback_reason: "Fallback reason", tracked_pings: "Tracked pings", jumps_ignored: "Jumps ignored", base_fare: "Base fare",
  distance_fare: "Distance fare", time_fare: "Time fare", minimum_fare_applied: "Minimum fare applied", computed_fare: "Computed fare",
  normal_fare: "Normal fare", surge_percent: "Surge (percent)", surge_amount: "Surge amount", fare_cap: "Fare cap", capped: "Capped",
  fee: "Fee", reason: "Reason", cancelled_by: "Cancelled by",
};
const FIELD_LABEL = {
  base_fare: "base fare", per_km: "per kilometre", per_min: "per minute", min_fare: "minimum fare", cancellation_fee: "cancellation fee",
  free_cancel_seconds: "free cancellation window (seconds)", commission_percent: "commission (percent)", surge_cap: "surge cap (×)",
};
const PRICING_FIELDS = Object.keys(FIELD_LABEL);
const MONEY_FIELDS = ["base_fare", "per_km", "per_min", "min_fare", "cancellation_fee"];

const DRIVERS_PAGE = 50;
const DRIVERS_CAP = 200; // the poll asks for min(this, rows loaded), and "Load more" stops here
const RIDES_PAGE = 25;
const SURGE_ROWS = 10;
const HISTORY_ROWS = 10;
const TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
const MAX_ZOOM = 19;
const MIN_ZOOM = 10;
const FIT_PADDING = [40, 40];
const FIT_MAX_ZOOM = 15; // fitting to a single dot must not zoom in to street level
const money = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" }); // money.format(paise / 100)

// ---------------------------------------------------------------- state

const session = getSession();
const state = {
  user: session ? session.user : null,
  tab: "overview",
  error: "", // the message of the last action
  pollError: "", // the message of the last failed poll; the last data stays on screen
  busy: false,
  tick: 0,
  // overview
  period: "today", // today, week or month; written only by the period buttons
  stats: null,
  // live map
  config: null, // from GET /places/map-config
  live: null,
  liveAt: "",
  surge: null,
  selected: null, // {kind: "driver" | "ride", id}
  showDrivers: true,
  showRides: true,
  fitted: false,
  // drivers
  drivers: [],
  driversLoaded: false,
  driversMore: false,
  driverFilter: "",
  driverQuery: "",
  driverDetailId: null,
  driverDetailKey: "", // the id the detail was loaded for, set BEFORE the calls so a failure is not retried every poll
  driverDetail: null, // {missing: true} or {driver, earnings, rides, ratings}
  // rides
  rides: [],
  ridesLoaded: false,
  ridesMore: false,
  ridesStale: true, // the list is (re)loaded only on the first visit, Search and Refresh
  rideFilter: { status: "", rider_id: "", driver_id: "", period: "all" },
  rideDetailId: null,
  rideDetail: null,
  rideMissing: false,
  // pricing
  pricing: null, // the latest answer of GET /admin/pricing-rules
  history: [],
  form: null, // {version, rule}: what the form was loaded with; only handlers change it
  formStale: false,
  saved: "",
};

let loading = false;
const view = {
  map: null,
  drivers: { layer: null, markers: new Map() },
  rides: { layer: null, markers: new Map() },
  overlay: [], // the drop-off marker and dashed line of the selected ride
  overlayRide: null,
};
const rendered = {}; // JSON of what was last drawn in a region, so a poll does not replace a button between mouse down and up

// Every element by its id.
const $ = Object.fromEntries([...document.querySelectorAll("[id]")].map((element) => [element.id, element]));

const hash = HASH_PATTERN.exec(location.hash);
if (hash) {
  state.tab = hash[1];
  if (hash[2] !== undefined && state.tab === "drivers") state.driverDetailId = Number(hash[2]);
  if (hash[2] !== undefined && state.tab === "rides") state.rideDetailId = Number(hash[2]);
}

// ---------------------------------------------------------------- helpers (the only ones)

function paiseToRupees(paise) {
  return (paise / 100).toFixed(2);
}

// "12.5" -> 1250, "12" -> 1200, anything else -> null. Parsed by splitting the text, never with floats.
function rupeesToPaise(text) {
  const trimmed = text.trim();
  if (!/^\d+(\.\d{1,2})?$/.test(trimmed)) return null;
  const [rupees, fraction = ""] = trimmed.split(".");
  return Number(rupees) * 100 + Number(fraction.padEnd(2, "0"));
}

// "5.2 km, 14 min"
function formatTrip(distanceM, durationS) {
  if (distanceM === null || durationS === null) return "-";
  return `${(distanceM / 1000).toFixed(1)} km, ${Math.round(durationS / 60)} min`;
}

// Bars drawn as inline SVG. labels[i] is [short axis text, hover text]; values[i] is [full bar, optional darker bar inside it].
function drawBars(svg, labels, values, title) {
  const width = 360;
  const height = 200;
  const left = 4;
  const top = 18;
  const bottom = 26;
  svg.replaceChildren();
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", title);
  const max = Math.max(1, ...values.map((pair) => pair[0]));
  const slot = (width - left * 2) / values.length;
  const plotHeight = height - top - bottom;

  const maxLabel = document.createElementNS(SVG_NS, "text");
  maxLabel.setAttribute("x", left);
  maxLabel.setAttribute("y", 11);
  maxLabel.setAttribute("class", "chart-text");
  maxLabel.textContent = `max ${Number.isInteger(max) ? max : max.toFixed(2)}`;
  svg.append(maxLabel);

  values.forEach((pair, index) => {
    const group = document.createElementNS(SVG_NS, "g");
    pair.forEach((amount, layer) => {
      const bar = document.createElementNS(SVG_NS, "rect");
      const barHeight = (amount / max) * plotHeight;
      bar.setAttribute("x", left + index * slot + slot * 0.1);
      bar.setAttribute("y", top + plotHeight - barHeight);
      bar.setAttribute("width", slot * 0.8);
      bar.setAttribute("height", barHeight);
      bar.setAttribute("class", layer === 0 ? "bar" : "bar-inner");
      group.append(bar);
    });
    const hover = document.createElementNS(SVG_NS, "title");
    hover.textContent = labels[index][1];
    group.append(hover);
    svg.append(group);
  });

  const axis = document.createElementNS(SVG_NS, "line");
  axis.setAttribute("x1", left);
  axis.setAttribute("x2", width - left);
  axis.setAttribute("y1", top + plotHeight);
  axis.setAttribute("y2", top + plotHeight);
  axis.setAttribute("class", "chart-axis");
  svg.append(axis);

  for (const [index, anchor, x] of [[0, "start", left], [Math.floor((values.length - 1) / 2), "middle", width / 2], [values.length - 1, "end", width - left]]) {
    const text = document.createElementNS(SVG_NS, "text");
    text.setAttribute("x", x);
    text.setAttribute("y", height - 8);
    text.setAttribute("text-anchor", anchor);
    text.setAttribute("class", "chart-text");
    text.textContent = labels[index][0];
    svg.append(text);
  }
}

// Adds, moves and removes the markers of one store ({layer, markers}) so that they match `items` (each has an integer id).
// Polling moves the markers that exist; it never recreates them.
function syncMarkers(store, items, create, update) {
  const present = new Set();
  for (const item of items) {
    present.add(item.id);
    const marker = store.markers.get(item.id);
    if (marker === undefined) store.markers.set(item.id, create(item));
    else update(marker, item);
  }
  for (const [id, marker] of store.markers) {
    if (!present.has(id)) {
      store.layer.removeLayer(marker);
      store.markers.delete(id);
    }
  }
}

// Runs an action, then reloads the current tab (unless reload is false) and redraws. A failure shows its message.
async function act(fn, reload = true) {
  state.error = "";
  state.busy = true;
  render();
  try {
    await fn();
  } catch (err) {
    state.error = err.message;
  }
  if (reload) await loadTab();
  state.busy = false;
  render();
}

// Fetches the data of the active tab. A poll (polling = true) fetches less: see the tab comments. A failed load keeps the last data.
async function loadTab(polling = false) {
  if (!state.user || state.user.role !== "admin") return;
  loading = true;
  try {
    // ---- overview: the stats of the chosen period (a poll only on every third tick)
    if (state.tab === "overview" && (!polling || state.tick % 3 === 0)) {
      const period = state.period;
      const now = new Date();
      const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()); // local midnight
      start.setDate(start.getDate() - (period === "today" ? 0 : period === "week" ? 6 : 29));
      const query = new URLSearchParams({
        since: start.toISOString(),
        until: now.toISOString(),
        bucket: period === "today" ? "hour" : "day",
        utc_offset_minutes: String(-now.getTimezoneOffset()),
      });
      const stats = await api("GET", `/admin/stats?${query}`);
      if (state.period === period) state.stats = stats; // a slow answer for an old period is dropped
    }

    // ---- live map: every poll; the surge zones every third tick
    if (state.tab === "live") {
      if (state.config === null) state.config = await api("GET", "/places/map-config");
      state.live = await api("GET", "/admin/live");
      state.liveAt = new Date().toLocaleTimeString();
      if (!polling || state.tick % 3 === 0 || state.surge === null) state.surge = await api("GET", "/admin/surge");
    }

    // ---- drivers: every poll, from the start, as many rows as are loaded (at most 200)
    if (state.tab === "drivers") {
      const limit = Math.min(DRIVERS_CAP, Math.max(DRIVERS_PAGE, state.drivers.length));
      const query = new URLSearchParams({ limit: String(limit) });
      if (state.driverFilter) query.set("status", state.driverFilter);
      if (state.driverQuery) query.set("q", state.driverQuery);
      const rows = await api("GET", `/admin/drivers?${query}`);
      state.drivers = rows;
      state.driversLoaded = true;
      if (!polling) state.driversMore = rows.length === limit;
      else if (rows.length < limit) state.driversMore = false;

      if (state.driverDetailId !== null && state.driverDetailKey !== String(state.driverDetailId)) {
        const id = state.driverDetailId;
        state.driverDetailKey = String(id);
        // The driver ids are ascending, so the first driver after id - 1 is the one asked for when it exists.
        const found = await api("GET", `/admin/drivers?after_id=${id - 1}&limit=1`);
        if (found.length === 1 && found[0].id === id) {
          state.driverDetail = {
            driver: found[0],
            earnings: await api("GET", `/admin/drivers/${id}/earnings`),
            rides: await api("GET", `/admin/rides?driver_id=${id}&limit=5`),
            ratings: await api("GET", `/admin/ratings?user_id=${found[0].user_id}&limit=5`),
          };
        } else {
          state.driverDetail = { missing: true };
        }
      }
    }

    // ---- rides: the list on the first visit, Search and Refresh only; an open ride that is not finished on every poll
    if (state.tab === "rides") {
      if (!polling && state.ridesStale) {
        state.ridesStale = false;
        const query = new URLSearchParams({ limit: String(RIDES_PAGE) });
        const filter = state.rideFilter;
        if (filter.status) query.set("status", filter.status);
        if (filter.rider_id) query.set("rider_id", filter.rider_id);
        if (filter.driver_id) query.set("driver_id", filter.driver_id);
        if (filter.period !== "all") {
          const since = new Date();
          since.setHours(0, 0, 0, 0);
          since.setDate(since.getDate() - (filter.period === "week" ? 6 : 0));
          query.set("since", since.toISOString());
        }
        state.rides = await api("GET", `/admin/rides?${query}`);
        state.ridesLoaded = true;
        state.ridesMore = state.rides.length === RIDES_PAGE;
      }
      const open = state.rideDetailId !== null && (!polling || (state.rideDetail !== null && !FINISHED.includes(state.rideDetail.status)));
      if (open) {
        try {
          state.rideDetail = await api("GET", `/admin/rides/${state.rideDetailId}`);
          state.rideMissing = false;
        } catch (err) {
          if (err.status !== 404) throw err;
          state.rideDetail = null;
          state.rideMissing = true;
        }
      }
    }

    // ---- pricing: when the tab opens and after a save; never on a poll, so an edit is never overwritten
    if (state.tab === "pricing" && !polling) {
      state.pricing = await api("GET", "/admin/pricing-rules");
      state.history = await api("GET", `/admin/pricing-rules/${state.pricing.rules[0].vehicle_type}/changes?limit=${HISTORY_ROWS}`);
    }
    state.pollError = "";
  } catch (err) {
    state.pollError = err.message;
  }
  loading = false;
}

// ---------------------------------------------------------------- render (never touches a form input)

function render() {
  const loggedIn = state.user !== null;
  const isAdmin = loggedIn && state.user.role === "admin";

  const text = state.error || state.pollError;
  $.message.hidden = text === "";
  $.message.textContent = text;

  $["login-section"].hidden = loggedIn;
  $["user-bar"].hidden = !loggedIn;
  $["user-name"].textContent = loggedIn ? state.user.name : "";
  $["wrong-role-section"].hidden = !loggedIn || isAdmin;
  if (loggedIn) $["wrong-role-text"].textContent = `This account is a ${state.user.role}. Open /${state.user.role}/ instead.`;

  $.nav.hidden = !isAdmin;
  for (const button of $.nav.querySelectorAll("button")) button.classList.toggle("current", button.dataset.tab === state.tab);
  for (const tab of ["overview", "live", "drivers", "rides", "pricing"]) $[`${tab}-section`].hidden = !isAdmin || state.tab !== tab;
  if (!isAdmin) return;

  let fragment = `#${state.tab}`;
  if (state.tab === "drivers" && state.driverDetailId !== null) fragment += `/${state.driverDetailId}`;
  if (state.tab === "rides" && state.rideDetailId !== null) fragment += `/${state.rideDetailId}`;
  history.replaceState(null, "", fragment);

  // ---- overview
  for (const button of $["period-buttons"].querySelectorAll("button")) button.classList.toggle("current", button.dataset.period === state.period);
  $["period-text"].textContent = {
    today: "Today, per hour (your local time).",
    week: "The last 7 days, per day (your local time).",
    month: "The last 30 days, per day (your local time).",
  }[state.period];
  $["stats-empty"].hidden = state.stats !== null;
  if (state.tab === "overview" && state.stats !== null && JSON.stringify(state.stats) !== rendered.stats) {
    rendered.stats = JSON.stringify(state.stats);
    const s = state.stats;
    const groups = [
      [$["cards-rides"], [
        ["Rides requested", s.rides.requested],
        ["Completed", s.rides.completed],
        ["Cancelled", s.rides.cancelled],
        ["No driver found", s.rides.no_driver_found],
        ["Still active", s.rides.active],
        ["Completion rate", s.rides.completion_rate === null ? NO_DATA : `${s.rides.completion_rate.toFixed(1)}%`],
        ["Cancellation rate", s.rides.cancellation_rate === null ? NO_DATA : `${s.rides.cancellation_rate.toFixed(1)}%`],
        ["No-driver rate", s.rides.no_driver_rate === null ? NO_DATA : `${s.rides.no_driver_rate.toFixed(1)}%`],
        ["Average fare", s.trips.avg_fare === null ? NO_DATA : money.format(s.trips.avg_fare / 100)],
        ["Average distance and time", s.trips.avg_distance_m === null ? NO_DATA : formatTrip(s.trips.avg_distance_m, s.trips.avg_duration_s)],
        ["Median time to assign", s.trips.median_time_to_assign_s === null ? NO_DATA : `${s.trips.median_time_to_assign_s.toFixed(1)} s`],
        ["Mean time to assign", s.trips.mean_time_to_assign_s === null ? NO_DATA : `${s.trips.mean_time_to_assign_s.toFixed(1)} s`],
        ["Offer acceptance rate", s.offers.acceptance_rate === null ? NO_DATA : `${s.offers.acceptance_rate.toFixed(1)}%`],
        ["Surged rides", s.surge.rides_surged],
        ["Highest multiplier", `${(s.surge.max_surge_percent / 100).toFixed(1)}x`],
        ["New riders", s.users.new_riders],
        ["New drivers", s.users.new_drivers],
      ]],
      [$["cards-money"], [
        ["Fares settled (gross)", money.format(s.money.total.gross / 100)],
        ["Platform revenue", money.format(s.money.total.platform_fee / 100)],
        ["Driver earnings", money.format(s.money.total.driver_earning / 100)],
        ["Payments settled", s.money.total.rides],
        ["Paid in cash", s.money.cash.rides],
        ["Paid from a wallet", s.money.wallet.rides],
      ]],
      [$["cards-now"], [
        ["Drivers online", s.now.online_drivers === null ? "unavailable" : s.now.online_drivers],
        ["Looking for a driver", s.now.active_rides.REQUESTED],
        ["Driver assigned", s.now.active_rides.DRIVER_ASSIGNED],
        ["Driver arrived", s.now.active_rides.DRIVER_ARRIVED],
        ["Trips in progress", s.now.active_rides.IN_PROGRESS],
        ["Offers waiting for an answer", s.now.pending_offers],
      ]],
    ];
    for (const [target, cards] of groups) {
      target.replaceChildren(...cards.map(([label, value]) => {
        const card = document.createElement("div");
        const term = document.createElement("dt");
        const description = document.createElement("dd");
        term.textContent = label;
        description.textContent = String(value);
        card.append(term, description);
        return card;
      }));
    }
    const settlement = s.money.settlement;
    $["settlement-line"].textContent =
      `Settlement of these payments: the platform owes drivers ${money.format(settlement.owed_to_driver / 100)} (wallet rides), ` +
      `drivers owe the platform ${money.format(settlement.owed_by_driver / 100)} (cash rides), net ${money.format(settlement.net / 100)} ` +
      `(positive: the platform owes the drivers).`;

    const unit = s.bucket === "hour" ? "hour" : "day";
    const labels = s.series.map((entry) => {
      const date = new Date(entry.start * 1000);
      return s.bucket === "hour"
        ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
        : date.toLocaleDateString([], { month: "short", day: "numeric" });
    });
    $["chart-rides-caption"].textContent = `Rides requested per ${unit} (the darker part completed)`;
    drawBars(
      $["chart-rides"],
      s.series.map((entry, index) => [labels[index], `${labels[index]}: ${entry.rides} requested, ${entry.completed} completed`]),
      s.series.map((entry) => [entry.rides, entry.completed]),
      `Rides requested per ${unit}`,
    );
    $["chart-money-caption"].textContent = `Platform revenue and gross fares per ${unit}, in rupees, by settlement time (the darker part is platform revenue)`;
    drawBars(
      $["chart-money"],
      s.series.map((entry, index) => [labels[index], `${labels[index]}: ${money.format(entry.gross / 100)} gross, ${money.format(entry.platform_fee / 100)} platform revenue`]),
      s.series.map((entry) => [entry.gross / 100, entry.platform_fee / 100]),
      `Platform revenue and gross fares per ${unit}`,
    );
  }

  // ---- live map
  if (state.tab === "live") {
    $["live-time"].textContent = state.liveAt === "" ? "Loading..." : `Showing data from ${state.liveAt}`;
    if (state.live !== null) {
      const live = state.live;
      const c = live.counts.drivers;
      const activeRides = Object.values(live.counts.rides).reduce((sum, n) => sum + n, 0);
      $["live-counts"].textContent = `Drivers online: ${c.online} (free ${c.free}, offered ${c.offered}, on a ride ${c.on_ride}). Active rides: ${activeRides}.`;
      $["live-warning"].hidden = !live.truncated;
      $["live-warning"].textContent =
        `The map is capped: showing ${live.drivers.length} of ${live.drivers_total} online drivers and ${live.rides.length} of ${live.rides_total} active rides.`;
    }

    // The map is created the first time the section is visible: Leaflet cannot measure a hidden container.
    if (view.map === null && state.config !== null) {
      const c = state.config;
      view.map = L.map("map", {
        preferCanvas: true,
        center: [c.center_lat, c.center_lng],
        zoom: c.zoom,
        minZoom: MIN_ZOOM,
        maxZoom: MAX_ZOOM,
        maxBounds: [[c.south, c.west], [c.north, c.east]],
        maxBoundsViscosity: 1.0,
      });
      L.tileLayer(TILE_URL, { attribution: ATTRIBUTION, maxZoom: MAX_ZOOM }).addTo(view.map);
      view.drivers.layer = L.layerGroup().addTo(view.map);
      view.rides.layer = L.layerGroup().addTo(view.map);
      view.map.invalidateSize();
    }

    if (view.map !== null && state.live !== null) {
      syncMarkers(
        view.drivers,
        state.live.drivers,
        (driver) => {
          const marker = L.circleMarker([driver.lat, driver.lng], { radius: 7, weight: 1, color: "#ffffff", fillColor: DRIVER_COLOR[driver.state], fillOpacity: 0.95 }).addTo(view.drivers.layer);
          if (Number.isInteger(driver.id)) marker.bindTooltip(`Driver ${driver.id}`);
          marker.on("click", () => act(async () => { state.selected = { kind: "driver", id: driver.id }; }, false));
          return marker;
        },
        (marker, driver) => {
          marker.setLatLng([driver.lat, driver.lng]);
          marker.setStyle({ fillColor: DRIVER_COLOR[driver.state] });
        },
      );
      syncMarkers(
        view.rides,
        state.live.rides,
        (ride) => {
          const marker = L.circleMarker([ride.pickup_lat, ride.pickup_lng], { radius: 6, weight: 4, color: RIDE_COLOR[ride.status], fillColor: "#ffffff", fillOpacity: 0.8 }).addTo(view.rides.layer);
          if (Number.isInteger(ride.id)) marker.bindTooltip(`Ride ${ride.id}`);
          marker.on("click", () => act(async () => { state.selected = { kind: "ride", id: ride.id }; }, false));
          return marker;
        },
        (marker, ride) => marker.setStyle({ color: RIDE_COLOR[ride.status] }),
      );

      for (const [store, shown] of [[view.drivers, state.showDrivers], [view.rides, state.showRides]]) {
        if (shown && !view.map.hasLayer(store.layer)) store.layer.addTo(view.map);
        if (!shown && view.map.hasLayer(store.layer)) store.layer.remove();
      }

      // The first data fits the map once. After that only the Fit map button moves it.
      if (!state.fitted && (state.live.drivers.length > 0 || state.live.rides.length > 0)) {
        state.fitted = true;
        view.map.fitBounds(
          [...state.live.drivers.map((d) => [d.lat, d.lng]), ...state.live.rides.map((r) => [r.pickup_lat, r.pickup_lng])],
          { padding: FIT_PADDING, animate: false, maxZoom: FIT_MAX_ZOOM },
        );
      }

      // The selected ride: its drop-off and a dashed line from the pickup (numbers only). Redrawn only when the selection changes.
      const selectedRide = state.selected && state.selected.kind === "ride" ? state.live.rides.find((ride) => ride.id === state.selected.id) : undefined;
      const wanted = selectedRide === undefined ? null : selectedRide.id;
      if (view.overlayRide !== wanted) {
        for (const layer of view.overlay) layer.remove();
        view.overlay = [];
        view.overlayRide = wanted;
        if (selectedRide !== undefined) {
          const dropoff = [selectedRide.dropoff_lat, selectedRide.dropoff_lng];
          view.overlay.push(
            L.circleMarker(dropoff, { radius: 6, weight: 2, color: "#111827", fillColor: "#111827", fillOpacity: 1 }).bindTooltip("Drop-off").addTo(view.map),
            L.polyline([[selectedRide.pickup_lat, selectedRide.pickup_lng], dropoff], { color: "#111827", weight: 2, dashArray: "6 6" }).addTo(view.map),
          );
        }
      }
    }

    // The side panel always says the state in words: a colour is never the only signal.
    {
      const rows = [];
      const buttons = [];
      let heading = "Nothing selected. Click a dot on the map.";
      if (state.selected !== null && state.live !== null) {
        if (state.selected.kind === "driver") {
          const driver = state.live.drivers.find((d) => d.id === state.selected.id);
          heading = driver === undefined ? "No longer online" : `Driver ${driver.id}`;
          if (driver !== undefined) {
            rows.push(["Name", driver.name], ["Vehicle plate", driver.plate_number === null ? "-" : driver.plate_number], ["State", `online, ${DRIVER_STATE_TEXT[driver.state]}`]);
            if (driver.active_ride_id !== null) {
              rows.push(["Active ride", driver.active_ride_id]);
              buttons.push(["Open ride", "ride", driver.active_ride_id]);
            }
            buttons.push(["Open driver", "driver", driver.id]);
          }
        } else {
          const ride = state.live.rides.find((r) => r.id === state.selected.id);
          heading = ride === undefined ? "No longer active" : `Ride ${ride.id}`;
          if (ride !== undefined) {
            rows.push(
              ["Status", RIDE_STATUS_TEXT[ride.status]], ["Rider", ride.rider_name], ["Driver", ride.driver_id === null ? "none yet" : `Driver ${ride.driver_id}`],
              ["Pickup", ride.pickup_address], ["Drop-off", ride.dropoff_address],
              ["Fare estimate", ride.fare_estimate === null ? "-" : money.format(ride.fare_estimate / 100)],
            );
            buttons.push(["Open ride", "ride", ride.id]);
          }
        }
      }
      // Redrawn only when what it shows changes (not on every move of a dot), so a poll cannot replace a button under the mouse.
      const panelKey = JSON.stringify([heading, rows, buttons]);
      if (panelKey !== rendered.panel) {
        rendered.panel = panelKey;
        const title = document.createElement("h3");
        title.textContent = heading;
        const list = document.createElement("dl");
        list.className = "fields";
        for (const [label, value] of rows) {
          const term = document.createElement("dt");
          const description = document.createElement("dd");
          term.textContent = label;
          description.textContent = String(value);
          list.append(term, description);
        }
        const actions = buttons.map(([label, kind, id]) => {
          const button = document.createElement("button");
          button.type = "button";
          button.textContent = label;
          button.dataset.open = kind;
          button.dataset.id = id;
          return button;
        });
        $["live-panel"].replaceChildren(title, list, ...actions);
      }
    }

    if (state.surge !== null && JSON.stringify(state.surge) !== rendered.surge) {
      rendered.surge = JSON.stringify(state.surge);
      $["surge-note"].textContent = `Computed ${state.surge.age_seconds} s ago. Showing the ${Math.min(SURGE_ROWS, state.surge.zones.length)} busiest of ${state.surge.zones.length} zones.`;
      $["surge-body"].replaceChildren(...state.surge.zones.slice(0, SURGE_ROWS).map((zone) => {
        const row = document.createElement("tr");
        for (const cell of [zone.zone, zone.demand, zone.supply, `${zone.pressure_percent}%`, `${(zone.surge_percent / 100).toFixed(1)}x`]) {
          const td = document.createElement("td");
          td.textContent = String(cell);
          row.append(td);
        }
        return row;
      }));
    }
  }

  // ---- drivers
  if (state.tab === "drivers") {
    $["no-drivers"].hidden = !state.driversLoaded || state.drivers.length > 0;
    $["drivers-more"].hidden = !state.driversMore;
    $["drivers-more-note"].textContent = state.driversMore && state.drivers.length >= DRIVERS_CAP ? "Showing 200 drivers: narrow the filter to see the others." : "";
    if (JSON.stringify(state.drivers) !== rendered.drivers) {
      rendered.drivers = JSON.stringify(state.drivers);
      $["drivers-body"].replaceChildren(...state.drivers.map((driver) => {
        const row = document.createElement("tr");
        row.dataset.id = driver.id;
        row.className = "clickable";
        const rating = driver.rating_count === 0 ? "no ratings" : `★ ${driver.rating_average.toFixed(1)} (${driver.rating_count})`;
        const cells = [
          driver.id, driver.user.name, driver.user.email, driver.license_number, driver.vehicle ? driver.vehicle.plate_number : "-",
          driver.verification_status, driver.state === "offline" ? "offline" : `${driver.online ? "online" : "offline"}, ${DRIVER_STATE_TEXT[driver.state]}`, rating, driver.completed_trips,
        ];
        for (const cell of cells) {
          const td = document.createElement("td");
          td.textContent = String(cell);
          row.append(td);
        }
        const actions = document.createElement("td");
        for (const [action, label] of [["approve", "Approve"], ["reject", "Reject"]]) {
          const button = document.createElement("button");
          button.type = "button";
          button.textContent = label;
          button.dataset.action = action;
          button.dataset.id = driver.id;
          actions.append(button);
        }
        row.append(actions);
        return row;
      }));
    }

    // The driver detail panel: the fields of the row, the earnings, the last rides and the last ratings.
    const detail = state.driverDetailId === null ? null : state.driverDetail;
    $["driver-detail"].hidden = state.driverDetailId === null;
    $["driver-detail-loading"].hidden = state.driverDetailId === null || detail !== null;
    $["driver-detail-missing"].hidden = !(detail !== null && detail.missing);
    $["driver-detail-body"].hidden = detail === null || detail.missing === true;
    const fresh = detail !== null && !detail.missing ? state.drivers.find((d) => d.id === state.driverDetailId) || detail.driver : null;
    const detailKey = JSON.stringify([detail, fresh]);
    if (detail !== null && !detail.missing && detailKey !== rendered.driverDetail) {
      rendered.driverDetail = detailKey;
      const earnings = detail.earnings;
      const lines = [
        ["Driver", fresh.id], ["Name", fresh.user.name], ["Email", fresh.user.email], ["Phone", fresh.user.phone === null ? "-" : fresh.user.phone],
        ["License", fresh.license_number], ["Vehicle", fresh.vehicle ? `${fresh.vehicle.plate_number}, ${fresh.vehicle.color} ${fresh.vehicle.model}` : "none"],
        ["Verification", fresh.verification_status], ["Now", fresh.state === "offline" ? "offline" : `${fresh.online ? "online" : "offline"}, ${DRIVER_STATE_TEXT[fresh.state]}`],
        ["Rating", fresh.rating_count === 0 ? "no ratings" : `★ ${fresh.rating_average.toFixed(2)} from ${fresh.rating_count} ratings`],
        ["Completed trips", fresh.completed_trips],
      ];
      for (const [target, pairs] of [
        [$["driver-detail-fields"], lines],
        [$["driver-detail-earnings"], [
          ["Trips", earnings.trips], ["Cancellation fees", earnings.cancellation_fees], ["Fares", money.format(earnings.total.gross / 100)],
          ["Platform fee", money.format(earnings.total.platform_fee / 100)], ["Driver earnings", money.format(earnings.total.driver_earning / 100)],
          ["Cash rides", `${earnings.cash.rides}, fares ${money.format(earnings.cash.gross / 100)}`],
          ["Wallet rides", `${earnings.wallet.rides}, fares ${money.format(earnings.wallet.gross / 100)}`],
          ["Settlement", `the platform owes the driver ${money.format(earnings.settlement.owed_to_driver / 100)}, the driver owes the platform ${money.format(earnings.settlement.owed_by_driver / 100)}, net ${money.format(earnings.settlement.net / 100)}`],
        ]],
      ]) {
        target.replaceChildren(...pairs.flatMap(([label, value]) => {
          const term = document.createElement("dt");
          const description = document.createElement("dd");
          term.textContent = label;
          description.textContent = String(value);
          return [term, description];
        }));
      }
      $["driver-detail-rides"].replaceChildren(...detail.rides.map((ride) => {
        const row = document.createElement("tr");
        for (const cell of [ride.id, new Date(ride.created_at).toLocaleString(), RIDE_STATUS_TEXT[ride.status], ride.final_fare === null ? "-" : money.format(ride.final_fare / 100)]) {
          const td = document.createElement("td");
          td.textContent = String(cell);
          row.append(td);
        }
        const open = document.createElement("td");
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = "Open ride";
        button.dataset.open = "ride";
        button.dataset.id = ride.id;
        open.append(button);
        row.append(open);
        return row;
      }));
      $["driver-detail-ratings"].replaceChildren(...detail.ratings.map((rating) => {
        const item = document.createElement("li");
        item.textContent = `${"★".repeat(rating.score)}${"☆".repeat(5 - rating.score)} on ride ${rating.ride_id}: ${rating.comment === null ? "(no comment)" : rating.comment}`;
        return item;
      }));
      if (detail.ratings.length === 0) {
        const item = document.createElement("li");
        item.textContent = "No ratings yet.";
        $["driver-detail-ratings"].replaceChildren(item);
      }
    }
  }

  // ---- rides
  if (state.tab === "rides") {
    $["no-rides"].hidden = !state.ridesLoaded || state.rides.length > 0;
    $["rides-more"].hidden = !state.ridesMore;
    if (JSON.stringify(state.rides) !== rendered.rides) {
      rendered.rides = JSON.stringify(state.rides);
      $["rides-body"].replaceChildren(...state.rides.map((ride) => {
        const row = document.createElement("tr");
        row.dataset.id = ride.id;
        row.className = "clickable";
        const cells = [
          ride.id, new Date(ride.created_at).toLocaleString(), RIDE_STATUS_TEXT[ride.status], ride.rider_id, ride.driver_id === null ? "-" : ride.driver_id,
          `${ride.pickup_address} → ${ride.dropoff_address}`, ride.fare_estimate === null ? "-" : money.format(ride.fare_estimate / 100),
          ride.final_fare === null ? "-" : money.format(ride.final_fare / 100), `${(ride.surge_percent / 100).toFixed(1)}x`, ride.payment_method,
        ];
        for (const cell of cells) {
          const td = document.createElement("td");
          td.textContent = String(cell);
          row.append(td);
        }
        return row;
      }));
    }

    $["ride-detail"].hidden = state.rideDetailId === null;
    $["ride-detail-loading"].hidden = state.rideDetailId === null || state.rideDetail !== null || state.rideMissing;
    $["ride-detail-missing"].hidden = !state.rideMissing;
    $["ride-detail-body"].hidden = state.rideDetail === null;
    if (state.rideDetail !== null && JSON.stringify(state.rideDetail) !== rendered.rideDetail) {
      rendered.rideDetail = JSON.stringify(state.rideDetail);
      const ride = state.rideDetail;
      const driver = ride.driver;
      const pairs = [
        ["Ride", ride.id], ["Status", RIDE_STATUS_TEXT[ride.status]], ["Requested", new Date(ride.created_at).toLocaleString()],
        ["Rider", `${ride.rider.name}, ${ride.rider.email} (id ${ride.rider.id})`],
        ["Driver", driver === null ? "none" : `${driver.name}, ${driver.email} (id ${driver.id})`],
        ["Vehicle", driver === null || driver.plate_number === null ? "-" : `${driver.plate_number}, ${driver.color} ${driver.model}`],
        ["Pickup", ride.pickup_address], ["Drop-off", ride.dropoff_address],
        ["Estimated trip", formatTrip(ride.distance_m, ride.duration_s)], ["Billed trip", formatTrip(ride.actual_distance_m, ride.actual_duration_s)],
        ["Fare estimate", ride.fare_estimate === null ? "-" : money.format(ride.fare_estimate / 100)],
        ["Final fare", ride.final_fare === null ? "-" : money.format(ride.final_fare / 100)],
        ["Surge", `${(ride.surge_percent / 100).toFixed(1)}x`], ["Payment method", ride.payment_method],
      ];
      const payment = ride.payment;
      const earning = ride.earning;
      const moneyPairs = payment === null
        ? [["Payment", "none"]]
        : [["Payment", `${payment.method}, ${money.format(payment.amount / 100)}, ${payment.status}`]];
      moneyPairs.push(earning === null
        ? ["Earning", "none"]
        : ["Earning", `gross ${money.format(earning.gross_amount / 100)}, platform fee ${money.format(earning.platform_fee / 100)}, driver earning ${money.format(earning.driver_earning / 100)}, commission ${earning.commission_percent}%`]);
      const breakdown = ride.fare_breakdown === null ? [["Fare breakdown", "none (the ride is not settled)"]] : Object.entries(ride.fare_breakdown).map(([key, value]) => [
        BREAKDOWN_LABEL[key] || key, typeof value === "object" && value !== null ? JSON.stringify(value) : key === "reason" && REASON_TEXT[value] ? REASON_TEXT[value] : String(value),
      ]);
      for (const [target, list] of [[$["ride-detail-fields"], pairs], [$["ride-detail-money"], moneyPairs], [$["ride-detail-breakdown"], breakdown]]) {
        target.replaceChildren(...list.flatMap(([label, value]) => {
          const term = document.createElement("dt");
          const description = document.createElement("dd");
          term.textContent = label;
          description.textContent = String(value);
          return [term, description];
        }));
      }
      $["ride-detail-events"].replaceChildren(...ride.events.map((event) => {
        const row = document.createElement("tr");
        const change = `${event.from_status === null ? "(created)" : RIDE_STATUS_TEXT[event.from_status]} → ${RIDE_STATUS_TEXT[event.to_status]}`;
        for (const cell of [new Date(event.created_at).toLocaleString(), change, ACTOR_TEXT[event.actor]]) {
          const td = document.createElement("td");
          td.textContent = cell;
          row.append(td);
        }
        return row;
      }));
      $["ride-detail-no-offers"].hidden = ride.offers.length > 0;
      $["ride-detail-offers"].replaceChildren(...ride.offers.map((offer) => {
        const row = document.createElement("tr");
        const cells = [
          `Driver ${offer.driver_id}`, OFFER_STATUS_TEXT[offer.status], `${offer.pickup_distance_m} m`, new Date(offer.created_at).toLocaleString(),
          new Date(offer.expires_at).toLocaleString(), offer.responded_at === null ? "-" : new Date(offer.responded_at).toLocaleString(),
        ];
        for (const cell of cells) {
          const td = document.createElement("td");
          td.textContent = cell;
          row.append(td);
        }
        return row;
      }));
      $["ride-detail-ratings"].replaceChildren(...ride.ratings.map((rating) => {
        const item = document.createElement("li");
        item.textContent = `By the ${rating.from_role}: ${"★".repeat(rating.score)}${"☆".repeat(5 - rating.score)}, ${rating.comment === null ? "(no comment)" : rating.comment}`;
        return item;
      }));
      if (ride.ratings.length === 0) {
        const item = document.createElement("li");
        item.textContent = "No ratings.";
        $["ride-detail-ratings"].replaceChildren(item);
      }
    }
  }

  // ---- pricing (the inputs are written by the handlers, never here)
  if (state.tab === "pricing" && state.pricing !== null) {
    $["pricing-active"].textContent = `${state.pricing.active_rides} ${state.pricing.active_rides === 1 ? "ride is" : "rides are"} active right now.`;
    for (const field of PRICING_FIELDS) {
      const limit = state.pricing.limits[field];
      $[`range-${field}`].textContent = MONEY_FIELDS.includes(field)
        ? `Allowed: ${money.format(limit.min / 100)} to ${money.format(limit.max / 100)}`
        : field === "surge_cap" ? `Allowed: ${limit.min.toFixed(2)} to ${limit.max.toFixed(2)}, at most two decimals` : `Allowed: ${limit.min} to ${limit.max}`;
    }
    $["pricing-reload"].hidden = !state.formStale;
    $["pricing-saved"].textContent = state.saved;
    $["no-history"].hidden = state.history.length > 0;
    if (JSON.stringify(state.history) !== rendered.history) {
      rendered.history = JSON.stringify(state.history);
      $["pricing-history"].replaceChildren(...state.history.map((entry) => {
        const item = document.createElement("li");
        const lines = entry.changes.map((change) => {
          const isMoney = MONEY_FIELDS.includes(change.field);
          return `${FIELD_LABEL[change.field] || change.field}: ${isMoney ? money.format(change.old / 100) : change.old} → ${isMoney ? money.format(change.new / 100) : change.new}`;
        });
        item.textContent = `${new Date(entry.created_at).toLocaleString()}, ${entry.actor_name}, version ${entry.version_after}: ${lines.join("; ")}`;
        return item;
      }));
    }
  }

  for (const button of document.querySelectorAll("button")) button.disabled = state.busy;
  if (state.tab === "drivers") $["drivers-more"].disabled = state.busy || !state.driversMore || state.drivers.length >= DRIVERS_CAP;
}

// ---------------------------------------------------------------- handlers

$["login-form"].addEventListener("submit", (event) => {
  event.preventDefault();
  const form = new FormData($["login-form"]);
  act(async () => {
    const data = await api("POST", "/auth/login", { email: form.get("email"), password: form.get("password") });
    saveSession(data.access_token, data.user);
    location.reload(); // starts the page again as the logged-in user, on the tab of the URL hash
  }, false);
});

$["logout-button"].addEventListener("click", () => {
  clearSession();
  location.reload();
});

$.nav.addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  act(async () => {
    state.tab = button.dataset.tab;
    state.error = "";
    state.pollError = "";
    if (state.tab === "pricing") {
      // Loaded here, because the form is filled by this handler: render() never writes an input.
      await loadTab();
      if (state.pricing !== null) {
        const rule = state.pricing.rules[0];
        state.form = { version: rule.version, rule };
        state.formStale = false;
        state.saved = "";
        for (const field of PRICING_FIELDS) {
          $[`pricing-${field}`].value = MONEY_FIELDS.includes(field) ? paiseToRupees(rule[field]) : field === "surge_cap" ? rule[field].toFixed(2) : String(rule[field]);
        }
      }
    }
  }, button.dataset.tab !== "pricing");
});

// ---- overview
$["period-buttons"].addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  act(async () => {
    state.period = button.dataset.period;
    state.stats = null;
    rendered.stats = "";
  });
});

// ---- live map
$["toggle-drivers"].addEventListener("change", () => act(async () => { state.showDrivers = $["toggle-drivers"].checked; }, false));
$["toggle-rides"].addEventListener("change", () => act(async () => { state.showRides = $["toggle-rides"].checked; }, false));

$["fit-button"].addEventListener("click", () => act(async () => {
  if (view.map === null) return;
  const points = [
    ...(state.showDrivers && state.live ? state.live.drivers.map((d) => [d.lat, d.lng]) : []),
    ...(state.showRides && state.live ? state.live.rides.map((r) => [r.pickup_lat, r.pickup_lng]) : []),
  ];
  if (points.length > 0) view.map.fitBounds(points, { padding: FIT_PADDING, animate: false, maxZoom: FIT_MAX_ZOOM });
  else view.map.fitBounds([[state.config.south, state.config.west], [state.config.north, state.config.east]], { animate: false });
}, false));

// The "Open ride" and "Open driver" buttons of the side panel, the driver detail and the live panel.
document.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-open]");
  if (!button) return;
  const id = Number(button.dataset.id);
  act(async () => {
    if (button.dataset.open === "ride") {
      state.tab = "rides";
      state.rideDetailId = id;
      state.rideDetail = null;
      state.rideMissing = false;
      rendered.rideDetail = "";
    } else {
      state.tab = "drivers";
      state.driverDetailId = id;
      state.driverDetail = null;
      state.driverDetailKey = "";
      rendered.driverDetail = "";
    }
  });
});

// ---- drivers
$["driver-search-form"].addEventListener("submit", (event) => {
  event.preventDefault();
  act(async () => {
    state.driverFilter = $["driver-status"].value;
    state.driverQuery = $["driver-search"].value.trim();
    state.drivers = [];
    state.driversLoaded = false;
  });
});

$["driver-status"].addEventListener("change", () => $["driver-search-form"].requestSubmit());

$["drivers-body"].addEventListener("click", (event) => {
  const button = event.target.closest("button");
  const row = event.target.closest("tr");
  if (!row) return;
  const id = Number(row.dataset.id);
  if (!button) {
    act(async () => {
      state.driverDetailId = id;
      state.driverDetail = null;
      state.driverDetailKey = "";
      rendered.driverDetail = "";
    });
    return;
  }
  act(async () => {
    const driver = state.drivers.find((d) => d.id === id);
    if (button.dataset.action === "reject") {
      const warning = driver && (driver.online || driver.active_ride_id !== null)
        ? "\n\nThis driver is online. Rejecting takes them offline; a ride in progress continues." : "";
      if (!confirm(`Reject driver ${id}?${warning}`)) return;
    }
    await api("POST", `/admin/drivers/${id}/${button.dataset.action}`);
    state.driverDetailKey = ""; // the open detail is loaded again
  });
});

$["drivers-more"].addEventListener("click", () => act(async () => {
  const last = state.drivers[state.drivers.length - 1];
  const limit = Math.min(DRIVERS_PAGE, DRIVERS_CAP - state.drivers.length);
  const query = new URLSearchParams({ limit: String(limit), after_id: String(last.id) });
  if (state.driverFilter) query.set("status", state.driverFilter);
  if (state.driverQuery) query.set("q", state.driverQuery);
  const rows = await api("GET", `/admin/drivers?${query}`);
  state.drivers = [...state.drivers, ...rows];
  state.driversMore = rows.length === limit;
}, false));

$["driver-detail-close"].addEventListener("click", () => act(async () => {
  state.driverDetailId = null;
  state.driverDetail = null;
  state.driverDetailKey = "";
}, false));

// ---- rides
$["ride-search-form"].addEventListener("submit", (event) => {
  event.preventDefault();
  act(async () => {
    const rider = $["ride-rider"].value.trim();
    const driver = $["ride-driver"].value.trim();
    if (rider !== "" && !/^\d{1,9}$/.test(rider)) throw new Error("Rider id must be a number.");
    if (driver !== "" && !/^\d{1,9}$/.test(driver)) throw new Error("Driver id must be a number.");
    state.rideFilter = { status: $["ride-status"].value, rider_id: rider, driver_id: driver, period: $["ride-period"].value };
    state.rides = [];
    state.ridesLoaded = false;
    state.ridesStale = true;
  });
});

$["rides-refresh"].addEventListener("click", () => act(async () => { state.ridesStale = true; }));

$["rides-body"].addEventListener("click", (event) => {
  const row = event.target.closest("tr");
  if (!row) return;
  const id = Number(row.dataset.id);
  act(async () => {
    state.rideDetailId = id;
    state.rideDetail = null;
    state.rideMissing = false;
    rendered.rideDetail = "";
  });
});

$["rides-more"].addEventListener("click", () => act(async () => {
  const query = new URLSearchParams({ limit: String(RIDES_PAGE), before_id: String(state.rides[state.rides.length - 1].id) });
  const filter = state.rideFilter;
  if (filter.status) query.set("status", filter.status);
  if (filter.rider_id) query.set("rider_id", filter.rider_id);
  if (filter.driver_id) query.set("driver_id", filter.driver_id);
  if (filter.period !== "all") {
    const since = new Date();
    since.setHours(0, 0, 0, 0);
    since.setDate(since.getDate() - (filter.period === "week" ? 6 : 0));
    query.set("since", since.toISOString());
  }
  const rows = await api("GET", `/admin/rides?${query}`);
  state.rides = [...state.rides, ...rows];
  state.ridesMore = rows.length === RIDES_PAGE;
}, false));

$["ride-detail-close"].addEventListener("click", () => act(async () => {
  state.rideDetailId = null;
  state.rideDetail = null;
  state.rideMissing = false;
}, false));

// ---- pricing
$["pricing-form"].addEventListener("submit", (event) => {
  event.preventDefault();
  act(async () => {
    state.saved = "";
    if (state.form === null) throw new Error("The pricing rule is not loaded yet.");
    const { version, rule } = state.form;
    const body = { version };
    const lines = [];
    for (const field of PRICING_FIELDS) {
      const typed = $[`pricing-${field}`].value.trim();
      let value;
      if (MONEY_FIELDS.includes(field)) {
        value = rupeesToPaise(typed);
        if (value === null) throw new Error(`The ${FIELD_LABEL[field]} must be rupees like 12.50.`);
      } else {
        value = typed === "" ? NaN : Number(typed);
        if (!Number.isFinite(value)) throw new Error(`The ${FIELD_LABEL[field]} must be a number.`);
      }
      if (value !== rule[field]) {
        body[field] = value;
        const isMoney = MONEY_FIELDS.includes(field);
        lines.push(`${FIELD_LABEL[field]} ${isMoney ? money.format(rule[field] / 100) : rule[field]} → ${isMoney ? money.format(value / 100) : value}`);
      }
    }
    if (lines.length === 0) {
      state.saved = "Nothing to save: no value was changed.";
      return;
    }
    const active = state.pricing ? state.pricing.active_rides : 0;
    if (!confirm(`Save these changes?\n\n${lines.join("\n")}\n\n${active} ${active === 1 ? "ride is" : "rides are"} active right now.`)) return;
    try {
      const result = await api("PATCH", `/admin/pricing-rules/${rule.vehicle_type}`, body);
      state.form = { version: result.rule.version, rule: result.rule };
      state.formStale = false;
      state.saved = result.changes.length === 0 ? "Nothing changed." : `Saved. Version ${result.rule.version}.`;
      for (const field of PRICING_FIELDS) {
        $[`pricing-${field}`].value = MONEY_FIELDS.includes(field) ? paiseToRupees(result.rule[field]) : field === "surge_cap" ? result.rule[field].toFixed(2) : String(result.rule[field]);
      }
    } catch (err) {
      if (err.status === 409) state.formStale = true;
      throw err;
    }
  });
});

$["pricing-reload"].addEventListener("click", () => act(async () => {
  await loadTab();
  if (state.pricing === null) return;
  const rule = state.pricing.rules[0];
  state.form = { version: rule.version, rule };
  state.formStale = false;
  state.saved = "";
  for (const field of PRICING_FIELDS) {
    $[`pricing-${field}`].value = MONEY_FIELDS.includes(field) ? paiseToRupees(rule[field]) : field === "surge_cap" ? rule[field].toFixed(2) : String(rule[field]);
  }
}, false));

// ---------------------------------------------------------------- start

// Polling keeps the pages current. Hidden tabs keep polling on purpose. A tick is skipped while the last one is still running.
setInterval(async () => {
  if (loading || state.busy) return;
  state.tick += 1;
  await loadTab(true);
  render();
}, POLL_MS);

render();
if (state.user && state.user.role === "admin") {
  // Starting on a tab is clicking its button, so the pricing form is filled by the same handler.
  $.nav.querySelector(`[data-tab="${state.tab}"]`).click();
}
