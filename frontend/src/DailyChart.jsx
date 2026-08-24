import React, { useMemo } from "react";
import ReactECharts from "echarts-for-react";

// 红涨绿跌
const UP_COLOR = "#e74c3c";
const DOWN_COLOR = "#2ecc71";
const TURNOVER_COLOR = "#f39c12";

/**
 * 板块日级主图+附图：
 *   主图 = 涨跌幅 K 线（蜡烛图，用 change_pct 构造 OHLC）+ 成交额柱状图（右轴）
 *   附图 = 主力净流入柱状图（涨红跌绿）
 * props:
 *   series = [{ code, name, points: [{trade_date, net_flow_yi, turnover_yi, change_pct}, ...] }, ...]
 *   title = 标题
 *   height = 图表高度
 */
export default function DailyChart({ series = [], title = "板块日级行情", height = 560 }) {
  const option = useMemo(() => {
    const s = series[0];
    if (!s || !s.points || !s.points.length) {
      return {
        title: { text: title, left: "center", textStyle: { fontSize: 14 } },
        graphic: { type: "text", left: "center", top: "middle", style: { text: "暂无数据", fontSize: 16, fill: "#999" } },
      };
    }

    const dates = s.points.map((p) => p.trade_date);
    // 涨跌幅 K 线：用 change_pct 构造 [open, close, low, high]
    //   涨(pct>=0)：实体从 0 到 pct，low=0, high=pct → 阳线(红)
    //   跌(pct<0) ：实体从 pct 到 0，low=pct, high=0 → 阴线(绿)
    const kline = s.points.map((p) => {
      if (p.change_pct == null) return null;
      const pct = Number(p.change_pct);
      return pct >= 0 ? [0, pct, 0, pct] : [0, pct, pct, 0];
    });
    const turnover = s.points.map((p) => (p.turnover_yi == null ? null : p.turnover_yi));
    const netFlow = s.points.map((p) => (p.net_flow_yi == null ? null : p.net_flow_yi));

    return {
      title: {
        text: title,
        left: "center",
        textStyle: { fontSize: 14 },
      },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        formatter: (params) => {
          if (!params || !params.length) return "";
          const date = params[0].axisValue;
          let html = `<b>${date}</b><br/>`;
          params.forEach((p) => {
            if (p.seriesType === "candlestick") {
              // K 线 value 为 [open, close, low, high]，展示 close（即涨跌幅）
              const close = Array.isArray(p.value) ? p.value[1] : null;
              if (close == null) return;
              const v = Number(close).toFixed(2);
              const color = Number(close) >= 0 ? UP_COLOR : DOWN_COLOR;
              html += `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${color};margin-right:4px;"></span>`;
              html += `涨跌幅: ${v}%<br/>`;
            } else {
              if (p.value == null) return;
              const v = Number(p.value).toFixed(2);
              const color =
                p.seriesName === "成交额(亿)"
                  ? TURNOVER_COLOR
                  : Number(p.value) >= 0
                    ? UP_COLOR
                    : DOWN_COLOR;
              html += `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${color};margin-right:4px;"></span>`;
              html += `${p.seriesName}: ${v}<br/>`;
            }
          });
          return html;
        },
      },
      legend: {
        data: ["涨跌幅", "成交额(亿)", "主力净流入(亿)"],
        top: 30,
        textStyle: { fontSize: 11 },
        itemWidth: 10,
        itemHeight: 10,
      },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      grid: [
        { left: 70, right: 70, top: 70, height: "48%" },   // 主图：涨跌幅 K 线 + 成交额
        { left: 70, right: 70, top: "64%", height: "24%" }, // 附图：主力净流入
      ],
      xAxis: [
        { type: "category", gridIndex: 0, data: dates, axisLabel: { show: false }, axisTick: { show: false } },
        { type: "category", gridIndex: 1, data: dates, axisLabel: { rotate: 45, fontSize: 10 } },
      ],
      yAxis: [
        { type: "value", gridIndex: 0, name: "涨跌幅(%)", splitLine: { lineStyle: { type: "dashed", color: "#e0e0e0" } } },
        { type: "value", gridIndex: 0, name: "成交额(亿)", position: "right", splitLine: { show: false } },
        { type: "value", gridIndex: 1, name: "净流入(亿)", splitLine: { lineStyle: { type: "dashed", color: "#e0e0e0" } } },
      ],
      series: [
        {
          name: "涨跌幅",
          type: "candlestick",
          xAxisIndex: 0,
          yAxisIndex: 0,
          itemStyle: {
            color: UP_COLOR,       // 阳线（涨）红色
            color0: DOWN_COLOR,    // 阴线（跌）绿色
            borderColor: UP_COLOR,
            borderColor0: DOWN_COLOR,
          },
          data: kline,
        },
        {
          name: "成交额(亿)",
          type: "bar",
          xAxisIndex: 0,
          yAxisIndex: 1,
          itemStyle: { color: TURNOVER_COLOR },
          data: turnover,
        },
        {
          name: "主力净流入(亿)",
          type: "bar",
          xAxisIndex: 1,
          yAxisIndex: 2,
          itemStyle: { color: (p) => (p.value >= 0 ? UP_COLOR : DOWN_COLOR) },
          data: netFlow,
        },
      ],
    };
  }, [series, title]);

  return <ReactECharts option={option} notMerge={true} style={{ height }} />;
}
