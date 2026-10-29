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
from Crypto.Cipher import AES
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- DUMMY SERVER FOR RENDER ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

def run_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "9272421741:AAGfy3DYfBMjFWxnohGk96i-mwpibidj_kQ")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

class SingleFileScraper:
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
        self.project_name = f"{self.domain.replace('.', '_')}_{int(time.time())}"
        self.output_dir = os.path.abspath(self.project_name)
        os.makedirs(self.output_dir, exist_ok=True)
        self.stats = {"styles_inlined": 0, "scripts_inlined": 0}

    def solve_slowaes_challenge(self, html_text):
        """InfinityFree / iFastNet এর slowAES চ্যালেঞ্জ নিজে থেকে সমাধান করবে"""
        try:
            a_match = re.search(r'toNumbers\(["\']([a-f0-9]+)["\']\)', html_text)
            matches = re.findall(r'toNumbers\(["\']([a-f0-9]+)["\']\)', html_text)
            if len(matches) >= 3:
                key = bytes.fromhex(matches[0])
                iv = bytes.fromhex(matches[1])
                ciphertext = bytes.fromhex(matches[2])
                
                cipher = AES.new(key, AES.MODE_CBC, iv)
                decrypted = cipher.decrypt(ciphertext)
                cookie_val = decrypted.hex()
                
                # কুকি সেশনে সেট করা
                self.session.cookies.set("__test", cookie_val, domain=self.domain)
                return True
        except Exception:
            pass
        return False

    def fetch_page_with_bypass(self, url):
        res = self.session.get(url, timeout=15)
        # যদি slowAES চ্যালেঞ্জ পেজ আসে
        if "slowAES" in res.text:
            if self.solve_slowaes_challenge(res.text):
                time.sleep(1)
                # কুকি সহ পুনরায় আসল পেজে রিকোয়েস্ট পাঠানো
                res = self.session.get(url, timeout=15)
        return res

    def process_and_bundle(self):
        try:
            res = self.fetch_page_with_bypass(self.url)
            if res.status_code != 200:
                raise Exception(f"HTTP Error: {res.status_code}")

            soup = BeautifulSoup(res.content, 'html.parser')

            # হেডারে base ট্যাগ যোগ করা যাতে কোনো ইমেজ বা ফন্ট না ভাঙে
            if soup.head:
                base_tag = soup.new_tag('base', href=f"{urlparse(self.url).scheme}://{self.domain}/")
                soup.head.insert(0, base_tag)

            # CSS ফাইলগুলো সরাসরি HTML এর ভেতরে আনা
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

            # JavaScript ফাইলগুলো সরাসরি HTML এর ভেতরে আনা
            for script in soup.find_all('script'):
                src = script.get('src')
                if src:
                    js_url = urljoin(self.url, src)
                    try:
                        js_res = self.session.get(js_url, timeout=6)
                        if js_res.status_code == 200:
                            new_script = soup.new_tag('script')
                            new_script.string = js_res.text
                            if script.get('type'):
                                new_script['type'] = script.get('type')
                            script.replace_with(new_script)
                            self.stats["scripts_inlined"] += 1
                    except Exception:
                        pass

            output_file_path = os.path.join(self.output_dir, "index.html")
            with open(output_file_path, "w", encoding="utf-8") as f:
                f.write(str(soup))

            zip_filename = f"{self.project_name}.zip"
            with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
                zipf.write(output_file_path, arcname="index_bundled.html")

            shutil.rmtree(self.output_dir, ignore_errors=True)
            return zip_filename
        except Exception as e:
            shutil.rmtree(self.output_dir, ignore_errors=True)
            raise e

async def download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ দয়া করে একটি URL দিন।\nব্যবহার: `/download <url>`", parse_mode="Markdown")
        return

    url = context.args[0]
    if not url.startswith(('http://', 'https://')):
        url = 'http://' + url

    msg = await update.message.reply_text("🚀 সাইটটি প্রসেস ও সিকিউরিটি বাইপাস করা হচ্ছে...")
    zip_filename = None

    try:
        scraper = SingleFileScraper(url)
        zip_filename = await asyncio.to_thread(scraper.process_and_bundle)

        with open(zip_filename, 'rb') as f:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=f,
                caption=f"✅ সম্পূর্ণ UI বান্ডেল প্রস্তুত: {url}"
            )
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ সমস্যা হয়েছে: {str(e)}")
    finally:
        if zip_filename and os.path.exists(zip_filename):
            os.remove(zip_filename)

def main():
    threading.Thread(target=run_dummy_server, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("download", download))
    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
