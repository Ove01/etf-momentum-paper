# -*- coding: utf-8 -*-
"""
ETF Momentum Paper Trader - GitHub Actions auto-run
Strategy: momentum rotation between 159915 & 513100
"""
import json, os, sys, argparse
from datetime import datetime, date
from pathlib import Path

CONFIG = {
    "initial_capital": 50_000,
    "commission": 0.0001,
    "lookback_days": 20,
    "threshold": 0.001,
    "data_dir": "paper_data",
}

def fetch_prices_akshare():
    """Try primary (NAV) then fallback (spot market price) data sources."""
    import akshare as ak
    import pandas as pd
    import time

    result = {}

    # --- Method 1: NAV from fund info (preferred, matches seed data) ---
    try:
        today = date.today().strftime("%Y%m%d")
        week_ago = (date.today() - pd.Timedelta(days=7)).strftime("%Y%m%d")
        for code, name in [("159915", "创业板ETF"), ("513100", "纳指ETF")]:
            for attempt in range(2):
                try:
                    df = ak.fund_etf_fund_info_em(fund=code, start_date=week_ago, end_date=today)
                    if df.empty: continue
                    # Try to find date and nav columns regardless of exact names
                    date_col = [c for c in df.columns if '日期' in str(c) or c == 'date'][0] if any('日期' in str(c) or c == 'date' for c in df.columns) else None
                    nav_col = [c for c in df.columns if '净值' in str(c) or c == 'nav'][0] if any('净值' in str(c) or c == 'nav' for c in df.columns) else None
                    if date_col and nav_col:
                        latest = df.sort_values(date_col).iloc[-1]
                        result[code] = {
                            "code": code, "name": name,
                            "price": float(latest[nav_col]),
                            "change_pct": 0.0,
                            "update_time": str(latest[date_col]),
                        }
                    break
                except Exception:
                    if attempt < 1: time.sleep(3)
    except Exception as e:
        print(f"  NAV method failed: {e}")

    # --- Method 2: Spot market price (fallback) ---
    if not result:
        try:
            spot = ak.fund_etf_spot_em()
            for code, name in [("159915", "创业板ETF"), ("513100", "纳指ETF")]:
                row = spot[spot["代码"] == code]
                if not row.empty:
                    result[code] = {
                        "code": code, "name": name,
                        "price": float(row.iloc[0]["最新价"]),
                        "change_pct": float(row.iloc[0].get("涨跌幅", 0)),
                        "update_time": date.today().isoformat(),
                    }
        except Exception as e:
            print(f"  Spot method also failed: {e}")

    if not result:
        raise RuntimeError("All data sources failed")

    return result

