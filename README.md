# Westock Monitor · 申万二级板块资金流监控系统

> 基于 `westock-data` CLI（腾讯自选股官方数据）+ 腾讯原始 HTTP 接口，
> 实时跟踪申万二级板块的主力资金流向，通过连续 n 日的净额率指标判定板块强度。

## 🎯 数据源原则

> **所有数据优先查 `westock-data` CLI；只有 westock 实在查不到、又确实需要的字段，才去计算或走腾讯原始 HTTP 兜底。**
> 不得本末倒置——能用接口直接拿到的值，不要用「成分股加权/反推/估算」等手段自己算。
> 落地到实现：`westock.py` 是唯一 westock 调用封装；`tencent_quote.py` 等仅补 westock 缺失的字段。

## 🗄️ 数据流原则

> **采集的数据必须先落数据库，页面一律从数据库加载。**
> 采集（collector 后台线程）负责调 westock/腾讯并写入 SQLite；API 只读库/内存缓存，
> 不在请求路径上同步调外部接口返回给页面。页面轮询 `/api/health` 的 `cache_updated`
> 感知新数据入库后自动刷新，而非手动刷新才看到最新数据。

## ✨ 功能特性

- **板块覆盖**：申万 2021 版二级行业（硬编码 134 个）+ 概念板块（722 个，源码持久化清单）
- **资金流跟踪**：主力净流入、成交额、流通市值、涨跌幅、换手率实时获取
- **强度判定**：5 档（强/偏强/普通/偏弱/弱），阈值按规模分档，沿用原脚本
- **日内分钟级**：当日累计值差分得到本分钟净流入，分时对比图展示净额率
- **n 日窗口可配**：默认 5 日，可切换 3/5/10/20
- **跨日汇总**：近 3 日 / 近 5 日净流入与净额率
- **可视化宽表**：板块名称 / 代码 / 流通值 / 规模 / 今日净流入 / 净额率 / 近 n 日明细 / 强度判定
- **行展开图表**：近 n 日净流入柱状图（双 y 轴）+ 当日分钟级资金流折线（含净额率曲线）
- **历史回看**：板块页 / 分时页加日期选择，从本地落库表读取任意历史交易日数据
- **分时分组**：宽表勾选「组」列，分时对比图只显示分组内板块（空则不干预原逻辑）
- **日线图**：板块名称/代码超链接跳转对应板块日线图 / 分时图

## 🏗️ 技术架构

```
┌──────────────────────────────────────────────────────────────┐
│                    数据采集层 (Python)                       │
│  collector.py（后台线程，定期/手动触发）                     │
│  ├─ westock-data CLI (subprocess) → MainNetFlow 主力净流入  │
│  ├─ 腾讯原始 HTTP (requests) → 板块指数涨跌幅/换手率/流通值  │
│  ├─ collect_minute_snapshot      分钟级快照差分             │
│  ├─ collect_sector_daily_snapshot 全板块日净流入落库        │
│  └─ backfill_sparse_sector_daily  稀疏板块历史回溯补采      │
│         ↓                                                     │
│  strength.py → 5档强度判定 + 分段线性插值                   │
└──────────────────────────┬───────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────┐
│                    存储层 (SQLite)                           │
│  storage.py                                                  │
│  ├─ sector_meta          板块元数据（代码/名称/流通市值）   │
│  ├─ minute_snapshot      分钟级快照（保留最近5个工作日）    │
│  ├─ minute_delta         分钟级增量（差分结果）             │
│  ├─ sector_daily         全板块日净流入（保留30交易日）     │
│  ├─ concept_daily        概念板块日净流入（保留20交易日）   │
│  └─ sector_circ_mv       流通市值/涨跌幅/换手率（按日落库）│
│  设计原则：采集先落库，页面/API 只读库/缓存                 │
└──────────────────────────┬───────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────┐
│                    API 层 (FastAPI)                          │
│  app.py                                                      │
│  ├─ GET  /api/sectors          板块宽表（读 data_cache）    │
│  ├─ GET  /api/sectors/history  历史回看宽表（读 sector_daily）│
│  ├─ GET  /api/sectors/concept  概念板块宽表                  │
│  ├─ GET  /api/sectors/{code}   单板块详情                   │
│  ├─ GET  /api/sectors/{code}/minute  单板块分钟级           │
│  ├─ GET  /api/minute/compare   分时对比（支持 trade_date）  │
│  └─ GET  /api/strength/ranking 强度排行                     │
└──────────────────────────┬───────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────┐
│                    前端 (React + ECharts + antd)            │
│  宽表 + 行展开图表 + 分时对比 + 日线图 + 日期选择 + 分组     │
└──────────────────────────────────────────────────────────────┘
```

