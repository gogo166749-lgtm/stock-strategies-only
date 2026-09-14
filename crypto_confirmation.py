"""Frozen 15m confirmation experiment; never sends orders or live buy signals."""
import bisect
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import crypto_radar as r
import crypto_backtest as bt

H=r.HOUR
Q=H//4
FEE=.0006
SLIP=.0002
FUNDING_BUDGET=.0006  # prospective 48h allowance, not future observed funding
START=int(datetime(2026,3,1,tzinfo=timezone.utc).timestamp()*1000)
SPLIT=int(datetime(2026,5,1,tzinfo=timezone.utc).timestamp()*1000)
END=int(datetime(2026,6,1,tzinfo=timezone.utc).timestamp()*1000)


def quarters(symbol,start,end,out):
    found={}
    for lo in range(start,end,180*Q):
        hi=min(end,lo+180*Q)
        rows=r.kucoin('kline/query',symbol=symbol,granularity=15,**{'from':lo-Q,'to':hi+Q})
        for x in rows:
            t=int(x[0])
            if start<=t<end:
                found[t]=x
        time.sleep(.2)
    raw=[found[t] for t in sorted(found)]
    (out/f'{symbol}_15m.json').write_text(json.dumps(raw))
    expected=list(range(start,end,Q))
    if sorted(found)!=expected:
        raise ValueError(f'15m資料缺漏: expected={len(expected)} actual={len(raw)}')
    cs=[]
    for x in raw:
        o,h,l,c,v=map(r.num,[*x[1:5],x[6]])
        if not 0<l<=min(o,c)<=max(o,c)<=h or v<0:
            raise ValueError('15m OHLC invalid')
        cs.append(dict(t=int(x[0]),o=o,h=h,l=l,c=c,v=v))
    print(f'{symbol}: 15m={len(cs)}, no gaps',flush=True)
    return cs


def confirmation(q,signal_time,plan,cutoff=None):
    """Only closed quarters after the scheduled signal; returns next OPEN index."""
    d=plan['direction'];z=plan['zone']
    first=bisect.bisect_left([x['t'] for x in q],signal_time)
    touched=False;sweep=None
    for i in range(first,min(first+48,len(q)-1)):
        c=q[i]
        if cutoff is not None and c['t']+Q>=cutoff:
            break
        if (c['l']<=plan['stop'] if d==1 else c['h']>=plan['stop']):
            return None,'zone_invalidated'
        if c['l']<=z['high'] and c['h']>=z['low']:
            touched=True
        if not touched or i<20:
            continue
        previous=q[max(0,i-80):i]
        if sweep:
            # A later violation of the swept extreme cancels this setup.
            if (c['l']<sweep['extreme'] if d==1 else c['h']>sweep['extreme']):
                return None,'sweep_failed'
            level=sweep['break_level']
            if d*(q[i-1]['c']-level)<=0<d*(c['c']-level):
                return dict(entry_i=i+1,confirmed_at=c['t']+Q,
                            stop=sweep['stop'],break_level=level,sweep_time=sweep['time']), 'confirmed'
        else:
            liquidity=r.pivots(previous,'l' if d==1 else 'h',d==-1)
            opposing=r.pivots(previous,'h' if d==1 else 'l',d==1)
            if not liquidity or not opposing:
                continue
            level=liquidity[-1][1]
            swept=(c['l']<level<c['c']) if d==1 else (c['h']>level>c['c'])
            if swept:
                extreme=c['l'] if d==1 else c['h']
                sweep=dict(extreme=extreme,stop=extreme-d*.1*r.atr(previous),
                           break_level=opposing[-1][1],time=c['t'])
    return None,'expired_without_confirmation'


def cost_target(entry,stop,d,fee=FEE,slip=SLIP):
    stop_fill=stop*(1-d*slip)
    loss=d*(entry-stop_fill)/entry+fee*(1+stop_fill/entry)+FUNDING_BUDGET
    if d*(entry-stop)<=0 or loss<=0:
        return None
    ratio=(1+fee+FUNDING_BUDGET+2*loss)/(1-fee) if d==1 else (1-fee-FUNDING_BUDGET-2*loss)/(1+fee)
    raw=entry*ratio/(1-d*slip)
    if raw<=0:return None
    return raw,loss


