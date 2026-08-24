import React, { useEffect, useState, useCallback, useRef, useMemo } from "react";
import {
  Layout, Table, Button, Input, Select, Card, Row, Col, Statistic,
  message, Space, Modal, Descriptions, Spin, Tabs, Switch, InputNumber,
  Tooltip, Badge, Drawer, DatePicker, Checkbox,
} from "antd";
import {
  ReloadOutlined, PlayCircleOutlined, ThunderboltOutlined,
  ArrowUpOutlined, ArrowDownOutlined, ApiOutlined, WarningOutlined,
} from "@ant-design/icons";
import dayjs from "dayjs";

import api, {
  fetchSectors, fetchSectorDetail, fetchSectorMinute,
  fetchHealth, refreshSectors, triggerMinuteCollect, fetchL1Summary,
} from "./api";
import { StrengthTag, NetFlowText, NetRateText, ScaleTag } from "./ui";
import { MinuteChart, DailyHistoryChart, NetRateCompareChart } from "./Charts";
import SectorDetail from "./SectorDetail";
import L1Tab from "./L1Tab";
import CompareChart from "./CompareChart";
import DailyChart from "./DailyChart";
import AlertsTab from "./AlertsTab";
import { fetchMinuteCompare, focusMinuteCollect, unfocusMinuteCollect, fetchUserPrefs, saveUserPrefs, fetchConceptSectors, fetchConceptSectorsHistory, fetchSectorsHistory, fetchErrors, fetchWatchlist, addWatchlist, removeWatchlist, refreshConcepts, fetchSectorDailyHistory } from "./api";
import AuthGuard from "./components/AuthGuard";

const { Header, Content, Footer } = Layout;

