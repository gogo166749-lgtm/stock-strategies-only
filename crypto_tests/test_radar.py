import unittest
from unittest.mock import patch
import crypto_radar as r


def series(n=100):
    return [dict(t=i*r.HOUR, o=100., h=102., l=98., c=100., v=100.) for i in range(n)]


class RadarTests(unittest.TestCase):
    def test_unclosed_excluded(self):
        rows = [[str(i*r.HOUR),'100','102','98','100','1','1','100','1'] for i in range(100)]
        rows.append([str(100*r.HOUR),'100','1000','1','1000','1','1','100','0'])
        self.assertEqual(len(r.candles(rows,1,100*r.HOUR+100)),100)

    def test_gap_and_stale_rejected(self):
        rows = [[str(i*r.HOUR),'100','102','98','100','1','1','100','1'] for i in range(100)]
        with self.assertRaises(ValueError):
            r.candles(rows,1,103*r.HOUR)
        with self.assertRaises(ValueError):
            r.candles(rows[:40]+rows[41:],1,100*r.HOUR)

    def test_confirmed_pivot_only(self):
        c=series(8)
        c[3]['h']=110
        c[-1]['h']=120
        self.assertEqual(r.pivots(c,'h'),[(3,110)])

    def test_wick_is_not_break(self):
        c=series(20)
        c[6]['h']=105
        c[-1].update(h=106,c=104)
        self.assertEqual(r.structure(c),[])
        c[-1]['c']=105.5
        self.assertEqual(r.structure(c)[-1]['direction'],1)

    def test_filled_gap_removed(self):
        c=series(6)
        c[1].update(o=100,c=105,h=106)
        c[2].update(l=104,h=108,c=106,o=105)
        c[3].update(l=105,h=109,c=107,o=106)
        c[4].update(l=101,h=108,c=103,o=106)
        self.assertFalse(any(z['i']==2 for z in r.zones(c,[],1)))

    def test_ob_requires_displacement(self):
        c=series(20)
        c[-2].update(o=101,c=99)
        self.assertFalse(r.zones(c,[dict(i=19,direction=1)],1))

    def test_long_short_geometry(self):
        for d in (1,-1):
            c=series()
            z=dict(low=96.,high=98.,i=95,kind='test') if d==1 else dict(low=102.,high=104.,i=95,kind='test')
            with patch.object(r,'trend',return_value=d), patch.object(r,'zones',return_value=[z]):
                p=r.analyze(c,c)
            self.assertIsNotNone(p)
            self.assertGreater(d*(p['entry']-p['stop']),0)
            self.assertAlmostEqual(d*(p['target']-p['entry']),2*abs(p['entry']-p['stop']))

    def test_overheated_rejected(self):
        c=series()
        c[-1]['v']=1000
        self.assertIsNone(r.analyze(c,c))

    def test_both_directions_and_no_fabrication(self):
        candidates=[dict(direction=1,score=90-i,rank=i) for i in range(7)]
        candidates.append(dict(direction=-1,score=40,rank=100))
        selected=r.pick(candidates)
        self.assertEqual(len(selected),5)
        self.assertIn(-1,[x['direction'] for x in selected])
        self.assertEqual(r.pick([]),[])

    def test_kucoin_closed_candles_and_quote_volume(self):
        rows=[[i*r.HOUR,100,102,98,100,999,12345] for i in range(101)]
        with patch.object(r,'kucoin',return_value=rows):
            result=r.kucoin_candles('XBTUSDTM',1,100*r.HOUR+20000)
        self.assertEqual(len(result),100)
        self.assertEqual(result[-1]['v'],12345)
        self.assertEqual(result[-1]['c'],100)

    def test_invalid_numeric(self):
        for x in ('nan','inf','-inf'):
            with self.assertRaises(ValueError): r.num(x)


if __name__=='__main__': unittest.main()