## 📂 项目结构

```
westock-monitor/
├── config.py              # 全局配置（n日窗口、规模分档阈值、保留策略等可配项）
├── sectors.py             # 申万二级板块代码列表（134个硬编码）
├── concept_sectors.py     # 概念板块清单（722个，源码持久化 + 发现逻辑）
├── westock.py             # westock-data CLI 封装（subprocess + 批量分批）
├── westock_fund_metrics.py # 板块资金指标计算（成交额口径等）
├── tencent_quote.py       # 腾讯行情 HTTP 接口封装（涨跌幅/换手率/流通市值，仅补 westock 缺失字段）
├── strength.py            # 强度计算（5档判定 + 分段线性插值 + n日窗口可配）
├── trading_calendar.py    # A股交易日历（Tushare 优先，缓存兜底）
├── collector.py           # 采集层（分钟差分 + 日级落库 + 板块刷新 + 稀疏补采）
├── storage.py             # 存储层（SQLite，分钟保留5工作日 + 日级保留30交易日）
├── app.py                 # FastAPI 后端
├── requirements.txt       # Python 依赖
├── start.sh               # 一键启动脚本 (macOS)
├── realtime_strength.py   # 原始脚本（保留作参考）
├── data/                  # 运行时生成（SQLite 数据库 + 板块列表缓存）
├── logs/                  # 运行时生成（采集日志）
└── frontend/              # React 前端
    ├── package.json
    ├── vite.config.js
    ├── index.html
    └── src/
        ├── main.jsx
        ├── App.jsx        # 主应用（宽表 + 日期选择 + 分组 + 分时对比）
        ├── Charts.jsx     # ECharts 图表组件（分时/日线/净额率）
        ├── CompareChart.jsx # 分时对比图
        ├── DailyChart.jsx   # 板块日线图
        ├── SectorDetail.jsx # 行展开详情（近n日 + 分钟图）
        ├── L1Tab.jsx        # 一级行业 Tab
        ├── ui.jsx         # 强度档位标签等 UI 组件
        ├── api.js         # axios API 封装
        └── index.css
```

## 🚀 快速开始

### 环境要求

- **Node.js** ≥ 18（macOS 用 Homebrew 装：`brew install node`）
- **Python** ≥ 3.8
- **npx**（随 Node.js 一起安装）

### 一键启动 (macOS)

```bash
./start.sh
```

启动后访问：

- **前端**：http://localhost:5173
- **后端 API**：http://localhost:8200
- **API 文档**：http://localhost:8200/docs

### 手动启动

#### 1. 后端

```bash
# 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 启动 FastAPI
uvicorn app:app --host 0.0.0.0 --port 8200 --reload
```

#### 2. 前端

```bash
cd frontend
npm install
npm run dev
```

#### 3. 采集循环（可选，后端已自动触发采集）

```bash
python collector.py --loop
```

## 📊 强度判定算法

### 规模分档（按流通市值）

| 档位 | 流通市值 | 很强(hi) | 偏强(mid) | 偏弱(lo) | 很弱(vlo) |
|------|---------|---------|----------|---------|----------|
| 大盘 | ≥4万亿  | 5%      | 2%       | -1%     | -1.5%    |
| 中盘 | 1~4万亿 | 7%      | 3%       | -1.5%   | -2%      |
| 小盘 | <1万亿  | 10%     | 4%       | -2%     | -3%      |

### 净额率 → 5档判定词映射

净额率 = 主力净流入额 ÷ 当日成交额 × 100%

```
净额率 ≥ hi   → "强"
净额率 ≥ mid  → "偏强"
净额率 ≥ lo   → "普通"
净额率 ≥ vlo  → "偏弱"
净额率 < vlo  → "弱"
```

### 连续强度值（-2 ~ +2，分段线性插值）

```
nr ≥ hi                → +2.0
[mid, hi]              → [1.0, 2.0]
[0, mid]               → [0, 1.0]
[lo, 0]                → [-1.0, 0]
[vlo, lo]              → [-2.0, -1.0]
```

### n 日窗口聚合

强度判定基于**近 n 日聚合净额率**：

```
近n日净额率 = 近n日主力净流入之和 ÷ 近n日成交额之和 × 100%
```

n 可在界面右上角切换（3 / 5 / 10 / 20）。

## 🔧 配置说明

