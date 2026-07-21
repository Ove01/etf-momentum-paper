# -*- coding: utf-8 -*-
"""
ETF 动量轮动策略回测
对比：原始版(含未来函数) vs 修正版(信号T日, 交易T+1日)
"""
import pandas as pd
import numpy as np

# =============================================
# 1. 加载数据
# =============================================
print("=" * 60)
print("  加载数据...")
print("=" * 60)

cyb = pd.read_csv("data_cyb.csv", parse_dates=["date"]).rename(columns={"close": "cyb"})
nq  = pd.read_csv("data_nq.csv",  parse_dates=["date"]).rename(columns={"close": "nq"})
sz  = pd.read_csv("data_sz.csv",  parse_dates=["date"]).rename(columns={"close": "sz"})

# 合并对齐日期
df = cyb.merge(nq, on="date", how="inner").merge(sz, on="date", how="inner")
df = df.sort_values("date").reset_index(drop=True)

# 过滤到 2014 年以后
df = df[df["date"] >= "2014-01-01"].copy()

print(f"  创业板ETF: {len(cyb)} 条")
print(f"  纳指ETF:   {len(nq)} 条")
print(f"  上证指数:  {len(sz)} 条")
print(f"  对齐后:    {len(df)} 个交易日 ({df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()})")

# =============================================
# 2. 计算信号
# =============================================
LOOKBACK = 20  # 近一月 ≈ 20 个交易日
THRESHOLD = 0.1  # 涨幅阈值

# 近一月涨幅
df["cyb_mom"] = df["cyb"].pct_change(LOOKBACK) * 100
df["nq_mom"]  = df["nq"].pct_change(LOOKBACK) * 100
df["sz_mom"]  = df["sz"].pct_change(LOOKBACK) * 100

# 丢弃前 LOOKBACK 行的 NaN
df = df.dropna(subset=["cyb_mom", "nq_mom", "sz_mom"]).copy()

# 找出涨幅最大的标的
mom_cols = ["cyb_mom", "nq_mom", "sz_mom"]
df["max_mom"] = df[mom_cols].max(axis=1)
df["max_name"] = df[mom_cols].idxmax(axis=1)

# === 版本A: 原始策略（含未来函数） ===
# T日收盘后用T日收盘价计算信号并交易（回测中不现实）
def signal_original(row):
    if row["max_mom"] <= THRESHOLD:
        return "cash"
    if row["max_name"] == "cyb_mom":
        return "cyb"
    elif row["max_name"] == "nq_mom":
        return "nq"
    return "cash"

df["signal_raw"] = df.apply(signal_original, axis=1)

# === 版本B: 修正策略（消除未来函数） ===
# T日收盘后产生信号，T+1日执行
df["signal_fixed"] = df["signal_raw"].shift(1)
df["signal_fixed"] = df["signal_fixed"].fillna("cash")

# =============================================
# 3. 回测引擎
# =============================================
def run_backtest(df, signal_col, label, commission=0.00005):
    """
    回测：每日按信号调仓
    - signal_col: 使用的信号列名
    - commission: 佣金费率（万0.5）
    """
    capital = 1_000_000  # 初始 100 万
    shares = 0
    position = "cash"
    equity_list = []
    trades = []
    trade_count = 0

    prices = {"cyb": df["cyb"].values, "nq": df["nq"].values}
    signals = df[signal_col].values
    dates = df["date"].values

    for i in range(len(df)):
        date = dates[i]
        signal = signals[i]
        px_cyb = prices["cyb"][i]
        px_nq  = prices["nq"][i]

        # 调仓
        if signal != position:
            # 卖出现有持仓
            if position == "cyb" and shares > 0:
                capital = shares * px_cyb * (1 - commission)
                trades.append((date, "SELL", "cyb", px_cyb))
                shares = 0
                trade_count += 1
            elif position == "nq" and shares > 0:
                capital = shares * px_nq * (1 - commission)
                trades.append((date, "SELL", "nq", px_nq))
                shares = 0
                trade_count += 1

            # 买入新标的
            if signal == "cyb":
                shares = capital * (1 - commission) / px_cyb
                trades.append((date, "BUY", "cyb", px_cyb))
                trade_count += 1
            elif signal == "nq":
                shares = capital * (1 - commission) / px_nq
                trades.append((date, "BUY", "nq", px_nq))
                trade_count += 1

            position = signal

        # 当日权益
        if position == "cyb":
            equity = shares * px_cyb
        elif position == "nq":
            equity = shares * px_nq
        else:
            equity = capital

        equity_list.append((date, equity))

    return pd.DataFrame(equity_list, columns=["date", "equity"]), trades, trade_count


