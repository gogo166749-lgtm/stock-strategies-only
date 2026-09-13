"""Read-only crypto watchlist. Python 3.11+, no trading credentials required."""
import argparse
from collections import Counter
from datetime import datetime, timezone, timedelta
import json
import math
import os
from pathlib import Path
import statistics as stats
import time
import urllib.error
import urllib.parse
import urllib.request

CG = 'https://api.coingecko.com/api/v3'
HOUR = 3_600_000


def get(url, params=None, headers=None):
    url += ('?' + urllib.parse.urlencode(params)) if params else ''
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f'API HTTP {exc.code}') from None
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(2 ** attempt)
    raise RuntimeError('API 暫時無法取得資料')


def kucoin(path, **params):
    data = get('https://api-futures.kucoin.com/api/v1/' + path, params)
    if data.get('code') != '200000':
        raise RuntimeError('KuCoin API 回傳錯誤')
    return data['data']


def kucoin_candles(inst, hours, now):
    rows = kucoin('kline/query', symbol=inst, granularity=hours*60,
                  **{'from': now-151*hours*HOUR, 'to': now})
    # Public API has no confirm field: exclude current interval plus 10 seconds.
    normalized = [[str(x[0]), *map(str,x[1:6]), '0', str(x[6]), '1']
                  for x in rows if int(x[0])+hours*HOUR <= now-10_000]
    return candles(normalized, hours, now)


