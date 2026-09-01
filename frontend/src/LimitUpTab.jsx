import React, { useEffect, useState, useCallback } from "react";
import { Card, Table, Space, Button, DatePicker, Statistic, Tag, message, Row, Col, Divider, Select } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import { fetchLimitUp, fetchLimitUpSummary } from "./api";

// 涨停票信息页：全市场涨停池（东方财富 getTopicZTPool 落库）
// 整理字段：代码/名称/涨停价/涨跌幅/连板数/首次涨停时间/最后封板时间/
//           开板次数/封单资金/换手率/成交额/流通市值/所属行业
export default function LimitUpTab({ gotoDaily }) {
  const [pool, setPool] = useState([]);
  const [loading, setLoading] = useState(false);
  const [date, setDate] = useState(dayjs());       // 查询日期
  const [meta, setMeta] = useState(null);          // {date, total}
  const [summary, setSummary] = useState(null);    // 综合信息（封板率等）
  const [lbcFilter, setLbcFilter] = useState(null);        // null=全部 / 1..4 / 5(5板+)
  const [industryFilter, setIndustryFilter] = useState(null); // null=全部 / 行业名
  const [timeFilter, setTimeFilter] = useState(null);      // null=全部 / early/mid/late

  const load = useCallback(async (d) => {
    setLoading(true);
    try {
      const ds = d ? d.format("YYYY-MM-DD") : undefined;
      const resp = await fetchLimitUp(ds);
      setPool(resp.pool || []);
      setMeta({ date: resp.date, total: resp.total });
      try {
        setSummary(await fetchLimitUpSummary(ds));
      } catch (e) {
        setSummary(null);
      }
    } catch (e) {
      message.error("涨停池加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(date); }, [date, load]);

  const maxLbc = pool.reduce((m, r) => Math.max(m, r.lbc ?? 0), 0);
  const fundSum = pool.reduce((s, r) => s + (r.fund ?? 0), 0);

  // 涨停时段分桶（与后端 summary.time_dist 口径一致）
  const timeBucket = (fbt) => {
    const hhmm = fbt ? parseInt(String(fbt).slice(0, 4), 10) : 0;
    if (!hhmm) return "unknown";
    if (hhmm <= 1000) return "early";
    if (hhmm < 1400) return "mid";
    return "late";
  };

  // 连板 + 行业 + 时段筛选后的涨停池
  const filteredPool = pool.filter((r) => {
    if (lbcFilter != null) {
      const lbc = r.lbc ?? 0;
      if (lbcFilter === 5) { if (lbc < 5) return false; }
      else if (lbc !== lbcFilter) return false;
    }
    if (industryFilter && (r.hybk ?? "") !== industryFilter) return false;
    if (timeFilter && timeBucket(r.fbt) !== timeFilter) return false;
    return true;
  });

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
    {
      title: "所属行业", dataIndex: "hybk", key: "hybk", width: 120, ellipsis: true,
      render: (v, r) => (v ? (
        r.hybk_code ? (
          <a onClick={() => gotoDaily?.(r.hybk_code)} title="跳转板块日线图">{v}</a>
        ) : v
      ) : "-"),
    },
    {
      title: "所属概念", dataIndex: "concepts", key: "concepts", width: 240,
      render: (v) => (v && v.length ? (
        <Space size={[0, 2]} wrap>
          {v.slice(0, 4).map((c) => (
            <Tag
              key={c.code || c.name}
              color="geekblue"
              style={{ marginRight: 2, fontSize: 11, cursor: "pointer" }}
              onClick={() => c.code && gotoDaily?.(c.code)}
              title="跳转板块日线图"
            >
              {c.name}
            </Tag>
          ))}
        </Space>
      ) : "-"),
    },
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
      {summary ? (
        <Card title="综合信息" size="small" style={{ marginBottom: 12 }}>
          <Row gutter={[24, 8]}>
            <Col>
              <Statistic
                title="封板率"
                value={summary.seal_rate ?? "-"}
                suffix="%"
                valueStyle={{ color: (summary.seal_rate ?? 0) >= 70 ? "#e74c3c" : "#f0a020" }}
              />
            </Col>
            <Col>
              <Statistic title="涨停家数" value={summary.limit_up_count} valueStyle={{ color: "#e74c3c" }} />
            </Col>
            <Col>
              <Statistic title="炸板家数" value={summary.zhap_ban_count} valueStyle={{ color: "#f0a020" }} />
            </Col>
            <Col>
              <Statistic title="跌停家数" value={summary.limit_down_count} valueStyle={{ color: "#2ecc71" }} />
            </Col>
            <Col>
              <Statistic title="最高连板" value={summary.max_lbc} suffix="板" />
            </Col>
            <Col>
              <Statistic title="总封单资金" value={(summary.total_fund / 1e8).toFixed(2)} suffix="亿" />
            </Col>
          </Row>
          <Divider style={{ margin: "8px 0" }} />
          <div style={{ marginBottom: 4 }}>
            连板梯队：
            {summary.lbc_dist.map((d, i) => {
              const next = summary.lbc_dist[i + 1];
              const rate = next && d.count > 0 ? (next.count / d.count) * 100 : null;
              return (
                <Tag key={d.board} color={d.count > 0 ? "red" : undefined} style={{ marginBottom: 4 }}>
                  {d.board} {d.count}
                  {rate != null && (
                    <span style={{ color: "#f0a020", fontWeight: 600 }}> ↑{rate.toFixed(1)}%</span>
                  )}
                </Tag>
              );
            })}
          </div>
          <div style={{ marginBottom: 4 }}>
            涨停时段：
            {summary.time_dist.map((d) => {
              const key = d.bucket.startsWith("早盘") ? "early"
                : d.bucket.startsWith("午盘") ? "mid"
                : d.bucket.startsWith("尾盘") ? "late" : null;
              const active = key && timeFilter === key;
              return (
                <Tag
                  key={d.bucket}
                  color={active ? "red" : undefined}
                  style={{ marginBottom: 4, cursor: key ? "pointer" : "default" }}
                  onClick={() => key && setTimeFilter(active ? null : key)}
                >
                  {d.bucket} {d.count}
                </Tag>
              );
            })}
          </div>
          <div>
            涨停行业：
            {summary.industry_top.map((d) => {
              const active = industryFilter === d.hybk;
              return (
                <Tag
                  key={d.hybk}
                  color={active ? "red" : "blue"}
                  style={{ marginBottom: 4, cursor: "pointer" }}
                  onClick={() => setIndustryFilter(active ? null : d.hybk)}
                >
                  {d.hybk} {d.count}
                </Tag>
              );
            })}
          </div>
        </Card>
      ) : (
        <Space size="large" style={{ marginBottom: 12 }}>
          <Statistic title="涨停家数" value={meta?.total ?? pool.length} />
          <Statistic title="最高连板" value={maxLbc} suffix="板" />
          <Statistic title="总封单资金" value={(fundSum / 1e8).toFixed(2)} suffix="亿" />
        </Space>
      )}
      <Space style={{ marginBottom: 12 }} wrap>
        <span>连板筛选：</span>
        <Select
          value={lbcFilter ?? "all"}
          onChange={(v) => setLbcFilter(v === "all" ? null : v)}
          style={{ width: 130 }}
          options={[
            { value: "all", label: "全部" },
            { value: 1, label: "首板" },
            { value: 2, label: "2板" },
            { value: 3, label: "3板" },
            { value: 4, label: "4板" },
            { value: 5, label: "5板+" },
          ]}
        />
        {industryFilter && (
          <Tag color="red" closable onClose={() => setIndustryFilter(null)}>
            行业: {industryFilter}
          </Tag>
        )}
        {timeFilter && (
          <Tag color="red" closable onClose={() => setTimeFilter(null)}>
            时段: {{ early: "早盘", mid: "午盘", late: "尾盘" }[timeFilter]}
          </Tag>
        )}
        <span style={{ color: "#999" }}>共 {filteredPool.length} 只</span>
      </Space>
      <Table
        rowKey="code"
        columns={columns}
        dataSource={filteredPool}
        loading={loading}
        size="small"
        scroll={{ x: 1600 }}
        pagination={{ pageSize: 50, showSizeChanger: false }}
      />
    </Card>
  );
}