def compute_metrics(equity_df):
    """计算回测指标"""
    eq = equity_df["equity"].values
    rets = np.diff(eq) / eq[:-1]

    total_ret = (eq[-1] / eq[0] - 1) * 100
    years = len(eq) / 252
    annual_ret = ((eq[-1] / eq[0]) ** (1/years) - 1) * 100

    # 最大回撤
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak * 100
    max_dd = dd.min()

    # 夏普比率
    rf_daily = 0.02 / 252
    excess = rets - rf_daily
    sharpe = np.sqrt(252) * np.mean(excess) / np.std(rets) if np.std(rets) > 0 else 0

    # Calmar 比率
    calmar = annual_ret / abs(max_dd) if max_dd != 0 else 0

    # 胜率（日）
    win_rate = np.sum(rets > 0) / len(rets) * 100

    # 最长连续亏损
    max_consec_loss = 0
    cur_consec = 0
    for r in rets:
        if r < 0:
            cur_consec += 1
            max_consec_loss = max(max_consec_loss, cur_consec)
        else:
            cur_consec = 0

    # 持仓天数
    hold_days = equity_df[equity_df["equity"] > equity_df["equity"].shift(1)].copy()
    # 简化：统计非现金的天数
    cash_days = np.sum(np.diff(eq) == 0)

    return {
        "total_ret": total_ret,
        "annual_ret": annual_ret,
        "max_dd": max_dd,
        "sharpe": sharpe,
        "calmar": calmar,
        "win_rate": win_rate,
        "max_consec_loss": max_consec_loss,
        "years": years,
    }


# =============================================
# 4. 运行对比
# =============================================

print("\n" + "=" * 60)
print("  回测中...")
print("=" * 60)

# 版本A: 原始策略
eq_a, trades_a, cnt_a = run_backtest(df, "signal_raw", "原始版(含未来函数)")
metrics_a = compute_metrics(eq_a)

# 版本B: 修正策略
eq_b, trades_b, cnt_b = run_backtest(df, "signal_fixed", "修正版(消除未来函数)")
metrics_b = compute_metrics(eq_b)

# =============================================
# 5. 对比结果
# =============================================

print("\n" + "=" * 60)
print("  回测结果对比")
print("=" * 60)

print(f"""
{'指标':<20} {'原始版(含未来函数)':>20} {'修正版(消除未来函数)':>20}
{'-'*60}
{'总收益':<20} {metrics_a['total_ret']:>19.1f}% {metrics_b['total_ret']:>19.1f}%
{'年化收益':<20} {metrics_a['annual_ret']:>19.2f}% {metrics_b['annual_ret']:>19.2f}%
{'最大回撤':<20} {metrics_a['max_dd']:>19.1f}% {metrics_b['max_dd']:>19.1f}%
{'夏普比率':<20} {metrics_a['sharpe']:>19.2f} {metrics_b['sharpe']:>19.2f}
{'Calmar比率':<20} {metrics_a['calmar']:>19.2f} {metrics_b['calmar']:>19.2f}
{'日胜率':<20} {metrics_a['win_rate']:>19.1f}% {metrics_b['win_rate']:>19.1f}%
{'最长连亏(天)':<20} {metrics_a['max_consec_loss']:>19} {metrics_b['max_consec_loss']:>19}
{'交易次数':<20} {cnt_a:>19} {cnt_b:>19}
{'回测年数':<20} {metrics_a['years']:>19.1f} {metrics_b['years']:>19.1f}
""")

# =============================================
# 6. 按年度分解收益
# =============================================
print("=" * 60)
print("  年度收益对比")
print("=" * 60)

eq_a["year"] = eq_a["date"].dt.year
eq_b["year"] = eq_b["date"].dt.year

yearly_a = eq_a.groupby("year").agg(start=("equity", "first"), end=("equity", "last"))
yearly_b = eq_b.groupby("year").agg(start=("equity", "first"), end=("equity", "last"))
yearly_a["ret_a"] = (yearly_a["end"] / yearly_a["start"] - 1) * 100
yearly_b["ret_b"] = (yearly_b["end"] / yearly_b["start"] - 1) * 100

