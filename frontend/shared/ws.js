// Relative, so this resolves to the same module instance as the pages' "/shared/api.js" import.
import { clearSession, getSession } from "./api.js";

const BASE_DELAY_MS = 1000;
const MAX_DELAY_MS = 30000;
const STABLE_AFTER_MS = 10000; // a connection must stay authenticated this long before the backoff starts over
const AUTH_TIMEOUT_MS = 10000; // covers a connect that hangs and a server that never answers "auth"
const PING_INTERVAL_MS = 20000;
const PONG_TIMEOUT_MS = 8000; // so a dead connection is noticed within PING_INTERVAL_MS + PONG_TIMEOUT_MS
const CLOSE_BAD_MESSAGE = 4400;
const CLOSE_UNAUTHORIZED = 4401;
const CLOSE_REPLACED = 4409;
const CLOSE_WATCHDOG = 4000; // our own code for "gave up on this connection", never sent by the server

const WS_URL = `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`;

let handlers = null; // { onEvent, onStatus }; null after disconnect(), so nothing is ever called back
let socket = null; // the one socket that is connecting or open; handlers of any other socket are ignored
let phase = "idle"; // "idle", "connecting", "open", "waiting" (for a retry), "closed" (4409 or 4400)
let closedCode = null; // the code of a terminal close
let attempt = 0; // retries since the last stable connection
let lost = false; // true once a connection was lost after connect(), so the next open reports reconnected
let authenticatedAt = 0; // Date.now() at auth_ok of the current socket
let retryTimer = null;
let watchdog = null; // one per connection: the connect/auth deadline first, then the pong deadline after each ping
let heartbeat = null;

// Stops any previous connection, then connects. onEvent(type, data) gets every message except auth_ok, pong, and error.
// onStatus(status, info) is called once per change: "connecting" {attempt}, "open" {reconnected},
// "reconnecting" {code, attempt, retryInSeconds}, "closed" {code} (4409 and 4400 only, no automatic retry).
export function connect({ onEvent, onStatus }) {
  disconnect();
  handlers = { onEvent, onStatus };
  attempt = 0;
  lost = false;
  openSocket();
}

// Clears every timer and closes the socket. After it returns nothing is called back, ever.
export function disconnect() {
  clearTimeout(retryTimer);
  clearTimeout(watchdog);
  clearInterval(heartbeat);
  retryTimer = watchdog = heartbeat = null;
  if (socket !== null) {
    socket.onopen = socket.onmessage = socket.onclose = socket.onerror = null;
    try {
      socket.close();
    } catch {
      // closing can throw; the socket is detached either way
    }
  }
  socket = null;
  handlers = null;
  phase = "idle";
}

// Connects now: for the Reconnect button, and for the listeners below.
export function reconnectNow() {
  if (handlers === null || socket !== null) return; // disconnected, or already connecting or open
  clearTimeout(retryTimer);
  retryTimer = null;
  attempt = 0;
  openSocket();
}

function openSocket() {
  if (handlers === null || socket !== null) return;
  // Read at every attempt, never cached. No session means a logout is in progress, so say nothing.
  const session = getSession();
  if (session === null) {
    phase = "idle";
    return;
  }
  const ws = new WebSocket(WS_URL);
  socket = ws;
  phase = "connecting";
  watchdog = setTimeout(() => connectionLost(CLOSE_WATCHDOG), AUTH_TIMEOUT_MS);

  ws.onopen = () => {
    if (socket === ws) ws.send(JSON.stringify({ type: "auth", data: { token: session.token } }));
  };
  ws.onmessage = (event) => {
    if (socket !== ws) return;
    const { type, data } = JSON.parse(event.data);
    // Before auth_ok the connect/auth watchdog keeps running: only auth_ok proves the server accepted us.
    if (phase === "connecting" && type !== "auth_ok") return;
    // Any frame proves the connection is alive.
    clearTimeout(watchdog);
    watchdog = null;
    if (type === "auth_ok") {
      phase = "open";
      authenticatedAt = Date.now();
      heartbeat = setInterval(sendPing, PING_INTERVAL_MS);
      console.info(`ws: open${lost ? " (reconnected)" : ""}`);
      handlers.onStatus("open", { reconnected: lost });
    } else if (type !== "pong" && type !== "error") {
      handlers.onEvent(type, data);
    }
  };
  ws.onclose = (event) => {
    if (socket === ws) connectionLost(event.code);
  };

  console.info(`ws: connecting (attempt ${attempt})`);
  handlers.onStatus("connecting", { attempt });
}

// The heartbeat, and the listeners below when a tab wakes up. Arms the pong deadline unless one is already running.
function sendPing() {
  if (phase !== "open") return;
  socket.send(JSON.stringify({ type: "ping", data: {} }));
  if (watchdog === null) watchdog = setTimeout(() => connectionLost(CLOSE_WATCHDOG), PONG_TIMEOUT_MS);
}

// The one place a connection ends: a close event or the watchdog. It does not wait for the browser's close event,
// because a half-dead connection can take minutes to report one: the old socket is detached first, so nothing
// it does later can reach us, and the loss is handled right away.
function connectionLost(code) {
  if (socket === null) return;
  clearTimeout(watchdog);
  clearInterval(heartbeat);
  watchdog = heartbeat = null;
  socket.onopen = socket.onmessage = socket.onclose = socket.onerror = null;
  try {
    socket.close();
  } catch {
    // closing can throw; the socket is detached either way
  }
  socket = null;
  lost = true;
  // A server that accepts us and drops us right away must keep escalating the backoff.
  if (phase === "open" && Date.now() - authenticatedAt >= STABLE_AFTER_MS) attempt = 0;

  // The callbacks come last in every branch: they may call disconnect() or connect() themselves.
  if (code === CLOSE_UNAUTHORIZED) {
    // Same as a REST 401 in api.js. No retry; the reload shows the login form.
    handlers = null;
    phase = "idle";
    console.info("ws: unauthorized (4401), clearing the session");
    clearSession();
    location.reload();
  } else if (code === CLOSE_REPLACED || code === CLOSE_BAD_MESSAGE) {
    // 4409: retrying would make tabs kick each other forever. 4400: our client never sends one, so it is a bug.
    phase = "closed";
    closedCode = code;
    console.info(`ws: closed (${code}), no automatic retry`);
    handlers.onStatus("closed", { code });
  } else {
    // Equal jitter: half of the delay is fixed, half is random, so many clients do not retry in step.
    const cap = Math.min(MAX_DELAY_MS, BASE_DELAY_MS * 2 ** attempt);
    const delay = cap / 2 + (Math.random() * cap) / 2;
    attempt++;
    phase = "waiting";
    retryTimer = setTimeout(() => {
      retryTimer = null;
      openSocket();
    }, delay);
    console.info(`ws: lost (code ${code}), retry ${attempt} in ${Math.round(delay / 1000)} s`);
    handlers.onStatus("reconnecting", { code, attempt, retryInSeconds: Math.round(delay / 1000) });
  }
}

// A sleeping laptop or a phone coming back leaves the connection in an unknown state. The same body for both events.
const wake = (event) => {
  if (event.type === "visibilitychange" && document.visibilityState === "hidden") return;
  if (handlers === null) return;
  if (phase === "waiting" || (phase === "closed" && closedCode === CLOSE_REPLACED)) reconnectNow();
  else if (phase === "open") sendPing(); // verifies the connection: no frame within the pong deadline means it is dead
};
document.addEventListener("visibilitychange", wake);
window.addEventListener("online", wake);
