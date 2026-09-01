import React, { useEffect, useState, useCallback, useRef } from "react";
import { Card, Row, Col, Statistic, Tag, Space, Button, Empty, Tooltip } from "antd";
import { ReloadOutlined, ClockCircleOutlined } from "@ant-design/icons";
import { fetchMarketOverview } from "./api";

// A股颜色约定：红涨绿跌（与全站 net-positive/net-negative 一致）
const UP = "#e74c3c";
const DOWN = "#2ecc71";
const FLAT = "#95a5a6";

const signColor = (v) => (v > 0 ? UP : v < 0 ? DOWN : FLAT);
const fmtSigned = (v, digits = 2) => {
  if (v == null || Number.isNaN(v)) return "-";
  return (v > 0 ? "+" : "") + Number(v).toFixed(digits);
};

/**
 * 核心指数卡片：名称 + 点位 + 涨跌额/涨跌幅 + 成交额 + 均线位置
 */
function IndexCard({ idx, ma }) {
  const color = signColor(idx.change_pct ?? 0);
  return (
    <Card size="small" style={{ textAlign: "center" }} bodyStyle={{ padding: "10px 6px" }}>
      <div style={{ fontSize: 13, color: "#555", marginBottom: 4, whiteSpace: "nowrap" }}>{idx.name}</div>
      <div style={{ fontSize: 22, fontWeight: 700, color, lineHeight: 1.15 }}>
        {idx.price != null ? idx.price.toFixed(2) : "-"}
      </div>
      <div style={{ fontSize: 12, color, marginTop: 3 }}>
        {fmtSigned(idx.change_amount)}&nbsp;
        <span style={{ fontWeight: 600 }}>{fmtSigned(idx.change_pct)}%</span>
      </div>
      <div style={{ fontSize: 11, color: "#999", marginTop: 3 }}>
        成交 {idx.amount_yi != null ? Number(idx.amount_yi).toFixed(0) : "-"}亿
      </div>
      {ma && (ma.ma5 != null || ma.ma20 != null) && (
        <div style={{ fontSize: 10, marginTop: 3, whiteSpace: "nowrap" }}>
          <span style={{ color: "#aaa" }}>5日</span>
          <span style={{ color: ma.above_ma5 === true ? UP : ma.above_ma5 === false ? DOWN : FLAT }}>
            {ma.ma5 != null ? ma.ma5.toFixed(0) : "-"}
          </span>
          <span style={{ color: "#aaa" }}> · 20日</span>
          <span style={{ color: ma.above_ma20 === true ? UP : ma.above_ma20 === false ? DOWN : FLAT }}>
            {ma.ma20 != null ? ma.ma20.toFixed(0) : "-"}
          </span>
        </div>
      )}
    </Card>
  );
}

/**
 * 资金流排行列表（净流入/净流出 Top）
 */
function FlowList({ title, rows, color }) {
  return (
    <Card title={title} size="small" style={{ marginBottom: 12 }}>
      {rows && rows.length ? (
        <div>
          {rows.map((r) => (
            <div key={r.l1_name} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "3px 0", borderBottom: "1px solid #f0f0f0" }}>
              <span style={{ fontSize: 13 }}>
                {r.l1_name}
                <span style={{ color: "#aaa", fontSize: 11, marginLeft: 6 }}>{r.sector_count}板块</span>
              </span>
              <span style={{ fontSize: 13, fontWeight: 600, color: signColor(r.total_net_flow_yi ?? 0) }}>
                {fmtSigned(r.total_net_flow_yi)}亿
              </span>
            </div>
          ))}
        </div>
      ) : (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无数据" />
      )}
    </Card>
  );
}

/**
 * 强弱板块分布条
 */
function StrengthBar({ breadth }) {
  const order = [
    { key: "strong", label: "强", color: "#e74c3c" },
    { key: "pian_qiang", label: "偏强", color: "#f39c12" },
    { key: "normal", label: "普通", color: "#95a5a6" },
    { key: "pian_ruo", label: "偏弱", color: "#3498db" },
    { key: "weak", label: "弱", color: "#2c3e50" },
  ];
  const total = order.reduce((s, o) => s + (breadth?.[o.key] || 0), 0);
  return (
    <Card title="板块强弱分布" size="small">
      {total > 0 ? (
        <>
          <div style={{ display: "flex", height: 22, borderRadius: 4, overflow: "hidden", marginBottom: 8 }}>
            {order.map((o) => {
              const v = breadth?.[o.key] || 0;
              return (
                <Tooltip key={o.key} title={`${o.label} ${v} 个`}>
                  <div style={{ width: `${(v / total) * 100}%`, background: o.color }} />
                </Tooltip>
              );
            })}
          </div>
          <Space size={10} wrap>
            {order.map((o) => (
              <span key={o.key} style={{ fontSize: 12 }}>
                <span style={{ display: "inline-block", width: 8, height: 8, borderRadius: 2, background: o.color, marginRight: 4 }} />
                {o.label} {breadth?.[o.key] || 0}
              </span>
            ))}
          </Space>
          <div style={{ fontSize: 12, color: "#888", marginTop: 8 }}>
            强板块 {breadth?.strong || 0} 个 · 弱板块 {breadth?.weak || 0} 个（共 {total} 个二级板块）
          </div>
        </>
      ) : (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无数据" />
      )}
    </Card>
  );
}

