import { api, clearSession, getSession, saveSession } from "/shared/api.js";
import { connect, disconnect, reconnectNow } from "/shared/ws.js";

const STATUS_TEXT = {
  REQUESTED: "Looking for a driver",
  DRIVER_ASSIGNED: "Your driver is on the way",
  DRIVER_ARRIVED: "Your driver has arrived. Tell them your trip code to start the trip",
  IN_PROGRESS: "Trip in progress",
  COMPLETED: "Trip completed",
  CANCELLED: "Ride cancelled",
  NO_DRIVER_FOUND: "No drivers are available nearby right now. Please try again in a moment.",
};
const CANCELLABLE = ["REQUESTED", "DRIVER_ASSIGNED", "DRIVER_ARRIVED"];
const FINISHED = ["COMPLETED", "CANCELLED", "NO_DRIVER_FOUND"];
const TRACKING = ["DRIVER_ASSIGNED", "DRIVER_ARRIVED", "IN_PROGRESS"]; // the driver's position is shown only in these
const CODE_STATUSES = ["DRIVER_ASSIGNED", "DRIVER_ARRIVED"]; // the trip code is asked for and shown only in these
const CANCEL_REASON_TEXT = {
  no_driver_yet: "No driver had been assigned yet",
  within_free_window: "You cancelled within the free cancellation window",
  late_cancellation: "You cancelled after the free cancellation window",
  driver_arrived: "Your driver had already arrived",
  driver_cancelled: "The driver cancelled",
};
const CLOSE_REPLACED = 4409; // ws.js reports it as "closed": this account has too many tabs
const MEANWHILE = "Updating every few seconds meanwhile.";
const TRACKING_TEXT = {
  connecting: "Live tracking: connecting...",
  open: "Live tracking: connected",
  paused: `Live tracking paused: this account is open in too many tabs. ${MEANWHILE}`,
};
const POLL_MS = 3000;
const ANIMATION_MS = 3000; // equal to the drivers' ping interval, so one glide ends as the next update arrives
const SNAP_DISTANCE_M = 500; // a bigger jump (the driver was moved by hand) is shown at once, not flown over the map
const DRIVER_COLOR = "#1a56db";
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
const ENTRY_LABEL = { TOPUP: "Added money", RIDE_CHARGE: "Ride payment", ADJUSTMENT: "Adjustment" };
const TOPUP_MIN_RUPEES = 100;
const TOPUP_MAX_RUPEES = 10000;
const TRIPS_PAGE = 20; // rows asked for at a time: the API default (its maximum is 50)
const MAX_SAVED_PLACES = 10; // enforced by the server; the page only shows "N of 10"
const SAVED_ADDRESS_MAX_LENGTH = 200; // the limit on the address of a saved place (rides allow 255)
const money = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" }); // money.format(paise / 100)

// "★★★★☆". Built as text, never as markup.
function stars(score) {
  return "★".repeat(score) + "☆".repeat(5 - score);
}