所有配置在 `config.py`，支持环境变量覆盖：

| 配置项 | 默认值 | 说明 |
|-------|-------|------|
| `STRENGTH_WINDOW_N` | 5 | 强度判定窗口天数 |
| `DISPLAY_DAYS` | 5 | 表头展示天数 |
| `SUMMARY_3D` | 3 | 近3日汇总窗口 |
| `SUMMARY_5D` | 5 | 近5日汇总窗口 |
| `MINUTE_INTERVAL` | 60 | 分钟级采集间隔(秒) |
| `MINUTE_CACHE_DAYS` | 5 | 分钟级数据保留最近几个工作日 |
| `WESTOCK_BATCH_SIZE` | 20 | westock-data 批量查询每批数量 |
| `WESTOCK_WORKERS` | 8 | westock-data 并发线程数 |
| `TENCENT_WORKERS` | 8 | 腾讯HTTP接口并发数 |
| `API_PORT` | 8200 | 后端端口 |

环境变量示例：

```bash
export STRENGTH_WINDOW_N=10
export MINUTE_INTERVAL=30
./start.sh
```

## 📡 API 接口

### `GET /api/sectors?n=5`

板块列表 + 当前强度（宽表主数据）。

**响应示例：**

```json
{
  "date": "2026-07-24",
  "last_update": "2026-07-24T14:30:00",
  "n_window": 5,
  "total": 134,
  "sectors": [
    {
      "code": "pt01801081",
      "name": "半导体",
      "l1": "电子",
      "circ_mv_yi": 50000.0,
      "scale": "大盘",
      "today_net_flow_yi": 5.09,
      "today_turnover_yi": 100.0,
      "today_net_rate": 5.09,
      "history": [...],
      "summary_3d": {"days": 3, "net_flow_yi": 12.5, "net_rate": 4.2},
      "summary_5d": {"days": 5, "net_flow_yi": 20.3, "net_rate": 3.8},
      "strength_value": 2.0,
      "strength_level": "强"
    }
  ]
}
```

### 其他接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查 + 存储状态 |
| GET | `/api/config` | 当前配置 |
| GET | `/api/sectors/{code}` | 单板块详情 |
| GET | `/api/sectors/{code}/minute` | 单板块当日分钟级数据 |
| GET | `/api/sectors/history?date=` | 历史回看宽表（读 sector_daily） |
| GET | `/api/sectors/concept` | 概念板块宽表 |
| GET | `/api/sectors/concept/history?date=` | 概念板块历史宽表 |
| GET | `/api/sectors/l1-summary` | 一级行业汇总 |
| GET | `/api/minute/compare` | 分时对比（支持 trade_date） |
| GET | `/api/strength/ranking` | 强度排行 |
| POST | `/api/refresh-sectors` | 手动刷新板块列表 |
| POST | `/api/refresh-concepts` | 手动刷新概念板块 |
| POST | `/api/collect/minute` | 手动触发一次分钟采集 |

完整接口文档：http://localhost:8200/docs

## 🧪 测试与自检

各模块都内置了 `__main__` 自检：

```bash
# 测试 westock-data CLI 封装
python westock.py

# 测试腾讯原始HTTP接口
python tencent_api.py

# 测试强度计算
python strength.py

# 测试存储层
python storage.py

# 刷新板块列表
python collector.py --refresh-sectors

# 测试分钟采集
python collector.py --test-minute

# 测试日级采集
python collector.py --test-daily
```

## ⚠️ 注意事项

1. **westock-data 版本**：本项目基于 `westock-data-skillhub@1.0.5`，
   老版本（1.0.3）的 `asfund`/`minute`/`quote`/`board` 命令已废弃。
2. **数据延迟**：`fund flow` 返回的 `MainNetFlow` 是当日累计值，
   分钟级净流入通过差分得到，本质不是"真分钟级"。
3. **成交额来源**：westock-data 不提供板块成交额，
   本项目通过腾讯原始 HTTP 接口（`proxy.finance.qq.com`）补充。
4. **交易日历**：仅判断 A 股交易时段（9:30-11:30, 13:00-15:00），
   非交易时段采集循环空转。
5. **历史数据**：板块日级历史数据每日收盘后落库到 `sector_daily`/`concept_daily`
   （保留 30/20 交易日），历史回看与日线图直接读库；未落库的早期日期才用
   `MainNetFlow5D/10D/20D` 累计差分估算，前端会标注 `estimated: true`。

## 📝 License

仅供学习研究使用，不构成投资建议。