/**
 * 大盘概况页：核心指数 + 市场情绪 + 资金面 + 强弱分布
 * 数据源：/api/market-overview（一次聚合），交易时段 30s 自动刷新
 */
export default function MarketOverview() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const timerRef = useRef(null);

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      setData(await fetchMarketOverview());
    } catch (e) {
      // 静默轮询失败不打扰，手动刷新失败才提示
      if (!silent) {
        // message 引入太重，这里用空状态兜底
      }
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    timerRef.current = setInterval(() => load(true), 30000);
    return () => clearInterval(timerRef.current);
  }, [load]);

  const indices = data?.indices || [];
  const senti = data?.sentiment || {};
  const flow = data?.fund_flow || {};
  const breadth = data?.breadth || {};
  const mb = data?.market_breadth || {};
  const vol = data?.volume || {};
  const sealRate = senti.seal_rate ?? null;
  // 指数均线映射：code → {ma5, ma20, above_ma5, above_ma20}
  const maMap = {};
  (data?.index_ma || []).forEach((m) => { maMap[m.code] = m; });

  return (
    <>
      {/* 核心指数行情 */}
      <Card
        size="small"
        style={{ marginBottom: 12 }}
        title={
          <Space>
            <span>核心指数</span>
            {data?.trading ? (
              <Tag color="red" icon={<ClockCircleOutlined />}>交易中</Tag>
            ) : (
              <Tag>非交易时段</Tag>
            )}
          </Space>
        }
        extra={
          <Space>
            <span style={{ fontSize: 12, color: "#999" }}>
              更新 {data?.last_update ? new Date(data.last_update).toLocaleTimeString() : "-"}
            </span>
            <Button size="small" icon={<ReloadOutlined />} onClick={() => load()} loading={loading}>刷新</Button>
          </Space>
        }
      >
        <Row gutter={8}>
          {indices.map((idx) => (
            <Col span={3} key={idx.code}>
              <IndexCard idx={idx} ma={maMap[idx.code]} />
            </Col>
          ))}
        </Row>
      </Card>

      {/* 量能（放量/缩量 + 量价信号） */}
      <Card title="量能（放量/缩量）" size="small" style={{ marginBottom: 12 }}>
        <Row gutter={24} align="middle">
          <Col>
            <Statistic
              title="量比（今/5日均）"
              value={vol.volume_ratio ?? "-"}
              valueStyle={{ color: (vol.volume_ratio ?? 1) >= 1 ? UP : DOWN, fontWeight: 600 }}
            />
          </Col>
          <Col>
            <Statistic
              title="放缩量"
              value={vol.volume_label ?? "-"}
              valueStyle={{
                color: vol.volume_label === "放量" ? "#e67e22" : vol.volume_label === "缩量" ? "#3498db" : FLAT,
                fontWeight: 600,
              }}
            />
          </Col>
          <Col>
            <Statistic title="今日成交额" value={vol.today_amount_yi != null ? vol.today_amount_yi.toFixed(0) : "-"} suffix="亿" />
          </Col>
          <Col>
            <Statistic title="昨日成交额" value={vol.prev_amount_yi != null ? vol.prev_amount_yi.toFixed(0) : "-"} suffix="亿" />
          </Col>
          <Col>
            <Statistic title="5日均额" value={vol.avg5_amount_yi != null ? vol.avg5_amount_yi.toFixed(0) : "-"} suffix="亿" />
          </Col>
          <Col flex="auto" style={{ textAlign: "right" }}>
            {vol.signal && (
              <Tag
                color={
                  vol.signal.includes("放量上涨") || vol.signal.includes("缩量下跌") ? "red"
                    : vol.signal.includes("放量下跌") ? "green"
                    : vol.signal.includes("缩量上涨") ? "orange"
                    : "default"
                }
                style={{ fontSize: 13, padding: "4px 12px" }}
              >
                {vol.signal}
              </Tag>
            )}
          </Col>
        </Row>
      </Card>

      {/* 市场涨跌家数（市场宽度） */}
      <Card title="市场涨跌家数" size="small" style={{ marginBottom: 12 }}>
        <Row gutter={24}>
          <Col>
            <Statistic title="上涨家数" value={mb.up_count ?? "-"} valueStyle={{ color: UP, fontWeight: 600 }} />
          </Col>
          <Col>
            <Statistic title="下跌家数" value={mb.down_count ?? "-"} valueStyle={{ color: DOWN, fontWeight: 600 }} />
          </Col>
          <Col>
            <Statistic title="平盘家数" value={mb.flat_count ?? "-"} valueStyle={{ color: FLAT }} />
          </Col>
          <Col>
            <Statistic
              title="涨跌比"
              value={mb.up_down_ratio ?? "-"}
              valueStyle={{ color: (mb.up_down_ratio ?? 1) >= 1 ? UP : DOWN, fontWeight: 600 }}
            />
          </Col>
        </Row>
        {mb.total > 0 && (
          <div style={{ display: "flex", height: 16, borderRadius: 3, overflow: "hidden", marginTop: 10 }}>
            <div style={{ width: `${(mb.up_count / mb.total) * 100}%`, background: UP }} title={`上涨 ${mb.up_count}`} />
            <div style={{ width: `${(mb.flat_count / mb.total) * 100}%`, background: "#bdc3c7" }} title={`平盘 ${mb.flat_count}`} />
            <div style={{ width: `${(mb.down_count / mb.total) * 100}%`, background: DOWN }} title={`下跌 ${mb.down_count}`} />
          </div>
        )}
      </Card>

      {/* 市场情绪仪表 */}
      <Row gutter={16} style={{ marginBottom: 12 }}>
        <Col span={4}>
          <Card size="small"><Statistic title="涨停家数" value={senti.limit_up_count ?? "-"} valueStyle={{ color: UP }} /></Card>
        </Col>
        <Col span={4}>
          <Card size="small"><Statistic title="跌停家数" value={senti.limit_down_count ?? "-"} valueStyle={{ color: DOWN }} /></Card>
        </Col>
        <Col span={4}>
          <Card size="small"><Statistic title="炸板家数" value={senti.zhap_ban_count ?? "-"} valueStyle={{ color: "#f0a020" }} /></Card>
        </Col>
        <Col span={4}>
          <Card size="small">
            <Statistic
              title="封板率"
              value={sealRate ?? "-"}
              suffix={sealRate != null ? "%" : ""}
              valueStyle={{ color: sealRate != null ? (sealRate >= 70 ? UP : "#f0a020") : FLAT }}
            />
          </Card>
        </Col>
        <Col span={4}>
          <Card size="small"><Statistic title="最高连板" value={senti.max_lbc ?? "-"} suffix={senti.max_lbc ? "板" : ""} valueStyle={{ color: UP }} /></Card>
        </Col>
        <Col span={4}>
          <Card size="small">
            <Statistic
              title="两市成交额"
              value={data?.total_amount_yi != null ? (data.total_amount_yi / 10000).toFixed(2) : "-"}
              suffix={data?.total_amount_yi != null ? "万亿" : ""}
              valueStyle={{ color: "#e67e22" }}
            />
          </Card>
        </Col>
      </Row>

      {/* 连板梯队 + 涨停行业分布 */}
      <Card title="连板梯队（含晋级率）" size="small" style={{ marginBottom: 12 }}>
        {senti.lbc_dist && senti.lbc_dist.length ? (
          <Space wrap>
            {senti.lbc_dist.map((d) => (
              <Tag key={d.board} color={d.n >= 2 ? "red" : "default"} style={{ marginBottom: 4 }}>
                {d.board} {d.count}
                {d.rate != null && (
                  <span style={{ color: "#f0a020", fontWeight: 600 }}> ↑{d.rate}% ({d.num}/{d.denom})</span>
                )}
              </Tag>
            ))}
          </Space>
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无连板数据" />
        )}
      </Card>

      <Card title="涨停行业分布" size="small" style={{ marginBottom: 12 }}>
        {senti.industry_top && senti.industry_top.length ? (
          <Space wrap>
            {senti.industry_top.map((d) => (
              <Tag key={d.hybk} color="blue" style={{ marginBottom: 4 }}>{d.hybk} {d.count}</Tag>
            ))}
          </Space>
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无涨停数据" />
        )}
      </Card>

      {/* 资金面 + 强弱分布 */}
      <Row gutter={16}>
        <Col span={12}>
          <FlowList title={`一级行业净流入 Top ${(flow.inflow_top || []).length}`} rows={flow.inflow_top} color={UP} />
          <FlowList title={`一级行业净流出 Top ${(flow.outflow_top || []).length}`} rows={flow.outflow_top} color={DOWN} />
        </Col>
        <Col span={12}>
          <Card size="small" style={{ marginBottom: 12 }}>
            <Statistic
              title="全市场主力净流入"
              value={flow.main_net_inflow_yi != null ? fmtSigned(flow.main_net_inflow_yi, 1) : "-"}
              suffix={flow.main_net_inflow_yi != null ? "亿" : ""}
              valueStyle={{ color: signColor(flow.main_net_inflow_yi ?? 0), fontWeight: 600 }}
            />
          </Card>
          <StrengthBar breadth={breadth} />
          <Card title="封单资金" size="small" style={{ marginTop: 12 }}>
            <Row gutter={16}>
              <Col span={12}>
                <Statistic title="总封单资金" value={senti.total_fund != null ? (senti.total_fund / 1e8).toFixed(1) : "-"} suffix="亿" />
              </Col>
              <Col span={12}>
                <Statistic title="平均封单" value={senti.avg_fund != null ? (senti.avg_fund / 1e8).toFixed(2) : "-"} suffix="亿" />
              </Col>
            </Row>
          </Card>
        </Col>
      </Row>
    </>
  );
}