def num(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('非有限數值')
    return value


def candles(rows, hours, now):
    result = []
    for row in rows:
        if row[8] != '1':
            continue
        t = int(row[0])
        if t + hours * HOUR > now:
            continue
        o, h, l, c = map(num, row[1:5])
        v = num(row[7])  # quote-currency volume, USDT
        if not (0 < l <= min(o, c) <= max(o, c) <= h and v >= 0):
            raise ValueError('K 線數值不合理')
        result.append(dict(t=t, o=o, h=h, l=l, c=c, v=v))
    result.sort(key=lambda c: c['t'])
    if len(result) < 80:
        raise ValueError('K 線不足 80 根')
    if not 0 <= now - (result[-1]['t'] + hours * HOUR) <= hours * HOUR:
        raise ValueError('K 線過期')
    if any(b['t'] - a['t'] != hours * HOUR for a, b in zip(result, result[1:])):
        raise ValueError('K 線缺漏或重複')
    return result


def atr(cs):
    return stats.mean(max(b['h']-b['l'], abs(b['h']-a['c']), abs(b['l']-a['c']))
                      for a, b in zip(cs[-15:-1], cs[-14:]))


def pivots(cs, key, high=True):
    # Two bars on either side; only confirmed pivots are used.
    return [(i, c[key]) for i, c in enumerate(cs) if 2 <= i < len(cs)-2
            and all((c[key] > cs[j][key] if high else c[key] < cs[j][key])
                    for j in (i-2, i-1, i+1, i+2))]


def trend(cs):
    hi, lo = pivots(cs, 'h'), pivots(cs, 'l', False)
    if len(hi) < 2 or len(lo) < 2:
        return 0
    if hi[-1][1] > hi[-2][1] and lo[-1][1] > lo[-2][1]:
        return 1
    if hi[-1][1] < hi[-2][1] and lo[-1][1] < lo[-2][1]:
        return -1
    return 0


def structure(cs):
    events = []
    for i in range(max(10, len(cs)-12), len(cs)):
        before = cs[:i]
        for direction, key in ((1, 'h'), (-1, 'l')):
            ps = pivots(before, key, direction == 1)
            if not ps:
                continue
            _, level = ps[-1]
            if direction * (cs[i-1]['c']-level) <= 0 < direction * (cs[i]['c']-level):
                prior = trend(before)
                label = ('結構轉向 CHOCH' if prior == -direction else
                         '順勢突破 BOS' if prior == direction else '區間結構突破（趨勢未明）')
                events.append(dict(i=i, direction=direction, label=label, level=level))
    return events


def zones(cs, events, direction):
    result = []
    for i in range(max(2, len(cs)-40), len(cs)):
        a, mid, c = cs[i-2:i+1]
        low, high = (a['h'], c['l']) if direction == 1 else (c['h'], a['l'])
        if low < high and direction*(mid['c']-mid['o']) > 0:
            # Only untouched gaps; partial fills are deliberately excluded.
            untouched = all(x['l'] > high if direction == 1 else x['h'] < low for x in cs[i+1:])
            if untouched:
                result.append(dict(kind='未回補缺口 FVG', low=low, high=high, i=i))
    for e in events:
        if e['direction'] != direction:
            continue
        i = e['i']
        if abs(cs[i]['c']-cs[i]['o']) < atr(cs[:i+1]):
            continue  # displacement required, not every opposite candle is an OB
        for j in range(i-1, max(-1, i-7), -1):
            if direction*(cs[j]['c']-cs[j]['o']) < 0:
                low, high = cs[j]['l'], cs[j]['h']
                if all(x['l'] > high if direction == 1 else x['h'] < low for x in cs[i+1:]):
                    result.append(dict(kind='位移突破前訂單塊 OB（全根範圍）', low=low, high=high, i=i))
                break
    return result


def analyze(cs, higher):
    a, p = atr(cs), cs[-1]['c']
    if a <= 0:
        return None
    volume_base = stats.mean(x['v'] for x in cs[-21:-1])
    if volume_base <= 0:
        return None
    vr = cs[-1]['v']/volume_base
    change = abs(p/cs[-25]['c']-1)
    if change > .10 or vr > 3 or abs(cs[-1]['c']-cs[-1]['o']) > 2*a:
        return None
    d = trend(higher)
    if not d:
        return None
    events = structure(cs)
    zs = zones(cs, events, d)
    choices = []
    for z in zs:
        edge = z['high'] if d == 1 else z['low']
        distance = d*(p-edge)
        if not 0 <= distance <= 1.5*a:
            continue
        entry = (z['low']+z['high'])/2
        stop = z['low']-.25*a if d == 1 else z['high']+.25*a
        risk = abs(entry-stop)
        target = entry+d*2*risk
        # Do not offer a 2R plan through the closest known opposing swing.
        opp = pivots(cs, 'h' if d == 1 else 'l', d == 1)
        ahead = [v for _, v in opp if d*(v-entry) > 0]
        if ahead and min(d*(v-entry) for v in ahead) < 2*risk:
            continue
        same = [e for e in events if e['direction'] == d]
        prior = cs[-21:-1]
        sweep = ((cs[-1]['l'] < min(x['l'] for x in prior) < p) if d == 1 else
                 (cs[-1]['h'] > max(x['h'] for x in prior) > p))
        score = 40 + (15 if same else 0) + (10 if sweep else 0) + min(vr, 2)*5 + 10*(1-distance/(1.5*a))
        choices.append(dict(direction=d, zone=z, entry=entry, stop=stop, target=target,
                            score=round(score, 1), price=p, volume_ratio=vr,
                            structure=same[-1]['label'] if same else '近 12 根未見同向結構突破',
                            sweep=sweep, candle_time=cs[-1]['t']+HOUR))
    return max(choices, key=lambda x:x['score']) if choices else None


def pick(candidates):
    ordered = sorted(candidates, key=lambda x:(-x['score'], x['rank']))
    selected = []
    for d in (1, -1):
        subset = [x for x in ordered if x['direction'] == d]
        if subset:
            selected.append(subset[0])
    selected += [x for x in ordered if x not in selected][:5-len(selected)]
    return sorted(selected, key=lambda x:-x['score'])


def scan():
    now = int(time.time()*1000)
    headers = {'User-Agent':'crypto-radar/1.0'}
    if os.getenv('COINGECKO_DEMO_API_KEY'):
        headers['x-cg-demo-api-key'] = os.environ['COINGECKO_DEMO_API_KEY']
    print('Fetching CoinGecko market-cap rankings', flush=True)
    coins = get(CG+'/coins/markets', dict(vs_currency='usd', order='market_cap_desc',
                per_page=250, page=1, sparkline='false'), headers)
    if not isinstance(coins, list):
        raise ValueError('市值清單無效')
    top = [x for x in coins if isinstance(x.get('market_cap_rank'), int) and 1 <= x['market_cap_rank'] <= 200]
    if {x['market_cap_rank'] for x in top} != set(range(1, 201)):
        raise ValueError('未取得完整 Top 200 排名，停止產生訊號')
    counts = Counter(x['symbol'].upper() for x in coins)
    print('Fetching KuCoin instruments', flush=True)
    contracts = kucoin('contracts/active')
    instruments = {}
    for x in contracts:
        if x.get('status') != 'Open' or x.get('settleCurrency') != 'USDT' or x.get('isInverse') or x.get('expireDate'):
            continue
        base = 'BTC' if x['baseCurrency'] == 'XBT' else x['baseCurrency']
        if base in instruments:
            instruments[base] = None  # ambiguous contract mapping
        else:
            instruments[base] = x
    skipped, candidates, audit = Counter(), [], []
    analyzed = matched = 0
    for coin in top:
        print(f"Scanning rank {coin['market_cap_rank']}/200", flush=True)
        sym = coin['symbol'].upper()
        contract = instruments.get(sym)
        inst = contract['symbol'] if contract else ''
        if counts[sym] != 1 or not contract:
            skipped['無對應永續合約或代號重複'] += 1
            continue
        matched += 1
        try:
            updated = datetime.fromisoformat(coin['last_updated'].replace('Z','+00:00')).timestamp()*1000
            if not 0 <= now-updated <= 2*HOUR:
                raise ValueError('市值資料過期')
            if num(coin['total_volume']) < 20_000_000:
                skipped['全市場成交量不足'] += 1
                continue
            c = kucoin_candles(inst, 1, now)
            if sum(x['v'] for x in c[-24:]) < 10_000_000:
                skipped['KuCoin 24小時成交量不足'] += 1
                continue
            if abs(c[-1]['c']/num(coin['current_price'])-1) > .10:
                raise ValueError('跨來源價格差異過大，代號映射待確認')
            h = kucoin_candles(inst, 4, now)
            analyzed += 1
            plan = analyze(c, h)
            if plan is None:
                skipped['無合格回踩訊號／已過熱'] += 1
                audit.append(dict(id=coin['id'], instrument=inst, status='no_setup'))
                continue
            plan.update(symbol=sym, instrument=inst, coin_id=coin['id'], rank=coin['market_cap_rank'],
                        funding=None, oi_usd=None)
            try:
                plan['funding'] = num(contract['fundingFeeRate'])*100
                interval = num(contract['currentFundingRateGranularity'])/HOUR
                if interval > 0 and plan['direction']*plan['funding']/interval > .01:
                    plan['score'] -= 10
            except (KeyError, ValueError, TypeError):
                pass
            try:
                plan['oi_usd'] = num(contract['openInterest'])*num(contract['multiplier'])*num(contract['markPrice'])
            except (KeyError, ValueError, TypeError):
                pass
            candidates.append(plan)
            audit.append(dict(id=coin['id'], instrument=inst, status='candidate'))
        except Exception as exc:
            skipped['資料錯誤或不足'] += 1
            audit.append(dict(id=coin['id'], instrument=inst, status='data_error', error=str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__))
        time.sleep(.3)
    if analyzed == 0:
        raise ValueError('沒有可完成分析的合約，停止產生訊號')
    return dict(time=now, universe=200, matched=matched, analyzed=analyzed,
                skipped=dict(skipped), candidates=pick(candidates), audit=audit,
                note='規則式候選，非勝率；未回測。僅涵蓋 KuCoin USDT 永續合約。')


def report(r):
    stamp = datetime.fromtimestamp(r['time']/1000, timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')
    lines = [f'加密貨幣多空雷達｜{stamp} 台灣時間',
             f"市值 Top {r['universe']}｜對應合約 {r['matched']}｜完成分析 {r['analyzed']}",
             'CoinGecko 排名＋KuCoin 1H／4H 已收線資料；價位單位 USDT。',
             '條件式回踩觀察，不是立即下單訊號。評分不是勝率。']
    for i, x in enumerate(r['candidates'], 1):
        d = '多' if x['direction']==1 else '空'
        z = x['zone']
        funding = '缺資料' if x['funding'] is None else f"{x['funding']:.4f}%／當期"
        oi = '缺資料' if x['oi_usd'] is None else f"${x['oi_usd']:,.0f}"
        lines += [f"\n{i}. {x['symbol']} 偏{d}｜排序分 {x['score']:.1f}",
                  f"4H 高低點偏{d}；1H {x['structure']}",
                  f"{z['kind']}：{z['low']:.8g}～{z['high']:.8g}",
                  f"已收線價 {x['price']:.8g}｜量比 {x['volume_ratio']:.2f}",
                  '最近一根掃前20根流動性並收回：'+('有' if x['sweep'] else '未見'),
                  f"假設區間中點進場 {x['entry']:.8g}｜失效 {x['stop']:.8g}｜2R參考 {x['target']:.8g}",
                  '觸區後需等低週期同向收線突破再評估；實際進場須重算風報比，失效則取消。',
                  f'資金費率 {funding}｜KuCoin 持倉量 {oi}（快照，非增減）']
    if not r['candidates']:
        lines.append('\n本次沒有符合條件的候選，不湊單。')
    for d, label in ((1,'多'),(-1,'空')):
        if not any(x['direction']==d for x in r['candidates']):
            lines.append(f'本次無合格偏{label}候選。')
    lines.append('\n排除統計：'+ '；'.join(f'{k} {v}' for k,v in r['skipped'].items()))
    lines.append('僅定時掃描，非即時觸價監控；未回測、未計交易成本，非獲利保證。')
    return '\n'.join(lines)


def send(text):
    token = os.environ['TELEGRAM_BOT_TOKEN'].strip()
    chat = os.environ['TELEGRAM_CHAT_ID'].strip()
    # Never log URLs, response bodies, or exceptions that might include secrets.
    for start in range(0, len(text), 3000):
        payload = urllib.parse.urlencode(dict(chat_id=chat, text=text[start:start+3000])).encode()
        try:
            req = urllib.request.Request(f'https://api.telegram.org/bot{token}/sendMessage', data=payload)
            with urllib.request.urlopen(req, timeout=20) as response:
                if not json.load(response).get('ok'):
                    raise ValueError()
        except Exception:
            raise RuntimeError('Telegram 發送失敗，請檢查 Secrets／網路') from None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--send', action='store_true')
    args = parser.parse_args()
    out = Path('crypto_output')
    out.mkdir(exist_ok=True)
    try:
        r = scan()
        message = report(r)
        (out/'latest.json').write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding='utf-8')
        (out/'latest.txt').write_text(message, encoding='utf-8')
    except Exception as exc:
        message = '加密貨幣雷達：本次資料取得或驗證失敗，未產生交易訊號。請查看 GitHub Actions。'
        reason = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        print('Data validation error: ' + reason, flush=True)
        (out/'error.txt').write_text(reason, encoding='utf-8')
        if args.send:
            send(message)
        print(message)
        raise SystemExit(1) from None
    print(message)
    if args.send:
        send(message)


if __name__ == '__main__':
    main()
