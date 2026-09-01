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

export const refreshConcepts = (keyword = "") =>
  api.post("/refresh-concepts", { keyword }).then((r) => r.data);

export const triggerMinuteCollect = () =>
  api.post("/collect/minute").then((r) => r.data);

export const fetchL1Summary = (n) =>
  api.get("/sectors/l1-summary", { params: { n } }).then((r) => r.data);

export const fetchConceptSectors = (n) =>
  api.get("/sectors/concept", { params: { n } }).then((r) => r.data);

export const fetchSectorsHistory = (date, n) =>
  api.get("/sectors/history", { params: { date, n } }).then((r) => r.data);

export const fetchConceptSectorsHistory = (date, n) =>
  api.get("/sectors/concept/history", { params: { date, n } }).then((r) => r.data);

export const fetchLimitUp = (date) =>
  api.get("/limit-up", { params: { date } }).then((r) => r.data);

export const fetchLimitUpSummary = (date) =>
  api.get("/limit-up/summary", { params: { date } }).then((r) => r.data);

// 大盘概况：核心指数 + 市场情绪 + 资金面 + 强弱分布
export const fetchMarketOverview = () =>
  api.get("/market-overview").then((r) => r.data);

export const fetchSectorDailyHistory = (codes, days = 30) =>
  api
    .get("/sector-daily-history", { params: { codes, days } })
    .then((r) => r.data);

export const fetchMinuteCompare = (method, start, end, source, codes, tradeDate) =>
  api
    .get("/minute/compare", { params: { method, start, end, source, codes, trade_date: tradeDate } })
    .then((r) => r.data);

export const focusMinuteCollect = (codes) =>
  api.post("/minute/focus", { codes }).then((r) => r.data);

export const unfocusMinuteCollect = () =>
  api.post("/minute/focus", { codes: [] }).then((r) => r.data);

// 强度档位告警
export const fetchAlerts = (limit = 100) =>
  api.get("/alerts", { params: { limit } }).then((r) => r.data);

export const fetchUserAlerts = () =>
  api.get("/user/alerts").then((r) => r.data);

export const saveUserAlerts = (alerts) =>
  api.post("/user/alerts", alerts).then((r) => r.data);

// 自选板块（watchlist）
export const fetchWatchlist = () =>
  api.get("/user/watchlist").then((r) => r.data);

export const addWatchlist = (codes) =>
  api.post("/user/watchlist", { codes }).then((r) => r.data);

export const removeWatchlist = (codes) =>
  api.delete("/user/watchlist", { codes }).then((r) => r.data);

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
