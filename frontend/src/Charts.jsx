import React, { useMemo } from "react";
import ReactECharts from "echarts-for-react";

/**
 * 分钟级资金流折线图
 * props: points = [{time, main_net_flow, minute_delta, turnover}, ...]
 */
export function MinuteChart({ points = [] }) {
  const option = useMemo(() => {
    const xs = points.map((p) => p.time || "");
    const cumNet = points.map((p) =>
      p.main_net_flow != null ? Number(p.main_net_flow) / 1e8 : null
    );
    const minDelta = points.map((p) =>
      p.minute_delta != null ? Number(p.minute_delta) / 1e8 : null
    );
    const turnover = points.map((p) =>
      p.turnover != null ? Number(p.turnover) / 1e8 : null
    );
    return {
      title: { text: "当日分钟级资金流", left: "center", textStyle: { fontSize: 14 } },
      tooltip: { trigger: "axis" },
      legend: { data: ["累计净流入(亿)", "本分钟增量(亿)", "累计成交额(亿)"], top: 28 },
      grid: { left: 50, right: 30, top: 80, bottom: 40 },
      xAxis: { type: "category", data: xs, axisLabel: { rotate: 45, fontSize: 10 } },
      yAxis: [
        { type: "value", name: "净流入(亿)" },
        { type: "value", name: "成交额(亿)" },
      ],
      series: [
        {
          name: "累计净流入(亿)",
          type: "line",
          smooth: true,
          itemStyle: { color: "#e74c3c" },
          data: cumNet,
        },
        {
          name: "本分钟增量(亿)",
          type: "bar",
          yAxisIndex: 0,
          itemStyle: {
            color: (params) =>
              params.value >= 0 ? "#e74c3c" : "#2ecc71",
          },
          data: minDelta,
        },
        {
          name: "累计成交额(亿)",
          type: "line",
          yAxisIndex: 1,
          smooth: true,
          itemStyle: { color: "#95a5a6" },
          data: turnover,
        },
      ],
    };
  }, [points]);

  return <ReactECharts option={option} style={{ height: 360 }} />;
}

/**
 * 近 n 日净流入柱状图
 * props: history = [{date, net_flow_yi, turnover_yi, net_rate, estimated}, ...]
 */
export function DailyHistoryChart({ history = [] }) {
  const option = useMemo(() => {
    // 反转数组：API 返回今日在前（倒序），图表应旧→新（左→右），今日在右端
    const reversed = [...history].reverse();
    const xs = reversed.map((h) => h.date || "");
    const net = reversed.map((h) => h.net_flow_yi ?? null);
    const turnover = reversed.map((h) => h.turnover_yi ?? null);
    const estimated = reversed.map((h) => h.estimated || false);
    return {
      title: {
        text: "近 n 日净流入 / 成交额",
        subtext: "虚线 = 估算值（非真单日数据）",
        left: "center",
        textStyle: { fontSize: 14 },
        subtextStyle: { fontSize: 11, color: "#999" },
      },
      tooltip: {
        trigger: "axis",
        formatter: (params) => {
          const p1 = params[0];
          const est = estimated[p1?.dataIndex];
          return `${p1?.axisValue}${est ? " (估算)" : ""}<br/>
            净流入: ${p1?.value?.toFixed(3) ?? "-"} 亿`;
        },
      },
      legend: { data: ["净流入(亿)", "成交额(亿)"], top: 48 },
      grid: { left: 50, right: 30, top: 90, bottom: 40 },
      xAxis: { type: "category", data: xs },
      yAxis: { type: "value", name: "亿元" },
      series: [
        {
          name: "净流入(亿)",
          type: "bar",
          itemStyle: {
            color: (params) => (params.value >= 0 ? "#e74c3c" : "#2ecc71"),
            borderColor: (params) =>
              estimated[params.dataIndex] ? "#333" : "transparent",
            borderWidth: (params) =>
              estimated[params.dataIndex] ? 1 : 0,
            borderType: (params) =>
              estimated[params.dataIndex] ? "dashed" : "solid",
          },
          data: net,
        },
        {
          name: "成交额(亿)",
          type: "line",
          smooth: true,
          itemStyle: { color: "#f39c12" },
          data: turnover,
        },
      ],
    };
  }, [history]);

  return <ReactECharts option={option} style={{ height: 320 }} />;
}

/**
 * 近3日/近5日净额率对比柱状图
 */
export function NetRateCompareChart({ sectors = [], top = 15 }) {
  const option = useMemo(() => {
    const sorted = [...sectors].sort(
      (a, b) => (b.summary_5d?.net_rate ?? -999) - (a.summary_5d?.net_rate ?? -999)
    );
    const picked = sorted.slice(0, top).reverse();
    const xs = picked.map((s) => s.name);
    const rate3 = picked.map((s) => s.summary_3d?.net_rate ?? null);
    const rate5 = picked.map((s) => s.summary_5d?.net_rate ?? null);
    return {
      title: { text: `近5日净额率 Top${top}`, left: "center", textStyle: { fontSize: 14 } },
      tooltip: { trigger: "axis", valueFormatter: (v) => (v == null ? "-" : v + "%") },
      legend: { data: ["近3日净额率(%)", "近5日净额率(%)"], top: 28 },
      grid: { left: 100, right: 30, top: 80, bottom: 40 },
      xAxis: { type: "value", name: "%" },
      yAxis: { type: "category", data: xs },
      series: [
        {
          name: "近3日净额率(%)",
          type: "bar",
          itemStyle: { color: "#3498db" },
          data: rate3,
        },
        {
          name: "近5日净额率(%)",
          type: "bar",
          itemStyle: { color: "#e74c3c" },
          data: rate5,
        },
      ],
    };
  }, [sectors, top]);

  return <ReactECharts option={option} style={{ height: 420 }} />;
}
