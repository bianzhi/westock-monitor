import React, { useMemo } from "react";
import ReactECharts from "echarts-for-react";

// 红涨绿跌
const UP_COLOR = "#e74c3c";
const DOWN_COLOR = "#2ecc71";
const TURNOVER_COLOR = "#f39c12";

/**
 * 板块日级 K 线主图 + 附图（对齐同花顺布局）：
 *   主图 = 价格 K 线（用 change_pct 反推连续收盘价构造 OHLC，涨红跌绿）
 *   附图 = 成交额柱状图 + 主力净流入柱状图（涨红跌绿）
 * 说明：腾讯板块指数历史 OHLC 不可回溯，K 线由历史涨跌幅 change_pct 反推
 * 相对收盘价（基准 100），形态为连续蜡烛，涨红跌绿。
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

    // 用 change_pct 反推连续收盘价，构造 K 线 OHLC。
    // open = 前一日 close（首日基准 100）；close = open × (1 + pct/100)；
    // high/low 用 open/close 近似（无真实高低点）。
    let prevClose = 100;
    const kline = s.points.map((p) => {
      if (p.change_pct == null) return "-"; // 空数据用 "-"，避免 null 导致 candlestick 崩溃
      const pct = Number(p.change_pct);
      const open = prevClose;
      const close = open * (1 + pct / 100);
      const high = Math.max(open, close);
      const low = Math.min(open, close);
      prevClose = close;
      // ECharts candlestick 数据顺序：[open, close, lowest, highest]
      return [open, close, low, high];
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
              // K 线 value 为 [open, close, low, high]，展示涨跌幅
              const arr = Array.isArray(p.value) ? p.value : null;
              if (!arr || arr[1] == null) return;
              const open = Number(arr[0]);
              const close = Number(arr[1]);
              const pct = open ? ((close - open) / open) * 100 : 0;
              const color = pct >= 0 ? UP_COLOR : DOWN_COLOR;
              html += `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${color};margin-right:4px;"></span>`;
              html += `涨跌幅: ${pct.toFixed(2)}%<br/>`;
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
        data: ["K线(相对价)", "成交额(亿)", "主力净流入(亿)"],
        top: 30,
        textStyle: { fontSize: 11 },
        itemWidth: 10,
        itemHeight: 10,
      },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      grid: [
        { left: 70, right: 30, top: 70, height: "46%" },   // 主图：K 线
        { left:70, right: 30, top: "62%", height: "16%" }, // 附图1：成交额
        { left: 70, right: 30, top: "79%", height: "16%" }, // 附图2：净流入
      ],
      xAxis: [
        { type: "category", gridIndex: 0, data: dates, axisLabel: { show: false }, axisTick: { show: false } },
        { type: "category", gridIndex: 1, data: dates, axisLabel: { show: false }, axisTick: { show: false } },
        { type: "category", gridIndex: 2, data: dates, axisLabel: { rotate: 45, fontSize: 10 } },
      ],
      yAxis: [
        { type: "value", gridIndex: 0, scale: true, name: "价格", splitLine: { lineStyle: { type: "dashed", color: "#e0e0e0" } } },
        { type: "value", gridIndex: 1, name: "成交额(亿)", splitLine: { show: false } },
        { type: "value", gridIndex: 2, name: "净流入(亿)", splitLine: { lineStyle: { type: "dashed", color: "#e0e0e0" } } },
      ],
      series: [
        {
          name: "K线(相对价)",
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
          xAxisIndex: 1,
          yAxisIndex: 1,
          itemStyle: { color: TURNOVER_COLOR },
          data: turnover,
        },
        {
          name: "主力净流入(亿)",
          type: "bar",
          xAxisIndex: 2,
          yAxisIndex: 2,
          itemStyle: { color: (p) => (p.value >= 0 ? UP_COLOR : DOWN_COLOR) },
          data: netFlow,
        },
      ],
    };
  }, [series, title]);

  return <ReactECharts option={option} notMerge={true} style={{ height }} />;
}
