import * as echarts from 'echarts';

const history = [
  {"date":"2026-08-17","net_flow_yi":200.4926,"turnover_yi":3883.8047,"estimated":false},
  {"date":"2026-08-18","net_flow_yi":-112.1731,"turnover_yi":3863.2216,"estimated":false},
  {"date":"2026-08-19","net_flow_yi":-99.8193,"turnover_yi":null,"estimated":true},
  {"date":"2026-08-20","net_flow_yi":-69.9821,"turnover_yi":2919.3264,"estimated":false},
  {"date":"2026-08-21","net_flow_yi":7.2229,"turnover_yi":2359.127,"estimated":false},
];
const reversed = [...history].reverse();
const xs = reversed.map(h => h.date);
const net = reversed.map(h => h.net_flow_yi ?? null);
const turnover = reversed.map(h => h.turnover_yi ?? null);
const option = {
  xAxis: { type: 'category', data: xs },
  yAxis: [
    { type: 'value', name: '净流入(亿)' },
    { type: 'value', name: '成交额(亿)', position: 'right', splitLine: {show:false} },
  ],
  series: [
    { name: '净流入(亿)', type: 'bar', yAxisIndex: 0, data: net,
      itemStyle: { color: (p) => (p.value >= 0 ? '#e74c3c' : '#2ecc71') } },
    { name: '成交额(亿)', type: 'line', yAxisIndex: 1, data: turnover },
  ],
};
const chart = echarts.init(null, null, { renderer: 'svg', ssr: true, width: 800, height: 320 });
chart.setOption(option);
const svg = chart.renderToSVGString();
console.log('<rect> 数量:', (svg.match(/<rect/g) || []).length);
console.log('含红色 #e74c3c:', svg.includes('e74c3c'));
console.log('含绿色 #2ecc71:', svg.includes('2ecc71'));
console.log('SVG 总长:', svg.length);