class PaperTrader:
    def __init__(self, config):
        self.cfg = config
        self.data_dir = Path(config["data_dir"])
        self.data_dir.mkdir(exist_ok=True)
        self.state_file = self.data_dir / "state.json"
        self.history_file = self.data_dir / "price_history.csv"
        self.trade_log_file = self.data_dir / "trade_log.csv"
        self.daily_file = self.data_dir / "daily_equity.csv"
        self.state = self._load_state()

    def _load_state(self):
        if self.state_file.exists():
            with open(self.state_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {
            "cash": self.cfg["initial_capital"],
            "position": None, "total_trades": 0,
            "start_date": date.today().isoformat(),
            "last_trade_date": None, "last_signal": None,
        }

    def _save_state(self):
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2, default=str)

    def _save_price(self, prices):
        import pandas as pd
        today = date.today().isoformat()
        if self.history_file.exists():
            existing = pd.read_csv(self.history_file)
            if today in existing["date"].values: return
        row = {"date": today}
        for code, info in prices.items():
            row[f"{code}_price"] = info["price"]
            row[f"{code}_change"] = info["change_pct"]
        pd.DataFrame([row]).to_csv(self.history_file, mode='a', header=not self.history_file.exists(), index=False)

    def _log_trade(self, action, code, name, price, shares, reason):
        import pandas as pd
        trade = {
            "date": date.today().isoformat(), "time": datetime.now().strftime("%H:%M:%S"),
            "action": action, "code": code, "name": name,
            "price": price, "shares": shares,
            "amount": round(price * shares, 2), "reason": reason,
        }
        pd.DataFrame([trade]).to_csv(self.trade_log_file, mode='a', header=not self.trade_log_file.exists(), index=False)

    def seed_history(self, days=60):
        import pandas as pd, akshare as ak
        from datetime import timedelta
        end_date = date.today().strftime("%Y%m%d")
        start_date = (date.today() - timedelta(days=days + 30)).strftime("%Y%m%d")
        price_rows = {}
        for code in ["159915", "513100"]:
            print(f"  Seeding {code}...")
            try:
                df = ak.fund_etf_fund_info_em(fund=code, start_date=start_date, end_date=end_date)
            except Exception as e:
                print(f"    WARNING: {e}")
                continue
            df = df.rename(columns={"净值日期": "date", "累计净值": "nav"})
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            for _, row in df.iterrows():
                d = row["date"]
                if d not in price_rows: price_rows[d] = {"date": d}
                price_rows[d][f"{code}_price"] = round(float(row["nav"]), 4)
                price_rows[d][f"{code}_change"] = 0
        if not price_rows:
            print("  Seed failed")
            return
        df_out = pd.DataFrame(list(price_rows.values())).sort_values("date")
        df_out.to_csv(self.history_file, index=False)
        print(f"  Seeded {len(df_out)} days.")

    def compute_signal(self, prices):
        import pandas as pd
        lb = self.cfg["lookback_days"]
        if not self.history_file.exists():
            return {"signal": "cash", "reason": f"No history yet"}
        hist = pd.read_csv(self.history_file)
        if len(hist) < lb:
            return {"signal": "cash", "reason": f"Need {lb} days, have {len(hist)}"}
        past_row = hist.iloc[-lb]
        moms = {}
        for code in ["159915", "513100"]:
            col = f"{code}_price"
            if col not in hist.columns: continue
            past_price = past_row[col]
            current_price = prices[code]["price"]
            if past_price and past_price > 0:
                moms[code] = (current_price - past_price) / past_price
        if not moms:
            return {"signal": "cash", "reason": "Cannot compute momentum"}
        cyb_mom = moms.get("159915", -999)
        nq_mom = moms.get("513100", -999)
        detail = {"cyb_mom": round(cyb_mom * 100, 2), "nq_mom": round(nq_mom * 100, 2)}

        # Rule: cyb > nq AND > threshold -> cyb
        if cyb_mom >= nq_mom and cyb_mom > self.cfg["threshold"]:
            return {"signal": "cyb", "reason": f'cyb({detail["cyb_mom"]}%) > nq({detail["nq_mom"]}%) -> 159915', "detail": detail}
        # Rule: nq > cyb AND > threshold -> nq
        if nq_mom > cyb_mom and nq_mom > self.cfg["threshold"]:
            return {"signal": "nq", "reason": f'nq({detail["nq_mom"]}%) > cyb({detail["cyb_mom"]}%) -> 513100', "detail": detail}
        return {"signal": "cash", "reason": f'Both below threshold: cyb={detail["cyb_mom"]}% nq={detail["nq_mom"]}%', "detail": detail}

    def execute(self, signal_info, prices):
        signal = signal_info["signal"]
        reason = signal_info["reason"]
        pos = self.state["position"]
        cash = self.state["cash"]
        code_map = {"cyb": ("159915", "创业板ETF"), "nq": ("513100", "纳指ETF")}
        target = code_map.get(signal)
        target_code = target[0] if target else None
        target_name = target[1] if target else None
        current_code = pos["code"] if pos else None
        trades_today = []
        if current_code == target_code and target_code is not None:
            return {"action": "hold", "position": pos, "cash": cash, "reason": f"Hold {target_name}", "trades": []}
        cr = self.cfg["commission"]
        if pos and pos["shares"] > 0:
            px = prices[pos["code"]]["price"]
            cash += pos["shares"] * px * (1 - cr)
            self._log_trade("SELL", pos["code"], pos["name"], px, pos["shares"], reason)
            trades_today.append(f"SELL {pos['name']} {pos['shares']}shares")
            self.state["total_trades"] += 1
            self.state["position"] = None
        if target_code:
            px = prices[target_code]["price"]
            shares = int(cash * (1 - cr) / px / 100) * 100
            if shares > 0:
                cost = shares * px * (1 + cr)
                cash -= cost
                self.state["position"] = {
                    "code": target_code, "name": target_name,
                    "shares": shares, "cost": px,
                    "buy_date": date.today().isoformat(),
                }
                self._log_trade("BUY", target_code, target_name, px, shares, reason)
                trades_today.append(f"BUY {target_name} {shares}shares")
                self.state["total_trades"] += 1
        self.state["cash"] = cash
        self.state["last_trade_date"] = date.today().isoformat()
        self.state["last_signal"] = signal
        return {"action": "trade" if trades_today else "hold", "position": self.state["position"], "cash": cash, "reason": reason, "trades": trades_today}

    def compute_equity(self, prices):
        cash = self.state["cash"]
        pos = self.state["position"]
        pv = pos["shares"] * prices[pos["code"]]["price"] if pos else 0
        total = cash + pv
        return {"cash": round(cash, 2), "position_value": round(pv, 2), "total_equity": round(total, 2), "total_return_pct": round((total / self.cfg["initial_capital"] - 1) * 100, 2)}

    def daily_update(self, prices):
        eq = self.compute_equity(prices)
        import pandas as pd
        row = {"date": date.today().isoformat(), "equity": eq["total_equity"], "cash": eq["cash"], "position": self.state["position"]["code"] if self.state["position"] else "", "return_pct": eq["total_return_pct"]}
        pd.DataFrame([row]).to_csv(self.daily_file, mode='a', header=not self.daily_file.exists(), index=False)

    def run(self):
        print(f"ETF Momentum Paper Trader | {datetime.now()}")
        prices = None
        try:
            prices = fetch_prices_akshare()
        except Exception as e:
            print(f"ERROR fetching prices: {e}")

        # Fallback: if data fetch failed, use last known prices from history
        if not prices:
            print("  Using fallback: last known prices from history...")
            try:
                import pandas as pd
                if self.history_file.exists():
                    hist = pd.read_csv(self.history_file)
                    last = hist.iloc[-1]
                    prices = {}
                    for code in ["159915", "513100"]:
                        col = f"{code}_price"
                        if col in hist.columns:
                            name = "创业板ETF" if code == "159915" else "纳指ETF"
                            prices[code] = {
                                "code": code, "name": name,
                                "price": float(last[col]),
                                "change_pct": 0.0,
                                "update_time": str(last["date"]),
                            }
                    print(f"  Fallback loaded: {list(prices.keys())}")
            except Exception as fe:
                print(f"  Fallback also failed: {fe}")

        if not prices:
            print("FATAL: No price data available. Skipping today.")
            return None
        for code, info in prices.items():
            print(f"  {info['name']}({code}): {info['price']:.4f}")
        self._save_price(prices)
        signal_info = self.compute_signal(prices)
        print(f"Signal: {signal_info['signal'].upper()} | {signal_info['reason']}")
        result = self.execute(signal_info, prices)
        print(f"Action: {result['action']}")
        eq = self.compute_equity(prices)
        print(f"Equity: RMB {eq['total_equity']:,.2f} ({eq['total_return_pct']:+.2f}%)")
        self._save_state()
        self.daily_update(prices)
        print(f"Trades: {self.state['total_trades']}")
        return {"prices": prices, "signal": signal_info, "result": result, "equity": eq}

    def reset(self):
        for f in [self.state_file, self.history_file, self.trade_log_file, self.daily_file]:
            if f.exists(): f.unlink()
        self.state = self._load_state()
        print("Reset done.")

