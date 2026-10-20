import asyncio
import io
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, List, Optional, Set
import httpx
from pypdf import PdfReader
from playwright.async_api import async_playwright
from telegram import Bot
from telegram.constants import ParseMode
from google import genai
from google.genai import types as genai_types

# ==========================================
# CONFIGURATION & ENVIRONMENT VARIABLES
# ==========================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CHECK_INTERVAL_SECONDS = 300
STATE_FILE = "sent_announcements.json"

NSE_HOME_URL = "https://www.nseindia.com"
NSE_API_URL = "https://www.nseindia.com/api/corporate-announcements?index=equities"
NSE_PDF_BASE_URL = "https://archives.nseindia.com/corporate/"

# Gemini AI Configuration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-2.0-flash"
PDF_MAX_CHARS = 15000  # Trim very large PDFs to stay within token limits

GEMINI_PROMPT_TEMPLATE = """You are a sharp financial analyst reviewing an NSE (National Stock Exchange of India) corporate filing.
Analyze the following document text and extract the most valuable insights for a retail investor.

Return your analysis in this exact HTML format (Telegram-compatible):

<b>🧠 AI Insights</b>

<b>📋 Summary:</b>
[2-3 sentence plain-English summary of what this filing is about]

<b>🔑 Key Points:</b>
• [Point 1]
• [Point 2]
• [Point 3 — add more if needed]

<b>💰 Financial Highlights:</b>
• [Any revenue, profit, EPS, dividend amounts, or financial ratios mentioned — or "None mentioned"]

<b>📅 Important Dates:</b>
• [Record dates, board meeting dates, AGM dates, etc. — or "None mentioned"]

<b>⚠️ Watch Out:</b>
• [Any risks, litigations, regulatory issues, or red flags — or "None identified"]

<b>🎯 Investor Takeaway:</b>
[One concise sentence on what this means for investors]

Document text:
---
{pdf_text}
---"""


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

# ==========================================
# RENDER HEALTH CHECK HTTP SERVER
# ==========================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    """Simple HTTP server to pass Render Web Service port checks."""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"NSE Telegram Bot is running OK.")

    def log_message(self, format, *args):
        return  # Suppress HTTP health log spam

def start_health_server():
    """Binds to the port assigned by Render (defaults to 10000)."""
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logging.info(f"Started health check HTTP server on port {port} for Render.")
    server.serve_forever()

# ==========================================
# STATE MANAGEMENT (DEDUPLICATION)
# ==========================================
def load_sent_ids() -> Set[str]:
    """Load previously sent announcement IDs to prevent duplicates."""
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
    """Save sent announcement IDs to local state file."""
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
    """
    Launches headless Chromium via Playwright with HTTP/2 disabled to bypass
    NSE Akamai CDN protocol resets and harvest session cookies.
    """
    logging.info("Harvesting fresh session cookies from NSE India...")
    cookies_dict = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-http2",  # Bypasses ERR_HTTP2_PROTOCOL_ERROR
                "--disable-blink-features=AutomationControlled",
                "--ignore-certificate-errors",
            ]
        )
        
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            extra_http_headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Sec-Ch-Ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1"
            }
        )
        
        page = await context.new_page()
        
        try:
            response = await page.goto(
                NSE_HOME_URL, 
                wait_until="networkidle", 
                timeout=60000
            )
            
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
    """Queries the internal NSE corporate announcements JSON API endpoint."""
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
            logging.warning("Received 403 Forbidden. Session cookies have expired.")
            return []
        else:
            logging.error(f"NSE API returned status code: {response.status_code}")
            return []


