import React, { useEffect, useState, useCallback, useRef } from "react";
import {
  Layout, Table, Button, Input, Select, Card, Row, Col, Statistic,
  message, Space, Modal, Descriptions, Spin, Tabs,
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

  // 防抖搜索
  const handleSearchChange = useCallback((e) => {
    const v = e.target.value;
    setSearchText(v);
    if (searchTimer.current) clearTimeout(searchTimer.current);
    searchTimer.current = setTimeout(() => setSearch(v), 300);
  }, []);

  const [warmupRetries, setWarmupRetries] = useState(0);  // 预热重试进度

  // 拉取板块列表（缓存预热期自动重试，最多 5 次，间隔 3s）
  const loadSectors = useCallback(async () => {
    setLoading(true);
    const MAX_RETRIES = 5;
    for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
      try {
        const data = await fetchSectors(n);
        setSectors(data.sectors || []);
        setLastUpdate(data.last_update || "");
        setWarmupRetries(0);
        message.success(`已加载 ${data.total} 个板块`);
        break;
      } catch (e) {
        const status = e.response?.status;
        if (status === 503 && attempt < MAX_RETRIES) {
          // 缓存预热中，自动重试
          setWarmupRetries(attempt + 1);
          message.loading({
            content: `数据预热中，${3}s 后自动重试 (${attempt + 1}/${MAX_RETRIES})...`,
            key: "warmup",
            duration: 3,
          });
          await new Promise((r) => setTimeout(r, 3000));
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
          <Button icon={<ReloadOutlined />} onClick={loadSectors} loading={loading}>
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
          ]}
        />
      </Content>

      <Footer style={{ textAlign: "center", color: "#999", fontSize: 12 }}>
        westock-monitor · 数据源: westock-data CLI + 腾讯自选股 ·
        最后更新: {lastUpdate || "-"}
      </Footer>
    </Layout>
  );
}
