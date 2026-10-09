import { getSession } from "/shared/api.js";

const WS_URL = `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`;

let socket = null; // the current WebSocket; handlers of any other socket are ignored

// Opens the socket and authenticates. onEvent(type, data) gets every message except auth_ok, pong, and error.
// onStatus("open") comes after auth_ok, onStatus("closed", code) when the socket ends.
// Every handler first checks that its socket is still the current one, so an old socket can never change state.
// No reconnect yet (M3.4): once it closes, events stop until the page is reloaded.
export function connect({ onEvent, onStatus }) {
  disconnect();
  const ws = new WebSocket(WS_URL);
  socket = ws;
  ws.onopen = () => {
    if (socket === ws) ws.send(JSON.stringify({ type: "auth", data: { token: getSession().token } }));
  };
  ws.onmessage = (event) => {
    if (socket !== ws) return;
    const { type, data } = JSON.parse(event.data);
    if (type === "auth_ok") onStatus("open");
    else if (type !== "pong" && type !== "error") onEvent(type, data);
  };
  ws.onclose = (event) => {
    if (socket === ws) onStatus("closed", event.code);
  };
}

// Closes the current socket without reporting.
export function disconnect() {
  const old = socket;
  socket = null; // its handlers now ignore everything
  if (old !== null) old.close();
}
