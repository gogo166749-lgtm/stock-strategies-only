import unittest
from unittest.mock import patch
import crypto_confirmation as m

def bar(i,o=100,h=101,l=99,c=100):
    return dict(t=i*m.Q,o=o,h=h,l=l,c=c,v=1e7)

class ConfirmationTests(unittest.TestCase):
    def fixture(self):
        q=[bar(i) for i in range(24)]
        q[20]=bar(20,100,101,97,100)
        q[21]=bar(21,100,103,99,102.5)
        q[22]=bar(22,103,104,102,103)
        plan=dict(direction=1,stop=95,zone=dict(low=98,high=100))
        return q,plan
    def pivots(self,cs,key,high=True):
        self.assertLessEqual(cs[-1]['t'],20*m.Q)
        return [(5,98 if key=='l' else 102)]
    def test_confirm_after_close_next_open(self):
        q,p=self.fixture()
        with patch.object(m.r,'pivots',side_effect=self.pivots),patch.object(m.r,'atr',return_value=2):
            result,reason=m.confirmation(q,20*m.Q,p)
        self.assertEqual(reason,'confirmed')
        self.assertEqual(result['entry_i'],22)
        self.assertEqual(result['confirmed_at'],22*m.Q)
        self.assertAlmostEqual(result['stop'],96.8)
    def test_wick_does_not_confirm(self):
        q,p=self.fixture()
        for i in range(21,24):q[i]=bar(i,100,103,99,100)
        with patch.object(m.r,'pivots',side_effect=self.pivots),patch.object(m.r,'atr',return_value=2):
            result,_=m.confirmation(q,20*m.Q,p)
        self.assertIsNone(result)
    def test_cutoff_prevents_confirmation_leakage(self):
        q,p=self.fixture()
        with patch.object(m.r,'pivots',side_effect=self.pivots),patch.object(m.r,'atr',return_value=2):
            result,_=m.confirmation(q,20*m.Q,p,22*m.Q)
        self.assertIsNone(result)
    def test_zone_invalidated(self):
        q,p=self.fixture();q[20]['l']=94
        self.assertEqual(m.confirmation(q,20*m.Q,p)[1],'zone_invalidated')
    def test_new_low_cancels(self):
        q,p=self.fixture();q[21]['l']=96
        with patch.object(m.r,'pivots',side_effect=self.pivots),patch.object(m.r,'atr',return_value=2):
            self.assertEqual(m.confirmation(q,20*m.Q,p)[1],'sweep_failed')
    def test_cost_target_both_directions(self):
        for d in (1,-1):
            target,loss=m.cost_target(100,100-d*2,d)
            fill=target*(1-d*m.SLIP)
            net=d*(fill-100)/100-m.FEE*(1+fill/100)-m.FUNDING_BUDGET
            self.assertAlmostEqual(net,2*loss)
    def test_open_entry_and_stop_first(self):
        q=[bar(0,100,120,90,101)]
        conf=dict(entry_i=0,stop=98,confirmed_at=0)
        trade,_=m.execute(q,conf,dict(direction=1),[130],[],m.Q)
        self.assertAlmostEqual(trade['entry'],100*(1+m.SLIP))
        self.assertEqual(trade['reason'],'stop_ambiguous')
    def test_resistance_blocks_trade(self):
        q=[bar(0)]
        trade,reason=m.execute(q,dict(entry_i=0,stop=98),dict(direction=1),[101],[],m.Q)
        self.assertIsNone(trade)
        self.assertEqual(reason,'insufficient_room_after_costs')
    def test_equity_tracks_unrealized_drawdown(self):
        trade=dict(entry_time=0,net_return=.02,net_r=1,risk_fraction=.02,
                   marks=[(1,-.04),(2,.02)])
        stats=m.metrics([trade])
        self.assertAlmostEqual(stats['account_return'],.01)
        self.assertAlmostEqual(stats['max_drawdown_15m'],.02)
    def test_empty_metrics(self):
        self.assertIsNone(m.metrics([])['win_rate'])
        self.assertEqual(m.metrics([])['account_return'],0)

if __name__=='__main__':unittest.main()
