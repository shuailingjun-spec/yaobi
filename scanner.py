"""
抓妖币扫描器 — GitHub Actions 版
条件: ①只有合约无现货 ②市值20M-30M ③OI/MC比值高
"""
import requests
import time
import csv
import os
from datetime import datetime, timezone

# ======== 可调参数 ========
MCAP_MIN = 15_000_000
MCAP_MAX = 35_000_000
OI_MC_RATIO_MIN = 0.10
# ==========================

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; YaobiScanner/1.0)"}

def fetch_json(url, retries=3):
    for i in range(retries + 1):
        try:
            r = requests.get(url, timeout=30, headers=HEADERS)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"  ⚠️ [{i+1}/{retries+1}] {url[:60]}: {e}")
            if i < retries:
                time.sleep(5)
    return None

def fetch_json_mirrors(mirrors, retries=3):
    """尝试多个 API 镜像，返回第一个成功的"""
    for url in mirrors:
        data = fetch_json(url, retries=retries)
        if data:
            return data
    return None

FAPI_MIRRORS = [
    "https://fapi.binance.com/fapi/v1/exchangeInfo",
    "https://fapi.binance.cloud/fapi/v1/exchangeInfo",
]
API_MIRRORS = [
    "https://api.binance.com/api/v3/exchangeInfo",
    "https://api1.binance.com/api/v3/exchangeInfo",
    "https://api2.binance.com/api/v3/exchangeInfo",
    "https://api3.binance.com/api/v3/exchangeInfo",
    "https://api.binance.us/api/v3/exchangeInfo",
]

def get_futures_only():
    """差集: 合约 - 现货"""
    print("[1/4] 获取币安合约+现货交易对...")
    f_data = fetch_json_mirrors(FAPI_MIRRORS)
    s_data = fetch_json_mirrors(API_MIRRORS)
    if not f_data or not s_data:
        raise SystemExit("无法连接币安API — 所有镜像均超时，可能 GitHub IP 被屏蔽")

    futures = set()
    for s in f_data.get("symbols", []):
        if s["symbol"].endswith("USDT") and s["contractType"] == "PERPETUAL" and s["status"] == "TRADING":
            futures.add(s["symbol"].replace("USDT", ""))

    spots = set()
    for s in s_data.get("symbols", []):
        if s["quoteAsset"] == "USDT" and s["status"] == "TRADING":
            spots.add(s["baseAsset"])

    result = sorted(futures - spots)
    print(f"  合约: {len(futures)} | 现货: {len(spots)} | 合约独有: {len(result)}")
    return result

def get_market_caps(symbols):
    """CoinGecko 批量查市值"""
    print(f"[2/4] CoinGecko 查市值 ({len(symbols)} 个)...")
    # 先搜 CoinGecko ID
    symbol_to_id = {}
    for sym in symbols:
        # 清洗代币名 (去1000/1000000前缀)
        clean = sym.replace("1000", "").replace("1000000", "")
        try:
            data = fetch_json(f"https://api.coingecko.com/api/v3/search?query={clean}")
            if data and data.get("coins"):
                symbol_to_id[sym] = data["coins"][0]["id"]
        except:
            pass
        time.sleep(1.2)  # CoinGecko 免费层限速

    print(f"  匹配到 {len(symbol_to_id)} 个代币")

    # 批量取市值
    results = {}
    ids = list(symbol_to_id.values())
    for i in range(0, len(ids), 200):
        batch = ids[i:i+200]
        id_str = ",".join(batch)
        data = fetch_json(
            f"https://api.coingecko.com/api/v3/coins/markets"
            f"?vs_currency=usd&ids={id_str}&order=market_cap_asc"
            f"&per_page=250&sparkline=false"
        )
        if data:
            for c in data:
                sym_upper = c["symbol"].upper()
                results[sym_upper] = {
                    "name": c["name"],
                    "market_cap": c.get("market_cap", 0) or 0,
                    "price": c.get("current_price", 0) or 0,
                    "volume_24h": c.get("total_volume", 0) or 0,
                    "price_chg_24h": c.get("price_change_percentage_24h"),
                }
        time.sleep(1.5)

    print(f"  获取到 {len(results)} 个市值")
    return results

