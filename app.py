import os
import re
import time
import shutil
import zipfile
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- DUMMY SERVER FOR RENDER (Web Service Support) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is running alive!")

def run_dummy_server():
    # Render নিজে থেকে একটি PORT এনভায়রনমেন্ট ভেরিয়েবল দেয়
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# --- CONFIGURATION ---
# Render Environment Variables থেকে টোকেন নেবে, না পেলে ডিফল্ট বসবে
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8759872379:AAFAm7sZeLWCzM-GbQbHoJo8FtGWD2zFjDY")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

MAX_DEPTH = 2
MAX_PAGES = 20

class BotScraper:
    def __init__(self, url):
        self.url = url
        self.domain = urlparse(url).netloc
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': USER_AGENT,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Connection': 'keep-alive'
        })
        
        self.visited_urls = set()
        self.downloaded_files = set()
        self.api_hits = set()
        self.error_log = []
        
        self.stats = {"pages": 0, "images": 0, "scripts": 0, "styles": 0, "apis": 0, "errors": 0}
        
        self.project_name = f"{self.domain.replace('.', '_')}_{int(time.time())}"
        self.base_folder = os.path.abspath(self.project_name)
        self.assets_folder = os.path.join(self.base_folder, "assets")
        self.api_folder = os.path.join(self.base_folder, "api_data")
        self.pages_folder = os.path.join(self.base_folder, "pages")
        self.error_folder = os.path.join(self.base_folder, "errors")
        
        os.makedirs(self.assets_folder, exist_ok=True)
        os.makedirs(self.api_folder, exist_ok=True)
        os.makedirs(self.pages_folder, exist_ok=True)
        os.makedirs(self.error_folder, exist_ok=True)

    def sanitize_filename(self, name):
        return re.sub(r'[\\/*?:"<>|]', "_", name).strip()

    def fetch_page(self, url):
        try:
            response = self.session.get(url, timeout=10)
            if response.status_code == 200:
                return 200, response.content
            if response.status_code == 403:
                self.session.headers['Accept'] = 'text/html'
                response2 = self.session.get(url, timeout=10)
                if response2.status_code == 200:
                    return 200, response2.content
            return response.status_code, None
        except Exception as e:
            return -1, str(e)

    def scan_for_apis(self, text_content, source_url):
        patterns = re.findall(r'["\']([/\w\-]+/(?:api|v1|v2)/[\w\-/]+)["\']', text_content)
        for endpoint in patterns:
            if endpoint.startswith('http') and urlparse(endpoint).netloc != self.domain:
                continue
            full_url = urljoin(self.url, endpoint)
            if full_url not in self.api_hits:
                self.api_hits.add(full_url)
                try:
                    res = self.session.get(full_url, timeout=5)
                    if res.status_code == 200:
                        name = self.sanitize_filename(urlparse(full_url).path.replace("/", "_").strip("_")) or "root_api"
                        with open(os.path.join(self.api_folder, f"{name}.json"), 'wb') as f:
                            f.write(res.content)
                        self.stats["apis"] += 1
                except Exception:
                    pass

    def download_asset(self, url, tag_type):
        if url in self.downloaded_files or len(self.downloaded_files) > 250:
            return

        try:
            parsed = urlparse(url)
            path = parsed.path
            filename = os.path.basename(path)
            
            if not filename or '.' not in filename:
                ext = 'js' if tag_type == 'script' else 'css' if tag_type == 'link' else 'png'
                filename = f"asset_{len(self.downloaded_files) + 1}.{ext}"
            else:
                filename = self.sanitize_filename(filename)

            dir_name = self.sanitize_filename(os.path.dirname(path).strip('/').replace('/', '_')) or "misc"
            save_dir = os.path.join(self.assets_folder, dir_name)
            os.makedirs(save_dir, exist_ok=True)
            save_path = os.path.join(save_dir, filename)

            r = self.session.get(url, stream=True, timeout=5)
            if r.status_code == 200:
                with open(save_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                self.downloaded_files.add(url)
                
                if tag_type == 'img':
                    self.stats["images"] += 1
                elif tag_type == 'script':
                    self.stats["scripts"] += 1
                    try:
                        self.scan_for_apis(r.content.decode('utf-8', errors='ignore'), url)
                    except Exception:
                        pass
                elif tag_type == 'link':
                    self.stats["styles"] += 1
        except Exception:
            pass

    def scrape_page(self, url, current_depth):
        if url in self.visited_urls or current_depth > MAX_DEPTH:
            return
        if len(self.visited_urls) >= MAX_PAGES:
            return
        if urlparse(url).netloc != self.domain:
            return

        self.visited_urls.add(url)
        status, content = self.fetch_page(url)

        if status != 200 or not content:
            err_msg = f"Failed {url}: Status {status}"
            self.stats["errors"] += 1
            self.error_log.append(err_msg)
            return

        path = urlparse(url).path.strip('/')
        if not path:
            file_path = os.path.join(self.pages_folder, "index.html")
        else:
            safe_name = self.sanitize_filename(path.replace('/', '_'))
            if not safe_name.endswith('.html'):
                safe_name += ".html"
            file_path = os.path.join(self.pages_folder, safe_name)

        try:
            with open(file_path, 'wb') as f:
                f.write(content)
            self.stats["pages"] += 1
        except Exception:
            self.stats["errors"] += 1

        try:
            soup = BeautifulSoup(content, 'html.parser')
            self.scan_for_apis(content.decode('utf-8', errors='ignore'), url)

            for tag in soup.find_all(['img', 'link', 'script']):
                asset_url = None
                tag_type = None
                if tag.name == 'img' and tag.get('src'):
                    asset_url = tag.get('src')
                    tag_type = 'img'
                elif tag.name == 'link' and 'stylesheet' in tag.get('rel', []) and tag.get('href'):
                    asset_url = tag.get('href')
                    tag_type = 'link'
                elif tag.name == 'script' and tag.get('src'):
                    asset_url = tag.get('src')
                    tag_type = 'script'

                if asset_url:
                    self.download_asset(urljoin(url, asset_url), tag_type)

            for link in soup.find_all('a', href=True):
                next_url = urljoin(url, link['href'])
                if next_url.startswith(('http://', 'https://')):
                    self.scrape_page(next_url, current_depth + 1)

            time.sleep(0.3)
        except Exception:
            pass

    def create_zip(self):
        zip_name = f"{self.project_name}.zip"
        with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(self.base_folder):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, self.base_folder)
                    zipf.write(file_path, arcname)
        shutil.rmtree(self.base_folder, ignore_errors=True)
        return zip_name

    def run(self):
        self.scrape_page(self.url, 0)
        return self.create_zip()

