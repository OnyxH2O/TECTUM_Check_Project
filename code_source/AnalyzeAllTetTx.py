import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from threading import Lock
import time
from datetime import datetime

# Constants
API_BASE_URL = "https://api.explorer.tectum.io/explorer/transactions"
ADDRESS_API_URL = "https://api.explorer.tectum.io/explorer/address"
PARAMS = {"currencyKey": "tectum-t12-tet", "limit": 100}
ADDRESS_PARAMS = {"currencyKey": "tectum-t12-tet"}
CSV_HEADERS = ["wal_address", "nb_tx", "tx_balance", "wal_balance", "diff_balance"]
PROCESSED_PAGES_FILE = "/content/drive/MyDrive/output/processed_pages.csv"
OUTPUT_CSV = "/content/drive/MyDrive/output/wallet_data.csv"
BATCH_SIZE = 200
WALLET_BATCH_SIZE = 250  # Batch size for wallet balance updates
FUTURE_TIMEOUT = 30  # Timeout for each future in seconds

# Global variables
wallet_data = {}
processed_pages = set()
lock = Lock()

# Configure retry strategy
retry_strategy = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)
session = requests.Session()
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)

def log(message, error=False, elapsed=None):
    """Simple logging function with timestamp"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    prefix = "ERROR: " if error else ""
    time_str = f" (took {elapsed:.2f} seconds)" if elapsed is not None else ""
    print(f"\r{timestamp} - {prefix}{message}{time_str}")

def initialize_cvs():
    """Initialize or load CSV files"""
    global processed_pages
    if os.path.exists(PROCESSED_PAGES_FILE):
        df = pd.read_csv(PROCESSED_PAGES_FILE)
        processed_pages = set(df['page'].tolist())
        log(f"Loaded {len(processed_pages)} entries from {PROCESSED_PAGES_FILE}")
    else:
        pd.DataFrame(columns=['page']).to_csv(PROCESSED_PAGES_FILE, index=False)
        processed_pages = set()
        log(f"No existing {PROCESSED_PAGES_FILE} found, initialized empty set")

    load_wallet_data()

def load_wallet_data():
    """Load wallet data from CSV file if it exists"""
    global wallet_data
    if os.path.exists(OUTPUT_CSV):
        df = pd.read_csv(OUTPUT_CSV)
        for _, row in df.iterrows():
            wallet_data[row['wal_address']] = {
                "nb_tx": int(row['nb_tx']),
                "tx_balance": float(row['tx_balance']),
                "wal_balance": float(row.get('wal_balance', -1)),  # Default to -1 if not present
                "diff_balance": float(row.get('diff_balance', 0.0))  # Default to 0.0 if not present
            }
        log(f"Loaded {len(wallet_data)} wallet entries from {OUTPUT_CSV}")
    else:
        log(f"No existing {OUTPUT_CSV} found, starting with empty wallet data")

def save_processed_pages():
    """Save processed pages to CSV file"""
    with lock:
        pd.DataFrame(list(processed_pages), columns=['page']).to_csv(PROCESSED_PAGES_FILE, index=False)

def save_wallet_data():
    """Save wallet data to CSV file"""
    with lock:
        data_list = [
            {"wal_address": addr, "nb_tx": data["nb_tx"], "tx_balance": round(data["tx_balance"], 8),
             "wal_balance": data["wal_balance"], "diff_balance": data["diff_balance"]}
            for addr, data in wallet_data.items()
        ]
        pd.DataFrame(data_list, columns=CSV_HEADERS).to_csv(OUTPUT_CSV, index=False)

def save_cvs():
    """Save wallet data and processed pages to CSV files"""
    save_wallet_data()
    save_processed_pages()

def get_total_pages():
    """Get total number of pages from API with retry"""
    start_time = time.time()
    try:
        response = session.get(API_BASE_URL, params={**PARAMS, "page": 1}, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("errorCode") == 0:
            total_pages = (data["count"] + PARAMS["limit"] - 1) // PARAMS["limit"]
            elapsed = time.time() - start_time
            log(f"Total pages: {total_pages}", elapsed=elapsed)
            return total_pages
        raise Exception(f"API error: {data.get('errorMsgs')}")
    except requests.RequestException as e:
        elapsed = time.time() - start_time
        log(f"Failed to get total pages after retries: {str(e)}", error=True, elapsed=elapsed)
        raise

def process_page(page_num):
    """Process a single page of transactions"""
    if page_num in processed_pages:
        return {}

    start_time = time.time()
    temp_wallet_data = {}
    try:
        response = session.get(API_BASE_URL, params={**PARAMS, "page": page_num}, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data.get("errorCode") != 0:
            raise Exception(f"API error on page {page_num}: {data.get('errorMsgs')}")

        for tx in data["transactions"]:
            from_addr = tx["from"]
            to_addr = tx["to"]
            amount = float(tx["amount"])
            for addr in (from_addr, to_addr):
                if addr not in temp_wallet_data:
                    temp_wallet_data[addr] = {"nb_tx": 0, "tx_balance": 0.0}
            temp_wallet_data[from_addr]["nb_tx"] += 1
            temp_wallet_data[from_addr]["tx_balance"] -= amount
            temp_wallet_data[to_addr]["nb_tx"] += 1
            temp_wallet_data[to_addr]["tx_balance"] += amount

        with lock:
            processed_pages.add(page_num)
        elapsed = time.time() - start_time
        print(".", end="", flush=True)
        return temp_wallet_data

    except requests.RequestException as e:
        elapsed = time.time() - start_time
        log(f"Failed to process page {page_num} after retries: {str(e)}", error=True, elapsed=elapsed)
        return {}
    except Exception as e:
        elapsed = time.time() - start_time
        log(f"Error processing page {page_num}: {str(e)}", error=True, elapsed=elapsed)
        return {}

def merge_temp_data(temp_data):
    """Merge temporary page data into main wallet_data"""
    with lock:
        for addr, data in temp_data.items():
            if addr not in wallet_data:
                wallet_data[addr] = {"nb_tx": 0, "tx_balance": 0.0, "wal_balance": -1.0, "diff_balance": 0.0}
            wallet_data[addr]["nb_tx"] += data["nb_tx"]
            wallet_data[addr]["tx_balance"] += data["tx_balance"]

def process_and_save_batch(future_to_page, completed_count):
    """Process a batch of pages and save CSVs"""
    start_time = time.time()
    for future in as_completed(future_to_page):
        page = future_to_page[future]
        try:
            temp_data = future.result(timeout=FUTURE_TIMEOUT)
            merge_temp_data(temp_data)
            completed_count += 1

            if completed_count % BATCH_SIZE == 0:
                save_start = time.time()
                save_cvs()
                save_elapsed = time.time() - save_start

        except Exception as e:
            log(f"Page {page} generated an exception: {str(e)}", error=True)

    batch_elapsed = time.time() - start_time
    log(f"Batch completed (processed {len(future_to_page)} pages in {batch_elapsed:.2f} seconds)")
    return completed_count

def get_wallet_balance(address):
    """Retrieve wallet balance from API"""
    start_time = time.time()
    try:
        response = session.get(ADDRESS_API_URL, params={**ADDRESS_PARAMS, "address": address}, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data.get("errorCode") == 0:
            balance = float(data["balance"])
            elapsed = time.time() - start_time
            print(".", end="", flush=True)
            return balance
        raise Exception(f"API error for address {address}: {data.get('errorMsgs')}")
    except requests.RequestException as e:
        elapsed = time.time() - start_time
        log(f"Failed to retrieve balance for {address}: {str(e)}", error=True, elapsed=elapsed)
        return None
    except Exception as e:
        elapsed = time.time() - start_time
        log(f"Error retrieving balance for {address}: {str(e)}", error=True, elapsed=elapsed)
        return None

def process_wallet_batch(future_to_address, updated_count):
    """Process a batch of wallet balance updates"""
    start_time = time.time()
    for future in as_completed(future_to_address):
        address = future_to_address[future]
        try:
            balance = future.result(timeout=FUTURE_TIMEOUT)
            if balance is not None:
                with lock:
                    wallet_data[address]["wal_balance"] = round(balance, 8)
                    wallet_data[address]["diff_balance"] = round(balance - wallet_data[address]["tx_balance"], 8)
                updated_count += 1

                if updated_count % WALLET_BATCH_SIZE == 0:
                    save_start = time.time()
                    save_wallet_data()
                    save_elapsed = time.time() - save_start
                    log(f"Saved CSV after updating {updated_count} wallets", elapsed=save_elapsed)
        except Exception as e:
            log(f"Failed to process balance for {address}: {str(e)}", error=True)

    batch_elapsed = time.time() - start_time
    log(f"Wallet batch completed (processed {len(future_to_address)} wallets in {batch_elapsed:.2f} seconds)")
    return updated_count

def update_wallet_balances(nb_workers=10):
    """Update wallet balances and calculate diff_balance for wallets with nb_tx >= 2 and wal_balance = -1"""
    log(f"Starting wallet balance update ({nb_workers})")
    start_time = time.time()

    wallets_to_update = [addr for addr, data in wallet_data.items() if ((data["nb_tx"] >= 2 or data["tx_balance"] >= 0.000001) and data["wal_balance"] == -1)]
    log(f"Found {len(wallets_to_update)} wallets with 2 or more transactions and wal_balance = -1")

    if not wallets_to_update:
        log("No wallets to update")
        return

    updated_count = 0
    with ThreadPoolExecutor(max_workers=nb_workers) as executor:
        for i in range(0, len(wallets_to_update), WALLET_BATCH_SIZE):
            batch_wallets = wallets_to_update[i:i + WALLET_BATCH_SIZE]
            print("")
            log(f"Starting wallet batch: wallets {i+1} to {i+len(batch_wallets)}")
            future_to_address = {executor.submit(get_wallet_balance, addr): addr for addr in batch_wallets}
            updated_count = process_wallet_batch(future_to_address, updated_count)

    if updated_count % WALLET_BATCH_SIZE != 0 or updated_count == 0:
        save_start = time.time()
        save_wallet_data()
        save_elapsed = time.time() - save_start
        log(f"Final save of CSVs after updating {updated_count} wallets", elapsed=save_elapsed)

    elapsed = time.time() - start_time
    log(f"Completed wallet balance update (updated {updated_count} wallets in {elapsed:.2f} seconds)")

def print_summary():
    """Print a summary of wallet data statistics"""
    total_wallets = len(wallet_data)
    total_nb_tx_half = sum(data["nb_tx"] for data in wallet_data.values()) / 2
    total_tx_balance_positive = sum(data["tx_balance"] for data in wallet_data.values() if data["tx_balance"] > 0)
    total_wal_balance = sum(data["wal_balance"] for data in wallet_data.values() if data["wal_balance"] > 0)
    total_diff_balance_positive = sum(data["diff_balance"] for data in wallet_data.values() if data["diff_balance"] > 0)

    log("Summary of wallet data:")
    print(f"  Total wallet addresses: {total_wallets}")
    print(f"  Total nb_tx / 2: {total_nb_tx_half:.2f}")
    print(f"  Total tx_balance (where > 0): {total_tx_balance_positive:.8f}")
    print(f"  Total wal_balance: {total_wal_balance:.8f}")
    print(f"  Total diff_balance (where > 0): {total_diff_balance_positive:.8f}")

def main():
    log("Starting blockchain processing")
    start_time = time.time()

    initialize_cvs()
    log(f"Initialized with {len(processed_pages)} previously processed pages")

    try:
        total_pages = get_total_pages()
    except Exception:
        log("Terminating due to failure in getting total pages", error=True)
        return

    completed_count = 0
    remaining_pages = [page for page in range(1, total_pages + 1) if page not in processed_pages]
    log(f"Processing {len(remaining_pages)} remaining pages out of {total_pages} total")

    with ThreadPoolExecutor(max_workers=10) as executor:
        for i in range(0, len(remaining_pages), BATCH_SIZE):
            batch_pages = remaining_pages[i:i + BATCH_SIZE]
            print("")
            log(f"Starting batch: pages {batch_pages[0]} to {batch_pages[-1]}")
            future_to_page = {executor.submit(process_page, page): page for page in batch_pages}
            completed_count = process_and_save_batch(future_to_page, completed_count)

    if completed_count % BATCH_SIZE != 0 or completed_count == 0:
        save_start = time.time()
        save_cvs()
        save_elapsed = time.time() - save_start
        log(f"Final save of CSVs", elapsed=save_elapsed)

    total_elapsed = time.time() - start_time
    log(f"Completed processing {completed_count} pages in total time {total_elapsed:.2f} seconds")

    # Step 2: Update wallet balances
    print("")
    update_wallet_balances()

    print("")
    log("Rerun update_wallet_balances again with less worker to process failed wallets")
    update_wallet_balances(5)

    wallet_data["0x485aa23be2e58d77920f8aafb7c9b3b5e5173275"]["diff_balance"] -= 10000000
    save_wallet_data()
    log(f"Final data written to {OUTPUT_CSV}")

    # Print summary
    print("")
    print_summary()

if __name__ == "__main__":
    main()
