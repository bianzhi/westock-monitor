import React, { useMemo } from "react";
import ReactECharts from "echarts-for-react";

// 多板块颜色轮换（最多 20 种）
const COLORS = [
  "#e74c3c", "#3498db", "#2ecc71", "#f39c12", "#9b59b6",
  "#1abc9c", "#e67e22", "#2980b9", "#27ae60", "#8e44ad",
  "#16a085", "#d35400", "#2c3e50", "#c0392b", "#7f8c8d",
  "#f1c40f", "#00bcd4", "#ff5722", "#795548", "#607d8b",
];

/**
 * 板块日级净流入折线图
 * props:
 *   series = [{ code, name, points: [{trade_date, net_flow_yi, turnover_yi}, ...] }, ...]
 *   mode = "net" | "turnover"  — 主力净流入(亿) / 成交额(亿)
 *   title = 标题
 *   height = 图表高度
 */
export default function DailyChart({ series = [], mode = "net", title = "板块日级净流入", height = 480 }) {
  const option = useMemo(() => {
    if (!series.length) {
      return {
        title: { text: title, left: "center", textStyle: { fontSize: 14 } },
        graphic: { type: "text", left: "center", top: "middle", style: { text: "暂无数据", fontSize: 16, fill: "#999" } },
      };
    }

    const isTurnover = mode === "turnover";

    return {
      title: {
        text: title,
        left: "center",
        textStyle: { fontSize: 14 },
      },
      tooltip: {
        trigger: "axis",
        formatter: (params) => {
          if (!params || !params.length) return "";
          const date = params[0].axisValue;
          let html = `<b>${date}</b><br/>`;
          params.forEach((p) => {
            // series data 是单个数值（category 轴），p.value 即净流入/成交额
            if (p.value == null) return;
            const v = Number(p.value).toFixed(2);
            const color = COLORS[p.seriesIndex % COLORS.length];
            html += `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${color};margin-right:4px;"></span>`;
            html += `${p.seriesName}: ${v} 亿<br/>`;
          });
          return html;
        },
      },
      legend: {
        type: "scroll",
        top: 30,
        data: series.map((s) => s.name),
        textStyle: { fontSize: 11 },
        itemWidth: 10,
        itemHeight: 10,
      },
      grid: { left: 60, right: 30, top: 80, bottom: 40 },
      xAxis: {
        type: "category",
        data: series[0]?.points?.map((p) => p.trade_date) || [],
        axisLabel: { rotate: 45, fontSize: 10 },
        splitLine: { show: false },
      },
      yAxis: {
        type: "value",
        name: isTurnover ? "成交额(亿)" : "主力净流入(亿)",
        splitLine: { lineStyle: { type: "dashed", color: "#e0e0e0" } },
      },
      series: series.map((s, idx) => ({
        name: s.name,
        type: "line",
        smooth: true,
        symbol: "circle",
        symbolSize: 4,
        lineStyle: { width: 2, color: COLORS[idx % COLORS.length] },
        itemStyle: { color: COLORS[idx % COLORS.length] },
        connectNulls: false,  // 缺交易日数据不连线
        data: (s.points || []).map((p) => {
          const v = isTurnover ? p.turnover_yi : p.net_flow_yi;
          return v == null ? null : v;
        }),
      })),
    };
  }, [series, mode, title]);

  return <ReactECharts option={option} style={{ height }} />;
}
