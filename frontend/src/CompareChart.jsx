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
 * 多板块分时对比图
 * props:
 *   series = [{ code, name, rank, points: [{time, minute_delta}, ...] }, ...]
 *   title = 标题
 *   height = 图表高度
 */
export default function CompareChart({ series = [], title = "板块分时对比", height = 480 }) {
  const option = useMemo(() => {
    if (!series.length) {
      return {
        title: { text: title, left: "center", textStyle: { fontSize: 14 } },
        graphic: { type: "text", left: "center", top: "middle", style: { text: "暂无数据", fontSize: 16, fill: "#999" } },
      };
    }

    // 取第一个板块的时间轴（使用 ISO timestamp，ECharts time 轴自动按实际时间间距渲染）

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
          const ts = params[0].axisValue;
          const d = new Date(ts);
          const time = d.toTimeString().slice(0, 8);
          let html = `<b>${time}</b><br/>`;
          params.forEach((p) => {
            if (p.value == null || p.value[1] == null) return;
            const v = Number(p.value[1]).toFixed(3);
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
      grid: { left: 50, right: 30, top: 80, bottom: 40 },
      xAxis: {
        type: "time",
        minInterval: 60000,  // 1 分钟最小刻度
        axisLabel: {
          rotate: 45,
          fontSize: 10,
          formatter: (value) => {
            const d = new Date(value);
            return d.toTimeString().slice(0, 5);  // HH:MM
          },
        },
        splitLine: { show: false },
      },
      yAxis: {
        type: "value",
        name: "本分钟净流入(亿)",
        splitLine: { lineStyle: { type: "dashed", color: "#e0e0e0" } },
      },
      series: series.map((s, idx) => ({
        name: s.name,
        type: "line",
        smooth: false,
        symbol: "none",
        lineStyle: { width: 2, color: COLORS[idx % COLORS.length] },
        itemStyle: { color: COLORS[idx % COLORS.length] },
        data: (s.points || []).map((p) => {
          if (p.is_open_anchor || p.minute_delta == null) return null;
          const ts = new Date(p.timestamp).getTime();
          return [ts, Number(p.minute_delta) / 1e8];
        }),
      })),
    };
  }, [series, title]);

  return <ReactECharts option={option} style={{ height }} />;
}
