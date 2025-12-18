import asyncio
import aiohttp
import logging
import os
import urllib.parse
import time
import re
import mimetypes
from typing import Optional, Dict, Any, List, Tuple
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler, Defaults
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, NetworkError
from dotenv import load_dotenv
from aiohttp import web
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime

# Load environment variables from .env file
load_dotenv()

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Disable httpx and telegram library verbose logging
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)

# Config from environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
TERABOX_API = os.getenv("TERABOX_API", "")

# MongoDB Configuration
MONGO_URI = os.getenv("MONGO_URI", "")
DB_NAME = os.getenv("DB_NAME", "viralbox_db")

# Channels
TELEGRAM_CHANNEL_ID = int(os.getenv("TELEGRAM_CHANNEL_ID", ""))
RESULT_CHANNEL_ID = int(os.getenv("RESULT_CHANNEL_ID", ""))

# Worker URL Base
WORKER_URL_BASE = os.getenv("WORKER_URL_BASE", "https://file.hivezone69.workers.dev")

# Channel Username for Watermark
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@hive_zone")

# Webhook Configuration for Koyeb
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
PORT = int(os.getenv("PORT", "8000"))

# Aria2 Configuration
ARIA2_RPC_URL = os.getenv("ARIA2_RPC_URL", "http://localhost:6800/jsonrpc")
ARIA2_SECRET = os.getenv("ARIA2_SECRET", "mysecret")
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/tmp/aria2_downloads")

# Terabox domains
TERABOX_DOMAINS = [
    "terabox.com", "1024terabox.com", "teraboxapp.com", "teraboxlink.com",
    "terasharelink.com", "terafileshare.com", "1024tera.com", "1024tera.cn",
    "teraboxdrive.com", "dubox.com"
]

# API timeout - increased for slow Terabox API
API_TIMEOUT = int(os.getenv("API_TIMEOUT", "120"))

# Validate required environment variables
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required!")

# --- MongoDB Database Setup ---
mongo_client = None
db = None
files_collection = None
mappings_collection = None

async def init_db():
    global mongo_client, db, files_collection, mappings_collection
    try:
        mongo_client = AsyncIOMotorClient(MONGO_URI)
        db = mongo_client[DB_NAME]
        files_collection = db["terabox_file_name"]
        mappings_collection = db["mappings"]
        
        # Create indexes for better performance
        await files_collection.create_index("file_name", unique=True)
        
        # Create index for mappings
        await mappings_collection.create_index("mapping", unique=True)
        await mappings_collection.create_index("message_id")
        
        logger.info("✅ MongoDB connected successfully")
    except Exception as e:
        logger.error(f"❌ MongoDB connection failed: {e}")
        raise

async def is_file_processed(file_name: str) -> bool:
    try:
        result = await files_collection.find_one({"file_name": file_name})
        return result is not None
    except Exception as e:
        logger.error(f"Error checking file in DB: {e}")
        return False

async def save_file_info(file_name: str, file_size: str):
    try:
        document = {
            "file_name": file_name,
            "file_size": file_size
        }
        await files_collection.insert_one(document)
        logger.info(f"✅ Saved to DB: {file_name}")
    except Exception as e:
        logger.warning(f"Failed to save to DB (might be duplicate): {e}")

import random
import string

def generate_random_mapping(length: int = 6) -> str:
    """Generate random alphanumeric mapping string"""
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for _ in range(length))

async def save_mapping(message_id: int) -> str:
    """Save message_id with random mapping and return the mapping"""
    max_attempts = 10
    for attempt in range(max_attempts):
        try:
            mapping = generate_random_mapping()
            document = {
                "mapping": mapping,
                "message_id": message_id
            }
            await mappings_collection.insert_one(document)
            logger.info(f"✅ Saved mapping: {mapping} -> {message_id}")
            return mapping
        except Exception as e:
            if attempt == max_attempts - 1:
                logger.error(f"Failed to save mapping after {max_attempts} attempts: {e}")
                raise
            # Duplicate mapping, try again
            continue
    raise Exception("Failed to generate unique mapping")