export default function App() {
  const [sectors, setSectors] = useState([]);
  const [loading, setLoading] = useState(false);
  const [health, setHealth] = useState(null);
  const [n, setN] = useState(5);
  const [selectedDate, setSelectedDate] = useState(null);  // null=今日；否则 YYYY-MM-DD 看历史
  const [search, setSearch] = useState("");
  const [detailCode, setDetailCode] = useState(null);
  const [detailData, setDetailData] = useState(null);
  const [minuteData, setMinuteData] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [lastUpdate, setLastUpdate] = useState("");
  const [searchText, setSearchText] = useState("");
  const searchTimer = useRef(null);
  const [activeTab, setActiveTab] = useState("concept");
  const [l1Data, setL1Data] = useState([]);
  const [l1Loading, setL1Loading] = useState(false);
  const l1LoadedN = useRef(null);  // 记录上次加载的 n 值，避免重复请求
  const [pageSize, setPageSize] = useState(50);
  const [pageNum, setPageNum] = useState(1);
  // 筛选受控状态：columns 每次重建（30s 自动刷新）时保留，不因引用变化重置
  const [strengthFilter, setStrengthFilter] = useState([]);
  const [consecutiveFilter, setConsecutiveFilter] = useState([]);
  const [scaleFilter, setScaleFilter] = useState([]);

  // 概念板块
  const [conceptSectors, setConceptSectors] = useState([]);
  const [conceptLoading, setConceptLoading] = useState(false);

  // 自选板块（watchlist）—— 登录走 Supabase，未登录走 localStorage
  const [watchlist, setWatchlist] = useState([]);
  const watchlistLoadedRef = useRef(false);
  const [watchlistUserId, setWatchlistUserId] = useState(null);

  // 分时图分组（group）：勾选的板块归入「分组」，分时图只显示分组内板块。
  // 纯前端状态，跨 l2/concept Tab 共享；空则不干预分时图原有逻辑。
  const [groupCodes, setGroupCodes] = useState([]);

  const loadWatchlist = useCallback(async () => {
    try {
      const data = await fetchWatchlist();
      if (data.user_id) {
        setWatchlistUserId(data.user_id);
        setWatchlist(data.watchlist || []);
      } else {
        // 未登录：从 localStorage 恢复
        setWatchlistUserId(null);
        const local = JSON.parse(localStorage.getItem("westock_watchlist") || "[]");
        setWatchlist(Array.isArray(local) ? local : []);
      }
      watchlistLoadedRef.current = true;
    } catch (e) {
      // supabase 未配置：静默降级 localStorage
      setWatchlistUserId(null);
      const local = JSON.parse(localStorage.getItem("westock_watchlist") || "[]");
      setWatchlist(Array.isArray(local) ? local : []);
      watchlistLoadedRef.current = true;
    }
  }, []);

  const toggleWatchlist = useCallback(async (code, isAdd) => {
    let next;
    if (isAdd) {
      next = [...watchlist, code];
    } else {
      next = watchlist.filter((c) => c !== code);
    }
    setWatchlist(next);  // 乐观更新
    try {
      if (watchlistUserId) {
        if (isAdd) {
          await addWatchlist([code]);
        } else {
          await removeWatchlist([code]);
        }
      } else {
        localStorage.setItem("westock_watchlist", JSON.stringify(next));
      }
    } catch (e) {
      message.error("自选保存失败: " + (e.response?.data?.detail || e.message));
      // 回滚
      setWatchlist(watchlist);
    }
  }, [watchlist, watchlistUserId]);

  // 分时图分组切换：勾选/取消勾选某板块的「分组」标记（纯前端，跨 Tab 共享）
  const toggleGroup = useCallback((code, checked) => {
    setGroupCodes((prev) =>
      checked ? [...prev, code] : prev.filter((c) => c !== code)
    );
  }, []);

  // 错误日志（开发者诊断，从主 Tab 降级到 Header 抽屉）
  const [errorLogs, setErrorLogs] = useState([]);
  const [errorLoading, setErrorLoading] = useState(false);
  const [errorDrawerOpen, setErrorDrawerOpen] = useState(false);

  const loadErrors = useCallback(async () => {
    setErrorLoading(true);
    try {
      const data = await fetchErrors(100);
      setErrorLogs(data.errors || []);
    } catch (e) {
      message.error("加载错误日志失败");
    } finally {
      setErrorLoading(false);
    }
  }, []);

  const loadConceptSectors = useCallback(async (force = false) => {
    setConceptLoading(true);
    const MAX_RETRIES = 5;
    for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
      try {
        const data = selectedDate
          ? await fetchConceptSectorsHistory(selectedDate, n)
          : await fetchConceptSectors(n);
        setConceptSectors(data.sectors || []);
        setConceptLoading(false);
        return;
      } catch (e) {
        const status = e.response?.status;
        if (status === 503 && attempt < MAX_RETRIES) {
          const delay = Math.min(1000 * Math.pow(1.8, attempt), 8000);
          await new Promise((r) => setTimeout(r, delay));
          continue;
        }
        message.error("概念板块加载失败: " + (e.response?.data?.detail || e.message));
        setConceptLoading(false);
        return;
      }
    }
    setConceptLoading(false);
  }, [n, selectedDate]);

  // 分时对比页
  const [compareMethod, setCompareMethod] = useState("rank");
  const [compareStart, setCompareStart] = useState(1);
  const [compareEnd, setCompareEnd] = useState(10);
  const [compareMode, setCompareMode] = useState("cumulative");  // minute / cumulative
  const [compareSource, setCompareSource] = useState("l2");  // l2 / concept
  const [manualCodes, setManualCodes] = useState("");  // 手动输入板块代码
  const [compareData, setCompareData] = useState(null);
  const [compareLoading, setCompareLoading] = useState(false);
  const compareSeqRef = useRef(0);  // 请求序号，丢弃过期响应避免旧数据覆盖新数据

  // 日线图（板块日级净流入）
  const [dailyCodes, setDailyCodes] = useState("");      // 手动输入板块代码
  const [dailyDays, setDailyDays] = useState(30);         // 近 N 交易日
  const [dailyMode, setDailyMode] = useState("net");      // net / turnover
  const [dailyData, setDailyData] = useState(null);
  const [dailyLoading, setDailyLoading] = useState(false);

  const loadDailyHistory = useCallback(async () => {
    const codes = dailyCodes.trim();
    if (!codes) {
      message.warning("请输入板块代码（逗号分隔，如 pt01801081,pt02003800）");
      return;
    }
    setDailyLoading(true);
    try {
      const data = await fetchSectorDailyHistory(codes, dailyDays);
      setDailyData(data);
    } catch (e) {
      message.error("加载日线图失败: " + (e.response?.data?.detail || e.message));
    } finally {
      setDailyLoading(false);
    }
  }, [dailyCodes, dailyDays]);

  // 跳转到某板块的日线图：设置代码 → 切 daily Tab → 自动加载（直接用入参 code 请求，
  // 避免依赖 dailyCodes 的异步 setState 时序）
  const gotoDaily = useCallback(async (code) => {
    if (!code) return;
    setDailyCodes(code);
    setActiveTab("daily");
    setDailyLoading(true);
    try {
      const data = await fetchSectorDailyHistory(code, dailyDays);
      setDailyData(data);
    } catch (e) {
      message.error("加载日线图失败: " + (e.response?.data?.detail || e.message));
    } finally {
      setDailyLoading(false);
    }
  }, [dailyDays]);

  // 跳转到某板块的分时图：设代码 + 来源 → 切分时对比 Tab（loadCompare 的 useEffect
  // 会因 manualCodes/source 变化自动重新加载，manual 模式只显示该板块）
  const gotoMinute = useCallback((code) => {
    if (!code) return;
    setManualCodes(code);
    setCompareSource(code.startsWith("pt02") ? "concept" : "l2");
    setActiveTab("compare");
  }, []);

  // 高频模式
  const [focusEnabled, setFocusEnabled] = useState(false);
  const autoRefreshRef = useRef(null);

  const comparePrefsSaveRef = useRef(null);

  // 加载用户偏好（恢复 m/n）—— 登录走 Supabase，未登录走 localStorage
  useEffect(() => {
    fetchUserPrefs().then((data) => {
      const prefs = data?.prefs || {};
      // 未登录或 supabase 未配置：从 localStorage 恢复
      if (!data?.user_id && Object.keys(prefs).length === 0) {
        try {
          const local = JSON.parse(localStorage.getItem("westock_prefs") || "{}");
          if (local.compare_start != null) setCompareStart(local.compare_start);
          if (local.compare_end != null) setCompareEnd(local.compare_end);
          if (local.compare_method) setCompareMethod(local.compare_method);
        } catch (e) { /* ignore */ }
        return;
      }
      if (prefs.compare_start != null) setCompareStart(prefs.compare_start);
      if (prefs.compare_end != null) setCompareEnd(prefs.compare_end);
      if (prefs.compare_method) setCompareMethod(prefs.compare_method);
    }).catch(() => {
      // supabase 未配置：静默降级 localStorage
      try {
        const local = JSON.parse(localStorage.getItem("westock_prefs") || "{}");
        if (local.compare_start != null) setCompareStart(local.compare_start);
        if (local.compare_end != null) setCompareEnd(local.compare_end);
        if (local.compare_method) setCompareMethod(local.compare_method);
      } catch (e) { /* ignore */ }
    });
  }, []);

  // 自动保存 m/n 到用户偏好（2s 防抖）—— 登录走 Supabase，未登录走 localStorage
  useEffect(() => {
    if (comparePrefsSaveRef.current) clearTimeout(comparePrefsSaveRef.current);
    comparePrefsSaveRef.current = setTimeout(() => {
      const payload = {
        compare_start: compareStart,
        compare_end: compareEnd,
        compare_method: compareMethod,
      };
      saveUserPrefs(payload).catch(() => {
        // 未登录或 supabase 未配置：降级 localStorage
        try {
          const local = JSON.parse(localStorage.getItem("westock_prefs") || "{}");
          Object.assign(local, payload);
          localStorage.setItem("westock_prefs", JSON.stringify(local));
        } catch (e) { /* ignore */ }
      });
    }, 2000);
  }, [compareStart, compareEnd, compareMethod]);

  // 退出对比 Tab 或关闭高频模式时取消聚焦
  useEffect(() => {
    if (activeTab !== "compare" && focusEnabled) {
      setFocusEnabled(false);
      unfocusMinuteCollect().catch(() => {});
      if (autoRefreshRef.current) clearInterval(autoRefreshRef.current);
    }
  }, [activeTab, focusEnabled]);

  const loadCompare = useCallback(async () => {
    const seq = ++compareSeqRef.current;
    setCompareLoading(true);
    // 分组非空：按分组代码加载（manual 模式，只显示分组内板块）；空则走原有逻辑
    const hasGroup = groupCodes.length > 0;
    const method = hasGroup ? "manual" : (manualCodes.trim() ? "manual" : compareMethod);
    const codes = hasGroup ? groupCodes.join(",") : (manualCodes.trim() || undefined);
    try {
      // 分时接口 trade_date 期望 YYYYMMDD；selectedDate 为 YYYY-MM-DD
      const td = selectedDate ? selectedDate.replace(/-/g, "") : undefined;
      const data = await fetchMinuteCompare(method, compareStart, compareEnd, compareSource, codes, td);
      if (seq === compareSeqRef.current) setCompareData(data);
    } catch (e) {
      if (seq === compareSeqRef.current) message.error("加载分时对比失败: " + (e.response?.data?.detail || e.message));
    } finally {
      if (seq === compareSeqRef.current) setCompareLoading(false);
    }
  }, [compareMethod, compareStart, compareEnd, compareSource, manualCodes, selectedDate, groupCodes]);

  // 筛选条件变化时自动重新加载（防抖 400ms，避免输入代码时每字符都请求）。
  // loadCompare 依赖 compareMethod/start/end/source/manualCodes，任一变化即触发；
  // compareMode（每分钟/累计/净额率）只影响前端渲染，不在此依赖中，故不触发重新拉取。
  useEffect(() => {
    const timer = setTimeout(() => { loadCompare(); }, 400);
    return () => clearTimeout(timer);
  }, [loadCompare]);

  // 防抖搜索
  const handleSearchChange = useCallback((e) => {
    const v = e.target.value;
    setSearchText(v);
    if (searchTimer.current) clearTimeout(searchTimer.current);
    searchTimer.current = setTimeout(() => setSearch(v), 300);
  }, []);

  const [warmupRetries, setWarmupRetries] = useState(0);  // 预热重试进度

  // 拉取板块列表（缓存预热期指数退避重试，最多 20 次，总时长 ~3 分钟）
  // silent=true 时（后台自动刷新/dirty-flag）不弹消息，只在首次/手动刷新时提示
  const loadSectors = useCallback(async (forceRefresh = false, silent = false) => {
    setLoading(true);
    // 历史日期：读落库历史接口（不支持 forceRefresh）
    if (selectedDate && !forceRefresh) {
      try {
        const data = await fetchSectorsHistory(selectedDate, n);
        setSectors(data.sectors || []);
        setLastUpdate(data.last_update || "");
        if (!silent) message.success(`已加载 ${data.total} 个板块（${selectedDate}）`);
      } catch (e) {
        message.error("加载历史板块失败: " + (e.response?.data?.detail || e.message));
      }
      setLoading(false);
      return;
    }
    const MAX_RETRIES = forceRefresh ? 1 : 20;  // 强制刷新时只试 1 次（后端同步等待）
    for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
      try {
        const data = await fetchSectors(n, forceRefresh);
        setSectors(data.sectors || []);
        setLastUpdate(data.last_update || "");
        setWarmupRetries(0);
        if (!silent) {
          message.success(`已加载 ${data.total} 个板块`);
        }
        break;
      } catch (e) {
        const status = e.response?.status;
        if (status === 503 && attempt < MAX_RETRIES && !forceRefresh) {
          setWarmupRetries(attempt + 1);
          // 指数退避：3s → 6s → 12s → 24s → 48s → 60s（封顶）
          const delay = Math.min(3000 * Math.pow(2, attempt), 60000);
          message.loading({
            content: `数据预热中，${Math.round(delay / 1000)}s 后重试 (${attempt + 1}/${MAX_RETRIES})...`,
            key: "warmup",
            duration: Math.round(delay / 1000),
          });
          await new Promise((r) => setTimeout(r, delay));
          continue;
        }
        message.error("加载失败: " + (e.response?.data?.detail || e.message));
      }
    }
    setLoading(false);
  }, [n, selectedDate]);

  // 健康检查 + 盘中宽表 dirty-flag 自动刷新
  // 后台每 60s 刷一次缓存（data_cache），前端 30s 轮询 health 拿 cache_updated；
  // 若 cache_updated 变化（后台有新数据入库）则触发 loadSectors，无需用户手动刷新。
  // 静态字段 lastCacheUpdatedRef 跨轮询保持，比对新旧值判定 dirty。
  const lastCacheUpdatedRef = useRef("");

  const loadHealth = useCallback(async () => {
    try {
      const h = await fetchHealth();
      setHealth(h);
      // dirty-flag：cache_updated 变化 → 触发宽表刷新（静默，不弹消息）
      const newUpdated = h?.cache_updated || "";
      if (newUpdated && newUpdated !== lastCacheUpdatedRef.current) {
        lastCacheUpdatedRef.current = newUpdated;
        // 仅在宽表类 Tab 活跃时刷新，避免切走时无谓请求
        if (activeTab === "l2" || activeTab === "concept") {
          loadSectors(false, true);
        }
      }
    } catch (e) {
      // ignore
    }
  }, [activeTab, loadSectors]);

  // 行展开：加载单板块分钟级
  const loadDetail = useCallback(async (code) => {
    setDetailCode(code);
    setDetailLoading(true);
    setDetailData(null);
    setMinuteData(null);
    try {
      const today = dayjs().format("YYYYMMDD");
      const [detail, minute] = await Promise.all([
        fetchSectorDetail(code, n),
        fetchSectorMinute(code, today),
      ]);
      setDetailData(detail);
      setMinuteData(minute);
    } catch (e) {
      message.error("加载详情失败: " + e.message);
    } finally {
      setDetailLoading(false);
    }
  }, [n]);

  // 初始化（只执行一次；loadHealth 依赖 activeTab，切 Tab 时若重新触发会导致
  // 每次切换都重新 loadSectors 并弹消息——用 ref 保护首次挂载只跑一次）
  const didInitRef = useRef(false);
  useEffect(() => {
    if (didInitRef.current) return;
    didInitRef.current = true;
    loadSectors();
    loadHealth();
    loadWatchlist();  // 自选板块加载（登录走 Supabase，未登录走 localStorage）
    const t = setInterval(loadHealth, 30000);
    return () => clearInterval(t);
  }, [loadSectors, loadHealth, loadWatchlist]);

  // n 切换时自动刷新已展开的详情
  useEffect(() => {
    if (detailCode) {
      loadDetail(detailCode);
    }
  }, [n]);

  // 行展开期间分钟图自动续命：detailCode 就绪且交易中时 60s 轮询重拉 minuteData
  // 盯盘场景：展开某板块后曲线持续生长，无需收起再展开
  useEffect(() => {
    if (!detailCode) return;
    const timer = setInterval(() => {
      // 仅交易中才续命，非交易时段停转避免无谓请求
      if (health?.trading) {
        const today = dayjs().format("YYYYMMDD");
        fetchSectorMinute(detailCode, today)
          .then((m) => setMinuteData(m))
          .catch(() => {});
      }
    }, 60000);
    return () => clearInterval(timer);
  }, [detailCode, health?.trading]);

  // 加载一级行业聚合数据
  // force=true 时跳过本地缓存判断，切 Tab 保证拉到最新
  const loadL1Summary = useCallback(async (force = false) => {
    // 非强制且已缓存且 n 没变 → 跳过（避免无谓请求）
    if (!force && l1Data.length > 0 && l1LoadedN.current === n) return;
    setL1Loading(true);
    try {
      const data = await fetchL1Summary(n);
      setL1Data(data.l1_summaries || []);
      l1LoadedN.current = n;
    } catch (e) {
      message.error("一级行业加载失败: " + (e.response?.data?.detail || e.message));
    } finally {
      setL1Loading(false);
    }
  }, [n, l1Data.length]);

  // Tab 切换时加载对应数据：切到哪个 Tab 就强制拉最新（静默），
  // 保证切回 Tab 展示的是最新数据而非旧缓存
  useEffect(() => {
    if (activeTab === "l1") {
      loadL1Summary(true);   // force：跳过 l1LoadedN 缓存
    }
    if (activeTab === "concept") {
      loadConceptSectors();  // 总是重拉（后端有 45s flow 缓存兜底，成本低）
    }
    if (activeTab === "l2") {
      loadSectors(false, true);  // 静默刷新 l2 宽表
    }
  }, [activeTab, loadL1Summary, loadConceptSectors, loadSectors]);

  // 自选置顶排序：watchlist 里的板块排前面，顺序同 watchlist；其余原序
  const sectorsSorted = useMemo(() => {
    if (!watchlist.length) return sectors;
    const inW = (c) => watchlist.includes(c);
    const starred = sectors.filter((s) => inW(s.code));
    const rest = sectors.filter((s) => !inW(s.code));
    return [...starred, ...rest];
  }, [sectors, watchlist]);

  // 多维度筛选：强度档位 + 连续流入 + 规模 三个条件 AND 过滤。
  // 选中某个/某几个选项后，下方只展示筛选结果（多条件同时生效）。
  const filteredSectors = useMemo(() => sectorsSorted.filter((r) => {
    if (strengthFilter.length && !strengthFilter.includes(r.strength_level)) return false;
    if (consecutiveFilter.length) {
      const n = r.consecutive_inflow_days ?? 0;
      const m = consecutiveFilter.some((v) =>
        v === "5+" ? n >= 5 : v === "3-4" ? n >= 3 && n <= 4 : v === "1-2" ? n >= 1 && n <= 2 : n === 0
      );
      if (!m) return false;
    }
    if (scaleFilter.length && !scaleFilter.includes(r.scale)) return false;
    return true;
  }), [sectorsSorted, strengthFilter, consecutiveFilter, scaleFilter]);

  const filteredConcepts = useMemo(() => conceptSectors.filter((r) => {
    if (strengthFilter.length && !strengthFilter.includes(r.strength_level)) return false;
    if (consecutiveFilter.length) {
      const n = r.consecutive_inflow_days ?? 0;
      const m = consecutiveFilter.some((v) =>
        v === "5+" ? n >= 5 : v === "3-4" ? n >= 3 && n <= 4 : v === "1-2" ? n >= 1 && n <= 2 : n === 0
      );
      if (!m) return false;
    }
    if (scaleFilter.length && !scaleFilter.includes(r.scale)) return false;
    return true;
  }), [conceptSectors, strengthFilter, consecutiveFilter, scaleFilter]);

  // 表格列定义（useMemo 避免每次渲染重建引用导致 Table 闪烁）
  // watchlist 进 deps：勾选状态变了列渲染要更新；置顶排序在 dataSource 处理
  //
  // 动态下拉筛选：统计当前 Tab 数据里各强度档位 / 连续流入天数的实际分布，
  // 下拉只显示存在的值并在值后标注个数（如「强 (5)」）。两个 Tab 共用 columns，
  // 故统计基于当前活跃 Tab 的 dataSource。
  const strengthDist = useMemo(() => {
    const rows = activeTab === "concept" ? conceptSectors : sectorsSorted;
    const dist = {};
    rows.forEach((r) => {
      const lv = r.strength_level;
      if (lv) dist[lv] = (dist[lv] || 0) + 1;
    });
    return dist;
  }, [activeTab, conceptSectors, sectorsSorted]);

  const consecutiveDist = useMemo(() => {
    const rows = activeTab === "concept" ? conceptSectors : sectorsSorted;
    const dist = { "0": 0, "1-2": 0, "3-4": 0, "5+": 0 };
    rows.forEach((r) => {
      const n = r.consecutive_inflow_days ?? 0;
      if (n >= 5) dist["5+"] += 1;
      else if (n >= 3) dist["3-4"] += 1;
      else if (n >= 1) dist["1-2"] += 1;
      else dist["0"] += 1;
    });
    return dist;
  }, [activeTab, conceptSectors, sectorsSorted]);

  const columns = useMemo(() => [
    {
      title: "★",
      key: "watch",
      fixed: "left",
      width: 45,
      render: (_, record) => {
        const inW = watchlist.includes(record.code);
        return (
          <a
            title={inW ? "取消自选" : "加入自选（置顶）"}
            onClick={(e) => { e.stopPropagation(); toggleWatchlist(record.code, !inW); }}
            style={{ fontSize: 16, color: inW ? "#f39c12" : "#ccc", fontWeight: inW ? 700 : 400 }}
          >
            {inW ? "★" : "☆"}
          </a>
        );
      },
    },
    {
      title: "组",
      key: "group",
      fixed: "left",
      width: 45,
      render: (_, record) => (
        <Checkbox
          checked={groupCodes.includes(record.code)}
          onChange={(e) => { toggleGroup(record.code, e.target.checked); }}
          title="勾选加入分时图分组"
        />
      ),
    },
    {
      title: "板块名称",
      dataIndex: "name",
      key: "name",
      fixed: "left",
      width: 140,
      sorter: (a, b) => (a.name || "").localeCompare(b.name || ""),
      render: (text, record) => (
        <a onClick={() => gotoDaily(record.code)}>{text}</a>
      ),
      filteredValue: search ? [search] : null,
      onFilter: (val, rec) => {
        const v = (val || "").toLowerCase();
        return (rec.name || "").toLowerCase().includes(v) || (rec.code || "").toLowerCase().includes(v);
      },
    },
    {
      title: "代码",
      dataIndex: "code",
      key: "code",
      width: 110,
      sorter: (a, b) => (a.code || "").localeCompare(b.code || ""),
      render: (text, record) => (
        <a onClick={() => gotoMinute(record.code)} title="查看分时图">{text}</a>
      ),
    },
    {
      title: "流通值(亿)",
      dataIndex: "circ_mv_yi",
      key: "circ_mv_yi",
      width: 110,
      sorter: (a, b) => (a.circ_mv_yi ?? 0) - (b.circ_mv_yi ?? 0),
      render: (v) => (v != null ? Number(v).toFixed(2) : "-"),
    },
    {
      title: "规模",
      dataIndex: "scale",
      key: "scale",
      width: 80,
      sorter: (a, b) => {
        const order = { "大盘": 0, "中盘": 1, "小盘": 2 };
        return (order[a.scale] ?? 9) - (order[b.scale] ?? 9);
      },
      render: (s) => <ScaleTag scale={s} />,
    },
    {
      title: "涨跌幅",
      dataIndex: "change_pct",
      key: "change_pct",
      width: 90,
      sorter: (a, b) => (a.change_pct ?? -999) - (b.change_pct ?? -999),
      render: (v) => v != null ? (
        <span style={{ color: v > 0 ? "#e74c3c" : v < 0 ? "#2ecc71" : "#95a5a6", fontWeight: 600 }}>
          {v > 0 ? "+" : ""}{v.toFixed(2)}%
        </span>
      ) : "-",
    },
    {
      title: "换手率",
      dataIndex: "turnover_rate",
      key: "turnover_rate",
      width: 90,
      sorter: (a, b) => (a.turnover_rate ?? 0) - (b.turnover_rate ?? 0),
      render: (v) => v != null ? v.toFixed(2) + "%" : "-",
    },
    {
      title: "今日成交额(亿)",
      dataIndex: "today_turnover_yi",
      key: "today_turnover",
      width: 130,
      sorter: (a, b) => (a.today_turnover_yi ?? 0) - (b.today_turnover_yi ?? 0),
      render: (v) => (v != null ? v.toFixed(2) : "-"),
    },
    {
      title: "今日净流入(亿)",
      key: "today_net",
      width: 150,
      sorter: (a, b) => (a.today_net_flow_yi ?? -1e18) - (b.today_net_flow_yi ?? -1e18),
      render: (_, r) => <NetFlowText value={r.today_net_flow_yi} digits={3} />,
    },
    {
      title: "今日净额率",
      key: "today_rate",
      width: 110,
      sorter: (a, b) => (a.today_net_rate ?? -999) - (b.today_net_rate ?? -999),
      render: (_, r) => <NetRateText value={r.today_net_rate} />,
    },
    {
      title: "资金强度",
      key: "fund_strength",
      width: 100,
      sorter: (a, b) => (a.fund_strength ?? -999) - (b.fund_strength ?? -999),
      render: (_, r) => r.fund_strength != null ? (
        <span style={{ color: r.fund_strength > 0 ? "#e74c3c" : "#2ecc71", fontWeight: 600 }}>
          {r.fund_strength.toFixed(3)}%
        </span>
      ) : "-",
    },
    {
      title: "连续流入",
      key: "consecutive",
      width: 90,
      sorter: (a, b) => (a.consecutive_inflow_days ?? 0) - (b.consecutive_inflow_days ?? 0),
      // 下拉筛选（与排序并存）：按连续流入天数分档，动态显示实际分布及个数
      filters: [
        { text: `≥5 天 (${consecutiveDist["5+"]})`, value: "5+" },
        { text: `3-4 天 (${consecutiveDist["3-4"]})`, value: "3-4" },
        { text: `1-2 天 (${consecutiveDist["1-2"]})`, value: "1-2" },
        { text: `0 天 (${consecutiveDist["0"]})`, value: "0" },
      ],
      // 受控筛选：columns 重建（30s 刷新）时保留选中值，不重置
      filteredValue: consecutiveFilter,
      onFilterChange: (vals) => setConsecutiveFilter(vals),
      onFilter: (value, r) => {
        const n = r.consecutive_inflow_days ?? 0;
        if (value === "5+") return n >= 5;
        if (value === "3-4") return n >= 3 && n <= 4;
        if (value === "1-2") return n >= 1 && n <= 2;
        return n === 0;
      },
      render: (_, r) => r.consecutive_inflow_days > 0 ? (
        <span style={{ color: "#e74c3c", fontWeight: 600 }}>
          {r.consecutive_inflow_days} 天
        </span>
      ) : <span style={{ color: "#95a5a6" }}>0</span>,
    },
    {
      title: "背离",
      key: "divergence",
      width: 70,
      sorter: (a, b) => (a.divergence ? 1 : 0) - (b.divergence ? 1 : 0),
      render: (_, r) => r.divergence ? (
        <Tooltip title="资金净流入但板块价格下跌，警惕出货/接盘陷阱">
          <span style={{ color: "#e67e22", fontWeight: 700 }}>⚠</span>
        </Tooltip>
      ) : null,
    },
    {
      title: "近3日净流入(亿)",
      key: "sum3_net",
      width: 160,
      sorter: (a, b) => ((a.summary_3d?.net_flow_yi ?? -1e18) - (b.summary_3d?.net_flow_yi ?? -1e18)),
      render: (_, r) => (
        <NetFlowText
          value={r.summary_3d?.net_flow_yi}
          digits={3}
        />
      ),
    },
    {
      title: "近3日净额率",
      key: "sum3_rate",
      width: 110,
      sorter: (a, b) => ((a.summary_3d?.net_rate ?? -999) - (b.summary_3d?.net_rate ?? -999)),
      render: (_, r) => <NetRateText value={r.summary_3d?.net_rate} />,
    },
    {
      title: "近5日净流入(亿)",
      key: "sum5_net",
      width: 160,
      sorter: (a, b) => ((a.summary_5d?.net_flow_yi ?? -1e18) - (b.summary_5d?.net_flow_yi ?? -1e18)),
      render: (_, r) => (
        <NetFlowText value={r.summary_5d?.net_flow_yi} digits={3} />
      ),
    },
    {
      title: "近5日净额率",
      key: "sum5_rate",
      width: 110,
      sorter: (a, b) => (a.summary_5d?.net_rate ?? -999) - (b.summary_5d?.net_rate ?? -999),
      render: (_, r) => (
        <Space size={2}>
          <NetRateText value={r.summary_5d?.net_rate} />
          {r.estimated && (
            <Tooltip title="缓存空仅今日 fallback，非真多日累加（盘后服务运行后会消失）">
              <span style={{ fontSize: 10, color: "#f39c12", border: "1px dashed #f39c12", borderRadius: 3, padding: "0 2px" }}>估</span>
            </Tooltip>
          )}
        </Space>
      ),
    },
    {
      title: "强度判定",
      key: "strength",
      fixed: "right",
      width: 110,
      // 兜底 undefined：strength_value 缺失时按极值排（与今日净流入列一致）
      sorter: (a, b) => (a.strength_value ?? -1e18) - (b.strength_value ?? -1e18),
      // 下拉筛选（与排序并存）：按强度档位，动态显示实际分布及个数
      filters: Object.entries(strengthDist).map(([lv, cnt]) => ({
        text: `${lv} (${cnt})`,
        value: lv,
      })),
      // 受控筛选：columns 重建（30s 刷新）时保留选中值，不重置
      filteredValue: strengthFilter,
      onFilterChange: (vals) => setStrengthFilter(vals),
      onFilter: (value, r) => r.strength_level === value,
      render: (_, r) => (
        <StrengthTag level={r.strength_level} value={r.strength_value} />
      ),
    },
  ], [watchlist, search, toggleWatchlist, loadDetail, strengthDist, consecutiveDist, strengthFilter, consecutiveFilter, groupCodes, toggleGroup]);

  // 行展开内容
  const expandedRowRender = (record) => (
    <SectorDetail
      record={record}
      detailCode={detailCode}
      detailData={detailData}
      minuteData={minuteData}
      detailLoading={detailLoading}
      onLoadDetail={loadDetail}
    />
  );

  return (
    <AuthGuard>
    <Layout style={{ minHeight: "100vh" }}>
      <Header style={{ background: "#fff", padding: "0 24px", display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <Space>
          <ThunderboltOutlined style={{ fontSize: 24, color: "#e74c3c" }} />
          <h2 style={{ margin: 0 }}>Westock Monitor · 板块资金流监控</h2>
        </Space>
        <Space>
          <Select
            value={n}
            onChange={setN}
            style={{ width: 110 }}
            options={[
              { value: 3, label: "n=3" },
              { value: 5, label: "n=5" },
              { value: 10, label: "n=10" },
              { value: 20, label: "n=20" },
            ]}
          />
          <Tooltip
            title={
              health?.data_sources
                ? Object.entries(health.data_sources).map(([k, v]) => (
                    <div key={k}>
                      <b>{k}</b>:{" "}
                      <span style={{ color: v.status === "ok" ? "#27ae60" : v.status === "degraded" || v.status === "skip" ? "#f39c12" : "#e74c3c" }}>
                        {v.status}
                      </span>
                      {v.version ? ` v=${v.version}` : ""}
                      {v.scope ? ` scope=${v.scope}` : ""}
                      {v.note ? ` · ${v.note}` : ""}
                      {v.error ? ` · err: ${v.error}` : ""}
                    </div>
                  ))
                : "数据源状态加载中..."
            }
          >
            <Badge
              offset={[-4, 4]}
              dot
              status={
                !health?.data_sources
                  ? "default"
                  : Object.values(health.data_sources).some((v) => v.status === "fail")
                  ? "error"
                  : Object.values(health.data_sources).some((v) => v.status === "degraded" || v.status === "skip")
                  ? "warning"
                  : "success"
              }
            >
              <ApiOutlined style={{ fontSize: 18 }} />
            </Badge>
          </Tooltip>
          <Button icon={<ReloadOutlined />} onClick={() => loadSectors(true)} loading={loading}>
            刷新
          </Button>
          <Button
            icon={<PlayCircleOutlined />}
            onClick={async () => {
              try {
                await triggerMinuteCollect();
                message.success("已触发采集");
              } catch (e) {
                message.error("触发失败: " + e.message);
              }
            }}
          >
            采集
          </Button>
          <Button
            onClick={async () => {
              try {
                const r = await refreshSectors();
                // 展示 diff：新增/剔除感知，让用户知道清单变更
                const added = r.added?.length || 0;
                const removed = r.removed?.length || 0;
                if (added || removed) {
                  message.success(
                    `已刷新 ${r.sectors_count} 个板块（新增 ${added} / 剔除 ${removed}）`,
                    8
                  );
                } else {
                  message.success(`已刷新 ${r.sectors_count} 个板块（清单无变更）`);
                }
                loadSectors();
              } catch (e) {
                message.error("刷新失败: " + e.message);
              }
            }}
          >
            刷新板块列表
          </Button>
          <Button
            onClick={() => {
              // 显式 keyword 补全：弹窗输入概念关键词，避免空 body 批量副作用
              let kw = "";
              Modal.confirm({
                title: "刷新概念板块",
                content: (
                  <Input
                    placeholder="输入概念关键词，如：低空经济 / AI 医疗"
                    onChange={(e) => { kw = e.target.value.trim(); }}
                    style={{ marginTop: 8 }}
                  />
                ),
                onOk: async () => {
                  if (!kw) {
                    message.warning("请输入关键词");
                    return Promise.reject();
                  }
                  try {
                    const r = await refreshConcepts(kw);
                    message.success(`已补全「${kw}」：新增 ${r.added?.length || 0} 个，总计 ${r.total_after}`);
                    loadConceptSectors();  // 刷新前端宽表
                  } catch (e) {
                    message.error("刷新概念板块失败: " + (e.response?.data?.detail || e.message));
                    throw e;
                  }
                },
              });
            }}
          >
            刷新概念
          </Button>
          <Tooltip
            title={
              errorLogs.length > 0
                ? `${errorLogs.length} 条错误/警告（开发者诊断）`
                : "无错误日志（开发者诊断）"
            }
          >
            <Badge
              count={errorLogs.length}
              size="small"
              offset={[-2, 2]}
              status={errorLogs.some((e) => e.level === "ERROR" || e.level === "CRITICAL") ? "error" : "warning"}
            >
              <WarningOutlined
                style={{ fontSize: 18, cursor: "pointer", color: errorLogs.length > 0 ? "#f39c12" : "#95a5a6" }}
                onClick={() => {
                  setErrorDrawerOpen(true);
                  if (errorLogs.length === 0) loadErrors();
                }}
              />
            </Badge>
          </Tooltip>
        </Space>
      </Header>

      <Drawer
        title="服务错误日志（开发者诊断面板）"
        placement="right"
        width={620}
        open={errorDrawerOpen}
        onClose={() => setErrorDrawerOpen(false)}
        extra={<Button size="small" onClick={loadErrors} loading={errorLoading}>刷新</Button>}
      >
        <Table
          rowKey={(_, i) => i}
          dataSource={errorLogs}
          size="small"
          loading={errorLoading}
          pagination={{ pageSize: 50, showSizeChanger: false }}
          columns={[
            { title: "时间", dataIndex: "time", key: "time", width: 180 },
            {
              title: "级别", dataIndex: "level", key: "level", width: 80,
              render: (v) => {
                const color = v === "ERROR" || v === "CRITICAL" ? "#e74c3c" : "#f39c12";
                return <span style={{ color, fontWeight: "bold" }}>{v}</span>;
              },
            },
            { title: "消息", dataIndex: "msg", key: "msg", ellipsis: true },
          ]}
        />
      </Drawer>

      <Content className="app-container">
        <Tabs
          activeKey={activeTab}
          onChange={setActiveTab}
          items={[
            {
              key: "l2",
              label: `二级板块 (${sectors.length})`,
              children: (
                <>
                  <Row gutter={16} style={{ marginBottom: 16 }}>
                    <Col span={6}>
                      <Card>
                        <Statistic title="板块总数" value={sectors.length} prefix={<ThunderboltOutlined />} />
                      </Card>
                    </Col>
                    <Col span={6}>
                      <Card>
                        <Statistic
                          title="交易状态"
                          value={health?.trading ? "交易中" : "非交易"}
                          valueStyle={{ color: health?.trading ? "#e74c3c" : "#95a5a6" }}
                        />
                      </Card>
                    </Col>
                    <Col span={6}>
                      <Card>
                        <Statistic
                          title="强档板块数"
                          value={sectors.filter((s) => s.strength_level === "强").length}
                          valueStyle={{ color: "#e74c3c" }}
                        />
                      </Card>
                    </Col>
                    <Col span={6}>
                      <Card>
                        <Statistic
                          title="弱档板块数"
                          value={sectors.filter((s) => s.strength_level === "弱").length}
                          valueStyle={{ color: "#2c3e50" }}
                        />
                      </Card>
                    </Col>
                  </Row>

                  <Card title="板块强度宽表" style={{ marginBottom: 16 }}>
                    <Space style={{ marginBottom: 12 }} wrap>
                      <DatePicker
                        value={selectedDate ? dayjs(selectedDate) : null}
                        onChange={(d) => { setSelectedDate(d ? d.format("YYYY-MM-DD") : null); }}
                        placeholder="选择日期（默认今日）"
                        allowClear
                        style={{ width: 180 }}
                      />
                      <Input.Search
                        placeholder="搜索板块名称/代码（实时筛选）"
                        allowClear
                        value={searchText}
                        onChange={handleSearchChange}
                        onSearch={setSearch}
                        style={{ width: 260 }}
                      />
                      <span>强度：</span>
                      <Select mode="multiple" allowClear value={strengthFilter} onChange={setStrengthFilter}
                        placeholder="全部强度" style={{ minWidth: 150 }}
                        options={[
                          { value: "强", label: "强" },
                          { value: "偏强", label: "偏强" },
                          { value: "普通", label: "普通" },
                          { value: "偏弱", label: "偏弱" },
                          { value: "弱", label: "弱" },
                        ]} />
                      <span>连续流入：</span>
                      <Select mode="multiple" allowClear value={consecutiveFilter} onChange={setConsecutiveFilter}
                        placeholder="全部" style={{ minWidth: 150 }}
                        options={[
                          { value: "5+", label: "≥5 天" },
                          { value: "3-4", label: "3-4 天" },
                          { value: "1-2", label: "1-2 天" },
                          { value: "0", label: "0 天" },
                        ]} />
                      <span>规模：</span>
                      <Select mode="multiple" allowClear value={scaleFilter} onChange={setScaleFilter}
                        placeholder="全部规模" style={{ minWidth: 130 }}
                        options={[
                          { value: "大盘", label: "大盘" },
                          { value: "中盘", label: "中盘" },
                          { value: "小盘", label: "小盘" },
                        ]} />
                    </Space>
                    <Table
                      rowKey="code"
                      columns={columns}
                      dataSource={filteredSectors}
                      loading={loading}
                      size="small"
                      scroll={{ x: 1400 }}
                      expandable={{ expandedRowRender, rowExpandable: () => true }}
                      pagination={{
                        current: pageNum,
                        pageSize,
                        showSizeChanger: true,
                        pageSizeOptions: [20, 50, 100],
                        showTotal: (total) => `共 ${total} 个板块`,
                        onChange: (page, size) => { setPageNum(page); setPageSize(size); },
                        onShowSizeChange: (_, size) => { setPageNum(1); setPageSize(size); },
                      }}
                    />
                  </Card>

                  <Card title="近5日净额率 Top15 对比">
                    <NetRateCompareChart sectors={sectors} top={15} />
                  </Card>
                </>
              ),
            },
            {
              key: "l1",
              label: `一级行业 (${l1Data.length})`,
              children: (
                <L1Tab
                  l1Data={l1Data}
                  l1Loading={l1Loading}
                  onSwitchToSector={(s) => gotoDaily(s.code)}
                  onDrillDownL1={(record) => {
                    // 宏观下钻：点一级行业名 → 切 l2 + 筛选该行业名（行业名即二级板块名前缀）
                    setActiveTab("l2");
                    setSearch(record.l1_name);
                    setSearchText(record.l1_name);
                  }}
                />
              ),
            },
            {
              key: "compare",
              label: "分时对比",
              children: (
                <>
                  <Card size="small" style={{ marginBottom: 12 }}>
                    <Space>
                      <DatePicker
                        value={selectedDate ? dayjs(selectedDate) : null}
                        onChange={(d) => { setSelectedDate(d ? d.format("YYYY-MM-DD") : null); }}
                        placeholder="选择日期（默认今日）"
                        allowClear
                        style={{ width: 180 }}
                      />
                      <span>排序方式：</span>
                      <Select
                        value={compareMethod}
                        onChange={(v) => {
                          setCompareMethod(v);
                          // 排序方式联动展示指标：净额率/资金强度排序 → 图与 tooltip 展示对应指标
                          if (v === "net_rate") setCompareMode("net_rate");
                          else if (v === "fund_strength") setCompareMode("fund_strength");
                          else setCompareMode("cumulative");
                        }}
                        style={{ width: 160 }}
                        options={[
                          { value: "rank", label: "按净流入排名" },
                          { value: "net_rate", label: "按净额率排名" },
                          { value: "fund_strength", label: "按资金强度排名" },
                          { value: "code", label: "按板块编号" },
                        ]}
                        disabled={!!manualCodes.trim()}
                      />
                      <Select
                        value={compareSource}
                        onChange={setCompareSource}
                        style={{ width: 110 }}
                        options={[
                          { value: "l2", label: "二级板块" },
                          { value: "concept", label: "概念板块" },
                        ]}
                      />
                      <Input
                        placeholder="或输入代码/名称（逗号分隔，如 pt02251441,CRO）"
                        value={manualCodes}
                        onChange={(e) => setManualCodes(e.target.value)}
                        style={{ width: 260 }}
                        allowClear
                      />
                      <span>第</span>
                      <Input
                        type="number"
                        min={1}
                        value={compareStart}
                        onChange={(e) => setCompareStart(Number(e.target.value) || 1)}
                        style={{ width: 70 }}
                      />
                      <span>到</span>
                      <Input
                        type="number"
                        min={1}
                        value={compareEnd}
                        onChange={(e) => setCompareEnd(Number(e.target.value) || 1)}
                        style={{ width: 70 }}
                      />
                      <span>个板块</span>
                      <Select
                        value={compareMode}
                        onChange={setCompareMode}
                        style={{ width: 100 }}
                        options={[
                          { value: "minute", label: "每分钟" },
                          { value: "cumulative", label: "累计" },
                          { value: "net_rate", label: "净额率" },
                          { value: "fund_strength", label: "资金强度" },
                        ]}
                      />
                      <Button type="primary" icon={<ReloadOutlined />} onClick={loadCompare} loading={compareLoading}>
                        加载
                      </Button>
                      <Button icon={<ReloadOutlined />} onClick={() => loadCompare()} loading={compareLoading}>
                        刷新
                      </Button>
                      {groupCodes.length > 0 && (
                        <Button
                          onClick={() => { setGroupCodes([]); message.info("已清空分组勾选"); }}
                        >
                          清空分组({groupCodes.length})
                        </Button>
                      )}
                      <span>|</span>
                      <Switch
                        checked={focusEnabled}
                        onChange={async (checked) => {
                          setFocusEnabled(checked);
                          if (checked) {
                            // 开启高频模式：注册选中板块 + 8s 自动轮询
                            const codes = compareData?.series?.map((s) => s.code) || [];
                            if (codes.length > 0) {
                              await focusMinuteCollect(codes);
                              message.info(`已开启高频模式，${codes.length} 个板块 8s 刷新`);
                              autoRefreshRef.current = setInterval(loadCompare, 8000);
                            } else {
                              message.warning("请先加载板块数据");
                              setFocusEnabled(false);
                            }
                          } else {
                            // 关闭高频模式
                            await unfocusMinuteCollect();
                            if (autoRefreshRef.current) clearInterval(autoRefreshRef.current);
                            message.info("已关闭高频模式");
                          }
                        }}
                        checkedChildren="高频"
                        unCheckedChildren="高频"
                      />
                      {focusEnabled && (
                        <span style={{ color: "#e74c3c", fontSize: 12 }}>8s 自动刷新中...</span>
                      )}
                    </Space>
                  </Card>
                  <CompareChart
                    title={
                      (groupCodes.length > 0
                        ? `板块分时对比 (分组: ${groupCodes.length} 个)`
                        : manualCodes.trim()
                          ? `板块分时对比 (指定: ${manualCodes.trim()})`
                          : `板块分时对比 (${compareMethod === "rank" ? "净流入排名" : "编号"} ${compareStart}-${compareEnd})`) +
                      (selectedDate ? ` · ${selectedDate}` : " · 今日")
                    }
                    mode={compareMode}
                    series={compareData?.series || []}
                    height={520}
                  />
                </>
              ),
            },
            {
              key: "concept",
              label: `概念板块 (${conceptSectors.length})`,
              children: (
                <Card title="概念板块宽表" style={{ marginBottom: 16 }}>
                  <Space style={{ marginBottom: 12 }} wrap>
                    <DatePicker
                      value={selectedDate ? dayjs(selectedDate) : null}
                      onChange={(d) => { setSelectedDate(d ? d.format("YYYY-MM-DD") : null); }}
                      placeholder="选择日期（默认今日）"
                      allowClear
                      style={{ width: 180 }}
                    />
                    <Input.Search
                      placeholder="搜索概念板块名称/代码（实时筛选）"
                      allowClear
                      value={searchText}
                      onChange={handleSearchChange}
                      onSearch={setSearch}
                      style={{ width: 260 }}
                    />
                    <span>强度：</span>
                    <Select mode="multiple" allowClear value={strengthFilter} onChange={setStrengthFilter}
                      placeholder="全部强度" style={{ minWidth: 150 }}
                      options={[
                        { value: "强", label: "强" },
                        { value: "偏强", label: "偏强" },
                        { value: "普通", label: "普通" },
                        { value: "偏弱", label: "偏弱" },
                        { value: "弱", label: "弱" },
                      ]} />
                    <span>连续流入：</span>
                    <Select mode="multiple" allowClear value={consecutiveFilter} onChange={setConsecutiveFilter}
                      placeholder="全部" style={{ minWidth: 150 }}
                      options={[
                        { value: "5+", label: "≥5 天" },
                        { value: "3-4", label: "3-4 天" },
                        { value: "1-2", label: "1-2 天" },
                        { value: "0", label: "0 天" },
                      ]} />
                    <span>规模：</span>
                    <Select mode="multiple" allowClear value={scaleFilter} onChange={setScaleFilter}
                      placeholder="全部规模" style={{ minWidth: 130 }}
                      options={[
                        { value: "大盘", label: "大盘" },
                        { value: "中盘", label: "中盘" },
                        { value: "小盘", label: "小盘" },
                      ]} />
                  </Space>
                  <Table
                    rowKey="code"
                    columns={columns}
                    dataSource={filteredConcepts}
                    loading={conceptLoading}
                    size="small"
                    scroll={{ x: 1400 }}
                    expandable={{ expandedRowRender, rowExpandable: () => true }}
                    pagination={{
                      current: pageNum,
                      pageSize, showSizeChanger: true, pageSizeOptions: [20, 50, 100],
                      showTotal: (total) => `共 ${total} 个概念板块`,
                      onChange: (page, size) => { setPageNum(page); setPageSize(size); },
                      onShowSizeChange: (_, size) => { setPageNum(1); setPageSize(size); },
                    }}
                  />
                </Card>
              ),
            },
            {
              key: "alerts",
              label: `档位告警`,
              children: <AlertsTab />,
            },
            {
              key: "daily",
              label: "日线图",
              children: (
                <Card title="板块日级净流入折线图" style={{ marginBottom: 16 }}>
                  <Space style={{ marginBottom: 12 }} wrap>
                    <span>板块代码：</span>
                    <Input
                      placeholder="逗号分隔，如 pt01801081,pt02003800"
                      value={dailyCodes}
                      onChange={(e) => setDailyCodes(e.target.value)}
                      style={{ width: 320 }}
                      allowClear
                    />
                    <span>近</span>
                    <InputNumber
                      min={1} max={60} value={dailyDays}
                      onChange={(v) => setDailyDays(Number(v) || 30)}
                      style={{ width: 70 }}
                    />
                    <span>个交易日</span>
                    <Select
                      value={dailyMode}
                      onChange={setDailyMode}
                      style={{ width: 130 }}
                      options={[
                        { value: "net", label: "主力净流入(亿)" },
                        { value: "turnover", label: "成交额(亿)" },
                      ]}
                    />
                    <Button type="primary" onClick={loadDailyHistory} loading={dailyLoading}>
                      加载
                    </Button>
                  </Space>
                  <DailyChart
                    title="板块日级净流入"
                    mode={dailyMode}
                    series={dailyData?.series || []}
                    height={520}
                  />
                </Card>
              ),
            },
          ]}
        />
      </Content>

      <Footer style={{ textAlign: "center", color: "#999", fontSize: 12 }}>
        westock-monitor · 数据源: westock-data CLI + 腾讯自选股 ·
        最后更新: {lastUpdate || "-"}
      </Footer>
    </Layout>
    </AuthGuard>
  );
}
