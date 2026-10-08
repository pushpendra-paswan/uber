import { api, clearSession, getSession, saveSession } from "/shared/api.js";

const POLL_MS = 3000;

const session = getSession();
const state = {
  user: session ? session.user : null,
  loaded: false,
  drivers: [],
  filter: "", // "" means all
  error: "",
  busy: false,
};
let refreshing = false;
let renderedDrivers = ""; // JSON of the drivers last drawn in the table

const message = document.getElementById("message");
const userBar = document.getElementById("user-bar");
const userName = document.getElementById("user-name");
const logoutButton = document.getElementById("logout-button");
const loginSection = document.getElementById("login-section");
const loginForm = document.getElementById("login-form");
const wrongRoleSection = document.getElementById("wrong-role-section");
const wrongRoleText = document.getElementById("wrong-role-text");
const driversSection = document.getElementById("drivers-section");
const statusFilter = document.getElementById("status-filter");
const refreshButton = document.getElementById("refresh-button");
const noDrivers = document.getElementById("no-drivers");
const driversBody = document.getElementById("drivers");

async function refresh() {
  if (!state.user || state.user.role !== "admin") return;
  try {
    state.drivers = await api("GET", "/admin/drivers" + (state.filter ? `?status=${state.filter}` : ""));
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
  const isAdmin = loggedIn && state.user.role === "admin";

  message.hidden = state.error === "";
  message.textContent = state.error;

  loginSection.hidden = loggedIn;
  userBar.hidden = !loggedIn;
  userName.textContent = loggedIn ? state.user.name : "";
  wrongRoleSection.hidden = !loggedIn || isAdmin;
  if (loggedIn) wrongRoleText.textContent = `This account is a ${state.user.role}. Open /${state.user.role}/ instead.`;

  driversSection.hidden = !isAdmin;
  noDrivers.hidden = !state.loaded || state.drivers.length > 0;

  // Only redraw when the data changed, so a poll cannot replace a button between mouse down and mouse up.
  if (JSON.stringify(state.drivers) !== renderedDrivers) {
    renderedDrivers = JSON.stringify(state.drivers);
    const rows = state.drivers.map((driver) => {
      const row = document.createElement("tr");
      const vehicle = driver.vehicle ? driver.vehicle.plate_number : "-";
      for (const text of [driver.id, driver.user.name, driver.user.email, driver.license_number, vehicle, driver.verification_status]) {
        const cell = document.createElement("td");
        cell.textContent = text;
        row.append(cell);
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
    });
    driversBody.replaceChildren(...rows);
  }

  for (const button of document.querySelectorAll("button")) button.disabled = state.busy;
}

loginForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const form = new FormData(loginForm);
  act(async () => {
    const data = await api("POST", "/auth/login", { email: form.get("email"), password: form.get("password") });
    saveSession(data.access_token, data.user);
    state.user = data.user;
    loginForm.reset();
  });
});

logoutButton.addEventListener("click", () => {
  clearSession();
  location.reload();
});

statusFilter.addEventListener("change", () => {
  act(async () => {
    state.filter = statusFilter.value;
  });
});

refreshButton.addEventListener("click", () => act(() => {}));

driversBody.addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (button) act(() => api("POST", `/admin/drivers/${button.dataset.id}/${button.dataset.action}`));
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