yearly = yearly_a[["ret_a"]].join(yearly_b[["ret_b"]])
yearly["diff"] = yearly["ret_a"] - yearly["ret_b"]

print(f"{'年份':<8} {'原始版':>10} {'修正版':>10} {'差值(未来函数)':>15}")
print("-" * 45)
for yr, row in yearly.iterrows():
    print(f"{yr:<8} {row['ret_a']:>9.1f}% {row['ret_b']:>9.1f}% {row['diff']:>14.1f}%")

# =============================================
# 7. 分段测试（验证过拟合）
# =============================================
print("\n" + "=" * 60)
print("  分段回测 (修正版)")
print("=" * 60)

segments = [
    ("2014-2015 牛市", "2014-01-01", "2015-12-31"),
    ("2016-2017 震荡", "2016-01-01", "2017-12-31"),
    ("2018 熊市",     "2018-01-01", "2018-12-31"),
    ("2019-2020 牛市", "2019-01-01", "2020-12-31"),
    ("2021-2022 震荡", "2021-01-01", "2022-12-31"),
    ("2023-2024 震荡", "2023-01-01", "2024-12-31"),
    ("2025-至今",     "2025-01-01", "2026-07-31"),
]

for seg_name, start, end in segments:
    mask = (df["date"] >= start) & (df["date"] <= end)
    df_seg = df[mask].copy()
    if len(df_seg) < 60:
        continue
    eq_seg, trades_seg, cnt_seg = run_backtest(df_seg, "signal_fixed", seg_name)
    m = compute_metrics(eq_seg)
    print(f"  {seg_name:<20}  年化: {m['annual_ret']:>7.2f}%  |  回撤: {m['max_dd']:>6.1f}%  |  夏普: {m['sharpe']:>5.2f}  |  交易: {cnt_seg:>3}次")

# =============================================
# 8. 参数敏感性简要测试
# =============================================
print("\n" + "=" * 60)
print("  参数敏感性 (修正版, 不同回看期)")
print("=" * 60)

print(f"{'回看天数':<10} {'年化收益':>10} {'最大回撤':>10} {'夏普':>8} {'交易次数':>8}")
print("-" * 50)

for lb in [5, 10, 15, 20, 30, 40, 60]:
    df_test = df.copy()
    df_test["cyb_mom"] = df_test["cyb"].pct_change(lb) * 100
    df_test["nq_mom"]  = df_test["nq"].pct_change(lb) * 100
    df_test["sz_mom"]  = df_test["sz"].pct_change(lb) * 100
    df_test["max_mom"] = df_test[["cyb_mom", "nq_mom", "sz_mom"]].max(axis=1)
    df_test = df_test.dropna(subset=["cyb_mom", "nq_mom", "sz_mom"])
    df_test["max_name"] = df_test[["cyb_mom", "nq_mom", "sz_mom"]].idxmax(axis=1)
    df_test["sig"] = df_test.apply(
        lambda r: "cash" if r["max_mom"] <= THRESHOLD
        else ("cyb" if r["max_name"] == "cyb_mom" else "nq"), axis=1
    )
    df_test["sig"] = df_test["sig"].shift(1).fillna("cash")

    eq_t, _, cnt_t = run_backtest(df_test, "sig", f"lb={lb}")
    m_t = compute_metrics(eq_t)
    print(f"{lb:<10} {m_t['annual_ret']:>9.2f}% {m_t['max_dd']:>9.1f}% {m_t['sharpe']:>7.2f} {cnt_t:>8}")

# =============================================
# 9. 保存权益曲线
# =============================================
eq_a.to_csv("equity_original.csv", index=False)
eq_b.to_csv("equity_fixed.csv", index=False)

print("\n" + "=" * 60)
print("  回测完成！")
print(f"  原始版年化: {metrics_a['annual_ret']:.2f}%  (含未来函数)")
print(f"  修正版年化: {metrics_b['annual_ret']:.2f}%  (可实盘)")
print(f"  未来函数虚增: {metrics_a['annual_ret'] - metrics_b['annual_ret']:.1f} 个百分点")
print("=" * 60)
