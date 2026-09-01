import React, { useMemo } from "react";
import ReactECharts from "echarts-for-react";

// 红涨绿跌
const UP_COLOR = "#e74c3c";
const DOWN_COLOR = "#2ecc71";
const TURNOVER_COLOR = "#f39c12";

/**
 * 板块日级行情主图 + 附图（对齐同花顺布局）：
 *   主图 = 真实价格 K 线（蜡烛图，红涨绿跌）
 *   附图 = 成交额柱状图 + 主力净流入柱状图（涨红跌绿）
 *
 * K 线构造：直接用后端落库的收盘价 close_price（sector_daily 表）。
 *   open = 前一日 close_price，close = 当日 close_price（首日平开）；
 *   今日盘中 close_price 未定，用「昨日 close_price × (1 + 涨跌幅/100)」补一根。
 *   不做「相对价格累积反推」——涨跌幅始终直接读落库的 change_pct，
 *   避免脏数据被累积放大成 554.67% 这类离谱值。
 *
 * props:
 *   series = [{ code, name, points: [{trade_date, net_flow_yi, turnover_yi, close_price, change_pct}, ...] }, ...]
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

    // 真实价格 K 线：open = 前一日 close，close = 当日落库 close_price
    let prevClose = null;
    const kline = s.points.map((p) => {
      let open, close;
      if (p.close_price != null) {
        close = Number(p.close_price);
        open = prevClose != null ? prevClose : close; // 首日平开
      } else if (prevClose != null && p.change_pct != null) {
        // 今日盘中：close_price 未定，用昨日收盘 + 涨跌幅构造
        open = prevClose;
        close = prevClose * (1 + Number(p.change_pct) / 100);
      } else {
        return "-"; // 无价格且无前值，跳过
      }
      prevClose = close;
      const high = Math.max(open, close);
      const low = Math.min(open, close);
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
              // 涨跌幅直接读落库 change_pct（不反算）
              const point = s.points[p.dataIndex];
              const pct = point ? point.change_pct : null;
              if (pct == null) return;
              const color = Number(pct) >= 0 ? UP_COLOR : DOWN_COLOR;
              html += `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${color};margin-right:4px;"></span>`;
              html += `涨跌幅: ${Number(pct) > 0 ? "+" : ""}${Number(pct).toFixed(2)}%<br/>`;
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
        data: ["K线", "成交额(亿)", "主力净流入(亿)"],
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
        { type: "value", gridIndex: 0, scale: true, name: "指数", splitLine: { lineStyle: { type: "dashed", color: "#e0e0e0" } } },
        { type: "value", gridIndex: 1, name: "成交额(亿)", splitLine: { show: false } },
        { type: "value", gridIndex: 2, name: "净流入(亿)", splitLine: { lineStyle: { type: "dashed", color: "#e0e0e0" } } },
      ],
      series: [
        {
          name: "K线",
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
