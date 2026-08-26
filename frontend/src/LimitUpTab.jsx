import React, { useEffect, useState, useCallback } from "react";
import { Card, Table, Space, Button, DatePicker, Statistic, Tag, message } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import { fetchLimitUp } from "./api";

// 涨停票信息页：全市场涨停池（东方财富 getTopicZTPool 落库）
// 整理字段：代码/名称/涨停价/涨跌幅/连板数/首次涨停时间/最后封板时间/
//           开板次数/封单资金/换手率/成交额/流通市值/所属行业
export default function LimitUpTab() {
  const [pool, setPool] = useState([]);
  const [loading, setLoading] = useState(false);
  const [date, setDate] = useState(dayjs());       // 查询日期
  const [meta, setMeta] = useState(null);          // {date, total}

  const load = useCallback(async (d) => {
    setLoading(true);
    try {
      const resp = await fetchLimitUp(d ? d.format("YYYY-MM-DD") : undefined);
      setPool(resp.pool || []);
      setMeta({ date: resp.date, total: resp.total });
    } catch (e) {
      message.error("涨停池加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(date); }, [date, load]);

  const maxLbc = pool.reduce((m, r) => Math.max(m, r.lbc ?? 0), 0);
  const fundSum = pool.reduce((s, r) => s + (r.fund ?? 0), 0);

  const fmtTime = (t) => {
    if (!t) return "-";
    const s = String(t).padStart(6, "0");
    return `${s.slice(0, 2)}:${s.slice(2, 4)}:${s.slice(4, 6)}`;
  };

  const columns = [
    { title: "代码", dataIndex: "code", key: "code", width: 100, fixed: "left" },
    { title: "名称", dataIndex: "name", key: "name", width: 120, fixed: "left" },
    {
      title: "涨停价", dataIndex: "price", key: "price", width: 90,
      sorter: (a, b) => (a.price ?? 0) - (b.price ?? 0),
      render: (v) => (v != null ? v.toFixed(2) : "-"),
    },
    {
      title: "涨跌幅", dataIndex: "change_pct", key: "change_pct", width: 90,
      sorter: (a, b) => (a.change_pct ?? 0) - (b.change_pct ?? 0),
      render: (v) => (v != null ? (
        <span style={{ color: "#e74c3c", fontWeight: 600 }}>+{v.toFixed(2)}%</span>
      ) : "-"),
    },
    {
      title: "连板数", dataIndex: "lbc", key: "lbc", width: 80,
      sorter: (a, b) => (a.lbc ?? 0) - (b.lbc ?? 0),
      render: (v) => v > 0 ? (
        <Tag color="#e74c3c">{v} 板</Tag>
      ) : "-",
    },
    {
      title: "首次涨停", dataIndex: "fbt", key: "fbt", width: 100,
      sorter: (a, b) => (a.fbt || "").localeCompare(b.fbt || ""),
      render: (v) => fmtTime(v),
    },
    {
      title: "最后封板", dataIndex: "lbt", key: "lbt", width: 100,
      sorter: (a, b) => (a.lbt || "").localeCompare(b.lbt || ""),
      render: (v) => fmtTime(v),
    },
    {
      title: "开板次数", dataIndex: "zbc", key: "zbc", width: 90,
      sorter: (a, b) => (a.zbc ?? 0) - (b.zbc ?? 0),
      render: (v) => (v != null ? v : "-"),
    },
    {
      title: "封单资金", dataIndex: "fund", key: "fund", width: 110,
      sorter: (a, b) => (a.fund ?? 0) - (b.fund ?? 0),
      render: (v) => (v != null ? (v / 1e8).toFixed(2) + "亿" : "-"),
    },
    {
      title: "换手率", dataIndex: "turnover_rate", key: "turnover_rate", width: 90,
      sorter: (a, b) => (a.turnover_rate ?? 0) - (b.turnover_rate ?? 0),
      render: (v) => (v != null ? v.toFixed(2) + "%" : "-"),
    },
    {
      title: "成交额(亿)", dataIndex: "amount", key: "amount", width: 110,
      sorter: (a, b) => (a.amount ?? 0) - (b.amount ?? 0),
      render: (v) => (v != null ? (v / 1e8).toFixed(2) : "-"),
    },
    {
      title: "流通市值(亿)", dataIndex: "ltsz", key: "ltsz", width: 120,
      sorter: (a, b) => (a.ltsz ?? 0) - (b.ltsz ?? 0),
      render: (v) => (v != null ? (v / 1e8).toFixed(2) : "-"),
    },
    { title: "所属行业", dataIndex: "hybk", key: "hybk", width: 110, ellipsis: true },
    {
      title: "涨停统计", key: "ztstat", width: 100,
      render: (_, r) => (r.zt_days != null || r.zt_ct != null)
        ? `${r.zt_days ?? "-"}天${r.zt_ct ?? "-"}次`
        : "-",
    },
  ];

  return (
    <Card
      title="涨停池"
      size="small"
      extra={
        <Space>
          <DatePicker
            value={date}
            onChange={(d) => setDate(d || dayjs())}
            allowClear={false}
            style={{ width: 140 }}
          />
          <Button icon={<ReloadOutlined />} onClick={() => load(date)} loading={loading}>
            刷新
          </Button>
        </Space>
      }
    >
      <Space size="large" style={{ marginBottom: 12 }}>
        <Statistic title="涨停家数" value={meta?.total ?? pool.length} />
        <Statistic title="最高连板" value={maxLbc} suffix="板" />
        <Statistic
          title="总封单资金"
          value={(fundSum / 1e8).toFixed(2)}
          suffix="亿"
        />
      </Space>
      <Table
        rowKey="code"
        columns={columns}
        dataSource={pool}
        loading={loading}
        size="small"
        scroll={{ x: 1400 }}
        pagination={{ pageSize: 50, showSizeChanger: false }}
      />
    </Card>
  );
}