// "★ 4.7 (31 ratings)". The average is null when there are none (or, for someone else, too few to show).
function formatRating(rating) {
  return `★ ${rating.average.toFixed(1)} (${rating.count} ${rating.count === 1 ? "rating" : "ratings"})`;
}

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
  socketStatus: "connecting", // "connecting", "open", "reconnecting", "closed", as reported by ws.js
  socketInfo: null, // the info that came with the status
  driver: null, // answer of GET /rides/{id}/driver
  driverLocation: null, // {lat, lng, updated_at} of the newest accepted update
  driverKey: null, // "rideId:driverId" the details were asked for; set before the call so a failure is not retried every poll
  otp: null, // the trip code, from GET /rides/{id}/otp
  otpKey: null, // the ride id the code was asked for; set before the call so a failure is not retried every poll
  wallet: null, // answer of GET /wallet: balance, reserved, available (paise)
  entries: [], // the last 10 ledger entries
  topups: [], // the last 5 top-ups
  topupKey: null, // the Idempotency-Key of the top-up being attempted; kept after a failure, so a retry is a replay
  topupAmount: null, // the amount (paise) that key was made for
  topupNotice: "", // "Payment received" and the like
  receipt: null, // answer of GET /rides/{id}/receipt for a finished ride that was charged
  receiptKey: null, // the ride id the receipt was asked for; set before the call so a failure is not retried every poll
  myRating: null, // answer of GET /ratings/me: {count, average}
  ratingStatus: null, // answer of GET /rides/{id}/rating for a COMPLETED ride
  ratingKey: null, // the finished ride's id ("none" while there is none) the ratings were loaded for; set before the calls so a failure is not retried every poll
  paymentMethod: "cash", // written only by the radios' change handler
  view: "ride", // "ride", "trips" or "places"; written only by the view buttons' handler
  trips: null, // rows of GET /rides/history shown in My trips; null until the view was opened
  tripsMore: false, // true while the last page was full, so there may be more
  tripStatus: "", // the status filter ("" is all); written only by its change handler
  tripPeriod: "all", // "all", "7" or "30" days; written only by its change handler
  fromTrips: false, // true while the shown finished ride was opened from My trips ("Back to my trips")
  places: null, // answer of GET /saved-places; null until loaded
  renamingId: null, // the saved place whose rename form is open
  error: "",
  busy: false,
};
let refreshing = false;
let driverMarker = null; // Leaflet marker, moved by animateDriver, never by render()
let animation = null; // {from, to, start} of the glide in progress
let animationFrame = null; // requestAnimationFrame id while the loop runs
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
const rideNote = document.getElementById("ride-note");
const codeSection = document.getElementById("code-section");
const tripCode = document.getElementById("trip-code");
const ridePickup = document.getElementById("ride-pickup");
const rideDropoff = document.getElementById("ride-dropoff");
const rideDriverRow = document.getElementById("ride-driver-row");
const rideDriver = document.getElementById("ride-driver");
const driverSection = document.getElementById("driver-section");
const driverName = document.getElementById("driver-name");
const driverVehicle = document.getElementById("driver-vehicle");
const driverRating = document.getElementById("driver-rating");
const driverTracking = document.getElementById("driver-tracking");
const driverUpdated = document.getElementById("driver-updated");
const reconnectButton = document.getElementById("reconnect-button");
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
const estimateSurge = document.getElementById("estimate-surge");
const estimateSurgeLines = document.getElementById("estimate-surge-lines");
const estimateNormal = document.getElementById("estimate-normal");
const estimateSurgeAmount = document.getElementById("estimate-surge-amount");
const fareSection = document.getElementById("fare-section");
const fareTrip = document.getElementById("fare-trip");
const fareTotal = document.getElementById("fare-total");
const fareEstimate = document.getElementById("fare-estimate");
const fareEstimateSurge = document.getElementById("fare-estimate-surge");
const fareSurge = document.getElementById("fare-surge");
const fareDistance = document.getElementById("fare-distance");
const fareDistanceNote = document.getElementById("fare-distance-note");
const fareTime = document.getElementById("fare-time");
const fareBase = document.getElementById("fare-base");
const fareDistanceFare = document.getElementById("fare-distance-fare");
const fareTimeFare = document.getElementById("fare-time-fare");
const fareMinimum = document.getElementById("fare-minimum");
const fareCapped = document.getElementById("fare-capped");
const fareCancellation = document.getElementById("fare-cancellation");
const fareFee = document.getElementById("fare-fee");
const fareReason = document.getElementById("fare-reason");
const rideTrip = document.getElementById("ride-trip");
const rideFare = document.getElementById("ride-fare");
const rideFareSurge = document.getElementById("ride-fare-surge");
const walletSection = document.getElementById("wallet-section");
const walletBalance = document.getElementById("wallet-balance");
const walletReserved = document.getElementById("wallet-reserved");
const topupNotice = document.getElementById("topup-notice");
const topupForm = document.getElementById("topup-form");
const topupAmount = document.getElementById("topup-amount");
const topupList = document.getElementById("topup-list");
const entryList = document.getElementById("entry-list");
const paymentRadios = document.querySelectorAll('input[name="payment"]');
const paymentHint = document.getElementById("payment-hint");
const ridePayment = document.getElementById("ride-payment");
const farePayment = document.getElementById("fare-payment");
const receiptSection = document.getElementById("receipt-section");
const receiptNumber = document.getElementById("receipt-number");
const receiptIssued = document.getElementById("receipt-issued");
const receiptPickup = document.getElementById("receipt-pickup");
const receiptDropoff = document.getElementById("receipt-dropoff");
const receiptDriver = document.getElementById("receipt-driver");
const receiptTripRow = document.getElementById("receipt-trip-row");
const receiptTrip = document.getElementById("receipt-trip");
const receiptLines = document.getElementById("receipt-lines");
const receiptPayment = document.getElementById("receipt-payment");
const printReceiptButton = document.getElementById("print-receipt-button");
const myRating = document.getElementById("my-rating");
const ratingSection = document.getElementById("rating-section");
const ratingForm = document.getElementById("rating-form");
const ratingComment = document.getElementById("rating-comment");
const ratingExpires = document.getElementById("rating-expires");
const ratingMine = document.getElementById("rating-mine");
const ratingMineText = document.getElementById("rating-mine-text");
const ratingMineComment = document.getElementById("rating-mine-comment");
const ratingClosed = document.getElementById("rating-closed");
const viewNav = document.getElementById("view-nav");
const viewButtons = document.querySelectorAll("[data-view]"); // the three nav buttons and "Go to ride"
const busyBanner = document.getElementById("busy-banner");
const busyBannerText = document.getElementById("busy-banner-text");
const tripsSection = document.getElementById("trips-section");
const tripStatusSelect = document.getElementById("trip-status");
const tripPeriodSelect = document.getElementById("trip-period");
const tripsRefreshButton = document.getElementById("trips-refresh-button");
const tripsEmpty = document.getElementById("trips-empty");
const tripList = document.getElementById("trip-list");
const tripsMoreButton = document.getElementById("trips-more-button");
const placesSection = document.getElementById("places-section");
const placesCount = document.getElementById("places-count");
const placesEmpty = document.getElementById("places-empty");
const placesList = document.getElementById("places-list");
const renameForm = document.getElementById("rename-form");
const renameInput = document.getElementById("rename-input");
const renameCancelButton = document.getElementById("rename-cancel-button");
const quickPlacesBlock = document.getElementById("quick-places-block");
const quickPlaces = document.getElementById("quick-places");
const saveForms = { pickup: document.getElementById("save-pickup-form"), dropoff: document.getElementById("save-dropoff-form") };
const saveInputs = { pickup: document.getElementById("save-pickup-label"), dropoff: document.getElementById("save-dropoff-label") };
const saveButtons = { pickup: saveForms.pickup.querySelector("button"), dropoff: saveForms.dropoff.querySelector("button") };
const backToTripsButton = document.getElementById("back-to-trips-button");
let shownTopups = null; // the JSON of the top-ups in the DOM, so polling does not rebuild their buttons under a click
let shownTrips = null; // the same for the trip rows and the saved places (rebuilt only when they change)
let shownPlaces = null;

// "5.2 km, 14 min". Used by the estimate panel and the ride view. Old rides have null values.
function formatTrip(distanceM, durationS) {
  if (distanceM === null || durationS === null) return "-";
  return `${(distanceM / 1000).toFixed(1)} km, ${Math.max(1, Math.round(durationS / 60))} min`;
}

// Your own rating, and the rating status of a COMPLETED ride. Not called on every poll: refresh() calls it when the page
// loads and once per finished ride, and the submit handler calls it again after a rating.
async function loadRatings() {
  const finished = state.ride !== null && FINISHED.includes(state.ride.status) ? state.ride : null;
  state.ratingKey = finished === null ? "none" : finished.id;
  state.myRating = await api("GET", "/ratings/me");
  state.ratingStatus = finished !== null && finished.status === "COMPLETED" ? await api("GET", `/rides/${finished.id}/rating`) : null;
}

