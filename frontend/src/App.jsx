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

  // 拉取板块列表
  const loadSectors = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchSectors(n);
      setSectors(data.sectors || []);
      setLastUpdate(data.last_update || "");
      message.success(`已加载 ${data.total} 个板块`);
    } catch (e) {
      message.error("加载失败: " + (e.response?.data?.detail || e.message));
    } finally {
      setLoading(false);
    }
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

  // L1 一级行业表格列定义
  const l1Columns = [
    {
      title: "一级行业",
      dataIndex: "l1_name",
      key: "l1_name",
      fixed: "left",
      width: 130,
      sorter: (a, b) => a.l1_name.localeCompare(b.l1_name),
    },
    { title: "板块数", dataIndex: "sector_count", key: "count", width: 70, sorter: (a, b) => a.sector_count - b.sector_count },
    {
      title: "总流通值(亿)",
      dataIndex: "total_circ_mv_yi",
      key: "mv",
      width: 130,
      sorter: (a, b) => (a.total_circ_mv_yi ?? 0) - (b.total_circ_mv_yi ?? 0),
      render: (v) => (v != null ? Number(v).toFixed(0) : "-"),
    },
    {
      title: "总净流入(亿)",
      key: "net",
      width: 140,
      sorter: (a, b) => (a.total_net_flow_yi ?? -1e18) - (b.total_net_flow_yi ?? -1e18),
      render: (_, r) => <NetFlowText value={r.total_net_flow_yi} digits={2} />,
    },
    {
      title: "净额率",
      key: "rate",
      width: 100,
      sorter: (a, b) => (a.net_rate ?? -999) - (b.net_rate ?? -999),
      render: (_, r) => <NetRateText value={r.net_rate} />,
    },
    {
      title: "平均强度",
      dataIndex: "avg_strength_value",
      key: "avg_val",
      width: 100,
      sorter: (a, b) => a.avg_strength_value - b.avg_strength_value,
      render: (v) => v?.toFixed(2),
    },
    {
      title: "强",
      dataIndex: "strong_count",
      key: "strong",
      width: 55,
      sorter: (a, b) => (a.strong_count ?? 0) - (b.strong_count ?? 0),
      render: (v) => v > 0 ? <span style={{ color: "#e74c3c", fontWeight: 600 }}>{v}</span> : v,
    },
    {
      title: "弱",
      dataIndex: "weak_count",
      key: "weak",
      width: 55,
      sorter: (a, b) => (a.weak_count ?? 0) - (b.weak_count ?? 0),
      render: (v) => v > 0 ? <span style={{ color: "#3498db", fontWeight: 600 }}>{v}</span> : v,
    },
    {
      title: "强度分布",
      key: "dist",
      width: 180,
      sorter: (a, b) => {
        const score = (d) => (d["强"]||0)*2 + (d["偏强"]||0)*1 + (d["偏弱"]||0)*-1 + (d["弱"]||0)*-2;
        const da = a.strength_distribution || {};
        const db = b.strength_distribution || {};
        return score(da) - score(db);
      },
      render: (_, r) => {
        const d = r.strength_distribution || {};
        const order = ["强", "偏强", "普通", "偏弱", "弱"];
        return (
          <Space size={2}>
            {order.map((lv) => (
              <span key={lv} style={{
                fontSize: 10, padding: "0 3px", borderRadius: 2,
                background: lv === "强" ? "#e74c3c" : lv === "偏强" ? "#f39c12" : lv === "普通" ? "#95a5a6" : lv === "偏弱" ? "#3498db" : "#2c3e50",
                color: "#fff", opacity: d[lv] > 0 ? 1 : 0.3,
              }}>
                {lv}{d[lv]}
              </span>
            ))}
          </Space>
        );
      },
    },
    {
      title: "最强二级板块",
      key: "top",
      width: 220,
      sorter: (a, b) => {
        const va = a.top_sectors?.[0]?.strength_value ?? -99;
        const vb = b.top_sectors?.[0]?.strength_value ?? -99;
        return va - vb;
      },
      render: (_, r) => (
        <Space direction="vertical" size={1}>
          {(r.top_sectors || []).map((s, i) => (
            <span key={i} style={{ fontSize: 12 }}>
              <a style={{ marginRight: 4 }} onClick={() => { setActiveTab("l2"); setSearch(s.name); setTimeout(() => loadDetail(s.code), 100); }}>
                {s.name}
              </a>
              <StrengthTag level={s.strength_level} value={s.strength_value} />
            </span>
          ))}
        </Space>
      ),
    },
  ];

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
  const expandedRowRender = (record) => {
    if (detailCode !== record.code) {
      return (
        <div style={{ padding: 8 }}>
          <Button size="small" onClick={() => loadDetail(record.code)}>
            加载详情图表
          </Button>
        </div>
      );
    }
    if (detailLoading) {
      return (
        <div style={{ padding: 40, textAlign: "center" }}>
          <Spin tip="加载中..." />
        </div>
      );
    }
    return (
      <div className="chart-container">
        <Row gutter={16}>
          <Col span={12}>
            <DailyHistoryChart history={detailData?.records || []} />
          </Col>
          <Col span={12}>
            <MinuteChart points={minuteData?.points || []} />
          </Col>
        </Row>
        {detailData && (
          <Descriptions
            size="small"
            bordered
            column={4}
            style={{ marginTop: 12 }}
          >
            <Descriptions.Item label="代码">{detailData.code}</Descriptions.Item>
            <Descriptions.Item label="流通市值(亿)">
              {detailData.circ_mv_yi?.toFixed(2) ?? "-"}
            </Descriptions.Item>
            <Descriptions.Item label="规模">
              <ScaleTag scale={detailData.scale} />
            </Descriptions.Item>
            <Descriptions.Item label="强度">
              <StrengthTag
                level={detailData.strength?.level}
                value={detailData.strength?.value}
              />
            </Descriptions.Item>
          </Descriptions>
        )}
      </div>
    );
  };

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
                <>
                  <Row gutter={16} style={{ marginBottom: 16 }}>
                    <Col span={6}>
                      <Card>
                        <Statistic title="一级行业数" value={l1Data.length} />
                      </Card>
                    </Col>
                    <Col span={6}>
                      <Card>
                        <Statistic
                          title="最强行业"
                          value={l1Data[0]?.l1_name || "-"}
                          valueStyle={{ color: "#e74c3c", fontSize: 16 }}
                        />
                      </Card>
                    </Col>
                    <Col span={6}>
                      <Card>
                        <Statistic
                          title="强板块 ≥3 的行业"
                          value={l1Data.filter((l) => l.strong_count >= 3).length}
                          valueStyle={{ color: "#e74c3c" }}
                        />
                      </Card>
                    </Col>
                    <Col span={6}>
                      <Card>
                        <Statistic
                          title="弱板块 ≥3 的行业"
                          value={l1Data.filter((l) => l.weak_count >= 3).length}
                          valueStyle={{ color: "#3498db" }}
                        />
                      </Card>
                    </Col>
                  </Row>

                  <Card title="一级行业聚合宽表" style={{ marginBottom: 16 }}>
                    <Table
                      rowKey="l1_name"
                      columns={l1Columns}
                      dataSource={l1Data}
                      loading={l1Loading}
                      size="small"
                      scroll={{ x: 1100 }}
                      pagination={{
                        pageSize: 31,
                        showSizeChanger: true,
                        pageSizeOptions: [10, 31, 50],
                        showTotal: (total) => `共 ${total} 个一级行业`,
                      }}
                    />
                  </Card>
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
  );
}
