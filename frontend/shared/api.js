// sessionStorage is per tab, so one browser can be rider, driver, and admin in three tabs at once.
const SESSION_KEY = "session";

export function saveSession(token, user) {
  sessionStorage.setItem(SESSION_KEY, JSON.stringify({ token, user }));
}

export function getSession() {
  const saved = sessionStorage.getItem(SESSION_KEY);
  return saved ? JSON.parse(saved) : null;
}

export function clearSession() {
  sessionStorage.removeItem(SESSION_KEY);
}

export async function api(method, path, body) {
  const session = getSession();
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (session) headers["Authorization"] = `Bearer ${session.token}`;

  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  // Empty and non-JSON bodies become null.
  const data = await response.json().catch(() => null);
  if (response.ok) return data;

  // A 401 with a token means it expired or is invalid. A 401 without one is just a wrong password.
  if (response.status === 401 && session) {
    clearSession();
    location.reload();
  }

  let message = `Request failed (${response.status})`;
  const detail = data && data.detail;
  if (typeof detail === "string") {
    message = detail;
  } else if (Array.isArray(detail)) {
    // FastAPI 422: loc is like ["body", "email"], so the last part is the field name.
    message = detail.map((item) => `${item.loc[item.loc.length - 1]}: ${item.msg}`).join("; ");
  }
  const error = new Error(message);
  error.status = response.status;
  throw error;
}
