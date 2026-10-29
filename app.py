import os
import re
import json
import time
import shutil
import zipfile
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, urljoin
import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- DUMMY SERVER FOR RENDER ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is running alive!")

def run_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# --- CONFIGURATION ---
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "9272421741:AAGfy3DYfBMjFWxnohGk96i-mwpibidj_kQ")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def format_cookies(cookie_raw):
    if not cookie_raw:
        return None
    cookie_raw = cookie_raw.strip()
    if cookie_raw.startswith('[') and cookie_raw.endswith(']'):
        try:
            items = json.loads(cookie_raw)
            return "; ".join([f"{item['name']}={item['value']}" for item in items if 'name' in item and 'value' in item])
        except Exception:
            pass
    return cookie_raw

class SingleFileScraper:
    def __init__(self, url, raw_cookies=None):
        self.url = url
        self.domain = urlparse(url).netloc
        self.session = requests.Session()
        
        headers = {
            'User-Agent': USER_AGENT,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Connection': 'keep-alive'
        }
        
        parsed_cookie = format_cookies(raw_cookies)
        if parsed_cookie:
            headers['Cookie'] = parsed_cookie

        self.session.headers.update(headers)
        
        self.project_name = f"{self.domain.replace('.', '_')}_{int(time.time())}"
        self.output_dir = os.path.abspath(self.project_name)
        os.makedirs(self.output_dir, exist_ok=True)
        
        self.stats = {"styles_inlined": 0, "scripts_inlined": 0}

    def sanitize_filename(self, name):
        return re.sub(r'[\\/*?:"<>|]', "_", name).strip()

    def process_and_bundle(self):
        try:
            # ১. মূল HTML পেজ আনা
            res = self.session.get(self.url, timeout=15)
            if res.status_code != 200:
                raise Exception(f"Failed to load page. Status: {res.status_code}")

            soup = BeautifulSoup(res.content, 'html.parser')

            # ২. সমস্ত CSS ফাইল এনে HTML এর ভেতরে <style> ট্যাগে বসানো
            for link in soup.find_all('link'):
                rel = link.get('rel', [])
                if isinstance(rel, list) and 'stylesheet' in rel or rel == 'stylesheet':
                    href = link.get('href')
                    if href:
                        css_url = urljoin(self.url, href)
                        try:
                            css_res = self.session.get(css_url, timeout=6)
                            if css_res.status_code == 200:
                                style_tag = soup.new_tag('style')
                                style_tag.string = css_res.text
                                link.replace_with(style_tag)
                                self.stats["styles_inlined"] += 1
                        except Exception:
                            pass

            # ৩. সমস্ত JavaScript ফাইল এনে HTML এর ভেতরে <script> ট্যাগে বসানো
            for script in soup.find_all('script'):
                src = script.get('src')
                if src:
                    js_url = urljoin(self.url, src)
                    try:
                        js_res = self.session.get(js_url, timeout=6)
                        if js_res.status_code == 200:
                            new_script = soup.new_tag('script')
                            new_script.string = js_res.text
                            # কোনো কোনো স্ক্রিপ্টে type মডিউল থাকে
                            if script.get('type'):
                                new_script['type'] = script.get('type')
                            script.replace_with(new_script)
                            self.stats["scripts_inlined"] += 1
                    except Exception:
                        pass

            # ৪. একসাথে বান্ডেল করা HTML ফাইল সেভ করা
            output_file_path = os.path.join(self.output_dir, "index.html")
            with open(output_file_path, "w", encoding="utf-8") as f:
                f.write(str(soup))

            # ৫. জিপ ফাইল তৈরি করা
            zip_filename = f"{self.project_name}.zip"
            with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
                zipf.write(output_file_path, arcname="index_bundled.html")

            shutil.rmtree(self.output_dir, ignore_errors=True)
            return zip_filename

        except Exception as e:
            shutil.rmtree(self.output_dir, ignore_errors=True)
            raise e

# --- BOT HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **HTML + CSS + JavaScript All-in-One Bot**\n\n"
        "এই বটটি সব CSS ও JavaScript সরাসরি HTML এর ভেতরে যুক্ত করে একটি মাত্র ফাইল বানিয়ে দেবে।\n\n"
        "ব্যবহারবিধি:\n"
        "`/download <URL> [Cookie]`",
        parse_mode="Markdown"
    )

async def download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ দয়া করে একটি URL দিন।\nব্যবহার: `/download <url> [cookie]`", parse_mode="Markdown")
        return

    url = context.args[0]
    raw_cookies = " ".join(context.args[1:]) if len(context.args) > 1 else None

    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url

    msg = await update.message.reply_text(f"🚀 প্রসেস করা হচ্ছে...\nসব HTML, CSS ও Script এক ফাইলে সাজানো হচ্ছে...", parse_mode="Markdown")
    zip_filename = None

    try:
        scraper = SingleFileScraper(url, raw_cookies=raw_cookies)
        zip_filename = await asyncio.to_thread(scraper.process_and_bundle)

        file_size_mb = os.path.getsize(zip_filename) / (1024 * 1024)
        stats_text = (
            f"✅ **কাজ সম্পন্ন হয়েছে!**\n\n"
            f"🎨 CSS যুক্ত করা হয়েছে: {scraper.stats['styles_inlined']} টি\n"
            f"📜 JavaScript যুক্ত করা হয়েছে: {scraper.stats['scripts_inlined']} টি\n"
            f"📦 ফাইল সাইজ: {file_size_mb:.2f} MB\n\n"
            f"👉 জিপ ফাইলের ভেতরের `index_bundled.html` ফাইলটিতে HTML, CSS এবং JS সব একসাথে রয়েছে।"
        )
        await msg.edit_text(stats_text, parse_mode="Markdown")

        with open(zip_filename, 'rb') as f:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=f,
                caption=f"All-in-one HTML bundle for {url}"
            )
    except Exception as e:
        await msg.edit_text(f"❌ এরর: {str(e)}")
    finally:
        if zip_filename and os.path.exists(zip_filename):
            os.remove(zip_filename)

def main():
    threading.Thread(target=run_dummy_server, daemon=True).start()
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("download", download))
    
    print("Bot is running...")
    application.run_polling()

if __name__ == "__main__":
    main()
