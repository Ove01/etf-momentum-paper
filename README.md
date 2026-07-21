# ETF 动量轮动 — 模拟盘

> 策略来源：[知乎「普通散户有什么"笨"方法在A股赚钱？」](https://www.zhihu.com/question/605657565/answer/1985447874876678562)
>
> 回测：2014-2026 年化 26%（修正版，消除未来函数）

---

## 策略规则

```
每天 14:50 检查：
  创业板ETF(159915) 近一月涨幅  vs  纳指ETF(513100) 近一月涨幅

  ├─ 创业板最大 且 > 0.1%  →  满仓 159915
  ├─ 纳指最大   且 > 0.1%  →  满仓 513100
  └─ 其他                  →  空仓
```

---

## 本地使用

```bash
# 初始化（首次运行）
pip install akshare pandas requests
python paper_trader.py --reset --seed 60

# 每日运行（模拟 14:50 操作）
python paper_trader.py

# 查看当前状态
python paper_trader.py --report
```

---

## GitHub Actions 自动运行

### 首次配置

1. 在 GitHub 创建仓库（例如 `etf-momentum-paper`）
2. 推送代码：
   ```bash
   git add -A
   git commit -m "ETF momentum paper trader"
   git branch -M main
   git remote add origin https://github.com/你的用户名/etf-momentum-paper.git
   git push -u origin main
   ```
3. 本地先跑一次初始化：
   ```bash
   python paper_trader.py --reset --seed 60
   git add paper_data/
   git commit -m "chore: initialize paper trading state"
   git push
   ```

### 之后

- ✅ 每个交易日 14:50 自动运行（UTC 6:50）
- ✅ 数据自动提交回仓库
- ✅ 随时在 Actions 页面手动触发
- ✅ 出差/放假/电脑关机都照常跑

### 查看结果

- `paper_data/report_YYYY-MM-DD.md` → 每日报告
- `paper_data/trade_log.csv` → 完整交易记录
- `paper_data/daily_equity.csv` → 每日权益曲线

---

## 文件结构

```
├── paper_trader.py          ← 主程序
├── backtest.py              ← 历史回测
├── .github/workflows/
│   └── paper_trade.yml      ← 自动运行配置
└── paper_data/              ← 运行时数据（自动维护）
    ├── state.json           ← 当前持仓/现金
    ├── price_history.csv    ← 每日价格
    ├── trade_log.csv        ← 交易记录
    ├── daily_equity.csv     ← 每日权益
    └── report_*.md          ← 每日报告
```
