import React, { useMemo } from "react";
import ReactECharts from "echarts-for-react";

// 红涨绿跌
const UP_COLOR = "#e74c3c";
const DOWN_COLOR = "#2ecc71";
const TURNOVER_COLOR = "#f39c12";

/**
 * 板块日级主图+附图：主图主力净流入柱状图，附图涨跌幅 + 成交额柱状图。
 * 涨（正值）红色，跌（负值）绿色。
 * props:
 *   series = [{ code, name, points: [{trade_date, net_flow_yi, turnover_yi, change_pct}, ...] }, ...]
 *   title = 标题
 *   height = 图表高度
 */
export default function DailyChart({ series = [], title = "板块日级行情", height = 520 }) {
  const option = useMemo(() => {
    const s = series[0];
    if (!s || !s.points || !s.points.length) {
      return {
        title: { text: title, left: "center", textStyle: { fontSize: 14 } },
        graphic: { type: "text", left: "center", top: "middle", style: { text: "暂无数据", fontSize: 16, fill: "#999" } },
      };
    }

    const dates = s.points.map((p) => p.trade_date);
    const netFlow = s.points.map((p) => (p.net_flow_yi == null ? null : p.net_flow_yi));
    const changePct = s.points.map((p) => (p.change_pct == null ? null : p.change_pct));
    const turnover = s.points.map((p) => (p.turnover_yi == null ? null : p.turnover_yi));

    return {
      title: {
        text: title,
        left: "center",
        textStyle: { fontSize: 14 },
      },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        formatter: (params) => {
          if (!params || !params.length) return "";
          const date = params[0].axisValue;
          let html = `<b>${date}</b><br/>`;
          params.forEach((p) => {
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
          });
          return html;
        },
      },
      legend: {
        data: ["主力净流入(亿)", "涨跌幅(%)", "成交额(亿)"],
        top: 30,
        textStyle: { fontSize: 11 },
        itemWidth: 10,
        itemHeight: 10,
      },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      grid: [
        { left: 70, right: 30, top: 70, height: "38%" },   // 主图：主力净流入
        { left: 70, right: 30, top: "55%", height: "16%" }, // 附图1：涨跌幅
        { left: 70, right: 30, top: "75%", height: "16%" }, // 附图2：成交额
      ],
      xAxis: [
        { type: "category", gridIndex: 0, data: dates, axisLabel: { show: false }, axisTick: { show: false } },
        { type: "category", gridIndex: 1, data: dates, axisLabel: { show: false }, axisTick: { show: false } },
        { type: "category", gridIndex: 2, data: dates, axisLabel: { rotate: 45, fontSize: 10 } },
      ],
      yAxis: [
        { type: "value", gridIndex: 0, name: "净流入(亿)", splitLine: { lineStyle: { type: "dashed", color: "#e0e0e0" } } },
        { type: "value", gridIndex: 1, name: "涨跌幅(%)", splitLine: { lineStyle: { type: "dashed", color: "#e0e0e0" } } },
        { type: "value", gridIndex: 2, name: "成交额(亿)", splitLine: { lineStyle: { type: "dashed", color: "#e0e0e0" } } },
      ],
      series: [
        {
          name: "主力净流入(亿)",
          type: "bar",
          xAxisIndex: 0,
          yAxisIndex: 0,
          itemStyle: { color: (p) => (p.value >= 0 ? UP_COLOR : DOWN_COLOR) },
          data: netFlow,
        },
        {
          name: "涨跌幅(%)",
          type: "bar",
          xAxisIndex: 1,
          yAxisIndex: 1,
          itemStyle: { color: (p) => (p.value >= 0 ? UP_COLOR : DOWN_COLOR) },
          data: changePct,
        },
        {
          name: "成交额(亿)",
          type: "bar",
          xAxisIndex: 2,
          yAxisIndex: 2,
          itemStyle: { color: TURNOVER_COLOR },
          data: turnover,
        },
      ],
    };
  }, [series, title]);

  return <ReactECharts option={option} notMerge={true} style={{ height }} />;
}
