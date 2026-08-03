import React from "react";
import { Row, Col, Card, Statistic, Table, Space } from "antd";
import { StrengthTag, NetFlowText, NetRateText } from "./ui";

/**
 * 一级行业聚合视图
 * props: l1Data, l1Loading, onSwitchToSector
 */
export default function L1Tab({ l1Data, l1Loading, onSwitchToSector }) {
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
              <a style={{ marginRight: 4 }} onClick={() => onSwitchToSector(s)}>
                {s.name}
              </a>
              <StrengthTag level={s.strength_level} value={s.strength_value} />
            </span>
          ))}
        </Space>
      ),
    },
  ];

  return (
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
  );
}
