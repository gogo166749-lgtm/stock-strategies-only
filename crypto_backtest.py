"""Pilot only: historical zone-touch execution proxy, not radar accuracy."""
import bisect
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import crypto_radar as radar

H = radar.HOUR
FEE = .0006  # hypothetical per-side taker fee, not user's actual fee tier
SLIP = .0002  # hypothetical adverse fill adjustment


def history(symbol, start, end, output):
    found = {}
    for lo in range(start, end, 450*H):
        hi = min(end, lo+450*H)
        rows = radar.kucoin('kline/query', symbol=symbol, granularity=60,
                           **{'from':lo-H, 'to':hi+H})
        for row in rows:
            t = int(row[0])
            if start <= t < end:
                found[t] = row
        time.sleep(.3)
    raw = [found[t] for t in sorted(found)]
    (output/f'{symbol}_ohlcv.json').write_text(json.dumps(raw))
    gaps=[(a[0],b[0]) for a,b in zip(raw,raw[1:]) if b[0]-a[0]!=H]
    print(f'{symbol}: bars={len(raw)}, gaps={gaps[:5]}',flush=True)
    normalized = [[str(x[0]), *map(str,x[1:6]), '0', str(x[6]), '1'] for x in raw]
    cs = radar.candles(normalized,1,end)
    if cs[0]['t'] != start or cs[-1]['t'] != end-H or len(cs) != (end-start)//H:
        raise ValueError('歷史 K 線覆蓋不足，不縮短區間冒充完成')
    return cs


def funding_history(symbol, start, end, output):
    found = {}
    for lo in range(start-8*H, end, 3*24*H):
        rows = radar.kucoin('contract/funding-rates', symbol=symbol,
                           **{'from':lo-8*H, 'to':min(end,lo+3*24*H)+8*H})
        for x in rows:
            t=int(x['timepoint'])
            if start-8*H <= t < end:
                found[t]=radar.num(x['fundingRate'])
        time.sleep(.3)
    result=sorted(found.items())
    (output/f'{symbol}_funding.json').write_text(json.dumps(result))
    if not result or result[0][0] > start or result[-1][0] < end-8*H:
        raise ValueError('歷史資金費率覆蓋不足')
    if any(b[0]-a[0]>8*H for a,b in zip(result,result[1:])):
        raise ValueError('歷史資金費率存在超過 8 小時缺口')
    return result


