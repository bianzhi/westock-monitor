import axios from "axios";

const api = axios.create({
  baseURL: "/api",
  timeout: 90000,
});

export const fetchSectors = (n, forceRefresh = false) =>
  api.get("/sectors", { params: { n, force_refresh: forceRefresh } }).then((r) => r.data);

export const fetchSectorDetail = (code, n) =>
  api.get(`/sectors/${code}`, { params: { n } }).then((r) => r.data);

export const fetchSectorMinute = (code, tradeDate) =>
  api
    .get(`/sectors/${code}/minute`, { params: { trade_date: tradeDate } })
    .then((r) => r.data);

export const fetchRealtimeMinute = (tradeDate) =>
  api
    .get("/minute/realtime", { params: { trade_date: tradeDate } })
    .then((r) => r.data);

export const fetchStrengthRanking = (n, top) =>
  api
    .get("/strength/ranking", { params: { n, top } })
    .then((r) => r.data);

export const fetchHealth = () => api.get("/health").then((r) => r.data);
export const fetchConfig = () => api.get("/config").then((r) => r.data);
export const fetchErrors = (limit = 100) => api.get("/errors", { params: { limit } }).then((r) => r.data);

export const refreshSectors = () =>
  api.post("/refresh-sectors").then((r) => r.data);

export const triggerMinuteCollect = () =>
  api.post("/collect/minute").then((r) => r.data);

export const fetchL1Summary = (n) =>
  api.get("/sectors/l1-summary", { params: { n } }).then((r) => r.data);

export const fetchConceptSectors = (n) =>
  api.get("/sectors/concept", { params: { n } }).then((r) => r.data);

export const fetchMinuteCompare = (method, start, end, source, codes, tradeDate) =>
  api
    .get("/minute/compare", { params: { method, start, end, source, codes, trade_date: tradeDate } })
    .then((r) => r.data);

export const focusMinuteCollect = (codes) =>
  api.post("/minute/focus", { codes }).then((r) => r.data);

export const unfocusMinuteCollect = () =>
  api.post("/minute/focus", { codes: [] }).then((r) => r.data);

// 用户偏好
export const fetchUserPrefs = () =>
  api.get("/user/prefs").then((r) => r.data);

export const saveUserPrefs = (prefs) =>
  api.post("/user/prefs", prefs).then((r) => r.data);

// 设置 Supabase auth token 到请求头（AuthGuard 登录后调用）
export const setAuthToken = (token) => {
  if (token) {
    api.defaults.headers.common["Authorization"] = `Bearer ${token}`;
  } else {
    delete api.defaults.headers.common["Authorization"];
  }
};

export default api;
