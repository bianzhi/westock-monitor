import React from "react";
import { Button, Spin, Row, Col, Descriptions } from "antd";
import { StrengthTag, ScaleTag } from "./ui";
import { MinuteChart, DailyHistoryChart } from "./Charts";

/**
 * 行展开内容：单板块详情图表
 * props: record, detailCode, detailData, minuteData, detailLoading, onLoadDetail
 */
export default function SectorDetail({
  record,
  detailCode,
  detailData,
  minuteData,
  detailLoading,
  onLoadDetail,
}) {
  if (detailCode !== record.code) {
    return (
      <div style={{ padding: 8 }}>
        <Button size="small" onClick={() => onLoadDetail(record.code)}>
          加载详情图表
        </Button>
      </div>
    );
  }
  if (detailLoading) {
    return (
      <div style={{ padding: 40, textAlign: "center" }}>
        <Spin tip="加载中..." />
      </div>
    );
  }
  return (
    <div className="chart-container">
      <Row gutter={16}>
        <Col span={12}>
          <DailyHistoryChart history={detailData?.records || []} />
        </Col>
        <Col span={12}>
          <MinuteChart points={minuteData?.points || []} />
        </Col>
      </Row>
      {detailData && (
        <Descriptions
          size="small"
          bordered
          column={4}
          style={{ marginTop: 12 }}
        >
          <Descriptions.Item label="代码">{detailData.code}</Descriptions.Item>
          <Descriptions.Item label="流通市值(亿)">
            {detailData.circ_mv_yi?.toFixed(2) ?? "-"}
          </Descriptions.Item>
          <Descriptions.Item label="规模">
            <ScaleTag scale={detailData.scale} />
          </Descriptions.Item>
          <Descriptions.Item label="强度">
            <StrengthTag
              level={detailData.strength?.level}
              value={detailData.strength?.value}
            />
          </Descriptions.Item>
        </Descriptions>
      )}
    </div>
  );
}
