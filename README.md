# ₿ BTC Futures Trader

A lightweight **FastAPI** web application for placing Binance USDT-M Futures limit orders with automatic Take Profit and Stop Loss — straight from your browser.

---

## Features

- **BUY (Long) & SELL (Short)** forms with a single input — just enter the limit price
- **Live order preview** — TP, SL, quantity, and notional value update as you type
- **Configurable margin** — edit the margin amount directly in the UI, synced from the server config
- **Confirmation dialog** — review all order details before submitting
- **Live terminal** — real-time log stream via SSE (Server-Sent Events) shown below the forms
- **Auto-reconnect** — terminal reconnects automatically if the SSE connection drops

---

## Order Logic

| Parameter    | Value                        |
| ------------ | ---------------------------- |
| Symbol       | `BTCUSDT`                    |
| Order type   | `LIMIT` (entry)              |
| Take Profit  | `TAKE_PROFIT_MARKET` (±1000) |
| Stop Loss    | `STOP_MARKET` (±400)         |
| Working type | `MARK_PRICE`                 |
| Close mode   | `closePosition=true`         |

Quantity is calculated as:

```
quantity = AMOUNT × LEVERAGE / LIMIT_PRICE
```

---

## Project Structure

```
.
├── main.py               # FastAPI app — routes, config, order logic
├── keys.py               # API credentials (not committed)
├── requirements.txt      # Python dependencies
└── templates/
    └── index.html        # Frontend UI with live terminal
```

---

## Setup

### 1. Clone & install

```bash
git clone https://github.com/your-username/btc-futures-trader.git
cd btc-futures-trader
pip install -r requirements.txt
```

### 2. Add your API keys

Create a `keys.py` file in the project root:

```python
api    = "YOUR_BINANCE_API_KEY"
secret = "YOUR_BINANCE_SECRET_KEY"
```

> ⚠️ Never commit `keys.py`. It is listed in `.gitignore`.

### 3. Configure

Edit the config block at the top of `main.py`:

```python
USE_TESTNET  = False       # True → uses testnet.binancefuture.com
SYMBOL       = "BTCUSDT"
LEVERAGE     = 10
AMOUNT       = 2000.0      # Margin in USDT
```

### 4. Run

```bash
uvicorn main:app --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser.

---

## Testnet

To test safely without real funds, set `USE_TESTNET = True` in `main.py`. The app will point to:

```
https://testnet.binancefuture.com
```

Get testnet credentials at [testnet.binancefuture.com](https://testnet.binancefuture.com).

---

## Requirements

```
fastapi
uvicorn[standard]
binance-connector
jinja2
python-multipart
```

---

## .gitignore

Make sure your `.gitignore` includes:

```
keys.py
__pycache__/
*.pyc
.env
```

```bash
Invoke-RestMethod -Uri https://api.ipify.org
```

---

## Disclaimer

> This tool is for **educational purposes only**. Trading cryptocurrency futures involves significant financial risk. Use at your own risk. The authors are not responsible for any financial losses.

---

## License

MIT
