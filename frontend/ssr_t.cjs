const echarts = require('echarts');
const net = [200.4926, -112.1731, -99.8193, -69.9821, 7.2229];
const turnover = [3883.8047, 3863.2216, null, 2919.3264, 2359.127];
const xs = ['08-17','08-18','08-19','08-20','08-21'];
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
console.log('rect:', (svg.match(/<rect/g)||[]).length, 'red:', svg.includes('e74c3c'), 'green:', svg.includes('2ecc71'));
