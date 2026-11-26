"""
AI customer-service bot for Telegram — FREE version (uses Groq's free tier).
It answers customer questions using ONLY the business info below.
"""

import os
from dotenv import load_dotenv
from groq import Groq
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# Load environment variables from .env file
load_dotenv()


# --- 1. Your keys (read from environment variables so they never sit in your code) ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
WHATSAPP_LINK = os.environ["WHATSAPP_LINK"]
client = Groq(api_key=os.environ["GROQ_API_KEY"])

# Free model on Groq. If it's ever retired, pick another from https://console.groq.com/docs/models
MODEL = "openai/gpt-oss-20b"

# --- 2. The business the bot works for. Edit this to fit any business! ---
BUSINESS_INFO = f"""
Business name: Kikiscurls and Co
What we sell: Braiding hair extensions
Location: Online only (no physical store)
Opening hours: Monday to Saturday, 7am-8pm. Closed on Sunday.
Products and prices:
- French curls: N6,500 per bundle
- Italian curls: N5,000 per bundle
- Bone straight extensions: N4,000 per bundle
How to order: Place your order on WhatsApp: {WHATSAPP_LINK}
Delivery: Nationwide. The delivery fee depends on your location and is confirmed on WhatsApp.
Delivery time: 2-3 business days within Lagos. For other states, the time is confirmed on WhatsApp.
Payment: Bank transfer only.
"""

SYSTEM_PROMPT = f"""You are a friendly customer-service assistant for a small business.
Answer questions using ONLY the information below. Keep replies short and warm.
Write in plain text only: no markdown, no bold, no [text](link) formatting. Write links as the plain URL.
If you don't know the answer, say so politely and suggest the customer send a message on WhatsApp.
Never make up prices, colours, lengths or delivery fees that are not listed.

{BUSINESS_INFO}"""


# --- 3. Ask the AI for a reply ---
def ask_ai(history):
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}] + history,
        max_tokens=300,
    )
    return response.choices[0].message.content


# --- 4. Telegram handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["history"] = []  # fresh conversation
    await update.message.reply_text("Hi Queen! 👋 Welcome to Kikiscurls and Co 💇🏾‍♀️ What extension would you like to get?  We sell quality and affordableFrench curls, Italian curls or bone straight extensions!")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Remember the conversation so the bot understands follow-up questions
    history = context.user_data.setdefault("history", [])
    history.append({"role": "user", "content": update.message.text})

    try:
        reply = ask_ai(history[-10:])  # only send the last 10 messages
    except Exception as e:
        print("AI error:", e)
        history.pop()  # drop the message that failed
        await update.message.reply_text("Sorry, I'm having trouble right now. Please try again in a moment.")
        return

    history.append({"role": "assistant", "content": reply})
    await update.message.reply_text(reply)


# --- 5. Start the bot ---
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot is running... press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()