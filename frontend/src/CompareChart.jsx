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
 *   series = [{ code, name, rank, points: [{time, timestamp, minute_delta, main_net_flow, is_open_anchor}, ...] }, ...]
 *   mode = "minute" | "cumulative"  — 每分钟净流入 / 当日累计净流入
 *   title = 标题
 *   height = 图表高度
 */
export default function CompareChart({ series = [], mode = "minute", title = "板块分时对比", height = 480 }) {
  const option = useMemo(() => {
    if (!series.length) {
      return {
        title: { text: title, left: "center", textStyle: { fontSize: 14 } },
        graphic: { type: "text", left: "center", top: "middle", style: { text: "暂无数据", fontSize: 16, fill: "#999" } },
      };
    }

    const isCumulative = mode === "cumulative";

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
        minInterval: 60000,
        axisLabel: {
          rotate: 45,
          fontSize: 10,
          formatter: (value) => {
            const d = new Date(value);
            return d.toTimeString().slice(0, 5);
          },
        },
        splitLine: { show: false },
      },
      yAxis: {
        type: "value",
        name: isCumulative ? "当日累计净流入(亿)" : "本分钟净流入(亿)",
        splitLine: { lineStyle: { type: "dashed", color: "#e0e0e0" } },
      },
      series: series.map((s, idx) => {
        const validPoints = (s.points || []).filter((p) => {
          if (isCumulative) return p.main_net_flow != null;
          return !p.is_open_anchor && p.minute_delta != null;
        });
        // 数据点 ≤5 时显示圆点标记，否则隐藏（点太多会遮盖折线）
        const showSymbol = validPoints.length <= 5;
        return {
        name: s.name,
        type: "line",
        smooth: false,
        symbol: showSymbol ? "circle" : "none",
        symbolSize: showSymbol ? 5 : 0,
        lineStyle: { width: 2, color: COLORS[idx % COLORS.length] },
        itemStyle: { color: COLORS[idx % COLORS.length] },
        connectNulls: isCumulative,  // 累计模式连接 null 点，画连续折线
        data: (s.points || []).map((p) => {
          if (isCumulative) {
            // 累计模式：使用 main_net_flow，跳过空值
            if (p.main_net_flow == null) return null;
            const ts = new Date(p.timestamp).getTime();
            return [ts, Number(p.main_net_flow) / 1e8];
          }
          // 分钟模式：跳过开盘锚点和空值
          if (p.is_open_anchor || p.minute_delta == null) return null;
          const ts = new Date(p.timestamp).getTime();
          return [ts, Number(p.minute_delta) / 1e8];
        }),
      }
    }),
    };
  }, [series, mode, title]);

  return <ReactECharts option={option} style={{ height }} />;
}