def aggregate(cs):
    groups={}
    for c in cs:
        groups.setdefault(c['t']//(4*H)*(4*H),[]).append(c)
    result=[]
    for t, group in sorted(groups.items()):
        if len(group)!=4 or [x['t'] for x in group] != [t+j*H for j in range(4)]:
            continue
        result.append(dict(t=t,o=group[0]['o'],h=max(x['h'] for x in group),
                           l=min(x['l'] for x in group),c=group[-1]['c'],v=sum(x['v'] for x in group)))
    return result


def settle_bar(bar, direction, stop, target):
    # Adverse gap uses opening price; stop first if OHLC touches both boundaries.
    if direction*(bar['o']-stop)<=0:
        return bar['o'], 'stop_gap'
    sl = bar['l']<=stop if direction==1 else bar['h']>=stop
    tp = bar['h']>=target if direction==1 else bar['l']<=target
    if sl:
        return stop, 'stop_ambiguous' if tp else 'stop'
    if tp:
        return target, 'target'
    return None


def execute(cs, signal_i, plan, funding, fee=FEE, slip=SLIP):
    d, limit, stop = plan['direction'],plan['entry'],plan['stop']
    fill_i=None
    # Only bars after signal is known; pending orders expire after 12 hours.
    for j in range(signal_i+1,min(len(cs),signal_i+13)):
        c=cs[j]
        if d*(c['o']-stop)<=0:
            return None, j
        if c['l']<=limit<=c['h']:
            fill_i=j
            break
        if (c['l']<=stop if d==1 else c['h']>=stop):
            return None,j
    if fill_i is None:
        return None,min(len(cs)-1,signal_i+12)
    entry=limit*(1+d*slip)
    risk=d*(entry-stop)
    target=plan['target']
    if risk<=0 or d*(target-entry)<=0:
        return None,fill_i
    end_i=min(len(cs)-1,fill_i+47)  # hold at most 48 hourly bars
    raw_exit,reason=cs[end_i]['c'],'timeout' if end_i==fill_i+47 else 'end_of_sample'
    exit_i=end_i
    for j in range(fill_i,end_i+1):
        c=cs[j]
        if j==fill_i:
            # Intrabar touch time unknown: count stops, never claim a same-bar target.
            if c['l']<=stop if d==1 else c['h']>=stop:
                raw_exit,reason,exit_i=stop,'entry_bar_stop',j
                break
            continue
        event=settle_bar(c,d,stop,target)
        if event:
            raw_exit,reason=event
            exit_i=j
            break
    exit_price=raw_exit*(1-d*slip)
    gross=d*(exit_price-entry)/entry
    fees=fee*(1+exit_price/entry)
    funding_cost=0.
    times=[x['t'] for x in cs]
    # Unknown intrabar times: charge payments in entry/exit bars, omit ambiguous credits.
    for t,rate in funding:
        if cs[fill_i]['t']<=t<cs[exit_i]['t']+H:
            k=max(0,bisect.bisect_right(times,t)-1)
            cost=d*rate*cs[k]['o']/entry  # OHLC open proxy for mark price
            if k in (fill_i,exit_i) and cost<0:
                continue
            funding_cost+=cost
    net=gross-fees-funding_cost
    return dict(signal_time=cs[signal_i]['t']+H,entry_time=cs[fill_i]['t'],
                exit_time=cs[exit_i]['t']+H,direction=d,entry=entry,exit=exit_price,
                stop=stop,target=target,gross_return=gross,fees=fees,
                funding_cost=funding_cost,net_return=net,net_r=net/(risk/entry),reason=reason),exit_i


def stats(trades):
    n=len(trades)
    returns=[x['net_return'] for x in trades]
    gains=sum(max(x,0) for x in returns)
    losses=-sum(min(x,0) for x in returns)
    return dict(trades=n,wins=sum(x>0 for x in returns),
                win_rate=sum(x>0 for x in returns)/n if n else None,
                mean_net_return=sum(returns)/n if n else None,
                profit_factor=gains/losses if losses else None,
                mean_net_r=sum(x['net_r'] for x in trades)/n if n else None,
                note='無虧損時 profit_factor 為 null；無交易時統計為 null。未計投組收益率或最大回撤。')


def run_symbol(cs, funding, start, end):
    higher=aggregate(cs)
    closes=[x['t']+4*H for x in higher]
    trades=[]
    blocked_until=-1
    counts=Counter()
    used=set()
    for i,c in enumerate(cs):
        signal_time=c['t']+H
        if signal_time<start or signal_time>=end or signal_time//H%24 not in (0,10):
            continue
        counts['scheduled_scans']+=1
        if i<=blocked_until:
            counts['position_or_pending']+=1
            continue
        known=cs[max(0,i-149):i+1]
        k=bisect.bisect_right(closes,signal_time)
        h=higher[max(0,k-150):k]
        if len(known)<80 or len(h)<80 or sum(x['v'] for x in known[-24:])<10_000_000:
            counts['insufficient_data_or_volume']+=1
            continue
        plan=radar.analyze(known,h)
        if not plan:
            counts['no_setup']+=1
            continue
        key=(plan['direction'],plan['zone']['kind'],plan['zone']['low'],plan['zone']['high'])
        if key in used:
            counts['duplicate_zone']+=1
            continue
        used.add(key)
        counts['setups']+=1
        trade,blocked_until=execute(cs,i,plan,funding)
        if trade:
            trades.append(trade)
        else:
            counts['not_filled_or_invalid']+=1
    boundary=start+(end-start)*2//3
    # Ordered descriptive split, not a genuine future/untouched holdout.
    return dict(counts=dict(counts),all=stats(trades),
                first_60_days=stats([x for x in trades if x['signal_time']<boundary]),
                final_30_days=stats([x for x in trades if x['signal_time']>=boundary]),trades=trades)


def pct(v):
    return '無樣本' if v is None else f'{v*100:.2f}%'


def main():
    out=Path('backtest_output');out.mkdir(exist_ok=True)
    end=int(time.time()*1000)//(24*H)*(24*H)  # last complete UTC day
    start=end-90*24*H
    result=dict(start=start,end=end,fee_per_side=FEE,slippage_per_side=SLIP,results={},errors={})
    result['strategy_sha256']=hashlib.sha256(Path('crypto_radar.py').read_bytes()).hexdigest()
    for symbol in ('XBTUSDTM','ETHUSDTM','SOLUSDTM'):
        print('Downloading '+symbol,flush=True)
        try:
            cs=history(symbol,start-30*24*H,end,out)
            funding=funding_history(symbol,start,end,out)
            result['results'][symbol]=run_symbol(cs,funding,start,end)
        except Exception as exc:
            result['errors'][symbol]=str(exc) if isinstance(exc,(ValueError,RuntimeError)) else type(exc).__name__
        print('Finished '+symbol,flush=True)
    (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    date=lambda t:datetime.fromtimestamp(t/1000,timezone.utc).strftime('%Y-%m-%d')
    lines=['# 加密貨幣候選區域回測試跑',f'UTC {date(start)} 至 {date(end)}（終點不含）',
           '這是 BTC、ETH、SOL 的區間中點觸價進場代理，不是 Top 200 雷達或人工確認策略的準確率。',
           '沿用已收線 1H/4H 訊號，每天 UTC 00:00、10:00 掃描。未調整訊號參數。',
           '假設每邊手續費 0.06%、每邊不利滑點 0.02%；資金費率用歷史結算值與當時 1H 開盤價近似名目價值。',
           '待成交12小時，持倉最多48小時，每幣不重疊；同根雙觸以停損優先。進場K只算停損、不計停利。',
           '|合約|交易數|淨勝率|每筆平均淨報酬|每筆平均淨R|', '|---|---:|---:|---:|---:|']
    for sym,r in result['results'].items():
        a=r['all'];nr='無樣本' if a['mean_net_r'] is None else f"{a['mean_net_r']:.3f}"
        lines.append(f"|{sym}|{a['trades']}|{pct(a['win_rate'])}|{pct(a['mean_net_return'])}|{nr}|")
    lines += ['\n限制：沒有歷史市值排名／全市場量／OI 排序；沒有還原 Top 5 選幣，固定三個存活合約可能偏樣本。',
              '沒有還原人工低週期確認。費率及滑點為假設，資金費率資料最多8小時間隔檢查仍可能漏掉短週期缺值。',
              '前60／後30天僅時間切分，不稱為真正樣本外驗證；不因結果不佳而調參重算。',
              '未計投組配置、槓桿與清算，故不報投組收益率／最大回撤。少量樣本不能證實準確率。']
    if result['errors']:
        lines += ['\n資料失敗（不納入績效）：',json.dumps(result['errors'],ensure_ascii=False)]
    report='\n'.join(lines)
    (out/'REPORT.md').write_text(report,encoding='utf-8')
    print(report,flush=True)
    if result['errors']:raise SystemExit(1)


if __name__=='__main__':main()