def generate_report(trader, result):
    if not result: return
    eq = result["equity"]
    prices = result["prices"]
    sig = result["signal"]
    pos = trader.state["position"]
    today = date.today().isoformat()
    lines = [f"# Daily Report - {today}", "", "## Market", ""]
    for code, info in prices.items():
        lines.append(f"- {info['name']}({code}): {info['price']:.4f}")
    lines += ["", "## Signal", f"- **{sig['signal'].upper()}**: {sig['reason']}", "", "## Account", f"- Cash: RMB {eq['cash']:,.2f}", f"- Position: RMB {eq['position_value']:,.2f}", f"- **Total: RMB {eq['total_equity']:,.2f}**", f"- Return: {eq['total_return_pct']:+.2f}%", ""]
    if pos:
        lines.append(f"Holding: {pos['name']}({pos['code']}) x{pos['shares']}")
    else:
        lines.append("Holding: Cash")
    report = "\n".join(lines)
    report_file = trader.data_dir / f"report_{today}.md"
    with open(report_file, 'w', encoding='utf-8') as f:
        f.write(report)
    return report

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()
    trader = PaperTrader(CONFIG)
    if args.reset: trader.reset()
    if args.seed > 0: trader.seed_history(days=args.seed)
    if args.report:
        try:
            prices = fetch_prices_akshare()
            eq = trader.compute_equity(prices)
            print(f"Equity: RMB {eq['total_equity']:,.2f} ({eq['total_return_pct']:+.2f}%)")
        except Exception as e:
            print(f"Error: {e}")
        sys.exit(0)
    result = trader.run()
    if result: generate_report(trader, result)