// The first page of the trip list (append is false), or the page after the last row shown (append is true). The window comes
// from the browser's local midnight, so the server needs no timezone. Called when the view opens, on a filter change, on Refresh
// and on "Load more", never on the poll.
async function loadTrips(append) {
  const query = new URLSearchParams({ limit: TRIPS_PAGE });
  if (state.tripStatus !== "") query.set("status", state.tripStatus);
  if (state.tripPeriod !== "all") {
    const start = new Date();
    start.setHours(0, 0, 0, 0);
    start.setDate(start.getDate() - (Number(state.tripPeriod) - 1));
    query.set("since", start.toISOString());
  }
  if (append) query.set("before_id", state.trips[state.trips.length - 1].id);
  const rows = await api("GET", `/rides/history?${query}`);
  state.trips = append ? [...state.trips, ...rows] : rows;
  state.tripsMore = rows.length === TRIPS_PAGE;
}

// The saved places: loaded with the page, when the view opens and after every change, never on the poll.
async function loadPlaces() {
  state.places = await api("GET", "/saved-places");
  if (!state.places.some((place) => place.id === state.renamingId)) state.renamingId = null;
}

async function login(email, password) {
  const data = await api("POST", "/auth/login", { email, password });
  saveSession(data.access_token, data.user);
  state.user = data.user;
  if (state.user.role === "rider") connect(socketHandlers);
}

// Events only say "something changed": the page reacts by asking the REST API, which is the source of truth.
// A missed event is harmless, because the poll below catches up within 3 seconds.
const socketHandlers = {
  onEvent: (type, data) => {
    if (type === "driver_location") {
      const valid = [data.lat, data.lng, data.updated_at].every(Number.isFinite);
      if (state.ride === null || data.ride_id !== state.ride.id || !valid) return;
      applyDriverLocation(data);
      render();
    } else if (type === "ride_updated" && state.ride !== null && data.ride_id === state.ride.id) {
      refresh().then(render);
    }
  },
  onStatus: (status, info) => {
    state.socketStatus = status;
    state.socketInfo = info;
    // Events published while the socket was down are lost, so ask the REST API again.
    if (status === "open" && info.reconnected) {
      state.driverKey = null; // the next refresh also fetches the driver details and last location again
      if (!refreshing && !state.busy) {
        refreshing = true;
        refresh().then(() => {
          refreshing = false;
          render();
        });
      }
    }
    render();
  },
};

// Used by the details fetch and by the socket. Only stores the position and starts a glide;
// the marker itself is created by render() and moved by animateDriver().
function applyDriverLocation(location) {
  // A slow REST answer must not overwrite a newer update from the socket.
  if (state.driverLocation !== null && location.updated_at < state.driverLocation.updated_at) return;
  state.driverLocation = { lat: location.lat, lng: location.lng, updated_at: location.updated_at };
  if (driverMarker === null) return;

  // Start from where the marker is drawn now, not from the previous target.
  const from = driverMarker.getLatLng();
  const to = L.latLng(location.lat, location.lng);
  if (state.map.distance(from, to) > SNAP_DISTANCE_M || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    driverMarker.setLatLng(to);
    animation = null;
    return;
  }
  animation = { from, to, start: performance.now() };
  if (animationFrame === null) animationFrame = requestAnimationFrame(animateDriver);
}

// Time-based, so a background tab (where frames pause) is at the right place as soon as it returns.
// CSS transitions are not used: they fight Leaflet's own zoom and pan transforms and do nothing for SVG circles.
function animateDriver(now) {
  animationFrame = null;
  if (animation === null || driverMarker === null) return;
  const t = Math.min(1, (now - animation.start) / ANIMATION_MS);
  const { from, to } = animation;
  driverMarker.setLatLng([from.lat + (to.lat - from.lat) * t, from.lng + (to.lng - from.lng) * t]);
  if (t < 1) animationFrame = requestAnimationFrame(animateDriver);
}

