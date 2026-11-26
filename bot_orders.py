"""
Kikiscurls and Co — AI customer-service bot for Telegram
that ALSO takes orders and saves them to a Google Sheet.
"""

import os
import json
import re
from datetime import datetime, timedelta

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
MODEL = "openai/gpt-oss-120b"  # bigger free model: better at maths and tools

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

SYSTEM_PROMPT = f"""You are a friendly, polite customer-service assistant for Kikiscurls and Co.
Answer questions using ONLY the information below. Keep replies short and warm.
Write in plain text only: no markdown, no bold, no [text](link) formatting. Write links as the plain URL.
Never make up prices, colours, lengths, textures, stock or delivery fees that are not listed.

HOW TO TALK TO CUSTOMERS:
- Always be polite, patient and respectful, even if the customer is rude, upset or impatient.
- Read the whole conversation before you reply. Understand follow-up questions such as
  "how much for 3?" or "and the Italian one?" using what was said earlier.
- If the customer greets you, greet them back warmly. If they thank you, thank them too.
- If a message is unclear, politely ask what they mean instead of guessing.
- Customers often write in Nigerian Pidgin, slang, abbreviations or with spelling mistakes.
  Understand all of it. Examples: "abeg" = please, "wetin" = what, "how much e be?" / "hw much" = what is the price,
  "I wan buy" = I want to buy, "e dey?" = is it available, "una" = you (plural), "pls"/"plz" = please,
  "bundle wey" = which bundle, "send am" = send it, "I go take 3" = I will take 3, "na how much?" = how much is it.
- Match the customer's style: if they write in Pidgin, you may reply in light, friendly Pidgin mixed with
  simple English (e.g. "No wahala! French curls na N6,500 per bundle."). Otherwise reply in simple, friendly English.
  Always stay polite and clear.
- If they complain (late delivery, wrong item, payment issue), apologise sincerely and
  send them to WhatsApp so the team can sort it out personally.
- If they ask about something unrelated to our hair business, politely say you can only help
  with Kikiscurls and Co products and orders.

SEND TO WHATSAPP (always include the WhatsApp link):
- Questions about LENGTH (e.g. "how long is French curls?", "do you have 30 inches?"),
  colours, textures, pictures or samples of the hair, stock availability, or custom requests.
- Anything you cannot answer from the information below.
Say it kindly, for example: "For lengths, our team will gladly share the options and pictures
with you on WhatsApp: <link>"

TAKING ORDERS:
You can take orders. To place an order you need ALL of these:
customer's full name, phone number, delivery address (with state), and the items they want.
A customer can order SEVERAL products in one order (e.g. 2 French curls and 3 Bone straight).
Put every product and its number of bundles in the items list of ONE save_order call.
Ask for any missing details one or two at a time.
NEVER make up or guess a customer's name, phone number or address, and never give example names,
numbers or addresses. Only use details the customer has actually typed in this chat.
NEVER calculate prices or totals yourself. Always use the calculate_total tool and copy its numbers exactly.
Before saving, call calculate_total, repeat the full order (every product) with that total, and ask the customer to confirm.
Only call save_order AFTER the customer confirms, ONCE, with ALL products in the items list.
If the customer ADDS to or CHANGES an order that was already saved, call calculate_total for the
COMPLETE new order (old items + new items), confirm it, then call save_order again with the COMPLETE
order. This updates their existing order; it does not create a second one.
If nothing changed, do not call save_order again.
After saving, tell them the order is received, give them the hair total, and ask them to
tap our WhatsApp link to get the delivery fee and bank details, make payment, and send their receipt there.
Always include the WhatsApp link in that message.

{BUSINESS_INFO}"""

# The "items" list used by both tools: every product with its number of bundles
ITEMS_SCHEMA = {
    "type": "array",
    "description": "Every product in the order, with its number of bundles",
    "items": {
        "type": "object",
        "properties": {
            "product": {"type": "string", "enum": list(PRICES.keys())},
            "bundles": {"type": "integer", "description": "Number of bundles"},
        },
        "required": ["product", "bundles"],
    },
}

# Describe the tools so the AI knows it can use them
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "calculate_total",
            "description": "Work out the exact price of an order. Use this before telling the customer any total.",
            "parameters": {
                "type": "object",
                "properties": {"items": ITEMS_SCHEMA},
                "required": ["items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_order",
            "description": "Save a confirmed customer order (with ALL its products) to the orders spreadsheet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Customer's full name"},
                    "phone": {"type": "string", "description": "Customer's phone number"},
                    "items": ITEMS_SCHEMA,
                    "address": {"type": "string", "description": "Delivery address including state"},
                },
                "required": ["name", "phone", "items", "address"],
            },
        },
    },
]