# ==========================================
# PDF DOWNLOAD & TEXT EXTRACTION
# ==========================================
async def download_pdf(attachment_path: str) -> Optional[bytes]:
    """Downloads a PDF from NSE's archive server. Returns raw bytes or None on failure."""
    url = f"{NSE_PDF_BASE_URL}{attachment_path}"
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://www.nseindia.com/",
    }
    try:
        async with httpx.AsyncClient(headers=headers, timeout=30.0, follow_redirects=True) as client:
            response = await client.get(url)
            if response.status_code == 200 and "pdf" in response.headers.get("content-type", "").lower():
                logging.info(f"Downloaded PDF ({len(response.content):,} bytes) from {url}")
                return response.content
            else:
                logging.warning(f"PDF download failed for {url} — HTTP {response.status_code}")
                return None
    except Exception as e:
        logging.error(f"PDF download exception for {url}: {e}")
        return None


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extracts plain text from a PDF byte stream using pypdf. Returns empty string on failure."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages_text = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages_text.append(text.strip())
        full_text = "\n\n".join(pages_text)
        logging.info(f"Extracted {len(full_text):,} characters from PDF ({len(reader.pages)} pages).")
        return full_text
    except Exception as e:
        logging.error(f"PDF text extraction error: {e}")
        return ""


async def generate_insights(pdf_text: str) -> Optional[str]:
    """
    Sends extracted PDF text to Gemini 2.0 Flash and returns a Telegram-ready
    HTML insight block. Returns None if the API call fails or key is missing.
    """
    if not GEMINI_API_KEY:
        logging.warning("GEMINI_API_KEY not set. Skipping AI insight generation.")
        return None

    # Trim text to avoid exceeding context limits
    trimmed_text = pdf_text[:PDF_MAX_CHARS]
    if len(pdf_text) > PDF_MAX_CHARS:
        trimmed_text += "\n\n[... document truncated for analysis ...]"

    prompt = GEMINI_PROMPT_TEMPLATE.format(pdf_text=trimmed_text)

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=GEMINI_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=1024,
            )
        )
        insight_text = response.text.strip()
        logging.info("Gemini insight generated successfully.")
        return insight_text
    except Exception as e:
        logging.error(f"Gemini API error: {e}")
        return None


# ==========================================
# TELEGRAM FORMATTING & NOTIFICATIONS
# ==========================================
def format_telegram_message(item: Dict) -> str:
    """Formats raw JSON announcement items into clean HTML for Telegram."""
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
    """Sends the formatted HTML alert via Telegram Bot."""
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
        logging.error("CRITICAL: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing from Environment Variables!")
        return

    # Start background HTTP server thread for Render health checks
    threading.Thread(target=start_health_server, daemon=True).start()

    logging.info("Starting NSE Announcements Monitor Bot on Render...")
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    sent_ids = load_sent_ids()
    cookies = {}

    while True:
        try:
            # Ping self using Render's automatically injected URL to prevent service sleeping
            render_url = os.getenv("RENDER_EXTERNAL_URL")
            if render_url:
                try:
                    async with httpx.AsyncClient() as client:
                        await client.get(render_url, timeout=10.0)
                    logging.info("Self-ping successful. Service kept awake.")
                except Exception as ping_err:
                    logging.warning(f"Self-ping failed: {ping_err}")

            if not cookies:
                cookies = await get_nse_cookies()
                if not cookies:
                    logging.error("Failed to harvest cookies. Retrying in 60 seconds...")
                    await asyncio.sleep(60)
                    continue

            logging.info("Checking for new corporate announcements...")
            announcements = await fetch_corporate_announcements(cookies)

            if not announcements and cookies:
                logging.info("Session invalid or empty payload received. Resetting cookies.")
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

                        # --- PDF Insight Pipeline ---
                        attachment_path = item.get("attachment", "")
                        if attachment_path:
                            pdf_bytes = await download_pdf(attachment_path)
                            if pdf_bytes:
                                pdf_text = extract_pdf_text(pdf_bytes)
                                if pdf_text and len(pdf_text) > 100:
                                    insight = await generate_insights(pdf_text)
                                    if insight:
                                        await send_telegram_notification(bot, insight)
                                else:
                                    logging.info(f"PDF for seqId {seq_id} yielded no extractable text (may be scanned image).")
                        # ----------------------------

                        await asyncio.sleep(1.5)


            logging.info(f"Check completed. Sent {new_entries_count} new announcements.")

        except Exception as e:
            logging.error(f"Unexpected error in main execution loop: {e}", exc_info=True)

        logging.info(f"Sleeping for {CHECK_INTERVAL_SECONDS} seconds...")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot execution stopped.")