def execute(q,confirmed,plan,resistance,funding,cutoff):
    i=confirmed['entry_i'];d=plan['direction'];stop=confirmed['stop']
    if i>=len(q) or q[i]['t']>=cutoff:return None,'end_of_window'
    entry=q[i]['o']*(1+d*SLIP)
    geometry=cost_target(entry,stop,d)
    if not geometry:return None,'gap_invalidates_stop'
    target,loss=geometry
    ahead=[v for v in resistance if d*(v-entry)>0]
    if not ahead or min(d*(v-entry) for v in ahead)<d*(target-entry):
        return None,'insufficient_room_after_costs'
    funding_cost=0.;marks=[]
    max_i=min(len(q)-1,i+191)
    reason='timeout'
    for j in range(i,max_i+1):
        c=q[j]
        if c['t']>=cutoff:break
        event=bt.settle_bar(c,d,stop,target)
        exit_now=event is not None or j==max_i or c['t']+Q>=cutoff
        # Historical payments with OHLC open as the mark-value proxy.
        for t,rate in funding:
            if c['t']<=t<c['t']+Q:
                cost=d*rate*c['o']/entry
                if (j==i or exit_now) and cost<0:continue
                funding_cost+=cost
        price=c['c']
        if event:price,reason=event
        elif c['t']+Q>=cutoff:reason='window_end'
        fill=price*(1-d*SLIP)
        net=d*(fill-entry)/entry-FEE*(1+fill/entry)-funding_cost
        marks.append((c['t']+Q,net))
        if exit_now:
            return dict(entry_time=q[i]['t'],exit_time=c['t']+Q,direction=d,entry=entry,exit=fill,
                        stop=stop,target=target,net_return=net,net_r=net/loss,
                        funding_cost=funding_cost,reason=reason,risk_fraction=loss,
                        marks=marks,confirmed_at=confirmed['confirmed_at']), 'trade'
    return None,'end_of_window'


def metrics(trades):
    result=bt.stats(trades)
    result['note']='每幣獨立帳戶；回撤按15m淨清算估值。無虧損時profit factor未定義。'
    equity=peak=10000.;drawdown=0.;losing=longest=0
    for trade in sorted(trades,key=lambda x:x['entry_time']):
        fraction=min(1.,.01/trade['risk_fraction'])  # 1% planned risk; notional <= equity
        for _,net in trade['marks']:
            value=equity*(1+fraction*net)
            peak=max(peak,value)
            drawdown=max(drawdown,(peak-value)/peak)
        equity*=1+fraction*trade['net_return']
        losing=losing+1 if trade['net_return']<0 else 0
        longest=max(longest,losing)
    result.update(account_return=equity/10000-1,max_drawdown_15m=drawdown,max_losing_streak=longest,
                  sizing='每幣獨立10000起始本金，預估單筆風險1%，名目本金不超過帳戶本金。')
    return result


def run(cs,q,funding,start,end):
    higher=bt.aggregate(cs);hclose=[x['t']+4*H for x in higher]
    blocked=0;seen=set();trades=[];counts=Counter()
    for i,c in enumerate(cs):
        t=c['t']+H
        if not start<=t<end or t//H%24 not in (0,10):continue
        counts['scans']+=1
        if t<blocked:
            counts['pending_or_open']+=1;continue
        known=cs[max(0,i-149):i+1];k=bisect.bisect_right(hclose,t)
        h=higher[max(0,k-150):k]
        if len(known)<80 or len(h)<80 or sum(x['v'] for x in known[-24:])<1e7:continue
        plan=r.analyze(known,h)
        if not plan:continue
        counts['setups']+=1
        z=plan['zone'];key=(plan['direction'],z['kind'],z['low'],z['high'])
        if key in seen:counts['duplicate_zone']+=1;continue
        seen.add(key)
        candidate,reason=confirmation(q,t,plan,end)
        counts[reason]+=1
        blocked=min(t+12*H,end)
        if not candidate:continue
        d=plan['direction']
        levels=[v for _,v in r.pivots(known,'h' if d==1 else 'l',d==1)]
        # Reserve a small buffer before known resistance/support.
        levels=[v-d*.1*r.atr(known) for v in levels]
        trade,status=execute(q,candidate,plan,levels,funding,end)
        counts[status]+=1
        if trade:
            trade['signal_time']=t
            trades.append(trade);blocked=trade['exit_time']
    return dict(counts=dict(counts),metrics=metrics(trades),trades=trades)