# --- 3. Python does the maths and the saving (the AI never does the maths) ---
def check_items(items):
    """Check every item and add up the price. Returns (lines, total, bundles, problems)."""
    lines, total, total_bundles, problems = [], 0, 0, []
    for item in items or []:
        product = item.get("product")
        try:
            bundles = int(item.get("bundles", 0))
        except (TypeError, ValueError):
            bundles = 0
        if product not in PRICES or bundles < 1:
            problems.append(f"a valid product and number of bundles (got: {product}, {bundles})")
            continue
        cost = PRICES[product] * bundles
        lines.append(f"{bundles} x {product} = N{cost:,}")
        total += cost
        total_bundles += bundles
    if not lines:
        problems.append("at least one product")
    return lines, total, total_bundles, problems


def calculate_total(items):
    lines, total, _, problems = check_items(items)
    if problems:
        return "Could not calculate: " + ", ".join(problems)
    return "; ".join(lines) + f". TOTAL for hair: N{total:,} (delivery fee not included)."


def save_order(name, phone, items, address, telegram_user="", user_data=None, customer_text=""):
    # Check the details first, so empty or fake orders are never saved
    lines, total, total_bundles, missing = check_items(items)
    if len(str(name).strip()) < 2 or "?" in str(name):
        missing.append("full name")
    if len(str(phone).strip()) < 7:
        missing.append("phone number")
    if len(str(address).strip()) < 5:
        missing.append("delivery address")

    # Make sure the AI didn't invent details: they must appear in what the CUSTOMER typed
    said = str(customer_text).lower()
    said_digits = re.sub(r"\D", "", said)
    phone_digits = re.sub(r"\D", "", str(phone))
    if phone_digits and phone_digits[-7:] not in said_digits:
        missing.append("the customer's real phone number (the one given was not typed by the customer)")
    name_words = [w for w in re.findall(r"[a-z]+", str(name).lower()) if len(w) > 1]
    if name_words and not any(w in said for w in name_words):
        missing.append("the customer's real name (the one given was not typed by the customer)")

    if missing:
        print("⚠️ Order NOT saved, missing:", ", ".join(missing))
        return ("NOT SAVED. Missing or invalid: " + ", ".join(missing)
                + ". Do NOT make up details. Politely ask the customer to type them, then confirm the order again.")

    items_text = ", ".join(line.split(" = ")[0] for line in lines)   # e.g. "3 x French curls, 3 x Italian curls"
    user_data = user_data if user_data is not None else {}
    now = datetime.now()

    # Did this customer already save an order in the last 24 hours? Then UPDATE that row.
    row = user_data.get("order_row")
    saved_at = user_data.get("order_time")
    same_row = False
    if row and saved_at and now - saved_at < timedelta(hours=24):
        # Safety check: make sure that row still holds THIS customer's order
        # (rows move if you delete or sort rows in the sheet)
        existing = sheet.row_values(row)
        digits = lambda p: re.sub(r"\D", "", str(p))[-7:]   # compare the last 7 digits only
        same_row = len(existing) >= 3 and digits(existing[2]) == digits(user_data.get("order_phone"))
    if same_row:
        sheet.update(range_name=f"A{row}:I{row}", values=[[
            now.strftime("%Y-%m-%d %H:%M"),
            name, phone, items_text, total_bundles, total, address, telegram_user, "New (updated)",
        ]])
        user_data["order_time"] = now
        user_data["order_phone"] = str(phone)
        print(f"✏️ Order UPDATED (row {row}): {name} - {items_text} = N{total:,}")
        return f"ORDER UPDATED: {items_text}. New total for hair: N{total:,} (delivery fee not included)."

    # Otherwise add a NEW row, and remember which row it is
    result = sheet.append_row([
        now.strftime("%Y-%m-%d %H:%M"),
        name, phone, items_text, total_bundles, total, address, telegram_user, "New",
    ])
    match = re.search(r"![A-Z]+(\d+)", str((result or {}).get("updates", {}).get("updatedRange", "")))
    if match:
        user_data["order_row"] = int(match.group(1))
        user_data["order_time"] = now
        user_data["order_phone"] = str(phone)
    print(f"✅ Order saved: {name} - {items_text} = N{total:,}")
    return f"ORDER SAVED: {items_text}. Total for hair: N{total:,} (delivery fee not included)."