async function refresh() {
  if (!state.user || state.user.role !== "rider") return;
  try {
    if (state.config === null) state.config = await api("GET", "/places/map-config");
    [state.wallet, state.entries, state.topups] = await Promise.all([
      api("GET", "/wallet"),
      api("GET", "/wallet/entries?limit=10"),
      api("GET", "/wallet/topups?limit=5"),
    ]);
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

    // The position comes from here once per ride, so a reloaded page has one at once; the socket gives the updates.
    if (state.ride && state.ride.driver_id !== null) {
      const key = `${state.ride.id}:${state.ride.driver_id}`;
      if (state.driverKey !== key) {
        state.driverKey = key;
        state.driver = await api("GET", `/rides/${state.ride.id}/driver`);
        if (state.driver.location !== null) applyDriverLocation(state.driver.location);
      } else if (TRACKING.includes(state.ride.status) && state.driver !== null && state.socketStatus !== "open") {
        // While the socket is down the marker keeps moving from here, every poll. applyDriverLocation drops stale answers.
        const details = await api("GET", `/rides/${state.ride.id}/driver`);
        if (details.location !== null) applyDriverLocation(details.location);
      }
    } else {
      state.driver = null;
    }

    // The code only exists while the driver is assigned or has arrived; it is gone once the trip starts or ends.
    if (state.ride && CODE_STATUSES.includes(state.ride.status)) {
      if (state.otpKey !== state.ride.id) {
        state.otpKey = state.ride.id;
        state.otp = (await api("GET", `/rides/${state.ride.id}/otp`)).otp;
      }
    } else {
      state.otp = null;
      state.otpKey = null;
    }

    // A receipt exists only for a finished ride that was charged (not a free cancellation, not a ride from before the fare
    // existed). Asked for once, and last, so a failure here cannot stop the checks above.
    const breakdown = state.ride === null ? null : state.ride.fare_breakdown;
    const charged =
      breakdown !== null && breakdown.kind !== "legacy" && ["COMPLETED", "CANCELLED"].includes(state.ride.status) && state.ride.final_fare > 0;
    if (charged && state.receiptKey !== state.ride.id) {
      state.receiptKey = state.ride.id;
      state.receipt = await api("GET", `/rides/${state.ride.id}/receipt`);
    }

    // Last, like the receipt: a failure here cannot stop the checks above.
    const ratingsFor = state.ride !== null && FINISHED.includes(state.ride.status) ? state.ride.id : "none";
    if (state.ratingKey !== ratingsFor) await loadRatings();

    // The quick-pick list of the request form. Once; after that only a change or opening the view reloads it. Last, like the above.
    if (state.places === null) await loadPlaces();
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

  // A new point makes the old route and estimate wrong, so drop them now.
  state.estimate = null;
  fetchEstimate();
  render();
}

// Used by a search result and by a saved place: sets the point, then centers the map on it. (A map click sets the point
// where the person clicked and does not move the map.)
function choosePoint(kind, lat, lng, address) {
  act(async () => {
    setPoint(kind, lat, lng, address);
    state.map.setView([lat, lng], PLACE_ZOOM, { animate: false });
  });
}

// Asks for the estimate of the chosen points. Used by setPoint and by a refused request (the price may have changed).
// Only the latest answer is applied.
function fetchEstimate() {
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
        if (requestNumber !== state.estimateRequest) return;
        // The same route again: keep the old array, because render() redraws the line and moves the map for a new one.
        if (state.estimate !== null && JSON.stringify(state.estimate.path) === JSON.stringify(estimate.path)) estimate.path = state.estimate.path;
        state.estimate = estimate;
      })
      .catch((err) => {
        if (requestNumber === state.estimateRequest) state.error = err.message;
      })
      .finally(() => {
        if (requestNumber === state.estimateRequest) state.estimating = false;
        render();
      });
  }
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
  const hasActiveRide = showRide && !FINISHED.includes(state.ride.status);
  // The map is touched only in the Ride view: Leaflet cannot measure a hidden container, and polling never moves it.
  const showMap = isRider && state.loaded && state.config !== null && state.view === "ride";

  message.hidden = state.error === "";
  message.textContent = state.error;

  loginSection.hidden = loggedIn;
  userBar.hidden = !loggedIn;
  userName.textContent = loggedIn ? state.user.name : "";
  wrongRoleSection.hidden = !loggedIn || isRider;
  if (loggedIn) wrongRoleText.textContent = `This account is a ${state.user.role}. Open /${state.user.role}/ instead.`;

  // The views: the header, the message, the rating line and the wallet are in all of them.
  requestSection.hidden = !isRider || !state.loaded || showRide || state.view !== "ride";
  rideSection.hidden = !showRide || state.view !== "ride";
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

  // Created and removed here; only animateDriver and applyDriverLocation move it. The tooltip is a fixed string.
  const tracking = showRide && TRACKING.includes(state.ride.status) && state.driverLocation !== null;
  if (!tracking && driverMarker !== null) {
    cancelAnimationFrame(animationFrame);
    animationFrame = null;
    animation = null;
    driverMarker.remove();
    driverMarker = null;
  } else if (tracking && showMap && driverMarker === null) {
    const { lat, lng } = state.driverLocation;
    driverMarker = L.circleMarker([lat, lng], {
      radius: 10,
      color: DRIVER_COLOR,
      fillColor: DRIVER_COLOR,
      fillOpacity: 1,
      interactive: false,
    })
      .bindTooltip("Driver", { permanent: true, direction: "top", offset: [0, -10] })
      .addTo(state.map);
    // Once, when the marker first appears. After that nothing moves or zooms the map.
    state.map.fitBounds(
      [[state.ride.pickup_lat, state.ride.pickup_lng], [state.ride.dropoff_lat, state.ride.dropoff_lng], [lat, lng]],
      { padding: FIT_PADDING, animate: false }
    );
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
        button.addEventListener("click", () => choosePoint(kind, place.lat, place.lng, place.display_name));
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
    estimateSurge.hidden = estimate.surge_percent <= 100;
    estimateSurgeLines.hidden = estimate.surge_percent <= 100;
    if (estimate.surge_percent > 100) {
      estimateSurge.textContent = `High demand in your area: fares are ${(estimate.surge_percent / 100).toFixed(1)}x higher right now.`;
      estimateNormal.textContent = money.format(estimate.normal_fare / 100);
      estimateSurgeAmount.textContent = money.format(estimate.surge_amount / 100);
    }
  }

  myRating.hidden = !isRider || state.myRating === null;
  if (!myRating.hidden) myRating.textContent = state.myRating.average === null ? "Your rating: no ratings yet" : `Your rating: ${formatRating(state.myRating)}`;

  // Only textContent below: names, notes and addresses are plain text. No input is touched here.
  walletSection.hidden = !isRider || state.wallet === null;
  if (!walletSection.hidden) {
    walletBalance.textContent = money.format(state.wallet.balance / 100);
    walletReserved.hidden = state.wallet.reserved === 0;
    walletReserved.textContent = `(${money.format(state.wallet.reserved / 100)} reserved for your current ride)`;
    topupNotice.hidden = state.topupNotice === "";
    topupNotice.textContent = state.topupNotice;

    const topupsJson = JSON.stringify(state.topups);
    if (topupsJson !== shownTopups) {
      shownTopups = topupsJson;
      topupList.replaceChildren(
        ...state.topups.map((topup) => {
          const row = document.createElement("li");
          const text = document.createElement("span");
          text.textContent = topup.status === "PENDING" ? `Pending top-up ${money.format(topup.amount / 100)}` : `Top-up ${money.format(topup.amount / 100)}: ${topup.status.toLowerCase()}`;
          row.append(text);
          if (topup.status === "PENDING") {
            const button = document.createElement("button");
            button.type = "button";
            button.textContent = "Check status";
            button.addEventListener("click", () =>
              act(async () => {
                const checked = await api("POST", `/wallet/topups/${topup.id}/sync`);
                state.topupNotice = { SUCCEEDED: "Payment received", EXPIRED: "That top-up expired" }[checked.status] || "Payment is still being confirmed";
              })
            );
            row.append(button);
          }
          return row;
        })
      );
    }
    entryList.replaceChildren(
      ...state.entries.map((entry) => {
        const row = document.createElement("li");
        const label = ENTRY_LABEL[entry.kind] + (entry.kind === "ADJUSTMENT" && entry.note ? `: ${entry.note}` : "");
        row.textContent = `${label}  ${entry.amount > 0 ? "+" : ""}${money.format(entry.amount / 100)}  (balance ${money.format(entry.balance_after / 100)})`;
        return row;
      })
    );
  }
  const showWalletHint = state.paymentMethod === "wallet" && state.estimate !== null && state.wallet !== null;
  paymentHint.hidden = !showWalletHint;
  if (showWalletHint) {
    paymentHint.textContent =
      `Your wallet: ${money.format(state.wallet.available / 100)} available. This trip can cost up to ${money.format(state.estimate.max_fare / 100)}.` +
      (state.wallet.available < state.estimate.max_fare ? " Add money or pay with cash." : "");
  }

  // Only textContent below: the receipt holds addresses and a driver's name, which are plain text.
  receiptSection.hidden = !isRider || state.receipt === null || state.view !== "ride";
  if (!receiptSection.hidden) {
    const receipt = state.receipt;
    receiptNumber.textContent = receipt.receipt_number;
    receiptIssued.textContent = `Issued ${new Date(receipt.issued_at).toLocaleString()}`;
    receiptPickup.textContent = receipt.pickup_address;
    receiptDropoff.textContent = receipt.dropoff_address;
    receiptDriver.textContent = `${receipt.driver_name}, ${receipt.vehicle.color} ${receipt.vehicle.model}, plate ${receipt.vehicle.plate_number}`;
    const lines = [];
    receiptTripRow.hidden = receipt.trip === null;
    if (receipt.trip !== null) {
      const trip = receipt.trip;
      receiptTrip.textContent = `${formatTrip(trip.distance_m, trip.duration_s)}${trip.distance_source === "estimate" ? " (estimated, tracking was not available)" : ""}`;
      lines.push(
        `Base fare: ${money.format(trip.base_fare / 100)}`,
        `Distance: ${money.format(trip.distance_fare / 100)}`,
        `Time: ${money.format(trip.time_fare / 100)}`
      );
      if (trip.minimum_fare_applied) lines.push("Minimum fare applied");
      if (trip.surge_percent > 100) {
        lines.push(`High-demand pricing: ${(trip.surge_percent / 100).toFixed(1)}x (+${money.format(trip.surge_amount / 100)})`);
      }
      if (trip.capped) lines.push("Capped at 150% of the estimate");
      lines.push(`Total: ${money.format(trip.total / 100)}`);
    } else {
      lines.push(`Cancellation fee ${money.format(receipt.cancellation.fee / 100)}`, CANCEL_REASON_TEXT[receipt.cancellation.reason] || "");
    }
    receiptLines.replaceChildren(
      ...lines.map((text) => {
        const row = document.createElement("li");
        row.textContent = text;
        return row;
      })
    );
    const payment = receipt.payment;
    receiptPayment.textContent =
      payment.method === "wallet"
        ? `Paid from your wallet (balance after: ${money.format(payment.wallet_balance_after / 100)})`
        : "Paid in cash to the driver";
  }

  if (showRide) {
    rideId.textContent = state.ride.id;
    ridePayment.textContent = `Payment: ${state.ride.payment_method}`;
    rideStatus.textContent = state.ride.status;
    rideStatusText.textContent = STATUS_TEXT[state.ride.status];

    // Who cancelled comes from the last CANCELLED event: the actor is the rider (this user), the driver, or the system.
    const cancelled = state.ride.status === "CANCELLED" ? state.events.findLast((event) => event.to_status === "CANCELLED") : null;
    let note = "";
    if (cancelled) {
      if (cancelled.actor_user_id === getSession().user.id) note = "You cancelled this ride.";
      else if (cancelled.actor_user_id === null) note = "Ride cancelled.";
      else note = "The driver cancelled this ride. You can request a new one.";
    }
    rideNote.hidden = note === "";
    rideNote.textContent = note;

    // The fare is shown for a settled ride only. Rides finished before the fare existed have no amount (final_fare is null).
    const breakdown = state.ride.fare_breakdown;
    const settled = state.ride.final_fare !== null && breakdown !== null && breakdown.kind !== "legacy";
    fareSection.hidden = !settled;
    fareTrip.hidden = !settled || breakdown.kind !== "trip";
    fareCancellation.hidden = !settled || breakdown.kind !== "cancellation";
    if (!fareTrip.hidden) {
      fareTotal.textContent = money.format(state.ride.final_fare / 100);
      fareEstimate.textContent = money.format(state.ride.fare_estimate / 100);
      fareEstimateSurge.hidden = state.ride.surge_percent <= 100;
      fareEstimateSurge.textContent = `(includes ${(state.ride.surge_percent / 100).toFixed(1)}x high-demand pricing)`;
      // Rides settled before M5.2 have no surge keys in the breakdown.
      const surged = breakdown.surge_percent !== undefined && breakdown.surge_percent > 100;
      fareSurge.hidden = !surged;
      if (surged) {
        fareSurge.textContent = `High-demand pricing: ${(breakdown.surge_percent / 100).toFixed(1)}x (+${money.format(breakdown.surge_amount / 100)} on a normal fare of ${money.format(breakdown.normal_fare / 100)})`;
      }
      fareDistance.textContent = `${(breakdown.distance_m / 1000).toFixed(1)} km`;
      fareDistanceNote.hidden = breakdown.distance_source !== "estimate";
      fareTime.textContent = `${Math.floor(breakdown.duration_s / 60)} min ${breakdown.duration_s % 60} s`;
      fareBase.textContent = money.format(breakdown.base_fare / 100);
      fareDistanceFare.textContent = money.format(breakdown.distance_fare / 100);
      fareTimeFare.textContent = money.format(breakdown.time_fare / 100);
      fareMinimum.hidden = !breakdown.minimum_fare_applied;
      fareCapped.hidden = !breakdown.capped;
    }
    if (!fareCancellation.hidden) {
      fareFee.textContent = breakdown.fee > 0 ? `Cancellation fee: ${money.format(breakdown.fee / 100)}` : "No cancellation fee.";
      fareReason.hidden = breakdown.fee === 0;
      fareReason.textContent = CANCEL_REASON_TEXT[breakdown.reason] || "";
    }

    // Rides from before payments existed are cash. A fee of 0 leaves nothing to pay.
    farePayment.hidden = !settled || state.ride.final_fare === 0;
    if (!farePayment.hidden) {
      const owed = money.format(state.ride.final_fare / 100);
      farePayment.textContent = state.ride.payment_method === "wallet" ? `Paid ${owed} from your wallet.` : `Pay ${owed} in cash to the driver.`;
    }

    codeSection.hidden = !CODE_STATUSES.includes(state.ride.status) || state.otp === null;
    tripCode.textContent = state.otp === null ? "" : state.otp;
    ridePickup.textContent = state.ride.pickup_address;
    rideDropoff.textContent = state.ride.dropoff_address;
    rideTrip.textContent = formatTrip(state.ride.distance_m, state.ride.duration_s);
    rideFare.textContent = state.ride.fare_estimate === null ? "-" : money.format(state.ride.fare_estimate / 100);
    rideFareSurge.hidden = state.ride.surge_percent <= 100;
    rideFareSurge.textContent = `(includes ${(state.ride.surge_percent / 100).toFixed(1)}x high-demand pricing)`;
    rideDriverRow.hidden = state.ride.driver_id === null;
    rideDriver.textContent = state.ride.driver_id;
    driverSection.hidden = state.driver === null;
    if (state.driver !== null) {
      const vehicle = state.driver.vehicle;
      driverName.textContent = state.driver.name;
      driverRating.textContent = state.driver.rating.average === null ? "New driver" : `Rating: ${formatRating(state.driver.rating)}`;
      driverVehicle.textContent = vehicle === null ? "-" : `${vehicle.color} ${vehicle.model}, plate ${vehicle.plate_number}`;
      const live = TRACKING.includes(state.ride.status);
      driverTracking.hidden = !live;
      driverUpdated.hidden = !live || state.driverLocation === null;
      const { socketStatus: status, socketInfo: info } = state;
      if (status === "reconnecting") {
        driverTracking.textContent = `Live tracking: connection lost. Reconnecting (attempt ${info.attempt}), next try in about ${info.retryInSeconds} s. ${MEANWHILE}`;
      } else if (status === "closed") {
        driverTracking.textContent = info.code === CLOSE_REPLACED ? TRACKING_TEXT.paused : `Live tracking stopped (code ${info.code}). ${MEANWHILE}`;
      } else {
        driverTracking.textContent = TRACKING_TEXT[status];
      }
      reconnectButton.hidden = !live || status !== "closed";
      if (state.driverLocation !== null) {
        driverUpdated.textContent = `Last location update: ${new Date(state.driverLocation.updated_at * 1000).toLocaleTimeString()}`;
      }
    }
    cancelButton.hidden = !CANCELLABLE.includes(state.ride.status);
    newRideButton.hidden = !FINISHED.includes(state.ride.status);

    // Only textContent: the comment is plain text. The form's inputs are never written here, only shown or hidden.
    const rating = state.ratingKey === state.ride.id ? state.ratingStatus : null;
    ratingSection.hidden = rating === null;
    if (rating !== null) {
      ratingForm.hidden = !rating.can_rate;
      ratingExpires.textContent = rating.expires_at === null ? "" : `You can rate until ${new Date(rating.expires_at).toLocaleString()}`;
      ratingMine.hidden = rating.mine === null;
      ratingClosed.hidden = rating.reason !== "window_closed";
      if (rating.mine !== null) {
        ratingMineText.textContent = `You rated this trip ${stars(rating.mine.score)} (${rating.mine.score} of 5)`;
        ratingMineComment.hidden = rating.mine.comment === null;
        ratingMineComment.textContent = rating.mine.comment === null ? "" : rating.mine.comment;
      }
    }

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

  // Views, trips and saved places. Only textContent below: addresses, names and labels are plain text. No input is written here.
  viewNav.hidden = !isRider || !state.loaded;
  for (const button of viewNav.querySelectorAll("button")) {
    if (button.dataset.view === state.view) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  busyBanner.hidden = !isRider || !hasActiveRide || state.view === "ride";
  if (hasActiveRide) busyBannerText.textContent = `You have a ride in progress: ${STATUS_TEXT[state.ride.status]}.`;

  tripsSection.hidden = !isRider || !state.loaded || state.view !== "trips";
  tripsEmpty.hidden = state.trips === null || state.trips.length > 0;
  tripsMoreButton.hidden = !state.tripsMore;
  const tripsJson = JSON.stringify(state.trips);
  if (tripsJson !== shownTrips) {
    shownTrips = tripsJson;
    tripList.replaceChildren(
      ...(state.trips === null ? [] : state.trips).map((trip) => {
        const row = document.createElement("li");
        const cancelledBy = { rider: "You cancelled", driver: "The driver cancelled" }[trip.cancelled_by] || "";
        let moneyLine = "";
        if (trip.final_fare !== null) {
          const amount = money.format(trip.final_fare / 100);
          if (trip.final_fare === 0) moneyLine = "No charge";
          else if (trip.status === "COMPLETED") moneyLine = trip.payment_method === "wallet" ? `Paid ${amount} from your wallet` : `Paid ${amount} in cash`;
          else moneyLine = `Cancellation fee ${amount}`;
        }
        const moneyText = [moneyLine, cancelledBy].filter((part) => part !== "").join(". ");
        const lines = [
          `${new Date(trip.created_at).toLocaleString()}: ${STATUS_TEXT[trip.status]}`,
          `${trip.pickup_address} \u2192 ${trip.dropoff_address}`,
          formatTrip(trip.distance_m, trip.duration_s),
          moneyText === "" ? "" : `${moneyText}.`,
          trip.driver_name === null ? "" : `Driver: ${trip.driver_name}`,
          trip.final_fare !== null && !trip.has_receipt ? "No receipt (no charge)" : "",
          trip.my_rating !== null ? `You rated ${stars(trip.my_rating)}` : trip.can_rate ? "Rate this trip" : "",
        ];
        for (const line of lines.filter((text) => text !== "")) {
          const paragraph = document.createElement("p");
          paragraph.textContent = line;
          row.append(paragraph);
        }
        const open = document.createElement("button");
        open.type = "button";
        open.textContent = "Open";
        // The finished-ride view shows one remembered ride, and a ride in progress takes its place, so only one at a time.
        open.addEventListener("click", () =>
          act(async () => {
            if (state.ride !== null && !FINISHED.includes(state.ride.status)) throw new Error("Finish or cancel your current ride first.");
            const ride = await api("GET", `/rides/${trip.id}`);
            // refresh() loads everything else for this ride: events, route, driver, receipt and rating, once per ride id.
            state.ride = ride;
            state.rideId = ride.id;
            state.ridePath = null;
            state.routeRideId = null;
            state.driver = null;
            state.driverLocation = null;
            state.driverKey = null;
            state.otp = null;
            state.otpKey = null;
            state.receipt = null;
            state.receiptKey = null;
            state.ratingStatus = null;
            state.ratingKey = null;
            ratingForm.reset();
            state.fromTrips = true;
            state.view = "ride";
          })
        );
        row.append(open);
        return row;
      })
    );
  }

  placesSection.hidden = !isRider || !state.loaded || state.view !== "places";
  quickPlacesBlock.hidden = state.places === null || state.places.length === 0;
  placesEmpty.hidden = state.places === null || state.places.length > 0;
  if (state.places !== null) placesCount.textContent = `${state.places.length} of ${MAX_SAVED_PLACES} places saved`;
  renameForm.hidden = state.renamingId === null;
  const placesJson = JSON.stringify([state.places, state.renamingId]);
  if (placesJson !== shownPlaces) {
    shownPlaces = placesJson;
    const places = state.places === null ? [] : state.places;
    quickPlaces.replaceChildren(
      ...places.map((place) => {
        const row = document.createElement("li");
        const text = document.createElement("span");
        text.textContent = `${place.label}: ${place.address}`;
        row.append(text);
        for (const kind of KINDS) {
          const button = document.createElement("button");
          button.type = "button";
          button.textContent = MARKER_LABEL[kind];
          button.addEventListener("click", () => choosePoint(kind, place.lat, place.lng, place.address));
          row.append(button);
        }
        return row;
      })
    );
    placesList.replaceChildren(
      ...places.map((place) => {
        const row = document.createElement("li");
        const text = document.createElement("span");
        text.textContent = `${place.label}: ${place.address}`;
        const rename = document.createElement("button");
        rename.type = "button";
        rename.textContent = "Rename";
        rename.addEventListener("click", () =>
          act(async () => {
            state.renamingId = place.id;
            renameInput.value = place.label;
          }).then(() => renameInput.focus())
        );
        const remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "Delete";
        remove.addEventListener("click", () => {
          if (!confirm("Delete this saved place? Your past trips are not changed.")) return;
          act(async () => {
            try {
              await api("DELETE", `/saved-places/${place.id}`);
            } catch (err) {
              if (err.status !== 404) throw err; // already gone: just show the list as it is
            }
            await loadPlaces();
          });
        });
        row.append(text, rename, remove);
        // The one rename form moves into the row being renamed, and back out of the list when none is.
        if (place.id === state.renamingId) row.append(renameForm);
        return row;
      })
    );
    if (state.renamingId === null) placesSection.append(renameForm);
  }

  for (const button of document.querySelectorAll("button")) button.disabled = state.busy;
  for (const select of [tripStatusSelect, tripPeriodSelect]) select.disabled = state.busy;
  requestButton.disabled = state.busy || state.points.pickup === null || state.points.dropoff === null || state.estimate === null;
  for (const kind of KINDS) saveButtons[kind].disabled = state.busy || state.points[kind] === null;
  backToTripsButton.hidden = !showRide || !state.fromTrips;
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
  disconnect();
  state.driver = null;
  state.driverLocation = null;
  state.driverKey = null;
  state.otp = null;
  state.otpKey = null;
  state.receipt = null;
  state.receiptKey = null;
  state.myRating = null;
  state.ratingStatus = null;
  state.ratingKey = null;
  render(); // removes the driver marker
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
  const acceptedSurgePercent = state.estimate.surge_percent; // the price the rider is looking at
  act(async () => {
    // Matching happens inside this request. A ride that found no driver is already over, so /rides/active
    // would answer 404 for it: remember the ride from this answer instead.
    try {
      state.ride = await api("POST", "/rides", {
        pickup_address: pickup.address,
        pickup_lat: pickup.lat,
        pickup_lng: pickup.lng,
        dropoff_address: dropoff.address,
        dropoff_lat: dropoff.lat,
        dropoff_lng: dropoff.lng,
        accepted_surge_percent: acceptedSurgePercent,
        payment_method: state.paymentMethod,
      });
    } catch (err) {
      fetchEstimate(); // for example "Prices have increased": the panel shows the current price, the message stays
      throw err;
    }
    state.rideId = state.ride.id;
  });
});

