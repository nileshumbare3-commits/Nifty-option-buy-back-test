import datetime
from flask import Flask, render_template, request, session, redirect, url_for
from kiteconnect import KiteConnect
import pandas as pd
import logging
import os

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)
app.secret_key = os.urandom(24) # Needed for session management

# --- Token Management ---
# Get the absolute path for the directory where this script is located
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(SCRIPT_DIR, "kite_session.json")

def save_token(data):
    with open(TOKEN_PATH, "w") as f:
        import json
        json.dump(data, f)

def load_token():
    try:
        with open(TOKEN_PATH, "r") as f:
            import json
            return json.load(f)
    except FileNotFoundError:
        return None

# --- Helper Functions ---

def get_instrument_token(instrument_df, exchange, tradingsymbol):
    """Finds instrument token from the instruments dataframe."""
    try:
        return instrument_df[(instrument_df.tradingsymbol == tradingsymbol) & (instrument_df.exchange == exchange)].instrument_token.iloc[0]
    except IndexError:
        logging.warning(f"Could not find instrument token for {tradingsymbol}")
        return None

def get_future_symbol(instrument_df, instrument_name, target_date):
    """Finds the current month's future symbol for a given instrument."""
    futures = instrument_df[
        (instrument_df.name == instrument_name) &
        (instrument_df.segment == 'NFO-FUT')
    ].copy()
    futures['expiry_date'] = pd.to_datetime(futures['expiry']).dt.date
    futures = futures[futures['expiry_date'] >= target_date.date()]
    futures = futures.sort_values(by='expiry_date')
    if futures.empty:
        return None
    return futures.iloc[0]['tradingsymbol']

def get_option_symbols(instrument_df, instrument_name, underlying_price, target_date, option_type, strike_diff):
    """Finds the nearest strike and the one further away for the next weekly expiry."""
    options = instrument_df[
        (instrument_df.name == instrument_name) &
        (instrument_df.segment == 'NFO-OPT') &
        (instrument_df.instrument_type == option_type)
    ].copy()
    options['expiry_date'] = pd.to_datetime(options['expiry']).dt.date

    days_to_expiry = (3 - target_date.weekday() + 7) % 7
    next_expiry = target_date + datetime.timedelta(days=days_to_expiry)
    if next_expiry.date() <= target_date.date():
        next_expiry += datetime.timedelta(days=7)

    weekly_options = options[options['expiry_date'] == next_expiry.date()]

    if weekly_options.empty:
        logging.warning(f"No {option_type} options found for {instrument_name} on expiry {next_expiry.date()}")
        return None, None

    weekly_options['strike_diff_abs'] = abs(weekly_options['strike'] - underlying_price)
    sell_strike_row = weekly_options.sort_values(by='strike_diff_abs').iloc[0]
    sell_strike_symbol = sell_strike_row['tradingsymbol']
    sell_strike_price = sell_strike_row['strike']

    if option_type == 'PE':
        buy_strike_price = sell_strike_price - strike_diff
    else: # 'CE'
        buy_strike_price = sell_strike_price + strike_diff

    # Find the closest available strike for the buy leg
    weekly_options['buy_strike_diff_abs'] = abs(weekly_options['strike'] - buy_strike_price)
    buy_strike_row = weekly_options.sort_values(by='buy_strike_diff_abs').iloc[0]

    buy_strike_symbol = buy_strike_row['tradingsymbol']

    return sell_strike_symbol, buy_strike_symbol

def calculate_avwap(df, sd_multiple=1.0):
    """Calculates AVWAP and standard deviation bands."""
    if df.empty or df['volume'].sum() == 0:
        df['avwap'], df['upper_band'], df['lower_band'] = 0, 0, 0
        return df

    df['price_volume'] = df['close'] * df['volume']
    df['cumulative_volume'] = df['volume'].cumsum()
    df['cumulative_price_volume'] = df['price_volume'].cumsum()
    df['avwap'] = df['cumulative_price_volume'] / df['cumulative_volume']

    df['sq_diff'] = ((df['close'] - df['avwap'])**2) * df['volume']
    df['cumulative_sq_diff'] = df['sq_diff'].cumsum()
    df['variance'] = df['cumulative_sq_diff'] / df['cumulative_volume']
    df['std_dev'] = df['variance']**0.5

    df['upper_band'] = df['avwap'] + (df['std_dev'] * sd_multiple)
    df['lower_band'] = df['avwap'] - (df['std_dev'] * sd_multiple)
    return df