def clean(text):
    # Telegram shows markdown symbols as plain text, so remove them
    return (text or "").replace("**", "").replace("__", "").replace("`", "").strip()


# --- 4. Ask the AI, and run any tools it asks for ---
def ask_ai(history, telegram_user, user_data, customer_text=""):
    """Returns (reply, notes). Notes record saved orders so the bot remembers them."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history
    notes = []
    # Everything the customer has typed (used to check the AI didn't invent details)
    customer_text = customer_text or " ".join(m["content"] for m in history if m["role"] == "user")

    for _ in range(3):  # the AI may use a tool, then answer
        response = client.chat.completions.create(
            model=MODEL, messages=messages, tools=TOOLS, tool_choice="auto", max_tokens=600,
        )
        msg = response.choices[0].message
        if not msg.tool_calls:
            return clean(msg.content), notes

        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ],
        })

        # If the AI tries to save the order in pieces, join the pieces into ONE order
        save_calls = [tc for tc in msg.tool_calls if tc.function.name == "save_order"]
        first_save = save_calls[0] if save_calls else None
        merged = None
        if save_calls:
            try:
                merged = json.loads(save_calls[0].function.arguments)
                for extra in save_calls[1:]:
                    merged["items"] = merged.get("items", []) + json.loads(extra.function.arguments).get("items", [])
            except Exception as e:
                print("Could not read order:", e)
                merged = None

        for tc in msg.tool_calls:
            try:
                if tc.function.name == "calculate_total":
                    result = calculate_total(json.loads(tc.function.arguments).get("items"))
                elif tc is first_save:
                    if merged is None:
                        result = "NOT SAVED. Could not read the order. Ask the customer to confirm again."
                    else:
                        result = save_order(merged.get("name"), merged.get("phone"),
                                            merged.get("items"), merged.get("address"),
                                            telegram_user, user_data, customer_text)
                        if result.startswith("ORDER"):
                            notes.append(result)
                else:
                    result = "Already included in the first save_order call."
            except Exception as e:
                print("Tool error:", e)
                result = "Something went wrong. Apologise and ask the customer to order on WhatsApp instead."
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    # Safety net: get a final answer without tools
    final = client.chat.completions.create(model=MODEL, messages=messages, max_tokens=400)
    return clean(final.choices[0].message.content), notes


# --- 5. Telegram handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["history"] = []
    context.user_data.pop("order_row", None)   # a fresh chat starts a fresh order
    context.user_data.pop("order_time", None)
    context.user_data.pop("order_phone", None)
    await update.message.reply_text(
        "Hey Queen 👑 Welcome to Kikiscurls and Co 💇🏾‍♀️\n"
        "We've got you covered with French curls, Italian curls and bone straight extensions. "
        "Ask me anything, or tell me what you'd like to order!"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    history = context.user_data.setdefault("history", [])
    history.append({"role": "user", "content": update.message.text})
    telegram_user = update.effective_user.username or update.effective_user.first_name or ""

    reply, notes = None, []
    for attempt in range(2):  # if the AI service hiccups, try once more
        try:
            customer_text = " ".join(m["content"] for m in history if m["role"] == "user")
            reply, notes = ask_ai(history[-14:], telegram_user, context.user_data, customer_text)
            break
        except Exception as e:
            print(f"AI error (attempt {attempt + 1}):", e)

    if not reply:
        history.pop()
        await update.message.reply_text("Sorry, I'm having trouble right now. Please try again in a moment.")
        return

    # Remember any saved order, so the bot never forgets or saves it twice
    for note in notes:
        history.append({"role": "system", "content": "Record: " + note})
    history.append({"role": "assistant", "content": reply})
    await update.message.reply_text(reply)


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # The bot can't see pictures, videos or voice notes, so it sends them to WhatsApp politely
    reply = (
        "Thank you! 😊 Please use this WhatsApp link to finalise your order colour: "
        + WHATSAPP_LINK + "\nA rep will attend to you shortly."
    )
    # Remember it in the conversation so the AI knows what happened
    history = context.user_data.setdefault("history", [])
    history.append({"role": "user", "content": "[The customer sent a picture, video, voice note or file]"})
    history.append({"role": "assistant", "content": reply})
    await update.message.reply_text(reply)


# --- 6. Start the bot ---
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    media = (filters.PHOTO | filters.VIDEO | filters.VOICE | filters.AUDIO
             | filters.Document.ALL | filters.Sticker.ALL)
    app.add_handler(MessageHandler(media, handle_media))
    print("Bot is running (with Google Sheets orders)... press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()