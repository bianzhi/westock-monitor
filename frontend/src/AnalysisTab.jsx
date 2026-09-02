import React, { useEffect, useState, useCallback, useMemo } from "react";
import { Card, Tabs, Space, Button, Tag, message, Spin, Statistic, Row, Col, Select, Switch, Tooltip } from "antd";
import { ReloadOutlined, InfoCircleOutlined } from "@ant-design/icons";
import ReactECharts from "echarts-for-react";
import { fetchAnalysis } from "./api";

const UP = "#e74c3c";
const DOWN = "#2ecc71";
const WARN = "#f0a020";

const INDEX_TABS = [
  { key: "sh000001", label: "上证指数" },
  { key: "sz399001", label: "深证成指" },
  { key: "sz399006", label: "创业板指" },
  { key: "sh000688", label: "科创50" },
];

const PHASE_COLOR = { 上涨: UP, 下跌: DOWN, 吸筹: WARN, 派发: "#7f8c8d" };

/**
 * 大盘指数 缠论 + 威科夫 量价分析页。
 * 主图 K 线 + 缠论标记（分型/中枢/买卖点），附图成交量（威科夫量价努力）。
 */
export default function AnalysisTab() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [code, setCode] = useState("sh000001");
  const [timeframe, setTimeframe] = useState("day");
  const [showFx, setShowFx] = useState(true);       // 分型
  const [showBi, setShowBi] = useState(true);       // 笔
  const [showZs, setShowZs] = useState(true);       // 中枢
  const [showBsp, setShowBsp] = useState(true);     // 买卖点
  const [showWyckoff, setShowWyckoff] = useState(true); // 威科夫信号

  const load = useCallback(async (tf) => {
    setLoading(true);
    try {
      setData(await fetchAnalysis(tf));
    } catch (e) {
      message.error("分析加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(timeframe); }, [timeframe, load]);

  const info = data?.[code];

  const option = useMemo(() => {
    if (!info) return null;
    const { klines, chanlun = {}, wyckoff = {} } = info;
    const dates = klines.map((k) => k.dt);
    const candle = klines.map((k) => [k.open, k.close, k.low, k.high]);
    const vols = klines.map((k) => k.vol);

    // 分型标记（顶▽ 底△）
    const fxPoints = (chanlun.fxs || []).map((f) => ({
      coord: [f.dt, f.price],
      symbol: "triangle",
      symbolRotate: f.type === "top" ? 0 : 180,
      symbolSize: 9,
      itemStyle: { color: f.type === "top" ? DOWN : UP },
      label: { show: true, formatter: f.type === "top" ? "顶" : "底", position: f.type === "top" ? "top" : "bottom", fontSize: 9 },
    }));

    // 买卖点标记
    const bspPoints = (chanlun.buy_sell_points || []).map((p) => ({
      coord: [p.dt, p.price],
      symbol: "circle",
      symbolSize: 12,
      itemStyle: { color: p.type.startsWith("b") ? UP : DOWN },
      label: { show: true, formatter: p.type.toUpperCase(), color: "#fff", fontSize: 9 },
    }));

    // 中枢区域（黄色矩形）
    const zsAreas = (chanlun.zss || []).map((z) => [
      { xAxis: z.start_dt, yAxis: z.zd, itemStyle: { color: "rgba(255,193,7,0.18)" } },
      { xAxis: z.end_dt, yAxis: z.zg },
    ]);

    // 笔的连线（向上红/向下绿）
    const biLines = (chanlun.bis || []).map((bi) => ({
      coords: [
        [bi.start_dt, bi.start],
        [bi.end_dt, bi.end],
      ],
      lineStyle: { color: bi.dir === "up" ? UP : DOWN, width: 1.5, type: "solid" },
    }));

    // 威科夫供需线（供给/需求/冰线）
    const wyLines = (wyckoff.supply_demand_lines || []).map((l) => {
      const sdt = dates[l.start_index] ?? l.start_index;
      const edt = dates[l.end_index] ?? l.end_index;
      return {
        coords: [
          [sdt, l.start_price],
          [edt, l.end_price],
        ],
        lineStyle: {
          color: l.line_type === "ice_line" ? "#722ed1" : l.line_type === "supply" ? "#fa541c" : "#13c2c2",
          width: 1,
          type: l.line_type === "ice_line" ? "dashed" : "dotted",
        },
      };
    });

    // 威科夫事件标记（SC/AR/BC/Spring/SOS 等）
    const wyEvents = (wyckoff.events || []).map((e) => ({
      coord: [e.dt, e.price],
      symbol: "rect",
      symbolSize: 7,
      itemStyle: { color: "#722ed1" },
      label: { show: true, formatter: e.event_type, fontSize: 8, color: "#722ed1", position: "bottom" },
    }));

    // 威科夫交易区间（吸筹/派发矩形区域）
    const wyTrAreas = (wyckoff.trading_ranges || []).map((tr) => {
      const sdt = dates[tr.start_index] ?? tr.start_index;
      const edt = dates[tr.end_index] ?? tr.end_index;
      return [
        { xAxis: sdt, yAxis: tr.lower, itemStyle: { color: tr.type === "accumulation" ? "rgba(19,194,194,0.12)" : "rgba(250,84,28,0.12)" } },
        { xAxis: edt, yAxis: tr.upper },
      ];
    });

    // 威科夫需求/供给出现信号
    const demandPoints = (wyckoff.demand_signals || []).map((p) => ({
      coord: [p.dt, p.price],
      symbol: "pin",
      symbolSize: 20,
      itemStyle: { color: "#13c2c2" },
      label: { show: true, formatter: "D", fontSize: 8, color: "#13c2c2", position: "bottom" },
    }));
    const supplyPoints = (wyckoff.supply_signals || []).map((p) => ({
      coord: [p.dt, p.price],
      symbol: "pin",
      symbolRotate: 180,
      symbolSize: 20,
      itemStyle: { color: "#fa541c" },
      label: { show: true, formatter: "S", fontSize: 8, color: "#fa541c", position: "top" },
    }));

    const markPointData = [
      ...(showFx ? fxPoints : []),
      ...(showBsp ? bspPoints : []),
      ...(showWyckoff ? wyEvents : []),
      ...(showWyckoff ? demandPoints : []),
      ...(showWyckoff ? supplyPoints : []),
    ];
    const markAreaData = [
      ...(showZs ? zsAreas : []),
      ...(showWyckoff ? wyTrAreas : []),
    ];
    const markLineData = [
      ...(showBi ? biLines : []),
      ...(showWyckoff ? wyLines : []),
    ];

    return {
      animation: false,
      tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
      legend: { data: ["K线", "成交量"], top: 5, textStyle: { fontSize: 11 } },
      grid: [
        { left: 60, right: 30, top: 40, bottom: 48, height: "55%" },
        { left: 60, right: 30, top: "74%", bottom: 38, height: "15%" },
      ],
      xAxis: [
        { type: "category", data: dates, gridIndex: 0, axisLabel: { show: false } },
        { type: "category", data: dates, gridIndex: 1, axisLabel: { rotate: 45, fontSize: 9 } },
      ],
      yAxis: [
        { type: "value", gridIndex: 0, scale: true, splitLine: { lineStyle: { type: "dashed", color: "#e0e0e0" } } },
        { type: "value", gridIndex: 1, splitLine: { show: false } },
      ],
      dataZoom: [
        { type: "inside", xAxisIndex: [0, 1] },
        { type: "slider", xAxisIndex: [0, 1], bottom: 4, height: 18 },
      ],
      series: [
        {
          name: "K线",
          type: "candlestick",
          data: candle,
          itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN },
          markPoint: { data: markPointData },
          markLine: { data: markLineData, symbol: "none" },
          markArea: { data: markAreaData },
        },
        {
          name: "成交量",
          type: "bar",
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: vols,
          itemStyle: { color: (p) => (klines[p.dataIndex]?.close >= klines[p.dataIndex]?.open ? UP : DOWN) },
        },
      ],
    };
  }, [info, showFx, showBi, showZs, showBsp, showWyckoff]);

  const wy = info?.wyckoff || {};
  const cz = info?.chanlun || {};
  const summary = info?.summary;

  return (
    <Card
      title="大盘指数 缠论 + 威科夫 量价分析"
      size="small"
      extra={
        <Space>
          <span>周期：</span>
          <Select
            value={timeframe}
            onChange={setTimeframe}
            style={{ width: 100 }}
            options={[
              { value: "day", label: "日线" },
              { value: "m30", label: "30分钟" },
              { value: "m5", label: "5分钟" },
              { value: "m1", label: "1分钟" },
            ]}
          />
          <Button icon={<ReloadOutlined />} onClick={() => load(timeframe)} loading={loading}>刷新</Button>
        </Space>
      }
    >
      <Tabs
        activeKey={code}
        onChange={setCode}
        items={INDEX_TABS.map((t) => ({ key: t.key, label: t.label }))}
        style={{ marginBottom: 12 }}
      />
      <Row gutter={[16, 8]} style={{ marginBottom: 12 }}>
        <Col><Space size={4}>分型<Switch size="small" checked={showFx} onChange={setShowFx} /></Space></Col>
        <Col><Space size={4}>笔<Switch size="small" checked={showBi} onChange={setShowBi} /></Space></Col>
        <Col><Space size={4}>中枢<Switch size="small" checked={showZs} onChange={setShowZs} /></Space></Col>
        <Col><Space size={4}>买卖点<Switch size="small" checked={showBsp} onChange={setShowBsp} /></Space></Col>
        <Col><Space size={4}>威科夫信号<Switch size="small" checked={showWyckoff} onChange={setShowWyckoff} /></Space></Col>
      </Row>
      {info ? (
        <>
          <Row gutter={[16, 8]} style={{ marginBottom: 12 }}>
            <Col>
              <Statistic title="威科夫阶段" value={wy.latest_phase || "-"}
                valueStyle={{ color: PHASE_COLOR[wy.latest_phase] || "#333" }} />
            </Col>
            <Col>
              <Statistic title="量能趋势" value={wy.volume_trend != null ? (wy.volume_trend > 0 ? "放量" : "缩量") : "-"} />
            </Col>
            <Col><Statistic title="分型数" value={cz.fxs?.length ?? 0} /></Col>
            <Col><Statistic title="笔数" value={cz.bis?.length ?? 0} /></Col>
            <Col><Statistic title="中枢数" value={cz.zss?.length ?? 0} /></Col>
            <Col><Statistic title="买卖点" value={cz.buy_sell_points?.length ?? 0} /></Col>
          </Row>
          {summary && (
            <div style={{ marginBottom: 12, padding: 12, background: "#fafafa", borderRadius: 6 }}>
              <Space size="large" wrap style={{ marginBottom: 4 }}>
                <span>缠论走势：<b>{summary.cz_trend || "-"}</b></span>
                <span>威科夫阶段：<b style={{ color: PHASE_COLOR[summary.wy_phase] || "#333" }}>{summary.wy_phase || "-"}</b></span>
                <span>现价：<b>{summary.latest_close}</b></span>
              </Space>
              <div style={{ marginTop: 4 }}>
                支撑位：{(summary.supports || []).map((s) => <Tag key={s.desc} color="green" style={{ marginRight: 4 }}>{s.level}（{s.desc}）</Tag>)}
              </div>
              <div style={{ marginTop: 4 }}>
                压力位：{(summary.resistances || []).map((r) => <Tag key={r.desc} color="red" style={{ marginRight: 4 }}>{r.level}（{r.desc}）</Tag>)}
              </div>
              <div style={{ marginTop: 4 }}>
                信号：{(summary.signals || []).map((s) => (
                  <Tooltip key={s.text} title={s.reason || "无依据"}>
                    <Tag color="blue" style={{ marginRight: 4, cursor: "help" }}>{s.text}</Tag>
                  </Tooltip>
                ))}
                {(summary.signals || []).length === 0 && "-"}
              </div>
              <div style={{ marginTop: 4, color: "#333" }}>
                <Tooltip title={<div>{(summary.advice_reasons || []).map((r, i) => <div key={i}>{r}</div>)}</div>}>
                  <span style={{ cursor: "help" }}>操作建议：<b>{summary.advice}</b> <InfoCircleOutlined style={{ color: "#999" }} /></span>
                </Tooltip>
              </div>
            </div>
          )}
          {option && <ReactECharts option={option} notMerge style={{ height: 560 }} />}
          <div style={{ marginTop: 12, fontSize: 12, color: "#666" }}>
            图例：
            <Tag color="red">K线(红涨绿跌)</Tag>
            <Tag color="orange">中枢(黄色矩形)</Tag>
            <Tag>分型(顶▽ / 底△)</Tag>
            <Tag color={UP}>B 买点</Tag>
            <Tag color={DOWN}>S 卖点</Tag>
            <span>威科夫阶段：吸筹(横盘低位)→上涨→派发(横盘高位)→下跌</span>
          </div>
        </>
      ) : (
        <Spin />
      )}
    </Card>
  );
}
