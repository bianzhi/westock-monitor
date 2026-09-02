import React, { useEffect, useState, useCallback, useMemo } from "react";
import { Card, Tabs, Space, Button, Tag, message, Spin, Statistic, Row, Col } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import ReactECharts from "echarts-for-react";
import { fetchAnalysis } from "./api";

const UP = "#e74c3c";
const DOWN = "#2ecc71";
const WARN = "#f0a020";

const INDEX_TABS = [
  { key: "sh000001", label: "上证指数" },
  { key: "sz399001", label: "深证成指" },
  { key: "sz399006", label: "创业板指" },
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

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await fetchAnalysis());
    } catch (e) {
      message.error("分析加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const info = data?.[code];

  const option = useMemo(() => {
    if (!info) return null;
    const { klines, chanlun = {} } = info;
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

    return {
      animation: false,
      tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
      legend: { data: ["K线", "成交量"], top: 5, textStyle: { fontSize: 11 } },
      grid: [
        { left: 60, right: 30, top: 40, height: "58%" },
        { left: 60, right: 30, top: "76%", height: "16%" },
      ],
      xAxis: [
        { type: "category", data: dates, gridIndex: 0, axisLabel: { show: false } },
        { type: "category", data: dates, gridIndex: 1, axisLabel: { rotate: 45, fontSize: 9 } },
      ],
      yAxis: [
        { type: "value", gridIndex: 0, scale: true, splitLine: { lineStyle: { type: "dashed", color: "#e0e0e0" } } },
        { type: "value", gridIndex: 1, splitLine: { show: false } },
      ],
      dataZoom: [{ type: "inside", xAxisIndex: [0, 1] }],
      series: [
        {
          name: "K线",
          type: "candlestick",
          data: candle,
          itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN },
          markPoint: { data: [...fxPoints, ...bspPoints] },
          markArea: { data: zsAreas },
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
  }, [info]);

  const wy = info?.wyckoff || {};
  const cz = info?.chanlun || {};

  return (
    <Card
      title="大盘指数 缠论 + 威科夫 量价分析"
      size="small"
      extra={<Space><Button icon={<ReloadOutlined />} onClick={load} loading={loading}>刷新</Button></Space>}
    >
      <Tabs
        activeKey={code}
        onChange={setCode}
        items={INDEX_TABS.map((t) => ({ key: t.key, label: t.label }))}
        style={{ marginBottom: 12 }}
      />
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