# ---------------- Aria2Client ----------------
class Aria2Client:
    def __init__(self, rpc_url: str, secret: Optional[str] = None):
        self.rpc_url = rpc_url
        self.secret = secret
        self.session: Optional[aiohttp.ClientSession] = None

    async def init_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600))

    async def close_session(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def _call_rpc(self, method: str, params: list = None):
        if params is None:
            params = []
        if self.secret:
            params.insert(0, f"token:{self.secret}")
        payload = {"jsonrpc": "2.0", "id": f"aria2_{int(time.time())}", "method": method, "params": params}
        try:
            await self.init_session()
            async with self.session.post(self.rpc_url, json=payload) as r:
                result = await r.json()
                if "error" in result:
                    return {"success": False, "error": result["error"]}
                return {"success": True, "result": result.get("result")}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def add_download(self, url: str, options: Dict[str, Any] = None):
        if options is None:
            options = {}
        opts = {"dir": DOWNLOAD_DIR, "continue": "true"}
        opts.update(options)
        return await self._call_rpc("aria2.addUri", [[url], opts])

    async def wait_for_download(self, gid: str):
        while True:
            status = await self._call_rpc("aria2.tellStatus", [gid])
            if not status["success"]:
                return status
            info = status["result"]
            if info["status"] == "complete":
                return {"success": True, "files": info["files"]}
            elif info["status"] in ["error", "removed"]:
                return {"success": False, "error": info.get("errorMessage", "Download failed")}
            await asyncio.sleep(2)

# ---------------- Global Task Queue ----------------
class GlobalTaskQueue:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.processing = False
        self.worker_task = None
        
    async def start_worker(self):
        """Start the background worker that processes tasks one by one"""
        if self.worker_task is None or self.worker_task.done():
            self.worker_task = asyncio.create_task(self._worker())
            logger.info("🚀 Global task queue worker started")
    
    async def _worker(self):
        """Background worker that processes queue items one by one"""
        while True:
            try:
                # Get next task from queue (wait if empty)
                task = await self.queue.get()
                
                self.processing = True
                logger.info(f"📋 Queue size: {self.queue.qsize()} | Processing new task")
                
                # Process the task
                await task["func"](**task["kwargs"])
                
                # Mark task as done
                self.queue.task_done()
                self.processing = False
                
                # Small delay between tasks
                await asyncio.sleep(1)
                
            except Exception as e:
                logger.error(f"❌ Error in queue worker: {e}")
                self.processing = False
    
    async def add_task(self, func, **kwargs):
        """Add a task to the queue"""
        await self.queue.put({"func": func, "kwargs": kwargs})
        logger.info(f"➕ Task added to queue. Queue size: {self.queue.qsize()}")

# Global queue instance
global_queue = GlobalTaskQueue()

# ---------------- Bot Logic ----------------
class TeraboxTelegramBot:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.aria2 = Aria2Client(ARIA2_RPC_URL, ARIA2_SECRET)
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    async def init_session(self):
        if not self.session:
            # Increased timeout for slow Terabox API
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))

    def is_terabox_url(self, url: str) -> bool:
        try:
            domain = urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
            return domain in TERABOX_DOMAINS or any(d in domain for d in TERABOX_DOMAINS)
        except:
            return False

    def add_watermark_to_filename(self, original_name: str) -> str:
        """Add 'Telegram - @hive_zone' before the original filename"""
        name, ext = os.path.splitext(original_name)
        return f"Telegram - {CHANNEL_USERNAME} {name}{ext}"

    async def download_from_terabox(self, url: str, max_retries: int = 3):
        """Download from Terabox with retry logic and proper timeout handling"""
        await self.init_session()
        
        for attempt in range(max_retries):
            try:
                logger.info(f"Terabox API call attempt {attempt + 1}/{max_retries}")
                
                # API ko proper URL format chahiye
                api_url = f"{TERABOX_API}?url={urllib.parse.quote(url)}"
                
                # Increased timeout for slow API - 120 seconds
                async with self.session.get(api_url, timeout=aiohttp.ClientTimeout(total=120)) as r:
                    # Check if response is OK
                    if r.status != 200:
                        logger.warning(f"⚠️ API returned status {r.status}")
                        if attempt == max_retries - 1:
                            return {"success": False, "error": f"API returned status {r.status}"}
                        await asyncio.sleep(3)
                        continue
                    
                    data = await r.json()
                    
                    # Check for successful response
                    if data.get("status") == "✅ Successfully":
                        logger.info(f"✅ Terabox API success")
                        return {"success": True, "data": data}
                    else:
                        logger.warning(f"⚠️ Terabox API unsuccessful: {data.get('status', 'Unknown')}")
                        if attempt == max_retries - 1:
                            return {"success": False, "error": data.get("status", "Unknown error")}
                        await asyncio.sleep(3)
                        
            except asyncio.TimeoutError:
                logger.warning(f"⏱️ Timeout on attempt {attempt + 1} (API is slow)")
                if attempt == max_retries - 1:
                    return {"success": False, "error": "API timeout - try again later"}
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"❌ Error on attempt {attempt + 1}: {str(e)}")
                if attempt == max_retries - 1:
                    return {"success": False, "error": str(e)}
                await asyncio.sleep(3)
        
        return {"success": False, "error": "Unknown error"}

    async def upload_to_telegram_with_retry(self, context: ContextTypes.DEFAULT_TYPE, file_path: str, 
                                           caption: str, mime_type: str, max_retries: int = 5):
        """Upload to Telegram with flood wait handling"""
        for attempt in range(max_retries):
            try:
                with open(file_path, "rb") as f:
                    if mime_type and mime_type.startswith("video"):
                        msg = await context.bot.send_video(
                            chat_id=TELEGRAM_CHANNEL_ID, 
                            video=f, 
                            caption=caption,
                            read_timeout=300,
                            write_timeout=300,
                            connect_timeout=60
                        )
                    elif mime_type and mime_type.startswith("image"):
                        msg = await context.bot.send_photo(
                            chat_id=TELEGRAM_CHANNEL_ID, 
                            photo=f, 
                            caption=caption,
                            read_timeout=300,
                            write_timeout=300,
                            connect_timeout=60
                        )
                    else:
                        msg = await context.bot.send_document(
                            chat_id=TELEGRAM_CHANNEL_ID, 
                            document=f, 
                            caption=caption,
                            read_timeout=300,
                            write_timeout=300,
                            connect_timeout=60
                        )
                
                # Extract file_id
                file_id = None
                if msg.video:
                    file_id = msg.video.file_id
                elif msg.photo:
                    file_id = msg.photo[-1].file_id
                elif msg.document:
                    file_id = msg.document.file_id
                
                return {"success": True, "message_id": msg.message_id, "file_id": file_id}
                    
            except RetryAfter as e:
                wait_time = e.retry_after + 2
                logger.warning(f"⏳ FloodWait: Waiting {wait_time}s")
                await asyncio.sleep(wait_time)
            except TimedOut as e:
                logger.warning(f"⏱️ Timeout on upload attempt {attempt + 1}")
                if attempt == max_retries - 1:
                    return {"success": False, "error": f"Upload timeout: {str(e)}"}
                await asyncio.sleep(5)
            except NetworkError as e:
                logger.warning(f"🌐 Network error: {str(e)}")
                if attempt == max_retries - 1:
                    return {"success": False, "error": f"Network error: {str(e)}"}
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"❌ Upload error: {str(e)}")
                if attempt == max_retries - 1:
                    return {"success": False, "error": str(e)}
                await asyncio.sleep(5)
        
        return {"success": False, "error": "Upload failed"}

