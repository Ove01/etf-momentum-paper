# -*- coding: utf-8 -*-
"""
ETF 动量轮动 — 浏览器自动化模拟盘
====================================
每天 14:50 自动运行：
  1. 通过 akshare/浏览器 抓取 ETF 实时行情
  2. 结合历史数据计算动量信号
  3. 模拟交易（纯虚拟，不涉及真实账户）
  4. 更新持仓和权益
  5. 生成 Markdown 日报

使用方法：
  python paper_trader.py           # 手动运行一次
  python paper_trader.py --reset   # 重置模拟盘
  python paper_trader.py --report  # 仅查看当前状态
"""
import json
import os
import sys
import argparse
from datetime import datetime, date
from pathlib import Path

# =============================================
# 配置
# =============================================
CONFIG = {
    "initial_capital": 100_000,
    "commission": 0.0001,          # 万1
    "lookback_days": 20,
    "threshold": 0.001,            # 0.1%
    "data_dir": "paper_data",
    "hist_files": {                 # AKShare 下载的历史数据
        "159915": "data_cyb.csv",
        "513100": "data_nq.csv",
        "000001": "data_sz.csv",
    },
}

# =============================================
# 数据抓取
# =============================================

def fetch_prices_akshare():
    """通过 akshare 抓取 ETF 最新净值（与 seed 数据口径一致）"""
    import akshare as ak
    import pandas as pd

    today = date.today().strftime("%Y%m%d")
    # 往前多取几天，防止今天还没更新净值
    week_ago = (date.today() - pd.Timedelta(days=7)).strftime("%Y%m%d")

    result = {}
    etf_map = {"159915": "创业板ETF", "513100": "纳指ETF"}

    for code, name in etf_map.items():
        try:
            df = ak.fund_etf_fund_info_em(fund=code, start_date=week_ago, end_date=today)
            if df.empty:
                continue
            df = df.rename(columns={"净值日期": "date", "累计净值": "nav"})
            latest = df.sort_values("date").iloc[-1]
            result[code] = {
                "code": code,
                "name": name,
                "price": float(latest["nav"]),
                "change_pct": 0.0,  # NAV 没有实时涨跌幅
                "update_time": str(latest["date"]),
            }
        except Exception as e:
            print(f"  WARNING: fetch {code} failed: {e}")

    return result


