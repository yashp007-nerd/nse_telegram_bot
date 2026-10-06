import asyncio
import json
import logging
import os
from typing import Dict, List, Set
import httpx
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
from playwright.async_api import async_playwright
from telegram import Bot
from telegram.constants import ParseMode

# ==========================================
# CONFIGURATION & ENVIRONMENT VARIABLES
# ==========================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CHECK_INTERVAL_SECONDS = 300
STATE_FILE = "sent_announcements.json"

NSE_HOME_URL = "https://www.nseindia.com"
NSE_API_URL = "https://www.nseindia.com/api/corporate-announcements?index=equities"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

# Dummy HTTP server to satisfy Render Web Service health checks
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"NSE Bot is running.")

    def log_message(self, format, *args):
        return  # Suppress health check HTTP logs

def start_dummy_server():
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logging.info(f"Started dummy HTTP server on port {port} for Render health checks.")
    server.serve_forever()

# ==========================================
# STATE MANAGEMENT
# ==========================================
def load_sent_ids() -> Set[str]:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(data)
        except Exception as e:
            logging.error(f"Error reading state file: {e}")
            return set()
    return set()

def save_sent_ids(sent_ids: Set[str]) -> None:
    try:
        truncated_ids = list(sent_ids)[-2000:]
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(truncated_ids, f, indent=2)
    except Exception as e:
        logging.error(f"Error saving state file: {e}")

# ==========================================
# NSE SESSION & SCRAPING ENGINE
# ==========================================
async def get_nse_cookies() -> Dict[str, str]:
    logging.info("Harvesting fresh session cookies from NSE India...")
    cookies_dict = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )
        page = await context.new_page()
        
        try:
            response = await page.goto(NSE_HOME_URL, wait_until="domcontentloaded", timeout=60000)
            if response and response.status == 200:
                await page.wait_for_timeout(3000)
                cookies = await context.cookies()
                for cookie in cookies:
                    cookies_dict[cookie["name"]] = cookie["value"]
                logging.info(f"Successfully harvested {len(cookies_dict)} session cookies.")
            else:
                status_code = response.status if response else "No Response"
                logging.error(f"Failed to load NSE homepage. HTTP Status: {status_code}")
        except Exception as e:
            logging.error(f"Playwright navigation error: {e}")
        finally:
            await browser.close()
    
    return cookies_dict

async def fetch_corporate_announcements(cookies: Dict[str, str]) -> List[Dict]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
        "X-Requested-With": "XMLHttpRequest",
    }
    
    async with httpx.AsyncClient(headers=headers, cookies=cookies, timeout=30.0, follow_redirects=True) as client:
        response = await client.get(NSE_API_URL)
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 403:
            logging.warning("Received 403 Forbidden. Session cookies expired.")
            return []
        else:
            logging.error(f"NSE API returned status code: {response.status_code}")
            return []

# ==========================================
# TELEGRAM FORMATTING & NOTIFICATIONS
# ==========================================
def format_telegram_message(item: Dict) -> str:
    company_name = item.get("sm_name", item.get("symbol", "N/A"))
    symbol = item.get("symbol", "N/A")
    desc = item.get("desc", "N/A")
    attmnt = item.get("attmntText", "")
    broadcast_time = item.get("an_dt", "N/A")
    attachment_url = item.get("attachment", "")
    
    message = (
        f"📢 <b>NSE Corporate Announcement</b>\n\n"
        f"🏢 <b>Company:</b> {company_name} (<code>{symbol}</code>)\n"
        f"🕒 <b>Time:</b> {broadcast_time}\n"
        f"📌 <b>Subject:</b> {desc}\n"
    )
    
    if attmnt:
        message += f"📝 <b>Details:</b> {attmnt}\n"

    if attachment_url:
        full_pdf_url = f"https://archives.nseindia.com/corporate/{attachment_url}"
        message += f"\n📄 <a href='{full_pdf_url}'>View Attached Document</a>"

    return message

async def send_telegram_notification(bot: Bot, message: str) -> bool:
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False
        )
        return True
    except Exception as e:
        logging.error(f"Failed to send Telegram message: {e}")
        return False

# ==========================================
# MAIN EXECUTION LOOP
# ==========================================
async def main_loop():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("CRITICAL: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing!")
        return

    # Start dummy server thread to pass Render Web Service port check
    threading.Thread(target=start_dummy_server, daemon=True).start()

    logging.info("Starting NSE Announcements Monitor Bot on Render...")
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    sent_ids = load_sent_ids()
    cookies = {}

    while True:
        try:
            if not cookies:
                cookies = await get_nse_cookies()
                if not cookies:
                    logging.error("Failed to harvest cookies. Retrying in 60 seconds...")
                    await asyncio.sleep(60)
                    continue

            logging.info("Checking for new corporate announcements...")
            announcements = await fetch_corporate_announcements(cookies)

            if not announcements and cookies:
                logging.info("Session invalid or empty payload. Resetting cookies.")
                cookies = {}
                await asyncio.sleep(10)
                continue

            new_entries_count = 0
            for item in reversed(announcements):
                seq_id = str(item.get("seqId", f"{item.get('symbol')}_{item.get('an_dt')}"))

                if seq_id not in sent_ids:
                    message = format_telegram_message(item)
                    success = await send_telegram_notification(bot, message)
                    
                    if success:
                        sent_ids.add(seq_id)
                        save_sent_ids(sent_ids)
                        new_entries_count += 1
                        await asyncio.sleep(1.5)

            logging.info(f"Check completed. Sent {new_entries_count} new announcements.")

        except Exception as e:
            logging.error(f"Unexpected error in main loop: {e}", exc_info=True)

        logging.info(f"Sleeping for {CHECK_INTERVAL_SECONDS} seconds...")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot execution stopped.")