# ---------------- Task Processing Functions ----------------
bot_instance = TeraboxTelegramBot()

async def process_single_link(url: str, link_number: int, total_links: int, 
                              context: ContextTypes.DEFAULT_TYPE, status_msg) -> Tuple[bool, Optional[Dict], Optional[str]]:
    """Process a single link"""
    try:
        await status_msg.edit_text(
            f"🔄 Processing link {link_number}/{total_links}...\n"
            f"⏳ Fetching Terabox info...",
            parse_mode=ParseMode.HTML
        )
        
        # Step 1: Get Terabox info
        logger.info(f"[{link_number}/{total_links}] Getting Terabox info")
        tb = await bot_instance.download_from_terabox(url)
        
        if not tb["success"]:
            error_msg = f"❌ Terabox API failed: {tb['error']}"
            logger.error(error_msg)
            return False, None, error_msg

        data = tb["data"]
        original_file_name = data.get("file_name", "unknown")
        file_size_str = data.get("file_size", "0")
        
        logger.info(f"[{link_number}/{total_links}] File: {original_file_name}")

        # Check if already processed
        if await is_file_processed(original_file_name):
            error_msg = f"⚠️ Already processed: {original_file_name}"
            logger.info(error_msg)
            return False, None, error_msg

        # Add watermark
        watermarked_name = bot_instance.add_watermark_to_filename(original_file_name)
        
        # Check file size (50MB limit for stability)
        try:
            size_val, size_unit = file_size_str.split()
            size_val = float(size_val)
            if size_unit.lower().startswith("kb"):
                size_mb = size_val / 1024
            elif size_unit.lower().startswith("mb"):
                size_mb = size_val
            elif size_unit.lower().startswith("gb"):
                size_mb = size_val * 1024
            else:
                size_mb = 0
        except:
            size_mb = 0

        if size_mb > 50:
            error_msg = f"❌ File too large: {file_size_str} (max 50MB)"
            logger.info(error_msg)
            return False, None, error_msg

        await status_msg.edit_text(
            f"🔄 Processing link {link_number}/{total_links}...\n"
            f"📦 {original_file_name}\n"
            f"⬇️ Downloading...",
            parse_mode=ParseMode.HTML
        )

        # Step 2: Download
        dl_url = data.get("streaming_url") or data.get("download_link")
        if not dl_url:
            error_msg = f"❌ No download link"
            logger.error(error_msg)
            return False, None, error_msg

        logger.info(f"📥 Starting download")
        dl = await bot_instance.aria2.add_download(dl_url, {"out": watermarked_name})
        
        if not dl["success"]:
            error_msg = f"❌ Download failed: {dl['error']}"
            logger.error(error_msg)
            return False, None, error_msg

        gid = dl["result"]
        logger.info(f"✅ Download started: {gid}")
        
        done = await bot_instance.aria2.wait_for_download(gid)
        if not done["success"]:
            error_msg = f"❌ Download error: {done['error']}"
            logger.error(error_msg)
            return False, None, error_msg

        fpath = done["files"][0]["path"]
        logger.info(f"✅ Downloaded: {fpath}")

        await status_msg.edit_text(
            f"🔄 Processing link {link_number}/{total_links}...\n"
            f"📦 {original_file_name}\n"
            f"⬆️ Uploading to channel...",
            parse_mode=ParseMode.HTML
        )

        # Step 3: Upload to Telegram
        caption_file = f"📁 File Name: {watermarked_name}\n📊 File Size: {file_size_str}"
        mime_type, _ = mimetypes.guess_type(fpath)
        
        upload_result = await bot_instance.upload_to_telegram_with_retry(
            context, fpath, caption_file, mime_type
        )
        
        if not upload_result["success"]:
            error_msg = f"❌ Upload failed: {upload_result['error']}"
            logger.error(error_msg)
            try:
                os.remove(fpath)
            except:
                pass
            return False, None, error_msg

        message_id = upload_result["message_id"]
        file_id = upload_result["file_id"]
        
        logger.info(f"✅ Uploaded - Msg ID: {message_id}")

        # Step 4: Save to DB (only file_name and file_size)
        await save_file_info(original_file_name, file_size_str)

        # Result data
        result_data = {
            "original_name": original_file_name,
            "watermarked_name": watermarked_name,
            "file_size": file_size_str,
            "message_id": message_id,
            "file_id": file_id,
            "mapping": None  # Will be set later
        }

        # Cleanup
        try:
            os.remove(fpath)
            logger.info(f"🗑️ Deleted: {fpath}")
        except Exception as e:
            logger.warning(f"Failed to delete: {e}")

        logger.info(f"[{link_number}/{total_links}] ✅ Success: {original_file_name}")
        return True, result_data, None

    except Exception as e:
        error_msg = f"❌ Unexpected error: {str(e)}"
        logger.error(f"Error: {str(e)}")
        return False, None, error_msg

