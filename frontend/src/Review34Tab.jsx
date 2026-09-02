import React, { useEffect, useState, useCallback } from "react";
import { Card, Table, Space, Button, DatePicker, Statistic, Tag, message, Row, Col, Spin, Select, Tooltip } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import { fetchReview34 } from "./api";

// 红涨绿跌
const UP = "#e74c3c";
const DOWN = "#2ecc71";
const WARN = "#f0a020";

const STAGE_OPTIONS = [
  { value: 1, label: "1进2（首板晋级）" },
  { value: 2, label: "2进3" },
  { value: 3, label: "3进4" },
  { value: 4, label: "4进5" },
];

// 评分计算细节（Tooltip 内容）
function ScoreDetail({ detail }) {
  if (!detail || !detail.length) return null;
  return (
    <div style={{ maxWidth: 340 }}>
      {detail.map((d, i) => (
        <div key={i} style={{ lineHeight: 1.7 }}>
          <b>{d.label}</b>：
          {d.score != null ? `${d.score}/${d.max}分` : "待补"}
          <span style={{ color: "#aaa", marginLeft: 6 }}>{d.desc}</span>
        </div>
      ))}
    </div>
  );
}

/**
 * 连板复盘页（理念源自《3j4_复盘》套表，扩展为 1进2/2进3/3进4/4进5 通用）：
 *   S1 大盘环境（能否做）—— 情绪锚点 + 操作/仓位建议
 *   S2 板块强度资金梯队（在哪做）—— 板块评分 + 主线判断
 *   S3 个股 N 板筛选（做哪只）—— 封板质量/换手/炸板 + 入选判断
 * 数据由 /api/review34 从落库涨停池聚合计算，只读库不调外部接口。
 */
