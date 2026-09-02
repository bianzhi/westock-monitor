import React, { useEffect, useState, useCallback } from "react";
import { Card, Table, Space, Button, Statistic, Tag, message, Row, Col, Spin } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { fetchAuction } from "./api";

// 红涨绿跌
const UP = "#e74c3c";
const DOWN = "#2ecc71";
const WARN = "#f0a020";
const GREY = "#7f8c8d";

const TIER_COLOR = {
  超预期: UP,
  符合预期: WARN,
  不及预期: GREY,
  大幅低开: DOWN,
  未知: undefined,
};

/**
 * 集合竞价页（9:15-9:25 竞价窗口）：
 *  ① 竞价情绪总览 —— 全市场高开/低开/平开家数 + 竞价情绪
 *  ② 竞价标的列表 —— 昨日涨停票的竞价高开幅度/竞价价/强度分档
 * 数据源：竞价高开幅度走腾讯 qt.gtimg.cn，情绪总览走东财涨跌分布。
 * 竞价量/竞价金额依赖东财 push2（暂不可达），未纳入。
 */
export default function AuctionTab() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await fetchAuction());
    } catch (e) {
      message.error("集合竞价加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const market = data?.market;
  const stocks = data?.stocks || [];
  const tierDist = data?.tier_dist || [];

  const columns = [
    { title: "代码", dataIndex: "code", key: "code", width: 100 },
    { title: "名称", dataIndex: "name", key: "name", width: 100 },
    { title: "连板", dataIndex: "lbc", key: "lbc", width: 70, render: (v) => (v > 1 ? <Tag color="red">{v}板</Tag> : v) },
    { title: "行业", dataIndex: "hybk", key: "hybk", width: 110, ellipsis: true },
    {
      title: "竞价高开", dataIndex: "auction_chg", key: "auction_chg", width: 100,
      sorter: (a, b) => (a.auction_chg ?? -999) - (b.auction_chg ?? -999),
      render: (v) => (v != null ? (
        <span style={{ color: v >= 0 ? UP : DOWN, fontWeight: 600 }}>{v > 0 ? "+" : ""}{v.toFixed(2)}%</span>
      ) : "-"),
    },
    { title: "竞价价", dataIndex: "auction_price", key: "auction_price", width: 90, render: (v) => (v != null ? v.toFixed(2) : "-") },
    { title: "昨收", dataIndex: "prev_close", key: "prev_close", width: 90, render: (v) => (v != null ? v.toFixed(2) : "-") },
    {
      title: "强度分档", dataIndex: "tier", key: "tier", width: 100,
      render: (v) => <Tag color={TIER_COLOR[v]}>{v}</Tag>,
    },
  ];

  return (
    <div>
      <Card
        title="竞价情绪总览"
        size="small"
        style={{ marginBottom: 12 }}
        extra={<Space><Button icon={<ReloadOutlined />} onClick={load} loading={loading}>刷新</Button></Space>}
      >
        {market ? (
          <Row gutter={[16, 8]}>
            <Col><Statistic title="竞价高开家数" value={market.up_count} valueStyle={{ color: UP }} /></Col>
            <Col><Statistic title="竞价低开家数" value={market.down_count} valueStyle={{ color: DOWN }} /></Col>
            <Col><Statistic title="平开家数" value={market.flat_count} /></Col>
            <Col><Statistic title="涨跌比" value={market.up_down_ratio ?? "-"} /></Col>
            <Col><Statistic title="竞价情绪" value={market.emotion} valueStyle={{ color: market.emotion === "偏强" ? UP : market.emotion === "偏弱" ? DOWN : WARN }} /></Col>
          </Row>
        ) : (
          <Spin />
        )}
      </Card>

      <Card
        title={`竞价标的（昨日涨停 ${stocks.length} 只）`}
        size="small"
        extra={<Space wrap>{tierDist.map((d) => <Tag key={d.tier} color={TIER_COLOR[d.tier]}>{d.tier} {d.count}</Tag>)}</Space>}
      >
        <Table
          rowKey="code"
          columns={columns}
          dataSource={stocks}
          loading={loading}
          size="small"
          scroll={{ x: 900 }}
          pagination={{ pageSize: 50, showSizeChanger: false }}
        />
      </Card>
    </div>
  );
}
