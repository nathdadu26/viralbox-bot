# ViralBox Terabox Telegram Bot

A powerful Telegram bot that processes **Terabox links from media captions**, downloads files using **Aria2**, uploads them to a Telegram channel with a **watermarked filename**, and generates **worker links** for sharing.

The bot uses a **global queue system** to process tasks one-by-one and prevent overload.

---

## Features

* 📥 Download files from Terabox links
* ⚡ Aria2 powered fast downloads
* 📤 Upload files automatically to Telegram channel
* 🏷️ Automatic filename watermark
* 🧠 Duplicate detection using MongoDB
* 🔗 Generates shareable worker links
* 📋 Global queue system (prevents overload)
* ⏱️ Flood wait handling
* 🔁 Retry system for API, download and upload
* 🚀 Webhook support (Koyeb / VPS / Docker)

---

## How it Works

1. User sends **photo / video / document** with Terabox links in caption.
2. Bot extracts the Terabox links.
3. Terabox API returns file information.
4. Aria2 downloads the file.
5. Bot uploads file to Telegram channel.
6. A worker link is generated and stored in MongoDB.
7. Result is posted in the result channel.

---

## Environment Variables

Create a `.env` file and configure the following:

```
BOT_TOKEN=YOUR_BOT_TOKEN
WEBHOOK_URL=https://your-domain.com
PORT=8000

TERABOX_API=TERABOX_API_ENDPOINT

MONGO_URI=YOUR_MONGODB_URI
DB_NAME=viralbox_db

TELEGRAM_CHANNEL_ID=-100XXXXXXXXXX
RESULT_CHANNEL_ID=-100XXXXXXXXXX

WORKER_URL_BASE=https://your-worker-domain

CHANNEL_USERNAME=@YourChannel

ARIA2_RPC_URL=http://localhost:6800/jsonrpc
ARIA2_SECRET=mysecret
DOWNLOAD_DIR=/tmp/aria2_downloads
```

---

## Install Dependencies

```
pip install -r requirements.txt
```

---

## Run the Bot

```
python bot.py
```

---

## Webhook Setup

After deploying the bot, set the webhook using the Telegram API.

Example (correct webhook format):

```
https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://psychiatric-danita-dadumoni-9e8282e4.koyeb.app/webhook/<BOT_TOKEN>
```

You can check webhook status:

```
https://api.telegram.org/botBOT_TOKEN/getWebhookInfo
```

---

## Health Check

```
https://your-domain.com/health
```

Response:

```
OK
```

---

## Deployment

The bot supports deployment on:

* Docker
* VPS
* Koyeb
* Railway
* Render

Webhook mode will automatically start when `WEBHOOK_URL` or `PORT` is set.

---

## Example Usage

Send a **photo or video with Terabox link in caption**:

```
https://terabox.com/s/xxxxx
```

Bot will:

* Download the file
* Upload it to channel
* Generate worker link
* Post result in result channel

---

## License

MIT License
