const AUTH_KEY = 'westock_auth';

function loadAuth() {
  try {
    const raw = localStorage.getItem(AUTH_KEY);
    if (!raw) return { user: null, session: null };
    return JSON.parse(raw);
  } catch {
    return { user: null, session: null };
  }
}

function saveAuth(user, session) {
  try {
    localStorage.setItem(AUTH_KEY, JSON.stringify({ user, session }));
  } catch {
    // ignore
  }
}

let _user = null;
let _session = null;
let _listeners = [];

const initial = loadAuth();
_user = initial.user;
_session = initial.session;

export function getAuth() {
  return { user: _user, session: _session };
}

export function setAuth(user, session) {
  _user = user;
  _session = session;
  saveAuth(user, session);
  _listeners.forEach((fn) => fn(user, session));
}

export function clearAuth() {
  _user = null;
  _session = null;
  saveAuth(null, null);
  _listeners.forEach((fn) => fn(null, null));
}

export function onAuthChange(fn) {
  _listeners.push(fn);
  return () => {
    _listeners = _listeners.filter((f) => f !== fn);
  };
}
