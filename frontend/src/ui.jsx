import React from "react";
import { Tag, Tooltip } from "antd";

/**
 * 强度档位颜色映射
 */
export const LEVEL_COLORS = {
  强: "#e74c3c",
  偏强: "#f39c12",
  普通: "#95a5a6",
  偏弱: "#3498db",
  弱: "#2c3e50",
};

/**
 * 强度档位标签
 * 可解释性：hover Tooltip 展示判定依据（规模档 × 净额率阈值 × n 日窗口）
 *          scale/window 来自 record（可选）；缺则只展 strength_value
 */
export function StrengthTag({ level = "普通", value = 0, scale = null, windowN = null, netRate = null }) {
  const color = LEVEL_COLORS[level] || "#95a5a6";
  // 拼可解释性提示：阈值表来自 config SCALE_THRESHOLDS，前端硬编码镜像一份
  const THRESHOLDS = {
    大盘: { hi: 5.0, mid: 2.0, lo: -1.0, vlo: -1.5 },
    中盘: { hi: 7.0, mid: 3.0, lo: -1.5, vlo: -2.0 },
    小盘: { hi: 10.0, mid: 4.0, lo: -2.0, vlo: -3.0 },
  };
  const sc = scale || "小盘";
  const th = THRESHOLDS[sc] || THRESHOLDS["小盘"];
  const lines = [
    `强度值 ${value}`,
    `规模档：${sc}（阈值 hi=${th.hi} mid=${th.mid} lo=${th.lo} vlo=${th.vlo}）`,
  ];
  if (windowN) lines.push(`窗口：近 ${windowN} 日聚合净额率`);
  if (netRate != null) lines.push(`当前净额率 ${netRate.toFixed(2)}%`);
  return (
    <Tooltip title={lines.join(" · ")}>
      <span className="strength-tag" style={{ background: color }}>
        {level}
      </span>
    </Tooltip>
  );
}

/**
 * 净流入数字着色：正红、负绿、零灰
 */
export function NetFlowText({ value, suffix = "", digits = 2 }) {
  if (value == null || Number.isNaN(value)) return <span className="net-zero">-</span>;
  const v = Number(value);
  const cls = v > 0 ? "net-positive" : v < 0 ? "net-negative" : "net-zero";
  const text = v.toFixed(digits) + suffix;
  return <span className={cls}>{text}</span>;
}

/**
 * 净额率着色：正红、负绿、零灰
 */
export function NetRateText({ value, digits = 4 }) {
  if (value == null || Number.isNaN(value)) return <span className="net-zero">-</span>;
  const v = Number(value);
  const cls = v > 0 ? "net-positive" : v < 0 ? "net-negative" : "net-zero";
  return <span className={cls}>{v.toFixed(digits)}%</span>;
}

/**
 * 规模档位标签
 */
export function ScaleTag({ scale = "小盘" }) {
  const colors = {
    大盘: "#8e44ad",
    中盘: "#2980b9",
    小盘: "#27ae60",
  };
  return (
    <Tag color={colors[scale] || "#95a5a6"} style={{ margin: 0 }}>
      {scale}
    </Tag>
  );
}