OI_MIRRORS = [
    "https://fapi.binance.com/fapi/v1/openInterest",
    "https://fapi.binance.cloud/fapi/v1/openInterest",
]
FR_MIRRORS = [
    "https://fapi.binance.com/fapi/v1/premiumIndex",
    "https://fapi.binance.cloud/fapi/v1/premiumIndex",
]

def get_oi_and_funding(symbols):
    """取币安合约 OI + 资金费率"""
    print(f"[3/4] 获取 OI + 资金费率...")
    oi_data = fetch_json_mirrors(OI_MIRRORS)
    fr_data = fetch_json_mirrors(FR_MIRRORS)

    oi_map = {}
    fr_map = {}
    target = set(symbols)

    if oi_data:
        for item in oi_data:
            sym = item["symbol"].replace("USDT", "")
            if sym in target:
                oi_map[sym] = float(item["openInterest"])
    if fr_data:
        for item in fr_data:
            sym = item["symbol"].replace("USDT", "")
            if sym in target:
                fr_map[sym] = float(item["lastFundingRate"])

    print(f"  OI: {len(oi_map)} | 费率: {len(fr_map)}")
    return oi_map, fr_map

def main():
    print("=" * 55)
    print(f"🔍 抓妖币扫描 — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"   合约独有 | MCap {MCAP_MIN/1e6:.0f}M-{MCAP_MAX/1e6:.0f}M | OI/MC>{OI_MC_RATIO_MIN*100:.0f}%")
    print("=" * 55)

    futures_only = get_futures_only()
    market_caps = get_market_caps(futures_only)
    oi_map, fr_map = get_oi_and_funding(futures_only)

    # 交叉比对
    print(f"\n[4/4] 筛选结果...")
    candidates = []
    for sym in futures_only:
        mc = market_caps.get(sym, {})
        mcap = mc.get("market_cap", 0)
        oi_usd = oi_map.get(sym, 0)
        price = mc.get("price", 0)
        oi_mc = oi_usd * price / mcap if mcap > 0 and price > 0 else 0

        if mcap > 0:
            candidates.append({
                "symbol": sym,
                "name": mc.get("name", ""),
                "market_cap": mcap,
                "price": price,
                "volume_24h": mc.get("volume_24h", 0),
                "price_chg_24h": mc.get("price_chg_24h"),
                "oi_usd": oi_usd * price,
                "oi_mc_ratio": oi_mc,
                "funding_rate": fr_map.get(sym),
            })

    # 排序: OI/MC 降序
    candidates.sort(key=lambda x: x["oi_mc_ratio"], reverse=True)

    # 保存完整 CSV
    csv_path = "report.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "symbol", "name", "market_cap", "price", "volume_24h",
            "price_chg_24h", "oi_usd", "oi_mc_ratio", "funding_rate"
        ])
        writer.writeheader()
        for c in candidates:
            writer.writerow(c)
    print(f"  ✅ 完整报告: {csv_path} ({len(candidates)} 条)")

    # 筛选核心候选
    hits = [c for c in candidates
            if MCAP_MIN <= c["market_cap"] <= MCAP_MAX
            and c["oi_mc_ratio"] >= OI_MC_RATIO_MIN]

    print(f"\n{'='*55}")
    print(f"🎯 核心候选: {len(hits)} 个")
    print(f"{'='*55}")
    if hits:
        print(f"{'代币':<10} {'名称':<18} {'市值':>10} {'价格':>10} {'OI(USD)':>12} {'OI/MC':>8} {'费率':>10}")
        print("-" * 80)
        for c in hits:
            fr_str = f"{c['funding_rate']*100:.4f}%" if c['funding_rate'] else "N/A"
            print(f"{c['symbol']:<10} {c['name']:<18} {c['market_cap']:>10,.0f} "
                  f"${c['price']:<10.6f} {c['oi_usd']:>12,.0f} "
                  f"{c['oi_mc_ratio']:>7.1%} {fr_str:>10}")
    else:
        print("  无完全匹配，看 Top 10 近似:")
        for c in candidates[:10]:
            fr_str = f"{c['funding_rate']*100:.4f}%" if c['funding_rate'] else "N/A"
            print(f"  {c['symbol']:<10} MC:{c['market_cap']:>12,.0f}  OI/MC:{c['oi_mc_ratio']:>7.1%}  FR:{fr_str}")

if __name__ == "__main__":
    main()
