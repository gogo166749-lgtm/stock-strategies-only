import unittest
from unittest.mock import patch
import crypto_backtest as b


def bar(t=0,o=100,h=101,l=99,c=100):
    return dict(t=t*b.H,o=o,h=h,l=l,c=c,v=1e7)


class BacktestTests(unittest.TestCase):
    def test_no_incomplete_4h(self):
        self.assertEqual(len(b.aggregate([bar(i) for i in range(7)])),1)

    def test_stop_before_target_and_gap(self):
        self.assertEqual(b.settle_bar(bar(h=120,l=80),1,90,110),(90,'stop_ambiguous'))
        self.assertEqual(b.settle_bar(bar(o=85,h=95,l=80),1,90,110),(85,'stop_gap'))
        self.assertEqual(b.settle_bar(bar(h=120,l=80),-1,110,90),(110,'stop_ambiguous'))

    def test_signal_bar_cannot_fill(self):
        cs=[bar(0,h=110,l=90)]+[bar(i,o=110,h=111,l=109,c=110) for i in range(1,15)]
        trade,_=b.execute(cs,0,dict(direction=1,entry=100,stop=95,target=110),[])
        self.assertIsNone(trade)

    def test_entry_bar_target_not_claimed(self):
        cs=[bar(0),bar(1,h=120,l=99,c=100),bar(2,h=101,l=94,c=96)]
        trade,_=b.execute(cs,0,dict(direction=1,entry=100,stop=95,target=110),[],fee=0,slip=0)
        self.assertEqual(trade['reason'],'stop')
        self.assertAlmostEqual(trade['net_return'],-.05)

    def test_fees_and_funding_lower_pnl(self):
        cs=[bar(0),bar(1),bar(2),bar(3,h=111,c=110)]
        plan=dict(direction=1,entry=100,stop=95,target=110)
        clean,_=b.execute(cs,0,plan,[],fee=0,slip=0)
        cost,_=b.execute(cs,0,plan,[(2*b.H,.001)],fee=.0006,slip=.0002)
        self.assertLess(cost['net_return'],clean['net_return'])
        self.assertGreater(cost['funding_cost'],0)

    def test_no_trade_is_not_zero_winrate(self):
        self.assertIsNone(b.stats([])['win_rate'])

    def test_future_4h_excluded(self):
        cs=[bar(i) for i in range(24*35)]
        seen=[]
        def spy(known,higher):
            seen.append(len(known))
            self.assertLessEqual(higher[-1]['t']+4*b.H,known[-1]['t']+b.H)
            return None
        with patch.object(b.radar,'analyze',side_effect=spy):
            b.run_symbol(cs,[],30*24*b.H,35*24*b.H)
        self.assertTrue(seen)


if __name__=='__main__':unittest.main()
