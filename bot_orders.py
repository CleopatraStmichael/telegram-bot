"""
Kikiscurls and Co — AI customer-service bot for Telegram
that ALSO takes orders and saves them to a Google Sheet.
"""

import os
import json
from datetime import datetime

import gspread
from dotenv import load_dotenv
from groq import Groq
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# --- 1. Keys and connections ---
load_dotenv()
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
WHATSAPP_LINK = os.environ["WHATSAPP_LINK"]
client = Groq(api_key=os.environ["GROQ_API_KEY"])
MODEL = "openai/gpt-oss-20b"

# Connect to Google Sheets (uses credentials.json in this folder)
gc = gspread.service_account(filename="credentials.json")
sheet = gc.open_by_key(os.environ["SHEET_ID"]).sheet1

# --- 2. Products and prices (Python does the maths, not the AI) ---
PRICES = {
    "French curls": 6500,
    "Italian curls": 5000,
    "Bone straight": 4000,
}

BUSINESS_INFO = f"""
Business name: Kikiscurls and Co
What we sell: Braiding hair extensions
Location: Online only (no physical store)
Opening hours: Monday to Saturday, 7am-8pm. Closed on Sunday.
Products and prices:
- French curls: N6,500 per bundle
- Italian curls: N5,000 per bundle
- Bone straight extensions: N4,000 per bundle
WhatsApp: {WHATSAPP_LINK}
Delivery: Nationwide. The delivery fee depends on location and is confirmed on WhatsApp.
Delivery time: 2-3 business days within Lagos. For other states, confirmed on WhatsApp.
Payment: Bank transfer only.
"""

SYSTEM_PROMPT = f"""You are a friendly customer-service assistant for Kikiscurls and Co.
Answer questions using ONLY the information below. Keep replies short and warm.
Write in plain text only: no markdown, no bold, no [text](link) formatting. Write links as the plain URL.
If you don't know something, say so politely and suggest messaging us on WhatsApp.
Never make up prices, colours, lengths or delivery fees that are not listed.

TAKING ORDERS:
You can take orders. To place an order you need ALL of these:
customer's full name, phone number, product, number of bundles, and delivery address (with state).
Ask for any missing details one or two at a time.
Before saving, repeat the order back and ask the customer to confirm.
Only call the save_order tool AFTER the customer confirms.
After saving, tell them the order is received, give them the hair total, and ask them to
tap our WhatsApp link to get the delivery fee and bank details, make payment, and send their receipt there.
Always include the WhatsApp link in that message.

{BUSINESS_INFO}"""

# Describe the save_order "tool" so the AI knows it can use it
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "save_order",
            "description": "Save a confirmed customer order to the orders spreadsheet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Customer's full name"},
                    "phone": {"type": "string", "description": "Customer's phone number"},
                    "product": {"type": "string", "enum": list(PRICES.keys())},
                    "bundles": {"type": "integer", "description": "Number of bundles"},
                    "address": {"type": "string", "description": "Delivery address including state"},
                },
                "required": ["name", "phone", "product", "bundles", "address"],
            },
        },
    }
]


# --- 3. The function that actually writes to Google Sheets ---
def save_order(name, phone, product, bundles, address, telegram_user=""):
    # Check the details first, so empty or fake orders are never saved
    missing = []
    if len(str(name).strip()) < 2 or "?" in str(name):
        missing.append("full name")
    if len(str(phone).strip()) < 7:
        missing.append("phone number")
    if len(str(address).strip()) < 5:
        missing.append("delivery address")
    if product not in PRICES:
        missing.append("product")
    try:
        bundles = int(bundles)
    except (TypeError, ValueError):
        bundles = 0
    if bundles < 1:
        missing.append("number of bundles")

    if missing:
        print("⚠️ Order NOT saved, missing:", ", ".join(missing))
        return ("NOT SAVED. Missing or invalid: " + ", ".join(missing)
                + ". Ask the customer for these details, then confirm the order again.")

    total = PRICES[product] * bundles
    sheet.append_row([
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        name, phone, product, bundles, total, address, telegram_user, "New",
    ])
    print(f"✅ Order saved: {name} - {bundles} x {product} = N{total:,}")
    return f"Order saved. Total for hair: N{total:,} (delivery fee not included)."

# --- 4. Ask the AI; if it wants to save an order, do it, then get its final reply ---
def ask_ai(history, telegram_user):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history
    response = client.chat.completions.create(
        model=MODEL, messages=messages, tools=TOOLS, tool_choice="auto", max_tokens=500,
    )
    msg = response.choices[0].message

    if not msg.tool_calls:
        return msg.content  # a normal answer

    # The AI asked to save an order
    messages.append({
        "role": "assistant",
        "content": msg.content or "",
        "tool_calls": [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in msg.tool_calls
        ],
    })
    for tc in msg.tool_calls:
        try:
            args = json.loads(tc.function.arguments)
            result = save_order(**args, telegram_user=telegram_user)
        except Exception as e:
            print("Save error:", e)
            result = "Saving failed. Apologise and ask the customer to order on WhatsApp instead."
        messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    # Let the AI tell the customer what happened
    final = client.chat.completions.create(model=MODEL, messages=messages, max_tokens=300)
    return final.choices[0].message.content


# --- 5. Telegram handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["history"] = []
    await update.message.reply_text(
        "Hi! 👋 Welcome to Kikiscurls and Co 💇🏾‍♀️ Ask me about our French curls, "
        "Italian curls or bone straight extensions, or tell me what you'd like to order!"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    history = context.user_data.setdefault("history", [])
    history.append({"role": "user", "content": update.message.text})
    telegram_user = update.effective_user.username or update.effective_user.first_name or ""

    try:
        reply = ask_ai(history[-12:], telegram_user)
    except Exception as e:
        print("AI error:", e)
        history.pop()
        await update.message.reply_text("Sorry, I'm having trouble right now. Please try again in a moment.")
        return

    history.append({"role": "assistant", "content": reply})
    await update.message.reply_text(reply)


# --- 6. Start the bot ---
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot is running (with Google Sheets orders)... press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()