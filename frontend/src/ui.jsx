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
 */
export function StrengthTag({ level = "普通", value = 0 }) {
  const color = LEVEL_COLORS[level] || "#95a5a6";
  return (
    <Tooltip title={`强度值 ${value}`}>
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