export default function Review34Tab() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [date, setDate] = useState(dayjs());
  const [stage, setStage] = useState(3);

  const load = useCallback(async (d, s) => {
    setLoading(true);
    try {
      const ds = d ? d.format("YYYY-MM-DD") : undefined;
      setData(await fetchReview34(ds, s));
    } catch (e) {
      message.error("连板复盘加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(date, stage); }, [date, stage, load]);

  const market = data?.market;
  const sectors = data?.sectors || [];
  const stocks = data?.stocks || [];
  const p = market?.promotion || {};

  const fmtRate = (v) => (v == null ? "-" : `${v}%`);

  const scoreRender = (v, r) => (
    <Tooltip title={<ScoreDetail detail={r.score_detail} />}>
      <span style={{ fontWeight: 600, color: v >= 7 ? UP : WARN, cursor: "help" }}>{v}</span>
    </Tooltip>
  );

  const sectorColumns = [
    { title: "板块", dataIndex: "name", key: "name", width: 120, fixed: "left" },
    {
      title: "涨停家数", dataIndex: "limit_up_count", key: "limit_up_count", width: 90,
      sorter: (a, b) => (a.limit_up_count ?? 0) - (b.limit_up_count ?? 0),
      render: (v) => <span style={{ color: UP, fontWeight: 600 }}>{v}</span>,
    },
    { title: "炸板", dataIndex: "zhap_count", key: "zhap_count", width: 70 },
    { title: "炸板率", dataIndex: "zhap_rate", key: "zhap_rate", width: 90, render: (v) => (v != null ? `${v}%` : "-") },
    {
      title: "梯队(5+/4/3/2/1)", key: "lbcd", width: 150,
      render: (_, r) => {
        const d = r.lbc_dist || {};
        return `${d["5+"]}/${d["4"]}/${d["3"]}/${d["2"]}/${d["1"]}`;
      },
    },
    { title: "梯队", dataIndex: "ladder", key: "ladder", width: 90 },
    {
      title: "资金强度", dataIndex: "fund_strength", key: "fund_strength", width: 100,
      sorter: (a, b) => (a.fund_strength ?? 0) - (b.fund_strength ?? 0),
      render: (v) => (v != null ? <span style={{ color: v >= 0 ? UP : DOWN }}>{v}%</span> : "-"),
    },
    {
      title: "主力净流入(亿)", dataIndex: "main_net_inflow", key: "main_net_inflow", width: 120,
      render: (v) => (v != null ? (v / 1e8).toFixed(2) : "-"),
    },
    {
      title: "评分", dataIndex: "score", key: "score", width: 80,
      sorter: (a, b) => (a.score ?? 0) - (b.score ?? 0),
      render: scoreRender,
    },
    { title: "主线", dataIndex: "is_main", key: "is_main", width: 80, render: (v) => (v ? <Tag color="red">主线</Tag> : "-") },
  ];

  const stockColumns = [
    { title: "代码", dataIndex: "code", key: "code", width: 100 },
    { title: "名称", dataIndex: "name", key: "name", width: 100 },
    { title: "行业", dataIndex: "hybk", key: "hybk", width: 100 },
    { title: "流通值(亿)", dataIndex: "ltsz_yi", key: "ltsz_yi", width: 100 },
    { title: "换手率", dataIndex: "turnover_rate", key: "turnover_rate", width: 90, render: (v) => (v != null ? `${v}%` : "-") },
    {
      title: "封板质量", dataIndex: "seal_quality", key: "seal_quality", width: 110,
      render: (v) => {
        const c = v === "硬板" ? UP : v === "回封板" ? WARN : DOWN;
        return <span style={{ color: c }}>{v}</span>;
      },
    },
    { title: "炸板次数", dataIndex: "zbc", key: "zbc", width: 90, render: (v) => <span style={{ color: v >= 3 ? DOWN : undefined }}>{v}</span> },
    {
      title: "封单资金(亿)", dataIndex: "fund", key: "fund", width: 110,
      render: (v) => (v != null ? (v / 1e8).toFixed(2) : "-"),
    },
    { title: "封单率", dataIndex: "fund_rate", key: "fund_rate", width: 90, render: (v) => (v != null ? `${v}%` : "-") },
    {
      title: "成交额(亿)", dataIndex: "amount", key: "amount", width: 110,
      render: (v) => (v != null ? (v / 1e8).toFixed(2) : "-"),
    },
    {
      title: "主力净流入(亿)", dataIndex: "main_net_inflow", key: "main_net_inflow", width: 120,
      render: (v) => (v != null ? <span style={{ color: v > 0 ? UP : v < 0 ? DOWN : "#95a5a6" }}>{(v / 1e8).toFixed(2)}</span> : "-"),
    },
    { title: "净额率", dataIndex: "net_rate", key: "net_rate", width: 90, render: (v) => (v != null ? `${v}%` : "-") },
    {
      title: "评分", dataIndex: "score", key: "score", width: 80,
      sorter: (a, b) => (a.score ?? 0) - (b.score ?? 0),
      render: (v, r) => (
        <Tooltip title={<ScoreDetail detail={r.score_detail} />}>
          <span style={{ fontWeight: 600, cursor: "help" }}>{v}</span>
        </Tooltip>
      ),
    },
    {
      title: "结论", key: "verdict", width: 100,
      render: (_, r) => {
        const v = r.verdict;
        if (v === "初筛通过") return <Tag color="green">初筛通过</Tag>;
        if (v === "否决") return <Tag color="red">否决</Tag>;
        if (v === "观察") return <Tag color="orange">观察</Tag>;
        return <Tag>剔除</Tag>;
      },
    },
  ];

  return (
    <div>
      <Card
        title="大盘环境（S1 · 能否做）"
        size="small"
        style={{ marginBottom: 12 }}
        extra={
          <Space>
            <DatePicker value={date} onChange={(d) => setDate(d || dayjs())} allowClear={false} style={{ width: 140 }} />
            <Select
              value={stage}
              onChange={(v) => setStage(v)}
              style={{ width: 170 }}
              options={STAGE_OPTIONS}
            />
            <Button icon={<ReloadOutlined />} onClick={() => load(date, stage)} loading={loading}>刷新</Button>
          </Space>
        }
      >
        {market ? (
          <>
            <Row gutter={[16, 8]}>
              <Col><Statistic title="涨停家数" value={market.limit_up_count} valueStyle={{ color: UP }} /></Col>
              <Col><Statistic title="炸板家数" value={market.zhap_ban_count} valueStyle={{ color: WARN }} /></Col>
              <Col><Statistic title="跌停家数" value={market.limit_down_count} valueStyle={{ color: DOWN }} /></Col>
              <Col><Statistic title="封板率" value={market.seal_rate} suffix="%" /></Col>
              <Col><Statistic title="炸板率" value={market.zhap_rate} suffix="%" /></Col>
              <Col><Statistic title="最高连板" value={market.max_lbc} suffix="板" /></Col>
            </Row>
            <div style={{ marginTop: 12 }}>
              晋级率：
              <Tag>1进2 {fmtRate(p.r12)}</Tag>
              <Tag>2进3 {fmtRate(p.r23)}</Tag>
              <Tag color="red">3进4 {fmtRate(p.r34)}（情绪锚点）</Tag>
              <Tag>4进5 {fmtRate(p.r45)}</Tag>
              {market.up_down_ratio != null && <Tag>涨跌比 {market.up_down_ratio}</Tag>}
            </div>
            <div style={{ marginTop: 12, padding: 12, background: "#fafafa", borderRadius: 6 }}>
              <Space size="large" wrap>
                <span>情绪阶段：<b style={{ fontSize: 18, color: UP }}>{market.emotion_stage}</b></span>
                <span>操作建议：<b>{market.action_advice}</b></span>
                <span>仓位建议：<b>{market.position_advice}</b></span>
              </Space>
              {market.notes && market.notes.length > 0 && (
                <div style={{ marginTop: 8, color: "#666", fontSize: 12 }}>{market.notes.join("；")}</div>
              )}
            </div>
          </>
        ) : (
          <Spin />
        )}
      </Card>

      <Card title="板块强度资金梯队（S2 · 在哪做）" size="small" style={{ marginBottom: 12 }}>
        <Table
          rowKey="name"
          columns={sectorColumns}
          dataSource={sectors}
          loading={loading}
          size="small"
          scroll={{ x: 1200 }}
          pagination={{ pageSize: 20, showSizeChanger: false }}
        />
      </Card>

      <Card title={`个股${stage}板筛选（S3 · 做哪只）`} size="small">
        <Table
          rowKey="code"
          columns={stockColumns}
          dataSource={stocks}
          loading={loading}
          size="small"
          scroll={{ x: 1500 }}
          pagination={false}
        />
      </Card>
    </div>
  );
}