# =============================================
# 模拟交易引擎
# =============================================

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
            "position": None,
            "total_trades": 0,
            "start_date": date.today().isoformat(),
            "last_trade_date": None,
            "last_signal": None,
            "equity_history": [],
        }

    def _save_state(self):
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2, default=str)

    def _save_price(self, prices):
        import pandas as pd
        # 如果今天已经存过了，跳过
        today = date.today().isoformat()
        if self.history_file.exists():
            existing = pd.read_csv(self.history_file)
            if today in existing["date"].values:
                return

        row = {"date": today}
        for code, info in prices.items():
            row[f"{code}_price"] = info["price"]
            row[f"{code}_change"] = info["change_pct"]
        pd.DataFrame([row]).to_csv(
            self.history_file,
            mode='a',
            header=not self.history_file.exists(),
            index=False
        )

    def _log_trade(self, action, code, name, price, shares, reason):
        import pandas as pd
        trade = {
            "date": date.today().isoformat(),
            "time": datetime.now().strftime("%H:%M:%S"),
            "action": action,
            "code": code,
            "name": name,
            "price": price,
            "shares": shares,
            "amount": round(price * shares, 2),
            "reason": reason,
        }
        pd.DataFrame([trade]).to_csv(
            self.trade_log_file,
            mode='a',
            header=not self.trade_log_file.exists(),
            index=False
        )

    def seed_history(self, days=60):
        """用 akshare 下载基金净值数据预填充，确保历史与实时数据口径一致"""
        import pandas as pd
        import akshare as ak
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
                if d not in price_rows:
                    price_rows[d] = {"date": d}
                price_rows[d][f"{code}_price"] = round(float(row["nav"]), 4)
                price_rows[d][f"{code}_change"] = 0

        if not price_rows:
            print("  Seed failed: no data retrieved")
            return

        df_out = pd.DataFrame(list(price_rows.values())).sort_values("date")
        df_out.to_csv(self.history_file, index=False)
        print(f"  Seeded {len(df_out)} days of NAV price history.")

    def compute_signal(self, prices):
        """计算动量信号：用 paper_data/price_history.csv 中的历史价格"""
        import pandas as pd

        lb = self.cfg["lookback_days"]

        # 使用模拟盘累积的每日价格历史
        if not self.history_file.exists():
            return {"signal": "cash", "reason": f"价格历史为空，首日空仓。运行 {lb} 天后可计算信号。"}

        hist = pd.read_csv(self.history_file)
        if len(hist) < lb:
            return {"signal": "cash", "reason": f"历史不足 ({len(hist)}天 < {lb}天)，继续积累数据"}

        # 取最近 lb 个交易日前的那一天
        # hist 按时间顺序排列，第 -lb 行就是 lb 天前
        past_row = hist.iloc[-lb]
        # 确保不是同一行（防止 lookback=1 时出错）
        if len(hist) == lb:
            past_row = hist.iloc[0]

        moms = {}
        for code in ["159915", "513100"]:
            col = f"{code}_price"
            if col not in hist.columns:
                continue
            past_price = past_row[col]
            current_price = prices[code]["price"]
            if past_price and past_price > 0:
                moms[code] = (current_price - past_price) / past_price

        if not moms:
            return {"signal": "cash", "reason": "无法计算涨幅"}

        max_code = max(moms, key=moms.get)
        max_mom = moms[max_code]

        detail = {
            "cyb_mom": round(moms.get("159915", 0) * 100, 2),
            "nq_mom": round(moms.get("513100", 0) * 100, 2),
            "past_date": str(past_row.get("date", "?")) if "date" in hist.columns else "?",
        }

        if max_mom <= self.cfg["threshold"]:
            return {
                "signal": "cash",
                "reason": f'涨幅最大 {detail["cyb_mom"] if max_code=="159915" else detail["nq_mom"]}% <= 阈值 0.1%',
                "detail": detail,
            }

        if max_code == "159915":
            return {"signal": "cyb", "reason": f'创业板近{lb}日涨幅 {detail["cyb_mom"]}% 最大', "detail": detail}
        else:
            return {"signal": "nq", "reason": f'纳指近{lb}日涨幅 {detail["nq_mom"]}% 最大', "detail": detail}

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

        # 持仓不变
        if current_code == target_code and target_code is not None:
            return {
                "action": "hold",
                "position": pos,
                "cash": cash,
                "reason": f"继续持有{target_name}",
                "trades": [],
            }

        cr = self.cfg["commission"]

        # 卖出
        if pos and pos["shares"] > 0:
            px = prices[pos["code"]]["price"]
            cash += pos["shares"] * px * (1 - cr)
            self._log_trade("卖出", pos["code"], pos["name"], px, pos["shares"], reason)
            trades_today.append(f"卖出 {pos['name']} {pos['shares']}股 @{px:.4f}")
            self.state["total_trades"] += 1
            self.state["position"] = None

        # 买入
        if target_code:
            px = prices[target_code]["price"]
            shares = int(cash * (1 - cr) / px / 100) * 100
            if shares > 0:
                cost = shares * px * (1 + cr)
                cash -= cost
                self.state["position"] = {
                    "code": target_code,
                    "name": target_name,
                    "shares": shares,
                    "cost": px,
                    "buy_date": date.today().isoformat(),
                }
                self._log_trade("买入", target_code, target_name, px, shares, reason)
                trades_today.append(f"买入 {target_name} {shares}股 @{px:.4f}")
                self.state["total_trades"] += 1

        self.state["cash"] = cash
        self.state["last_trade_date"] = date.today().isoformat()
        self.state["last_signal"] = signal

        return {
            "action": "trade" if trades_today else "hold",
            "position": self.state["position"],
            "cash": cash,
            "reason": reason,
            "trades": trades_today,
        }

    def compute_equity(self, prices):
        cash = self.state["cash"]
        pos = self.state["position"]
        pos_value = 0
        if pos:
            pos_value = pos["shares"] * prices[pos["code"]]["price"]
        total = cash + pos_value
        return {
            "cash": round(cash, 2),
            "position_value": round(pos_value, 2),
            "total_equity": round(total, 2),
            "total_return_pct": round((total / self.cfg["initial_capital"] - 1) * 100, 2),
        }

    def daily_update(self, prices):
        eq = self.compute_equity(prices)
        import pandas as pd
        row = {
            "date": date.today().isoformat(),
            "equity": eq["total_equity"],
            "cash": eq["cash"],
            "position": self.state["position"]["code"] if self.state["position"] else "",
            "return_pct": eq["total_return_pct"],
        }
        pd.DataFrame([row]).to_csv(
            self.daily_file,
            mode='a',
            header=not self.daily_file.exists(),
            index=False
        )

    def run(self):
        print("=" * 60)
        print(f"  ETF Momentum Paper Trader | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60)

        # 1. Fetch
        print("\n[1/4] Fetching real-time prices via akshare...")
        try:
            prices = fetch_prices_akshare()
        except Exception as e:
            print(f"  ERROR: {e}")
            return None

        for code, info in prices.items():
            arrow = "UP" if info["change_pct"] > 0 else "DOWN" if info["change_pct"] < 0 else "--"
            print(f"  {info['name']}({code}): {info['price']:.4f} [{arrow} {info['change_pct']:+.2f}%]")

        # 2. Save
        self._save_price(prices)

        # 3. Signal
        print(f"\n[2/4] Computing momentum signal (lookback={self.cfg['lookback_days']}d)...")
        signal_info = self.compute_signal(prices)
        print(f"  Signal: {signal_info['signal'].upper()}")
        print(f"  Reason: {signal_info['reason']}")
        if "detail" in signal_info:
            d = signal_info["detail"]
            print(f"  CYB 20d: {d['cyb_mom']}%  |  NQ 20d: {d['nq_mom']}%")

        # 4. Execute
        print("\n[3/4] Simulating trade execution...")
        result = self.execute(signal_info, prices)
        print(f"  Action: {result['action']}")
        if result["trades"]:
            for t in result["trades"]:
                print(f"  -> {t}")
        else:
            pos = self.state["position"]
            if pos:
                print(f"  Holding: {pos['name']}({pos['code']}) x{pos['shares']} shares @cost {pos['cost']:.4f}")
            else:
                print(f"  Cash only. Waiting for next signal.")

        # 5. Equity
        print("\n[4/4] Account equity...")
        eq = self.compute_equity(prices)
        print(f"  Cash:          RMB {eq['cash']:>12,.2f}")
        print(f"  Position:      RMB {eq['position_value']:>12,.2f}")
        print(f"  Total Equity:  RMB {eq['total_equity']:>12,.2f}")
        print(f"  Total Return:  {eq['total_return_pct']:>+11.2f}%")

        self._save_state()
        self.daily_update(prices)

        print(f"\n  Total trades: {self.state['total_trades']}")
        print(f"  Started:      {self.state['start_date']}")
        print("=" * 60)
        print("  Paper trading update complete.")
        print("=" * 60)

        return {"prices": prices, "signal": signal_info, "result": result, "equity": eq}

    def reset(self):
        """重置模拟盘"""
        for f in [self.state_file, self.history_file, self.trade_log_file, self.daily_file]:
            if f.exists():
                f.unlink()
        self.state = self._load_state()
        print("Paper trading account reset. Starting fresh.")


# =============================================
# 日报
# =============================================

def generate_report(trader, result):
    if not result:
        return
    eq = result["equity"]
    prices = result["prices"]
    sig = result["signal"]
    pos = trader.state["position"]

    today = date.today().isoformat()
    lines = [
        f"# Paper Trading Daily Report - {today}",
        "",
        "## Market Prices",
        "",
        "| Asset | Code | Price | Change |",
        "|-------|------|-------|--------|",
    ]
    for code, info in prices.items():
        emoji = "+" if info["change_pct"] > 0 else "-" if info["change_pct"] < 0 else " "
        lines.append(f"| {info['name']} | {code} | {info['price']:.4f} | {emoji}{info['change_pct']}% |")

    lines += [
        "",
        "## Signal",
        f"- **Signal**: `{sig['signal'].upper()}`",
        f"- **Reason**: {sig['reason']}",
        "",
        "## Account",
        "",
        "| Item | Value |",
        "|------|-------|",
        f"| Cash | RMB {eq['cash']:,.2f} |",
        f"| Position Value | RMB {eq['position_value']:,.2f} |",
        f"| **Total Equity** | **RMB {eq['total_equity']:,.2f}** |",
        f"| Total Return | **{eq['total_return_pct']:+.2f}%** |",
        "",
    ]
    if pos:
        lines.append(f"**Holding**: {pos['name']}({pos['code']}) x {pos['shares']} shares | cost {pos['cost']:.4f}")
    else:
        lines.append("**Holding**: Cash only")

    lines += [
        "",
        "---",
        f"Total Trades: {trader.state['total_trades']} | Started: {trader.state['start_date']}",
    ]

    report = "\n".join(lines)
    report_file = trader.data_dir / f"report_{today}.md"
    with open(report_file, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f"\nReport saved: {report_file}")
    return report


# =============================================
# CLI
# =============================================

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding='utf-8')

    parser = argparse.ArgumentParser(description="ETF Momentum Paper Trader")
    parser.add_argument("--reset", action="store_true", help="Reset paper trading account")
    parser.add_argument("--seed", type=int, default=0, help="Seed price history with N days from backtest data (e.g. --seed 60)")
    parser.add_argument("--report", action="store_true", help="Show current status only")
    args = parser.parse_args()

    trader = PaperTrader(CONFIG)

    if args.reset:
        trader.reset()

    if args.seed > 0:
        trader.seed_history(days=args.seed)

    if args.report:
        # Just show current state
        try:
            prices = fetch_prices_akshare()
            eq = trader.compute_equity(prices)
            print(f"\nCurrent Equity: RMB {eq['total_equity']:,.2f} ({eq['total_return_pct']:+.2f}%)")
            pos = trader.state["position"]
            if pos:
                print(f"Position: {pos['name']}({pos['code']}) x {pos['shares']} shares @{pos['cost']:.4f}")
            else:
                print("Position: Cash")
        except Exception as e:
            print(f"Error fetching prices: {e}")
        sys.exit(0)

    result = trader.run()
    if result:
        generate_report(trader, result)
    else:
        print("\nUpdate failed. State unchanged.")