# --- Main Application ---

@app.route('/')
def index():
    session_data = load_token()
    logged_in = "access_token" in session_data if session_data else False
    return render_template('index.html', results={}, logged_in=logged_in)

@app.route('/login', methods=['POST'])
def login():
    from flask import session
    session["api_key"] = request.form.get('api_key')
    session["api_secret"] = request.form.get('api_secret')
    kite = KiteConnect(api_key=session["api_key"])
    return redirect(kite.login_url())

@app.route('/kite_callback')
def kite_callback():
    from flask import session, redirect, url_for
    request_token = request.args.get('request_token')
    if not request_token:
        return "Error: Could not get request token.", 400

    try:
        kite = KiteConnect(api_key=session["api_key"])
        session_data = kite.generate_session(request_token, api_secret=session["api_secret"])
        session_data['api_key'] = session["api_key"] # Add api_key to the session data
        save_token(session_data)
        logging.info("Successfully generated and saved Kite session.")
        return redirect(url_for('index'))
    except Exception as e:
        logging.error(f"Error generating session: {e}")
        return f"Error generating session: {e}", 400

@app.route('/run_backtest', methods=['POST'])
def run_backtest():
    # --- 1. Get Parameters ---
    session_data = load_token()
    if not session_data:
        return render_template('index.html', results={"status": "Error", "message": "Not logged in. Please login first."})

    api_key = session_data.get('api_key')
    access_token = session_data.get('access_token')

    from_date_str = request.form.get('from_date')
    to_date_str = request.form.get('to_date')
    instrument_name = request.form.get('instrument')
    stop_loss = float(request.form.get('stop_loss'))
    target_profit = float(request.form.get('target_profit'))
    lot_size = int(request.form.get('lot_size'))
    strike_diff = float(request.form.get('strike_diff'))
    sd_multiple = float(request.form.get('sd_multiple'))

    results_log = []
    final_results = {}

    try:
        # --- 2. Initialize Kite Connect ---
        if not all([api_key, api_secret, access_token]):
             raise ValueError("API Key, Secret, and Access Token are required.")

        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)

        # --- 3. Get Instruments ---
        logging.info("Fetching instruments...")
        nfo_instruments = kite.instruments("NFO")
        instrument_df = pd.DataFrame(nfo_instruments)
        logging.info(f"Fetched {len(instrument_df)} NFO instruments.")

        # --- 4. Backtesting Loop ---
        from_date = datetime.datetime.strptime(from_date_str, '%Y-%m-%d')
        to_date = datetime.datetime.strptime(to_date_str, '%Y-%m-%d')

        total_pnl = 0
        trade_count = 0

        current_date = from_date
        while current_date <= to_date:
            day_str = current_date.strftime('%Y-%m-%d')
            if current_date.weekday() >= 5:
                logging.info(f"Skipping weekend: {day_str}")
                current_date += datetime.timedelta(days=1)
                continue

            logging.info(f"--- Processing {day_str} ---")

            try:
                # --- 5. Get Future Contract ---
                future_symbol = get_future_symbol(instrument_df, instrument_name, current_date)
                if not future_symbol:
                    logging.warning(f"No active {instrument_name} future found for {day_str}.")
                    current_date += datetime.timedelta(days=1)
                    continue
                future_token = get_instrument_token(instrument_df, 'NFO', future_symbol)

                # --- 6. Fetch Historical Data ---
                hist_from = current_date.replace(hour=9, minute=15)
                hist_to = current_date.replace(hour=15, minute=30)

                future_data = kite.historical_data(future_token, hist_from, hist_to, "minute")
                if not future_data:
                    logging.warning(f"No historical data for {future_symbol} on {day_str}")
                    current_date += datetime.timedelta(days=1)
                    continue

                future_df = pd.DataFrame(future_data)
                future_df['date'] = pd.to_datetime(future_df['date'])
                future_df = calculate_avwap(future_df, sd_multiple)

                # --- 7. Check for Breakout Signal ---
                signal_time = current_date.replace(hour=9, minute=30)
                signal_candle = future_df[future_df['date'].dt.time == signal_time.time()]

                if signal_candle.empty:
                    logging.info("No candle found at 9:30 AM.")
                    current_date += datetime.timedelta(days=1)
                    continue

                close_price = signal_candle.iloc[0]['close']
                upper_band = signal_candle.iloc[0]['upper_band']
                lower_band = signal_candle.iloc[0]['lower_band']

                trade_type = None
                option_type = None
                if close_price > upper_band:
                    trade_type = "SELL_PUT_SPREAD"
                    option_type = "PE"
                    logging.info(f"Signal: Price ({close_price}) > Upper Band ({upper_band}). Selling PUT spread.")
                elif close_price < lower_band:
                    trade_type = "SELL_CALL_SPREAD"
                    option_type = "CE"
                    logging.info(f"Signal: Price ({close_price}) < Lower Band ({lower_band}). Selling CALL spread.")
                else:
                    logging.info("No breakout signal.")
                    current_date += datetime.timedelta(days=1)
                    continue

                # --- 8. Find Option Contracts ---
                sell_sym, buy_sym = get_option_symbols(instrument_df, instrument_name, close_price, current_date, option_type, strike_diff)
                if not sell_sym or not buy_sym:
                    logging.warning("Could not find suitable option contracts.")
                    current_date += datetime.timedelta(days=1)
                    continue

                # --- 9. Simulate Trade ---
                sell_token = get_instrument_token(instrument_df, 'NFO', sell_sym)
                buy_token = get_instrument_token(instrument_df, 'NFO', buy_sym)

                sell_data = pd.DataFrame(kite.historical_data(sell_token, hist_from, hist_to, "minute"))
                buy_data = pd.DataFrame(kite.historical_data(buy_token, hist_from, hist_to, "minute"))

                if sell_data.empty or buy_data.empty:
                    logging.warning("Missing data for one or both option contracts.")
                    current_date += datetime.timedelta(days=1)
                    continue

                sell_data['date'] = pd.to_datetime(sell_data['date'])
                buy_data['date'] = pd.to_datetime(buy_data['date'])

                trade_df = pd.merge(sell_data.add_suffix('_sell'), buy_data.add_suffix('_buy'), left_on='date_sell', right_on='date_buy')
                trade_df = trade_df[trade_df['date_sell'].dt.time >= signal_time.time()]

                if trade_df.empty:
                    logging.warning("No option data post signal time.")
                    current_date += datetime.timedelta(days=1)
                    continue

                entry_premium = trade_df.iloc[0]['close_sell'] - trade_df.iloc[0]['close_buy']
                trade_log = {"date": day_str, "trade_type": trade_type, "sell_leg": sell_sym, "buy_leg": buy_sym, "entry_premium": round(entry_premium, 2)}

                # --- 10. Monitor for SL/TP ---
                for _, row in trade_df.iterrows():
                    pnl = (entry_premium - (row['close_sell'] - row['close_buy'])) * lot_size
                    if pnl >= target_profit:
                        trade_log.update({'status': 'TARGET', 'exit_time': row['date_sell'].strftime('%H:%M'), 'pnl': target_profit})
                        break
                    if pnl <= -stop_loss:
                        trade_log.update({'status': 'STOPLOSS', 'exit_time': row['date_sell'].strftime('%H:%M'), 'pnl': -stop_loss})
                        break

                if 'status' not in trade_log: # EOD exit
                    eod_pnl = (entry_premium - (trade_df.iloc[-1]['close_sell'] - trade_df.iloc[-1]['close_buy'])) * lot_size
                    trade_log.update({'status': 'EOD', 'exit_time': trade_df.iloc[-1]['date_sell'].strftime('%H:%M'), 'pnl': round(eod_pnl, 2)})

                results_log.append(trade_log)
                total_pnl += trade_log['pnl']
                trade_count += 1
                logging.info(f"Trade executed: {trade_log}")

            except Exception as e:
                logging.error(f"Error processing {day_str}: {e}", exc_info=True)

            current_date += datetime.timedelta(days=1)

        # --- 11. Final Results ---
        final_results = {
            "status": "Backtest Completed", "from_date": from_date_str, "to_date": to_date_str,
            "total_pnl": round(total_pnl, 2), "trade_count": trade_count, "trades": results_log
        }

    except Exception as e:
        logging.error(f"An error occurred during backtest: {e}", exc_info=True)
        final_results = {"status": "Error", "message": str(e)}

    return render_template('index.html', results=final_results)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(debug=True, port=port)