async def process_task(urls: List[str], context: ContextTypes.DEFAULT_TYPE, 
                      message_id: int, chat_id: int, user_message_id: int, user_media_message):
    """Process all links from one user message - ONE BY ONE"""
    reply_msg = None
    try:
        total_links = len(urls)
        successful_results = []
        failed_links = []

        # Send initial reply
        reply_msg = await context.bot.send_message(
            chat_id=chat_id,
            reply_to_message_id=user_message_id,
            text=f"⏳ Processing {total_links} link(s)...",
            parse_mode=ParseMode.HTML
        )

        logger.info(f"📋 Processing {total_links} links sequentially")

        # Process each link ONE BY ONE
        for idx, url in enumerate(urls, 1):
            logger.info(f"▶️ Processing link {idx}/{total_links}")
            
            # Pass reply_msg as status_msg parameter
            success, result_data, error_msg = await process_single_link(
                url, idx, total_links, context, reply_msg
            )

            if success and result_data:
                successful_results.append(result_data)
            elif error_msg:
                failed_links.append(f"Link {idx}: {error_msg}")
            
            # Small delay between links
            if idx < total_links:
                await asyncio.sleep(2)

        # Check if there were any errors
        if failed_links:
            # Error occurred - show error and keep messages
            error_summary = "❌ <b>Processing Failed</b>\n\n"
            error_summary += "\n".join(failed_links)
            
            if successful_results:
                error_summary += f"\n\n✅ Successfully processed: {len(successful_results)}"
            
            # Update reply with error (don't delete)
            try:
                await reply_msg.edit_text(error_summary, parse_mode=ParseMode.HTML)
            except:
                pass
            
            logger.warning(f"⚠️ Task completed with errors: {len(failed_links)} failed")
            return

        # All successful - copy user's media to result channel with worker URL
        if successful_results:
            try:
                # Generate mapping and save to DB for first successful result
                # (assuming single file per message, but works for multiple too)
                mapping = await save_mapping(successful_results[0]['message_id'])
                
                # Build worker URL
                worker_url = f"{WORKER_URL_BASE}/{mapping}"
                
                # Simple caption with just the worker URL
                result_caption = f"🔗 {worker_url}"
                
                # Copy user's media message to result channel with worker URL
                await context.bot.copy_message(
                    chat_id=RESULT_CHANNEL_ID,
                    from_chat_id=chat_id,
                    message_id=user_message_id,
                    caption=result_caption,
                    parse_mode=ParseMode.HTML
                )
                logger.info(f"✅ Copied media to result channel with worker URL: {worker_url}")
                
            except Exception as e:
                logger.error(f"Failed to copy to result channel: {e}")

        # Delete both messages (original + reply) on success
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=user_message_id)
            logger.info(f"🗑️ Deleted user message")
        except Exception as e:
            logger.warning(f"Failed to delete user message: {e}")
        
        try:
            await reply_msg.delete()
            logger.info(f"🗑️ Deleted reply message")
        except Exception as e:
            logger.warning(f"Failed to delete reply message: {e}")

        logger.info(f"✅ Task completed successfully: {len(successful_results)} files processed")

    except Exception as e:
        logger.error(f"❌ Error in process_task: {e}")
        # On unexpected error, show error and keep messages
        if reply_msg:
            try:
                await reply_msg.edit_text(
                    f"❌ <b>Unexpected Error</b>\n\n{str(e)}",
                    parse_mode=ParseMode.HTML
                )
            except:
                pass