# --- BOT HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hello! I am Website Source Downloader Bot.\n\n"
        "Commands:\n"
        "/download <url> - Download full source code & assets"
    )

async def download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Please provide a URL.\nUsage: /download https://example.com")
        return

    url = context.args[0]
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url

    msg = await update.message.reply_text(f"🚀 Starting Download...\nTarget: {url}")
    zip_filename = None

    try:
        scraper = BotScraper(url)
        zip_filename = await asyncio.to_thread(scraper.run)

        file_size_mb = os.path.getsize(zip_filename) / (1024 * 1024)
        stats_text = (
            f"✅ Download Complete!\n\n"
            f"📄 Pages: {scraper.stats['pages']}\n"
            f"🖼️ Images: {scraper.stats['images']}\n"
            f"📜 Scripts: {scraper.stats['scripts']}\n"
            f"🎨 Styles: {scraper.stats['styles']}\n"
            f"🔌 APIs: {scraper.stats['apis']}\n"
            f"📦 Size: {file_size_mb:.2f} MB"
        )
        await msg.edit_text(stats_text)

        if file_size_mb > 49.5:
            await update.message.reply_text("⚠️ ফাইল সাইজ ৫০MB-এর বেশি হওয়ায় পাঠানো যাচ্ছে না।")
        else:
            with open(zip_filename, 'rb') as f:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=f,
                    caption=f"Source code for {url}"
                )
    except Exception as e:
        await msg.edit_text(f"❌ Error occurred: {str(e)}")
    finally:
        if zip_filename and os.path.exists(zip_filename):
            os.remove(zip_filename)

def main():
    # 1. Start Web Server in Background for Render Port Binding
    threading.Thread(target=run_dummy_server, daemon=True).start()

    # 2. Run Telegram Bot
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("download", download))
    
    print("Bot is starting...")
    application.run_polling()

if __name__ == "__main__":
    main()
