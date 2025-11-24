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
DB_NAME = os.getenv("DB_NAME", "terabox_bot")

# Channels
TELEGRAM_CHANNEL_ID = int(os.getenv("TELEGRAM_CHANNEL_ID", ""))
RESULT_CHANNEL_ID = int(os.getenv("RESULT_CHANNEL_ID", ""))

# Channel Username for Watermark
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@hive_zone")

# Webhook Configuration for Koyeb
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # Your Koyeb app URL
PORT = int(os.getenv("PORT", "8000"))  # Koyeb automatically sets PORT

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

# API timeout
API_TIMEOUT = int(os.getenv("API_TIMEOUT", "30"))

# Validate required environment variables
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required!")

# --- MongoDB Database Setup ---
mongo_client = None
db = None
files_collection = None

async def init_db():
    global mongo_client, db, files_collection
    try:
        mongo_client = AsyncIOMotorClient(MONGO_URI)
        db = mongo_client[DB_NAME]
        files_collection = db["files"]
        
        # Create indexes for better performance
        await files_collection.create_index("file_name", unique=True)
        await files_collection.create_index("message_id")
        await files_collection.create_index("created_at")
        
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

async def save_file_info(file_name: str, file_size: str, message_id: int, file_id: str):
    try:
        document = {
            "file_name": file_name,
            "file_size": file_size,
            "message_id": message_id,
            "file_id": file_id,
            "created_at": datetime.utcnow()
        }
        await files_collection.insert_one(document)
        logger.info(f"✅ Saved to DB: {file_name}")
    except Exception as e:
        logger.warning(f"Failed to save to DB (might be duplicate): {e}")

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

# ---------------- Bot Logic ----------------
class TeraboxTelegramBot:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.aria2 = Aria2Client(ARIA2_RPC_URL, ARIA2_SECRET)
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        self.upload_semaphore = asyncio.Semaphore(1)  # Only 1 upload at a time

    async def init_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=API_TIMEOUT))

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
                logger.info(f"Terabox API call attempt {attempt + 1}/{max_retries} for URL: {url}")
                async with self.session.get(TERABOX_API, params={"url": url}) as r:
                    data = await r.json()
                    
                    # Log full API response
                    logger.info(f"=== TERABOX API RESPONSE (Attempt {attempt + 1}) ===")
                    logger.info(f"Status Code: {r.status}")
                    logger.info(f"Full Response: {data}")
                    logger.info("=" * 50)
                    
                    if data.get("status") == "✅ Successfully":
                        logger.info(f"✅ Terabox API success for URL: {url}")
                        logger.info(f"File Name: {data.get('file_name', 'N/A')}")
                        logger.info(f"File Size: {data.get('file_size', 'N/A')}")
                        return {"success": True, "data": data}
                    else:
                        logger.warning(f"⚠️ Terabox API returned unsuccessful status")
                        return {"success": False, "error": data.get("status", "Unknown error")}
            except asyncio.TimeoutError:
                logger.warning(f"⏱️ Timeout on attempt {attempt + 1}/{max_retries}")
                if attempt == max_retries - 1:
                    return {"success": False, "error": "API timeout after multiple retries"}
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"❌ Error on attempt {attempt + 1}/{max_retries}: {str(e)}")
                if attempt == max_retries - 1:
                    return {"success": False, "error": str(e)}
                await asyncio.sleep(2)
        
        return {"success": False, "error": "Unknown error after retries"}

    async def upload_to_telegram_with_retry(self, context: ContextTypes.DEFAULT_TYPE, file_path: str, 
                                           caption: str, mime_type: str, max_retries: int = 5):
        """Upload to Telegram with flood wait and error handling"""
        for attempt in range(max_retries):
            try:
                async with self.upload_semaphore:  # Ensure sequential upload
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
                    
                    # Extract file_id based on message type
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
                logger.warning(f"⏳ FloodWait: Waiting {wait_time}s before retry {attempt + 1}/{max_retries}")
                await asyncio.sleep(wait_time)
            except TimedOut as e:
                logger.warning(f"⏱️ Timeout on upload attempt {attempt + 1}/{max_retries}")
                if attempt == max_retries - 1:
                    return {"success": False, "error": f"Upload timeout: {str(e)}"}
                await asyncio.sleep(5)
            except NetworkError as e:
                logger.warning(f"🌐 Network error on attempt {attempt + 1}/{max_retries}: {str(e)}")
                if attempt == max_retries - 1:
                    return {"success": False, "error": f"Network error: {str(e)}"}
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"❌ Upload error on attempt {attempt + 1}/{max_retries}: {str(e)}")
                if attempt == max_retries - 1:
                    return {"success": False, "error": str(e)}
                await asyncio.sleep(5)
        
        return {"success": False, "error": "Upload failed after all retries"}

