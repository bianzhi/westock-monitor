import React, { useEffect, useState, useCallback, useRef } from "react";
import {
  Layout, Table, Button, Input, Select, Card, Row, Col, Statistic,
  message, Space, Modal, Descriptions, Spin, Tabs, Switch, InputNumber,
} from "antd";
import {
  ReloadOutlined, PlayCircleOutlined, ThunderboltOutlined,
  ArrowUpOutlined, ArrowDownOutlined,
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
import { fetchMinuteCompare, focusMinuteCollect, unfocusMinuteCollect, fetchUserPrefs, saveUserPrefs } from "./api";
import AuthGuard from "./components/AuthGuard";

const { Header, Content, Footer } = Layout;

export default function App() {
  const [sectors, setSectors] = useState([]);
  const [loading, setLoading] = useState(false);
  const [health, setHealth] = useState(null);
  const [n, setN] = useState(5);
  const [search, setSearch] = useState("");
  const [detailCode, setDetailCode] = useState(null);
  const [detailData, setDetailData] = useState(null);
  const [minuteData, setMinuteData] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [lastUpdate, setLastUpdate] = useState("");
  const [searchText, setSearchText] = useState("");
  const searchTimer = useRef(null);
  const [activeTab, setActiveTab] = useState("l2");
  const [l1Data, setL1Data] = useState([]);
  const [l1Loading, setL1Loading] = useState(false);
  const [pageSize, setPageSize] = useState(50);

  // 分时对比页
  const [compareMethod, setCompareMethod] = useState("rank");
  const [compareStart, setCompareStart] = useState(1);
  const [compareEnd, setCompareEnd] = useState(10);
  const [compareMode, setCompareMode] = useState("minute");  // minute / cumulative
  const [compareData, setCompareData] = useState(null);
  const [compareLoading, setCompareLoading] = useState(false);

  // 高频模式
  const [focusEnabled, setFocusEnabled] = useState(false);
  const autoRefreshRef = useRef(null);

  const comparePrefsSaveRef = useRef(null);

  // 加载用户偏好（恢复 m/n）
  useEffect(() => {
    fetchUserPrefs().then((data) => {
      const prefs = data?.prefs || {};
      if (prefs.compare_start != null) setCompareStart(prefs.compare_start);
      if (prefs.compare_end != null) setCompareEnd(prefs.compare_end);
      if (prefs.compare_method) setCompareMethod(prefs.compare_method);
    }).catch(() => {});
  }, []);

  // 自动保存 m/n 到用户偏好（2s 防抖）
  useEffect(() => {
    if (comparePrefsSaveRef.current) clearTimeout(comparePrefsSaveRef.current);
    comparePrefsSaveRef.current = setTimeout(() => {
      saveUserPrefs({
        compare_start: compareStart,
        compare_end: compareEnd,
        compare_method: compareMethod,
      }).catch(() => {});
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
    setCompareLoading(true);
    try {
      const data = await fetchMinuteCompare(compareMethod, compareStart, compareEnd);
      setCompareData(data);
    } catch (e) {
      message.error("加载分时对比失败: " + (e.response?.data?.detail || e.message));
    } finally {
      setCompareLoading(false);
    }
  }, [compareMethod, compareStart, compareEnd]);

  // 防抖搜索
  const handleSearchChange = useCallback((e) => {
    const v = e.target.value;
    setSearchText(v);
    if (searchTimer.current) clearTimeout(searchTimer.current);
    searchTimer.current = setTimeout(() => setSearch(v), 300);
  }, []);

  const [warmupRetries, setWarmupRetries] = useState(0);  // 预热重试进度

  // 拉取板块列表（缓存预热期指数退避重试，最多 20 次，总时长 ~3 分钟）
  const loadSectors = useCallback(async (forceRefresh = false) => {
    setLoading(true);
    const MAX_RETRIES = forceRefresh ? 1 : 20;  // 强制刷新时只试 1 次（后端同步等待）
    for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
      try {
        const data = await fetchSectors(n, forceRefresh);
        setSectors(data.sectors || []);
        setLastUpdate(data.last_update || "");
        setWarmupRetries(0);
        message.success(`已加载 ${data.total} 个板块`);
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
  }, [n]);

  // 健康检查
  const loadHealth = useCallback(async () => {
    try {
      const h = await fetchHealth();
      setHealth(h);
    } catch (e) {
      // ignore
    }
  }, []);

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

  // 初始化
  useEffect(() => {
    loadSectors();
    loadHealth();
    const t = setInterval(loadHealth, 30000);
    return () => clearInterval(t);
  }, [loadSectors, loadHealth]);

  // n 切换时自动刷新已展开的详情
  useEffect(() => {
    if (detailCode) {
      loadDetail(detailCode);
    }
  }, [n]);

  // 加载一级行业聚合数据
  const loadL1Summary = useCallback(async () => {
    setL1Loading(true);
    try {
      const data = await fetchL1Summary(n);
      setL1Data(data.l1_summaries || []);
    } catch (e) {
      message.error("一级行业加载失败: " + (e.response?.data?.detail || e.message));
    } finally {
      setL1Loading(false);
    }
  }, [n]);

  // Tab 切换时加载对应数据
  useEffect(() => {
    if (activeTab === "l1") {
      loadL1Summary();
    }
  }, [activeTab, loadL1Summary]);

  // 表格列定义
  const columns = [
    {
      title: "板块名称",
      dataIndex: "name",
      key: "name",
      fixed: "left",
      width: 140,
      sorter: (a, b) => (a.name || "").localeCompare(b.name || ""),
      render: (text, record) => (
        <a onClick={() => loadDetail(record.code)}>{text}</a>
      ),
      filteredValue: search ? [search] : null,
      onFilter: (val, rec) =>
        rec.name?.includes(val) || rec.code?.includes(val),
    },
    { title: "代码", dataIndex: "code", key: "code", width: 110, sorter: (a, b) => (a.code || "").localeCompare(b.code || "") },
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
      render: (_, r) => <NetRateText value={r.summary_5d?.net_rate} />,
    },
    {
      title: "强度判定",
      key: "strength",
      fixed: "right",
      width: 110,
      sorter: (a, b) => a.strength_value - b.strength_value,
      render: (_, r) => (
        <StrengthTag level={r.strength_level} value={r.strength_value} />
      ),
    },
  ];

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
                message.success(`已刷新 ${r.sectors_count} 个板块`);
                loadSectors();
              } catch (e) {
                message.error("刷新失败: " + e.message);
              }
            }}
          >
            刷新板块列表
          </Button>
        </Space>
      </Header>

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
                    <Space style={{ marginBottom: 12 }}>
                      <Input.Search
                        placeholder="搜索板块名称/代码（实时筛选）"
                        allowClear
                        value={searchText}
                        onChange={handleSearchChange}
                        onSearch={setSearch}
                        style={{ width: 260 }}
                      />
                    </Space>
                    <Table
                      rowKey="code"
                      columns={columns}
                      dataSource={sectors}
                      loading={loading}
                      size="small"
                      scroll={{ x: 1400 }}
                      expandable={{ expandedRowRender, rowExpandable: () => true }}
                      pagination={{
                        pageSize,
                        showSizeChanger: true,
                        pageSizeOptions: [20, 50, 100],
                        showTotal: (total) => `共 ${total} 个板块`,
                        onChange: (page, size) => setPageSize(size),
                        onShowSizeChange: (_, size) => setPageSize(size),
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
                  onSwitchToSector={(s) => { setActiveTab("l2"); setSearch(s.name); setTimeout(() => loadDetail(s.code), 100); }}
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
                      <span>排序方式：</span>
                      <Select
                        value={compareMethod}
                        onChange={(v) => { setCompareMethod(v); }}
                        style={{ width: 160 }}
                        options={[
                          { value: "rank", label: "按今日净流入排名" },
                          { value: "code", label: "按板块编号" },
                        ]}
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
                        style={{ width: 80 }}
                        options={[
                          { value: "minute", label: "每分钟" },
                          { value: "cumulative", label: "累计" },
                        ]}
                      />
                      <Button type="primary" icon={<ReloadOutlined />} onClick={loadCompare} loading={compareLoading}>
                        加载
                      </Button>
                      <Button icon={<ReloadOutlined />} onClick={() => loadCompare()} loading={compareLoading}>
                        刷新
                      </Button>
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
                    title={`板块分时对比 (${compareMethod === "rank" ? "净流入排名" : "编号"} ${compareStart}-${compareEnd})`}
                    mode={compareMode}
                    series={compareData?.series || []}
                    height={520}
                  />
                </>
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
