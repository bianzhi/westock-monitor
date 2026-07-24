#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每分钟拉取申万二级板块资金流，计算强度值(-2~+2)，写入 state/minute_strength_YYYYMMDD.json
强度量化规则：
  净额率 = 主力净流入 / 板块成交额
  大盘(流通市值>=4万亿):  很强>=5%  偏强>=2%  偏弱<-1%  很弱<-1.5%
  中盘(1~4万亿):          很强>=7%  偏强>=3%  偏弱<-1.5% 很弱<-2%
  小盘(<1万亿):           很强>=10% 偏强>=4%  偏弱<-2%   很弱<-3%
  映射为连续值: 很强=+2, 偏强=+1, 普通=0, 偏弱=-1, 很弱=-2
"""
import os, sys, json, time, subprocess
from datetime import datetime, date

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE_DIR, 'state')
LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# ---- 申万二级板块代码列表（124个，pt0180xxxx）----
# 实际使用时从接口动态获取，这里放常用的部分，完整列表见文末说明
SECTOR_CODES = [
    "pt01801030","pt01801040","pt01801050","pt01801060","pt01801070",
    "pt01801080","pt01801090","pt01801100","pt01801110","pt01801120",
    "pt01801130","pt01801140","pt01801150","pt01801160","pt01801170",
    "pt01801180","pt01801190","pt01801200","pt01801210","pt01801220",
    "pt01801230","pt01801240","pt01801250","pt01801260","pt01801270",
    "pt01801280","pt01801290","pt01801300","pt01801310","pt01801320",
    # ... 实际部署时补齐124个
]

def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line)
    with open(os.path.join(LOG_DIR, 'realtime.log'), 'a', encoding='utf-8') as f:
        f.write(line + '\n')

def is_trading_time():
    now = datetime.now()
    h, m = now.hour, now.minute
    # 上午 9:30-11:30, 下午 13:00-15:00
    if (h == 9 and m >= 30) or (10 <= h < 11) or (h == 11 and m <= 30):
        return True
    if (13 <= h < 15):
        return True
    return False

def fetch_sector_fund(code):
    """调用腾讯自选股接口获取单个板块资金流，返回 dict 或 None"""
    url = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/FundTrend/getFundTrend"
    params = {
        "app": "web",
        "code": code,
        "type": "sw2",  # 申万二级
        "_": int(time.time() * 1000)
    }
    try:
        import requests
        r = requests.get(url, params=params, timeout=8)
        data = r.json()
        # 解析净额、成交额（具体字段需按实际返回调整）
        # 这里用占位解析，实际部署时打印一次 data 看结构
        return data
    except Exception as e:
        log(f"fetch {code} failed: {e}")
        return None

def calc_strength(net_rate_pct, circ_mv_yi):
    """net_rate_pct: 净额率(百分比，如 5.2 表示 5.2%)
    circ_mv_yi: 流通市值(亿元)
    返回 -2.0 ~ +2.0 的连续强度值
    """
    if circ_mv_yi >= 40000:      # 大盘
        hi, mid, lo, vlo = 5.0, 2.0, -1.0, -1.5
    elif circ_mv_yi >= 10000:    # 中盘
        hi, mid, lo, vlo = 7.0, 3.0, -1.5, -2.0
    else:                        # 小盘
        hi, mid, lo, vlo = 10.0, 4.0, -2.0, -3.0

    nr = net_rate_pct
    if nr >= hi:
        return 2.0
    elif nr >= mid:
        # [mid, hi] -> [1.0, 2.0]
        return 1.0 + (nr - mid) / (hi - mid)
    elif nr >= 0:
        # [0, mid] -> [0, 1.0]
        return (nr / mid) * 1.0
    elif nr >= lo:
        # [lo, 0] -> [-1.0, 0]
        return (nr / lo) * 1.0
    else:
        # [vlo, lo] -> [-2.0, -1.0]
        return -1.0 + (nr - lo) / (vlo - lo)

def clean_old_files():
    import glob
    for f in glob.glob(os.path.join(STATE_DIR, 'minute_strength_*.json')):
        try:
            d = f.split('_')[-1].replace('.json', '')
            dt = datetime.strptime(d, '%Y%m%d')
            if (datetime.now() - dt).days > 7:
                os.remove(f)
                log(f"cleaned old: {f}")
        except:
            pass

def main():
    log("=== realtime_strength started ===")
    force = '--force' in sys.argv  # 非交易时段也跑（用于测试）
    history = {}  # code -> [{"time": "HH:MM", "strength": x.x, "net_rate": x.xx}, ...]

    while True:
        now = datetime.now()
        trading = is_trading_time()
        if not trading and not force:
            time.sleep(30)
            continue

        today = date.today().strftime('%Y%m%d')
        hhmm = now.strftime('%H:%M')

        # 拉数据
        snapshot = {}
        for code in SECTOR_CODES:
            data = fetch_sector_fund(code)
            if not data:
                continue
            # TODO: 按实际接口返回解析 net_inflow(元) 和 turnover(元)
            # 示例: net_inflow = data[...]; turnover = data[...]
            # net_rate_pct = net_inflow / turnover * 100
            # circ_mv_yi = ...  # 需要流通市值，可预先缓存
            # strength = calc_strength(net_rate_pct, circ_mv_yi)
            # snapshot[code] = {"net_rate": net_rate_pct, "strength": strength}
            pass  # 实际部署时填入解析逻辑

        # 写入 history
        for code, info in snapshot.items():
            if code not in history:
                history[code] = []
            history[code].append({
                "time": hhmm,
                "strength": round(info["strength"], 2),
                "net_rate": round(info["net_rate"], 2)
            })
            # 限制单板块最多保存 240 个点（4小时）
            if len(history[code]) > 240:
                history[code] = history[code][-240:]

        # 落盘
        out = {
            "date": today,
            "last_update": now.isoformat(),
            "sector_count": len(SECTOR_CODES),
            "history": history
        }
        path = os.path.join(STATE_DIR, f'minute_strength_{today}.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False)
        log(f"saved {path}, sectors={len(snapshot)}")

        clean_old_files()
        time.sleep(60)

if __name__ == '__main__':
    main()