# ⚡ NIFTY 50 Intraday Options Signal & Paper-Trading Bot

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![NIFTY Lot Size: 65](https://img.shields.io/badge/NIFTY_Lot_Size-65-emerald.svg)](https://www.nseindia.com/)
[![Built for Groww & Angel One](https://img.shields.io/badge/Brokers-Groww%20%7C%20Angel%20One-brightgreen.svg)](https://groww.in/)

A high-performance algorithmic intraday trading and real-time decision dashboard for **NIFTY 50 Call (CE) / Put (PE)** weekly options. Engineered with strict capital preservation for accounts starting at **₹5,000**, using an **AI Institutional Confluence Engine (ORB + VWAP + Supertrend + ADX + Volume Surge)** and live market feeds.

> **⚠️ Risk Disclaimer:** This software is for educational, research, and paper-trading purposes only. Options buying carries a high risk of capital loss due to intraday volatility and time (theta) decay. Never risk capital you cannot afford to lose.

---

## 🌟 Key Capabilities

### 1. 🎯 Groww Real-Time Option Chain Integration (Zero Paid API Required)
- Connects directly to **Groww's live Option Chain service** to fetch exact real-time order book LTPs, strike names, and contract IDs.
- **Dynamic Strike Auto-Selection:** Automatically filters and selects liquid nearest-expiry Call and Put contracts that strictly fit within a **₹5,000 capital limit** (targeting ₹50–₹75 option premiums for 1 lot = 65 qty).
- Real-time updates every 15 seconds without requiring any paid broker subscriptions.

### 2. 🖥️ Interactive Web Decision Dashboard (`http://127.0.0.1:5050`)
- **Live Trade Decision Table:** Displays exact contract names (with 1-click clipboard copy for Groww search), live LTPs, 1-lot costs, cash buffer remaining, Stop-Loss price, Target 1 (+35%), Target 2 (+50%), and breakout trigger levels.
- **Visual AI Signal Banner:** Instant alerts with audio chime notifications when institutional momentum triggers are met.
- **Step-by-Step Trade Execution Blueprint:** Embedded rules for entry, hard stop-loss, zero-risk breakeven lock, and theta time stops.

### 3. 🛡️ Strict Risk Management & Capital Protection
- **Lot Sizing:** Sized for 1 lot (65 qty) with a cash buffer for statutory exchange charges.
- **Hard Stop-Loss:** -18% fixed cutoff on option premium (max loss capped at ~₹600–₹800 per trade).
- **Zero-Risk Breakeven Lock:** The moment the option gains **+12%**, the Stop-Loss automatically moves to entry price for a ₹0 risk-free run.
- **Target Exits:** Target 1 at **+35%** (book profit), Target 2 at **+50%** (extended runner).
- **Anti-Theta Time Exit:** Maximum 35-minute holding period to eliminate sideways time decay.
- **Daily Trade Limit:** Maximum 1 high-probability trade per day (shut the screen after the trade).

### 4. 🤖 Optional Automated Angel One SmartAPI Connection
- Supports full automation and live WebSocket streaming via Angel One SmartAPI (TOTP authenticated).

---

## 🚀 Quick Start

### 1. Installation
Clone the repository and install requirements:
```bash
git clone https://github.com/NimeshDholakiya/nifty-intraday-bot.git
cd nifty-intraday-bot
pip install -r requirements.txt
```

### 2. Launching the Live Decision Dashboard
Simply double-click `start_dashboard.bat` or run:
```bash
python web_server.py
```
Open your browser at **`http://127.0.0.1:5050`**.

### 3. Running Offline Simulations & Backtests
Test strategy behavior across different market regimes with zero broker setup:
```bash
# Bullish breakout simulation
python main.py --mode demo --regime bullish_breakout

# Bearish breakdown simulation
python main.py --mode demo --regime bearish_breakdown

# Choppy sideways market (validates capital protection filter)
python main.py --mode demo --regime choppy

# Run all 16 unit tests
python -m unittest discover tests
```

---

## 📊 Strategy Architecture

```
09:15–09:30 AM IST ──► Opening Range Formation (High / Low recorded; entries blocked)
         │
09:30–11:15 AM IST ──► AI Confluence Engine:
                       ├── 1. Breakout above ORH / below ORL
                       ├── 2. Price alignment with Intraday VWAP
                       ├── 3. 5m Supertrend Direction
                       ├── 4. 15m Multi-Timeframe Trend Confirmation
                       └── 5. ADX ≥ 20 (Anti-Chop Filter) + Volume Surge
         │
Confidence ≥ 80%   ──► Groww Strike Selector:
                       Picks closest strike where 1 Lot (65 Qty) ≤ ₹5,000 (Premium ~₹50–₹75)
         │
Trade In Progress  ──► Trade Management:
                       ├── +12% Gain ──► SL moves to Entry (Risk = ₹0)
                       ├── Target 1  ──► +35% Profit Exit
                       ├── Target 2  ──► +50% Runner Exit
                       ├── SL Hit    ──► -18% Hard Exit
                       └── 35 Mins   ──► Time Exit (Anti-Theta Protection)
```

---

## ⚙️ Configuration (`.env`)

Copy the template and adjust settings as needed:
```bash
copy .env.example .env
```

| Parameter | Default | Description |
| :--- | :--- | :--- |
| `CAPITAL` | `5000` | Account capital in INR |
| `LOT_SIZE` | `65` | NIFTY 50 Option Lot Size |
| `MAX_TRADES_PER_DAY` | `1` | Daily discipline limit |
| `STOP_LOSS_PCT` | `0.18` | -18% Stop-Loss |
| `BREAKEVEN_TRIGGER_PCT` | `0.12` | +12% trigger to lock SL at cost |
| `TARGET_PCT` | `0.50` | +50% Profit Target |
| `MAX_HOLD_MINUTES` | `35` | Anti-Theta time stop |
| `MIN_ADX` | `20.0` | Minimum trend strength filter |

---

## 🧪 Unit Tests
A full test suite is included to verify technical indicators, position management, trailing stops, and risk sizing:
```bash
python -m unittest discover tests
```

---

## 📄 License
MIT License. Created by [Nimesh Dholakiya](https://github.com/NimeshDholakiya).