async def handle_media_with_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming media with links - ADD TO QUEUE"""
    m = update.effective_message
    if not m:
        return

    try:
        caption = m.caption or ""
        urls = re.findall(r"https?://[^\s]+", caption)
        urls = list(dict.fromkeys(urls))  # Remove duplicates

        if not urls:
            return

        terabox_links = [u for u in urls if bot_instance.is_terabox_url(u)]

        if not terabox_links:
            err_msg = await m.reply_text(
                "❌ No Terabox links found.",
                parse_mode=ParseMode.HTML
            )
            return

        logger.info(f"📨 Received {len(terabox_links)} Terabox links from user")
        
        # Add to queue (will be processed one by one by global worker)
        await global_queue.add_task(
            process_task,
            urls=terabox_links,
            context=context,
            message_id=m.message_id,
            chat_id=m.chat_id,
            user_message_id=m.message_id,
            user_media_message=m
        )

    except Exception as e:
        logger.error(f"Error in handle_media_with_links: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    if not m:
        return
    await m.reply_text(
        f"✅ <b>Bot is Running!</b>\n\n"
        f"📌 <b>Features:</b>\n"
        f"• One-by-one processing (no overload)\n"
        f"• Global queue system\n"
        f"• Automatic file renaming with watermark\n"
        f"• MongoDB storage for duplicate detection\n"
        f"• Flood wait handling\n\n"
        f"📋 <b>How to use:</b>\n"
        f"Send media (photo/video/document) with Terabox links in caption.\n\n"
        f"💡 <b>Example:</b>\n"
        f"Send a photo with caption:\n"
        f"<code>https://terabox.com/s/xxxxx</code>",
        parse_mode=ParseMode.HTML
    )

# Health check endpoint
async def health_check(request):
    return web.Response(text="OK", status=200)

async def webhook_handler(request):
    """Handle incoming webhook updates"""
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return web.Response(status=200)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.Response(status=500)

# Global application instance
application = None

async def start_webhook_server():
    """Start the webhook server"""
    global application
    
    # Initialize database
    await init_db()
    
    # Create application
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .defaults(Defaults(parse_mode=ParseMode.HTML))
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.Document.ALL,
        handle_media_with_links
    ))

    # Initialize the application
    await application.initialize()
    await application.start()
    
    # Start global queue worker
    await global_queue.start_worker()

    # Set webhook
    if WEBHOOK_URL:
        webhook_path = f"/webhook/{BOT_TOKEN}"
        full_webhook_url = f"{WEBHOOK_URL}{webhook_path}"
        await application.bot.set_webhook(url=full_webhook_url)
        logger.info(f"Webhook set to: {full_webhook_url}")
    else:
        logger.warning("WEBHOOK_URL not set!")

    # Create web application
    app = web.Application()
    app.router.add_get("/health", health_check)
    app.router.add_get("/", health_check)
    app.router.add_post(f"/webhook/{BOT_TOKEN}", webhook_handler)

    # Start web server
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    logger.info(f"🚀 Bot started in webhook mode on port {PORT}")
    logger.info(f"📢 Telegram Channel: {TELEGRAM_CHANNEL_ID}")
    logger.info(f"📊 Result Channel: {RESULT_CHANNEL_ID}")
    logger.info(f"🏷️ Watermark: {CHANNEL_USERNAME}")
    logger.info(f"📋 Global queue system active")

    # Keep the server running
    await asyncio.Event().wait()

def main():
    """Main entry point"""
    if WEBHOOK_URL or os.getenv("PORT"):
        logger.info("Starting in WEBHOOK mode")
        asyncio.run(start_webhook_server())
    else:
        logger.info("Starting in POLLING mode")
        
        async def run_polling():
            await init_db()
            
            app = (
                Application.builder()
                .token(BOT_TOKEN)
                .defaults(Defaults(parse_mode=ParseMode.HTML))
                .build()
            )

            app.add_handler(CommandHandler("start", start))
            app.add_handler(MessageHandler(
                filters.PHOTO | filters.VIDEO | filters.Document.ALL,
                handle_media_with_links
            ))
            
            # Start global queue worker
            await global_queue.start_worker()

            logger.info("🚀 Bot started in polling mode")
            logger.info(f"📢 Telegram Channel: {TELEGRAM_CHANNEL_ID}")
            logger.info(f"📊 Result Channel: {RESULT_CHANNEL_ID}")
            logger.info(f"📋 Global queue system active")
            
            await app.run_polling()
        
        asyncio.run(run_polling())

if __name__ == "__main__":
    main()