# ---------------- Handlers ----------------
bot_instance = TeraboxTelegramBot()

async def schedule_delete(msg, user_msg, delay=1200):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except:
        pass
    try:
        await user_msg.delete()
    except:
        pass

async def process_single_link(url: str, link_number: int, total_links: int, 
                              context: ContextTypes.DEFAULT_TYPE, status_msg) -> Tuple[bool, Optional[Dict], Optional[str]]:
    """
    Process a single link in the pipeline
    Returns: (success, result_data, error_message)
    """
    try:
        # Update status
        await status_msg.edit_text(
            f"📄 Processing link {link_number}/{total_links}...\n"
            f"⏳ Waiting for Terabox API response...",
            parse_mode=ParseMode.HTML
        )
        
        # Step 1: Download from Terabox
        logger.info(f"[Link {link_number}/{total_links}] Starting Terabox download for: {url}")
        tb = await bot_instance.download_from_terabox(url)
        
        if not tb["success"]:
            error_msg = f"❌ Link {link_number}/{total_links}: Terabox API failed\n{tb['error']}"
            logger.error(error_msg)
            return False, None, error_msg

        data = tb["data"]
        original_file_name = data.get("file_name", "unknown")
        
        logger.info(f"[Link {link_number}/{total_links}] Got response for file: {original_file_name}")

        # Check if already processed
        if await is_file_processed(original_file_name):
            error_msg = f"⚠️ Link {link_number}/{total_links}: File <b>{original_file_name}</b> already processed"
            logger.info(error_msg)
            return False, None, error_msg

        # Add watermark to filename
        watermarked_name = bot_instance.add_watermark_to_filename(original_file_name)
        logger.info(f"📝 Renamed: {original_file_name} → {watermarked_name}")

        # Update status
        await status_msg.edit_text(
            f"📄 Processing link {link_number}/{total_links}...\n"
            f"📦 File: {original_file_name}\n"
            f"⏳ Checking file size...",
            parse_mode=ParseMode.HTML
        )

        # Get file size
        file_size_str = data.get("file_size", "0")

        # Update status
        await status_msg.edit_text(
            f"📄 Processing link {link_number}/{total_links}...\n"
            f"📦 File: {original_file_name}\n"
            f"📊 Size: {file_size_str}\n"
            f"⬇️ Starting download...",
            parse_mode=ParseMode.HTML
        )

        # Step 2: Download file with Aria2
        dl_url = data.get("streaming_url") or data.get("download_link")
        if not dl_url:
            error_msg = f"❌ Link {link_number}/{total_links}: No download link for <b>{original_file_name}</b>"
            logger.error(error_msg)
            return False, None, error_msg

        logger.info(f"📥 Starting Aria2 download")
        
        dl = await bot_instance.aria2.add_download(dl_url, {"out": watermarked_name})
        
        if not dl["success"]:
            error_msg = f"❌ Link {link_number}/{total_links}: Download failed\n{dl['error']}"
            logger.error(error_msg)
            return False, None, error_msg

        gid = dl["result"]
        logger.info(f"✅ Aria2 download started with GID: {gid}")
        
        # Update status
        await status_msg.edit_text(
            f"📄 Processing link {link_number}/{total_links}...\n"
            f"📦 File: {original_file_name}\n"
            f"⬇️ Downloading...",
            parse_mode=ParseMode.HTML
        )
        
        done = await bot_instance.aria2.wait_for_download(gid)
        if not done["success"]:
            error_msg = f"❌ Link {link_number}/{total_links}: Download error\n{done['error']}"
            logger.error(error_msg)
            return False, None, error_msg

        fpath = done["files"][0]["path"]
        logger.info(f"✅ File downloaded: {fpath}")

        # Update status
        await status_msg.edit_text(
            f"📄 Processing link {link_number}/{total_links}...\n"
            f"📦 File: {original_file_name}\n"
            f"⬆️ Uploading to Telegram channel...",
            parse_mode=ParseMode.HTML
        )

        # Step 3: Upload to Telegram Channel (Sequential)
        caption_file = (
            f"📁 File Name: {watermarked_name}\n"
            f"📊 File Size: {file_size_str}"
        )
        
        mime_type, _ = mimetypes.guess_type(fpath)
        
        upload_result = await bot_instance.upload_to_telegram_with_retry(
            context, fpath, caption_file, mime_type
        )
        
        if not upload_result["success"]:
            error_msg = f"❌ Link {link_number}/{total_links}: Upload failed\n{upload_result['error']}"
            logger.error(error_msg)
            try:
                os.remove(fpath)
            except:
                pass
            return False, None, error_msg

        message_id = upload_result["message_id"]
        file_id = upload_result["file_id"]
        
        logger.info(f"✅ Uploaded to channel - Message ID: {message_id}, File ID: {file_id}")

        # Step 4: Save to Database
        await save_file_info(original_file_name, file_size_str, message_id, file_id)

        # Prepare result data
        result_data = {
            "original_name": original_file_name,
            "watermarked_name": watermarked_name,
            "file_size": file_size_str,
            "message_id": message_id,
            "file_id": file_id
        }

        # Cleanup
        try:
            os.remove(fpath)
            logger.info(f"🗑️ Deleted temp file: {fpath}")
        except Exception as e:
            logger.warning(f"Failed to delete temp file: {e}")

        logger.info(f"[Link {link_number}/{total_links}] Successfully processed: {original_file_name}")
        return True, result_data, None

    except Exception as e:
        error_msg = f"❌ Link {link_number}/{total_links}: Unexpected error\n{str(e)}"
        logger.error(f"Unexpected error processing link {link_number}: {str(e)}")
        return False, None, error_msg

