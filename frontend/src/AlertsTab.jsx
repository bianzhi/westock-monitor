import React, { useState, useEffect, useCallback } from "react";
import { Card, Table, Button, Space, Select, InputNumber, message, Empty, Tag, Divider } from "antd";
import { ReloadOutlined, SettingOutlined } from "@ant-design/icons";
import { fetchAlerts, fetchUserAlerts, saveUserAlerts } from "./api";
import { StrengthTag } from "./ui";

/**
 * 强度档位告警 Tab
 * - 上半：告警日志（档位变化如 普通→强）
 * - 下半：用户阈值配置（登录用户存 Supabase，未登录本地 localStorage）
 */
export default function AlertsTab() {
  const [alerts, setAlerts] = useState([]);
  const [loading, setLoading] = useState(false);
  const [threshold, setThreshold] = useState({
    strength_up: null,
    strength_down: null,
    levels: [],
    codes: "",
  });
  const [savingCfg, setSavingCfg] = useState(false);
  const [loggedIn, setLoggedIn] = useState(false);

  const LEVEL_OPTIONS = [
    { value: "强", label: "强" },
    { value: "偏强", label: "偏强" },
    { value: "普通", label: "普通" },
    { value: "偏弱", label: "偏弱" },
    { value: "弱", label: "弱" },
  ];

  const loadAlerts = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchAlerts(200);
      setAlerts(data.alerts || []);
    } catch (e) {
      message.error("加载告警失败: " + (e.response?.data?.detail || e.message));
    } finally {
      setLoading(false);
    }
  }, []);

  // 加载用户阈值（登录走 Supabase，未登录走 localStorage）
  const loadThreshold = useCallback(async () => {
    try {
      const data = await fetchUserAlerts();
      if (data.user_id) {
        setLoggedIn(true);
        const cfg = data.alerts || {};
        setThreshold({
          strength_up: cfg.strength_up ?? null,
          strength_down: cfg.strength_down ?? null,
          levels: cfg.levels || [],
          codes: Array.isArray(cfg.codes) ? (cfg.codes || []).join(",") : (cfg.codes || ""),
        });
      } else {
        setLoggedIn(false);
        // 未登录：从 localStorage 恢复
        const local = JSON.parse(localStorage.getItem("westock_alert_threshold") || "{}");
        setThreshold({
          strength_up: local.strength_up ?? null,
          strength_down: local.strength_down ?? null,
          levels: local.levels || [],
          codes: Array.isArray(local.codes) ? (local.codes || []).join(",") : (local.codes || ""),
        });
      }
    } catch (e) {
      // 未登录或 supabase 未配置：静默降级 localStorage
      setLoggedIn(false);
      const local = JSON.parse(localStorage.getItem("westock_alert_threshold") || "{}");
      setThreshold({
        strength_up: local.strength_up ?? null,
        strength_down: local.strength_down ?? null,
        levels: local.levels || [],
        codes: Array.isArray(local.codes) ? (local.codes || []).join(",") : (local.codes || ""),
      });
    }
  }, []);

  useEffect(() => {
    loadAlerts();
    loadThreshold();
  }, [loadAlerts, loadThreshold]);

  const saveThreshold = async () => {
    setSavingCfg(true);
    const cfg = {
      strength_up: threshold.strength_up,
      strength_down: threshold.strength_down,
      levels: threshold.levels,
      codes: threshold.codes.split(",").map((s) => s.trim()).filter(Boolean),
    };
    try {
      if (loggedIn) {
        await saveUserAlerts(cfg);
        message.success("阈值已保存到云端");
      } else {
        localStorage.setItem("westock_alert_threshold", JSON.stringify(cfg));
        message.success("阈值已保存到本地（登录后可云同步）");
      }
      loadAlerts();  // 阈值变了重新拉取（后端会按阈值过滤）
    } catch (e) {
      message.error("保存失败: " + (e.response?.data?.detail || e.message));
    } finally {
      setSavingCfg(false);
    }
  };

  const columns = [
    { title: "时间", dataIndex: "timestamp", key: "ts", width: 180,
      render: (v) => (v ? new Date(v).toLocaleString("zh-CN") : "-") },
    { title: "代码", dataIndex: "code", key: "code", width: 120 },
    { title: "板块", dataIndex: "name", key: "name", width: 120 },
    { title: "规模", dataIndex: "scale", key: "scale", width: 70,
      render: (v) => <Tag>{v || "-"}</Tag> },
    { title: "原档位", key: "old", width: 90,
      render: (_, r) => <StrengthTag level={r.old_level} value={r.old_value} /> },
    { title: "→", key: "arrow", width: 30, render: () => "→" },
    { title: "新档位", key: "new", width: 90,
      render: (_, r) => <StrengthTag level={r.new_level} value={r.new_value} /> },
    { title: "近n日净额率", dataIndex: "net_rate_n", key: "rate", width: 110,
      render: (v) => (v != null ? v.toFixed(2) + "%" : "-") },
    { title: "近n日净流入(亿)", dataIndex: "net_flow_n_yi", key: "flow", width: 130,
      render: (v) => (v != null ? v.toFixed(2) : "-") },
    { title: "交易日", dataIndex: "trade_date", key: "td", width: 100 },
  ];

  return (
    <>
      <Card
        title="强度档位变化告警"
        style={{ marginBottom: 16 }}
        extra={<Button size="small" icon={<ReloadOutlined />} onClick={loadAlerts} loading={loading}>刷新</Button>}
      >
        {alerts.length === 0 && !loading ? (
          <Empty description="暂无告警 — 盘中档位变化时自动写入" />
        ) : (
          <Table
            rowKey="id"
            dataSource={alerts}
            columns={columns}
            size="small"
            loading={loading}
            pagination={{ pageSize: 20, showSizeChanger: true, pageSizeOptions: [10, 20, 50] }}
          />
        )}
      </Card>

      <Card title={<Space><SettingOutlined /> 告警阈值配置</Space>} size="small">
        <p style={{ color: "#999", fontSize: 12, marginTop: 0 }}>
          {loggedIn
            ? "已登录：阈值存云端，多设备同步；后端按阈值过滤返回。"
            : "未登录：阈值存本地 localStorage；登录后可云同步。"}
        </p>
        <Space wrap size="middle">
          <span>仅看强度 ≥</span>
          <InputNumber
            value={threshold.strength_up}
            onChange={(v) => setThreshold({ ...threshold, strength_up: v })}
            min={-2} max={2} step={0.5} style={{ width: 80 }}
            placeholder="如 1.0"
          />
          <span>仅看强度 ≤</span>
          <InputNumber
            value={threshold.strength_down}
            onChange={(v) => setThreshold({ ...threshold, strength_down: v })}
            min={-2} max={2} step={0.5} style={{ width: 80 }}
            placeholder="如 -1.0"
          />
          <span>仅看档位</span>
          <Select
            mode="multiple"
            value={threshold.levels}
            onChange={(v) => setThreshold({ ...threshold, levels: v })}
            options={LEVEL_OPTIONS}
            style={{ minWidth: 200 }}
            placeholder="留空 = 全档位"
          />
          <span>仅看板块代码</span>
          <input
            value={threshold.codes}
            onChange={(e) => setThreshold({ ...threshold, codes: e.target.value })}
            placeholder="逗号分隔，如 pt01801081,pt01801082"
            style={{ width: 240, padding: "4px 11px", border: "1px solid #d9d9d9", borderRadius: 6 }}
          />
          <Button type="primary" onClick={saveThreshold} loading={savingCfg}>保存</Button>
        </Space>
        <Divider />
        <p style={{ color: "#666", fontSize: 12, marginTop: 0 }}>
          阈值说明：档位变化告警是全市场检测后写入 alert_log，此处阈值决定
          <b>展示哪些</b>。如设"仅看强度 ≥ 1.0"则只看升到偏强以上的告警，
          屏蔽震荡区的小幅档位抖动。
        </p>
      </Card>
    </>
  );
}
