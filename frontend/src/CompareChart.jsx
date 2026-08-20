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
 * 判断时间戳是否在 A 股交易时段内（9:30-11:30 / 13:00-15:00）。
 * 非交易时段（盘前/午休/盘后）的点不绘制折线。
 */
function isTradingTime(ts) {
  if (!ts) return false;
  const d = new Date(ts);
  if (isNaN(d.getTime())) return false;
  const h = d.getHours();
  const m = d.getMinutes();
  const t = h * 60 + m;
  // 上午 9:30-11:30
  if (t >= 570 && t <= 690) return true;
  // 下午 13:00-15:00
  if (t >= 780 && t <= 900) return true;
  return false;
}

/** 分钟数 → HH:MM 字符串 */
function fmtTime(minutes) {
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

/**
 * 多板块分时对比图
 * props:
 *   series = [{ code, name, rank, points: [{time, timestamp, minute_delta, main_net_flow, is_open_anchor}, ...] }, ...]
 *   mode = "minute" | "cumulative" | "net_rate"  — 每分钟净流入 / 当日累计净流入 / 当日净额率
 *   title = 标题
 *   height = 图表高度
 *
 * 上午/下午衔接：X 轴用 category（统一交易时间点列表），不含午休 11:30-13:00
 * 的时间点，因此 11:30 最后一个点直接连到 13:00 第一个点，无午休空档。
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
    const isNetRate = mode === "net_rate";

    // 1. 收集所有板块交易时段内的分钟点（去重排序），作为 category 轴 data
    const timeSet = new Set();
    series.forEach((s) => {
      (s.points || []).forEach((p) => {
        if (!isTradingTime(p.timestamp)) return;
        const d = new Date(p.timestamp);
        timeSet.add(d.getHours() * 60 + d.getMinutes());
      });
    });
    const times = Array.from(timeSet).sort((a, b) => a - b);
    const timeLabels = times.map(fmtTime);

    // 2. 每个 series 的 data 按 times 对齐（缺失填 null）
    const seriesData = series.map((s) => {
      const valueMap = new Map();
      (s.points || []).forEach((p) => {
        if (!isTradingTime(p.timestamp)) return;
        const d = new Date(p.timestamp);
        const t = d.getHours() * 60 + d.getMinutes();
        if (isCumulative) {
          if (p.main_net_flow != null) valueMap.set(t, Number(p.main_net_flow) / 1e8);
        } else if (isNetRate) {
          // 净额率模式：当日净额率 = 当日累计净流入 ÷ 当日累计成交额 × 100(%)
          if (p.main_net_flow != null && p.turnover != null && Number(p.turnover) !== 0) {
            valueMap.set(t, (Number(p.main_net_flow) / Number(p.turnover)) * 100);
          }
        } else {
          // 分钟模式：跳过开盘锚点（is_open_anchor）和空值
          if (!p.is_open_anchor && p.minute_delta != null) valueMap.set(t, Number(p.minute_delta) / 1e8);
        }
      });
      return times.map((t) => (valueMap.has(t) ? valueMap.get(t) : null));
    });

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
          const axisValue = params[0].axisValue; // HH:MM
          // 按资金额度倒序排序，使 tooltip 顺序与折线高低对应
          const sorted = [...params]
            .filter((p) => p.value != null)
            .sort((a, b) => Number(b.value) - Number(a.value));
          let html = `<b>${axisValue}</b><br/>`;
          sorted.forEach((p) => {
            const v = Number(p.value).toFixed(isNetRate ? 2 : 3);
            const color = COLORS[p.seriesIndex % COLORS.length];
            const unit = isNetRate ? "%" : " 亿";
            html += `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${color};margin-right:4px;"></span>`;
            html += `${p.seriesName}: ${v}${unit}<br/>`;
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
        type: "category",
        data: timeLabels,
        axisLabel: { rotate: 45, fontSize: 10 },
        splitLine: { show: false },
      },
      yAxis: {
        type: "value",
        name: isNetRate ? "当日净额率(%)" : isCumulative ? "当日累计净流入(亿)" : "本分钟净流入(亿)",
        splitLine: { lineStyle: { type: "dashed", color: "#e0e0e0" } },
      },
      series: series.map((s, idx) => {
        const data = seriesData[idx];
        // 数据点 ≤5 时显示圆点标记，否则隐藏（点太多会遮盖折线）
        const validCount = data.filter((v) => v != null).length;
        const showSymbol = validCount <= 5;
        return {
          name: s.name,
          type: "line",
          smooth: false,
          symbol: showSymbol ? "circle" : "none",
          symbolSize: showSymbol ? 5 : 0,
          lineStyle: { width: 2, color: COLORS[idx % COLORS.length] },
          itemStyle: { color: COLORS[idx % COLORS.length] },
          connectNulls: isCumulative || isNetRate, // 累计/净额率模式连接 null 点，画连续折线
          data,
        };
      }),
    };
  }, [series, mode, title]);

  return <ReactECharts option={option} notMerge={true} style={{ height }} />;
}