def main():
    out=Path('confirmation_output');out.mkdir(exist_ok=True)
    result=dict(start=START,split=SPLIT,end=END,results={},errors={},
                code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    for symbol in ('XBTUSDTM','ETHUSDTM','SOLUSDTM'):
        print('Start '+symbol,flush=True)
        try:
            cs=bt.history(symbol,START-30*24*H,END,out)
            q=quarters(symbol,START-24*H,END,out)
            funding=bt.funding_history(symbol,START,END,out)
            result['results'][symbol]=dict(
                development=run(cs,q,funding,START,SPLIT),
                validation=run(cs,q,funding,SPLIT,END))
        except Exception as exc:
            result['errors'][symbol]=str(exc) if isinstance(exc,(ValueError,RuntimeError)) else type(exc).__name__
        print('Finished '+symbol,flush=True)
    (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# 15分鐘確認版：凍結規則歷史試驗',
           '規則在取資料前固定；3/1～4/30為開發段，5/1～5/31為預留驗證段，均為2026年UTC。',
           '此期間未用於上一輪6～9月試跑。沒有調參，不能把少量樣本當成有效性證明。',
           '流程：4H方向→1H區域→15m觸區→掃已確認轉折後收回→後續15m收線突破先前反向轉折→下一根開盤。',
           '含雙邊手續費0.06%／邊、滑點0.02%／邊、歷史資金費率；成本後2R需在已知阻力前。',
           '|合約|區段|成交數|淨勝率|每筆淨R|獨立帳戶報酬|15m收盤回撤|最長連虧|',
           '|---|---|---:|---:|---:|---:|---:|---:|']
    for symbol,values in result['results'].items():
        for window,data in values.items():
            a=data['metrics'];v='無樣本' if a['mean_net_r'] is None else f"{a['mean_net_r']:.3f}"
            lines.append(f"|{symbol}|{window}|{a['trades']}|{bt.pct(a['win_rate'])}|{v}|{bt.pct(a['account_return'])}|{bt.pct(a['max_drawdown_15m'])}|{a['max_losing_streak']}|")
            print(symbol,window,'counts',json.dumps(data['counts']),flush=True)
    lines+=['\n只測三個存活合約，未重建歷史Top200／全市場量／OI排名，不是完整選幣績效。',
            '每幣獨立帳戶：單筆預算風險1%，名目不超過本金，無槓桿。回撤按15m收盤淨清算估值，未捕捉全部盤中風險。',
            '資金費率用15m開盤價近似標記價，進出場K僅計費不給模糊時點收入。預算funding0.06%不保證等於實際成本。',
            '區域待確認最長12h，持倉最多48h；分段末強制平倉，兩段重新開始。訊號後已越原失效價就取消。',
            '下一步合格門檻事前設定：每幣驗證至少30筆、淨期望R>0、profit factor>1、15m回撤<10%；這只是進入模擬追蹤門檻，並非保證。',
            '本程式僅研究，不發即時買賣訊號、不更改現有排程。']
    gates=[]
    for symbol,data in result['results'].items():
        a=data['validation']['metrics'];pf=a['profit_factor']
        gates.append(a['trades']>=30 and a['mean_net_r']>0 and pf is not None and pf>1 and a['max_drawdown_15m']<.1)
    passed=len(gates)==3 and all(gates) and not result['errors']
    lines.append('\n研究門檻：'+('通過，可進入模擬追蹤（非實盤）' if passed else '未通過／樣本不足，不升級為進場指令'))
    if result['errors']:lines.append('資料錯誤：'+json.dumps(result['errors'],ensure_ascii=False))
    report='\n'.join(lines);(out/'REPORT.md').write_text(report,encoding='utf-8');print(report,flush=True)
    if result['errors']:raise SystemExit(1)


if __name__=='__main__':main()
