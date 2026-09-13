# 加密貨幣多空雷達

獨立的 `crypto_radar.py` 和 `Crypto Radar` workflow，不修改台股選股或試算表。
Python 3.11 標準函式庫即可執行，不需要交易所帳戶或交易金鑰。

## 啟用

將本次新增檔案合併至 GitHub 預設分支 main。
沿用已設定的 TELEGRAM_BOT_TOKEN、TELEGRAM_CHAT_ID。
若 CoinGecko 公開 API 限流，可新增 COINGECKO_DEMO_API_KEY Secret。
到 Actions → Crypto Radar → Run workflow 測試。
成功後每日（含週末）台灣時間 08:00、18:00 排程，GitHub 可能延遲。
這是定時掃描，不是全天即時監控，不自動下單。

## 方法與實際覆蓋

每次從 CoinGecko 重新抓取市值前 200 名，不使用固定名單。
對應 KuCoin 上線中的 USDT 線性永續合約；排除重複代號，並檢查跨來源價格差。
符號與價格交叉檢查仍不等同合約地址身分驗證，交易前應確認資產身分。
無對應合約不分析，報告明列 200 名、對應數、完成分析數與排除統計。
全市場成交額至少 2,000 萬美元；KuCoin 最近 24 根已收 1H K 線成交額至少 1,000 萬 USDT。
依 KuCoin K 線第七欄成交額計算 USDT 成交額；OI 用合約張數 × 乘數 × 標記價換算。
僅採用結束時間已過至少 10 秒的完整 1H 和 4H K 線，最少 80 根，拒絕缺漏、重複、過期資料。

## 規則定義（操作性定義，不宣稱 SMC 有唯一標準）

- 結構高低點：左右各兩根確認，最後兩根不能當已確認轉折。
- 4H 最近兩組高低點同時上升／下降，分別判偏多／偏空。
- 1H 收盤穿越已確認轉折才算突破，影線不算。順原有結構稱 BOS，反向稱 CHOCH；原趨勢不明只稱結構突破。
- FVG：三根 K 線第一根與第三根影線間存在缺口，中間 K 線同向；形成後已碰觸的缺口排除。
- OB：結構突破 K 實體至少 ATR14，往前六根最近一根反向 K 的全根範圍；突破後碰觸即排除。
- 流動性掃蕩：最後一根越過前 20 根極值後收回。這是區間掃蕩代理，不宣稱捕捉所有流動性。
- 價格需在未回補區域外 1.5 ATR 以內。24H 絕對變動 >10%、量比 >3 或最後 K 實體 >2 ATR 則排除。
- 使用區域中點估算進場、區外 0.25 ATR 估失效、2R 估目標。若最近反向轉折阻擋 2R，排除。
- 觸區不等於進場；需低週期收線確認並按實際價格重新計算。程式沒有即時追蹤這個確認。
- 分數只用於排序，不是概率或已驗證勝率；未做歷史績效回測，未計手續費、滑點和資金費率成本。
- 最多五檔；兩方向都有合格候選時各至少一檔，缺某方向或不足五檔就明列，不強湊。
- 資金費率顯示當期值，按可取得的週期間隔正規化後，對順向過度擁擠扣分。
- 持倉量只顯示 KuCoin 美元快照，不推論多空或增減；未實作歷史 OI 變化。

## 驗證與輸出

`python -m unittest discover -s crypto_tests -v`

`python crypto_radar.py`：只取公開市場資料，不發送。

`python crypto_radar.py --send`：發到已設定的 Telegram。

每次輸出 crypto_output/latest.json 與 latest.txt，Actions 保留報告 30 天。
資料錯誤時退出失敗，通知「未產生交易訊號」；不沿用舊報告。
未實作跨日績效回顧、即時觸價通知或網頁介面。

資料文件：
- https://docs.coingecko.com/reference/coins-markets
- https://www.kucoin.com/docs-new/rest/futures-trading/market-data/get-klines

KuCoin 持倉量及費率來自本次請求快照，API 未提供該欄位更新時間，不能獨立驗證新鮮度。BTC 對應 XBT；重複基礎代號排除。