async def process_links_pipeline(urls: List[str], context: ContextTypes.DEFAULT_TYPE, update: Update):
    """
    Process multiple links in a pipeline - one after another
    """
    m = update.effective_message
    if not m:
        return

    total_links = len(urls)
    successful_results = []
    failed_links = []

    # Create initial status message
    status_msg = await m.reply_text(
        f"📄 Starting pipeline processing...\n"
        f"📊 Total links: {total_links}",
        parse_mode=ParseMode.HTML
    )

    # Process each link sequentially
    for idx, url in enumerate(urls, 1):
        logger.info(f"Pipeline: Processing link {idx}/{total_links}")
        
        success, result_data, error_msg = await process_single_link(
            url, idx, total_links, context, status_msg
        )

        if success and result_data:
            successful_results.append(result_data)
        elif error_msg:
            failed_links.append(error_msg)
        
        # Small delay between links
        if idx < total_links:
            await asyncio.sleep(2)

    # Final summary for status message
    summary = "📦 <b>Processing Complete!</b>\n\n"
    
    if successful_results:
        summary += f"✅ <b>Successful ({len(successful_results)}):</b>\n\n"
        for result in successful_results:
            summary += (
                f"📁 {result['watermarked_name']}\n"
                f"📊 Size: {result['file_size']}\n"
                f"🆔 Message ID: {result['message_id']}\n"
                f"🔗 File ID: <code>{result['file_id']}</code>\n\n"
            )
    
    if failed_links:
        summary += f"❌ <b>Failed ({len(failed_links)}):</b>\n\n"
        summary += "\n\n".join(failed_links)

    # Send results to result channel
    if successful_results:
        try:
            result_caption = f"📊 <b>Processed Files Report</b>\n\n"
            result_caption += f"✅ <b>Successfully Processed: {len(successful_results)}</b>\n\n"
            
            for result in successful_results:
                result_caption += (
                    f"━━━━━━━━━━━━━━━━\n"
                    f"📁 <b>File:</b> {result['original_name']}\n"
                    f"📊 <b>Size:</b> {result['file_size']}\n"
                    f"🆔 <b>Message ID:</b> {result['message_id']}\n"
                    f"🔗 <b>File ID:</b> <code>{result['file_id']}</code>\n\n"
                )
            
            if failed_links:
                result_caption += f"\n❌ <b>Failed: {len(failed_links)}</b>"
            
            await context.bot.copy_message(
                chat_id=RESULT_CHANNEL_ID,
                from_chat_id=m.chat.id,
                message_id=m.message_id,
                caption=result_caption,
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Failed to send to result channel: {e}")

    # Update status message with final summary
    try:
        await status_msg.edit_text(summary, parse_mode=ParseMode.HTML)
    except:
        pass

    # Delete user message
    try:
        await m.delete()
    except:
        pass

    # Schedule deletion of status message
    asyncio.create_task(schedule_delete(status_msg, None, delay=1200))

async def handle_media_with_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    if not m:
        return

    try:
        caption = m.caption or ""
        urls = re.findall(r"https?://[^\s]+", caption)
        urls = list(dict.fromkeys(urls))  # Remove duplicates

        if not urls:
            err_msg = await m.reply_text("❌ No links found in caption.", parse_mode=ParseMode.HTML)
            asyncio.create_task(schedule_delete(err_msg, m))
            return

        terabox_links = [u for u in urls if bot_instance.is_terabox_url(u)]

        if not terabox_links:
            err_msg = await m.reply_text(
                "❌ Not supported domain. Please send valid Terabox links.",
                parse_mode=ParseMode.HTML
            )
            asyncio.create_task(schedule_delete(err_msg, m))
            return

        logger.info(f"Starting pipeline for {len(terabox_links)} Terabox links")
        
        # Process all links in pipeline
        await process_links_pipeline(terabox_links, context, update)

    except Exception as e:
        logger.error(f"Error in handle_media_with_links: {e}")
        try:
            err_msg = await m.reply_text(f"❌ Error: {str(e)}", parse_mode=ParseMode.HTML)
            asyncio.create_task(schedule_delete(err_msg, m))
        except:
            pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    if not m:
        return
    await m.reply_text(
        f"✅ Bot is running!\n\n"
        f"📌 Features:\n"
        f"• Downloads from Terabox\n"
        f"• Renames files with {CHANNEL_USERNAME}\n"
        f"• Uploads to Telegram channel\n"
        f"• Stores file info in MongoDB\n\n"
        f"Send media with Terabox links in caption.",
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

    async def handle_media_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.application.create_task(handle_media_with_links(update, context))

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.Document.ALL,
        handle_media_wrapper
    ))

    # Initialize the application
    await application.initialize()
    await application.start()

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

    logger.info(f"Bot started in webhook mode on port {PORT}")
    logger.info(f"Telegram Channel ID: {TELEGRAM_CHANNEL_ID}")
    logger.info(f"Result Channel ID: {RESULT_CHANNEL_ID}")
    logger.info(f"Channel Username: {CHANNEL_USERNAME}")

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

            async def handle_media_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
                context.application.create_task(handle_media_with_links(update, context))

            app.add_handler(CommandHandler("start", start))
            app.add_handler(MessageHandler(
                filters.PHOTO | filters.VIDEO | filters.Document.ALL,
                handle_media_wrapper
            ))

            logger.info("Bot started in polling mode")
            logger.info(f"Telegram Channel ID: {TELEGRAM_CHANNEL_ID}")
            logger.info(f"Result Channel ID: {RESULT_CHANNEL_ID}")
            logger.info(f"Channel Username: {CHANNEL_USERNAME}")
            
            await app.run_polling()
        
        asyncio.run(run_polling())

if __name__ == "__main__":
    main()