for (const radio of paymentRadios) {
  radio.addEventListener("change", () => {
    state.paymentMethod = radio.value;
    render();
  });
}

topupForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const rupees = Number(topupAmount.value);
  act(async () => {
    if (!Number.isInteger(rupees) || rupees < TOPUP_MIN_RUPEES || rupees > TOPUP_MAX_RUPEES) {
      throw new Error("Enter a whole number of rupees between 100 and 10,000");
    }
    const amount = rupees * 100;
    // A new key for a new attempt. After a failure the same key is sent again, so a retry is a replay, never a second top-up.
    if (state.topupKey === null || state.topupAmount !== amount) {
      state.topupKey = crypto.randomUUID();
      state.topupAmount = amount;
    }
    // The one request that does not go through api(): shared/api.js cannot send the Idempotency-Key header, and it is shared
    // with the other pages. The error handling is the same: a readable message, and a 401 clears the session and reloads.
    const response = await fetch("/wallet/topups", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${getSession().token}`, "Idempotency-Key": state.topupKey },
      body: JSON.stringify({ amount }),
    });
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      if (response.status === 401) {
        clearSession();
        location.reload();
      }
      const detail = data && data.detail;
      if (typeof detail === "string") throw new Error(detail);
      if (Array.isArray(detail)) throw new Error(detail.map((item) => `${item.loc[item.loc.length - 1]}: ${item.msg}`).join("; "));
      throw new Error(`Request failed (${response.status})`);
    }
    state.topupKey = null;
    state.topupAmount = null;
    // Only web addresses: never a javascript: URL, whatever the API sends.
    const checkout = new URL(data.checkout_url);
    if (checkout.protocol !== "https:" && checkout.protocol !== "http:") throw new Error("The payment page address is not valid");
    location.assign(checkout.href);
  });
});

reconnectButton.addEventListener("click", () => act(() => reconnectNow()));

// The stars and the comment are written only here: cleared after a rating is accepted, kept after an error.
// With no star chosen nothing is sent.
ratingForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const chosen = ratingForm.querySelector('input[name="score"]:checked');
  if (chosen === null) {
    state.error = "Choose a star rating first";
    render();
    return;
  }
  const body = { score: Number(chosen.value) };
  const comment = ratingComment.value.trim();
  if (comment !== "") body.comment = comment;
  act(async () => {
    try {
      await api("POST", `/rides/${state.ride.id}/rating`, body);
    } catch (err) {
      // For example "already rated" (a second tab, a double click): show the message and what is true now.
      if (err.status === 409) await loadRatings();
      throw err;
    }
    ratingForm.reset();
    await loadRatings();
  });
});

// Not through act(): printing changes no state and calls no API. The print styles show only the receipt.
printReceiptButton.addEventListener("click", () => window.print());

// The quote and the cancel are one action, so a failed quote shows its error and cancels nothing.
cancelButton.addEventListener("click", () => {
  act(async () => {
    const quote = await api("GET", `/rides/${state.ride.id}/cancellation-fee`);
    const fee = quote.fee === 0 ? "There is no cancellation fee." : `You will be charged a cancellation fee of ${money.format(quote.fee / 100)}.`;
    if (!confirm(`Cancel this ride? ${fee}`)) return;
    await api("POST", `/rides/${state.ride.id}/cancel`);
  });
});

// "Request a new ride" and "Back to my trips" forget the finished ride in the same way; Back then opens the list again.
for (const button of [newRideButton, backToTripsButton]) {
  button.addEventListener("click", () => {
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
      state.driver = null;
      state.driverLocation = null;
      state.driverKey = null;
      state.otp = null;
      state.otpKey = null;
      state.receipt = null;
      state.receiptKey = null;
      state.ratingStatus = null;
      state.ratingKey = null; // the next refresh loads your rating again
      state.fromTrips = false;
      ratingForm.reset();
      for (const kind of KINDS) inputs[kind].value = "";
      pickRadios.pickup.checked = true;
      if (button === backToTripsButton) {
        state.view = "trips";
        await loadTrips(false);
      }
    });
  });
}

// The view buttons: Ride, My trips, Saved places, and "Go to ride" of the banner. The page keeps polling in every view.
for (const button of viewButtons) {
  button.addEventListener("click", () => {
    act(async () => {
      state.view = button.dataset.view;
      if (state.view === "trips") await loadTrips(false);
      if (state.view === "places") await loadPlaces();
      if (state.view === "ride") {
        // The map was hidden and may have been resized meanwhile. Shown first, then measured again once, here.
        render();
        if (state.map !== null) state.map.invalidateSize({ animate: false, pan: false });
      }
    });
  });
}

// The filters and the buttons of My trips. The selects are written by the browser, and read only here.
tripStatusSelect.addEventListener("change", () => {
  state.tripStatus = tripStatusSelect.value;
  act(() => loadTrips(false));
});
tripPeriodSelect.addEventListener("change", () => {
  state.tripPeriod = tripPeriodSelect.value;
  act(() => loadTrips(false));
});
tripsRefreshButton.addEventListener("click", () => act(() => loadTrips(false)));
tripsMoreButton.addEventListener("click", () => act(() => loadTrips(true)));

// "Save this place" under the pickup and the drop-off: the text of the point as it is now. The label is cleared after a save only.
for (const kind of KINDS) {
  saveForms[kind].addEventListener("submit", (event) => {
    event.preventDefault();
    act(async () => {
      const point = state.points[kind];
      const label = saveInputs[kind].value.trim();
      if (point === null) throw new Error("Choose the place first");
      if (label === "") throw new Error("Type a name for this place first");
      await api("POST", "/saved-places", {
        label,
        address: point.address.slice(0, SAVED_ADDRESS_MAX_LENGTH),
        lat: point.lat,
        lng: point.lng,
      });
      saveInputs[kind].value = "";
      await loadPlaces();
    });
  });
}

// The rename form is written only by the Rename buttons (above, in render) and read here.
renameForm.addEventListener("submit", (event) => {
  event.preventDefault();
  act(async () => {
    try {
      await api("PATCH", `/saved-places/${state.renamingId}`, { label: renameInput.value.trim() });
      state.renamingId = null;
    } catch (err) {
      if (err.status === 404) state.renamingId = null; // deleted meanwhile
      throw err; // for example "name taken": the form stays open
    } finally {
      await loadPlaces();
    }
  });
});
renameCancelButton.addEventListener("click", () =>
  act(async () => {
    state.renamingId = null;
  })
);

// Polling keeps the page current for now; WebSockets replace this in M3.
setInterval(async () => {
  if (refreshing || state.busy) return;
  refreshing = true;
  await refresh();
  refreshing = false;
  render();
}, POLL_MS);

if (state.user !== null && state.user.role === "rider") connect(socketHandlers);

// Back from Stripe's page: ?topup=success&id=N or ?topup=cancelled. The id is only a hint to ask the backend about,
// which checks that the top-up is ours; nothing in the address is believed.
const returned = new URLSearchParams(location.search);
if (returned.has("topup") && state.user !== null && state.user.role === "rider") {
  history.replaceState(null, "", location.pathname);
  if (returned.get("topup") === "cancelled") {
    state.topupNotice = "Top-up cancelled";
  } else if (returned.get("topup") === "success" && /^[0-9]+$/.test(returned.get("id") || "")) {
    act(async () => {
      const topup = await api("POST", `/wallet/topups/${returned.get("id")}/sync`);
      state.topupNotice = topup.status === "SUCCEEDED" ? "Payment received" : "Payment is still being confirmed";
    });
  }
}
render();
refresh().then(render);
