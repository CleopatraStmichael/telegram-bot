"""
Kikiscurls and Co — AI customer-service bot for Telegram
that ALSO takes orders and saves them to a Google Sheet.
"""

import os
import json
import re
from io import BytesIO
from urllib.parse import quote
from datetime import datetime, timedelta

import gspread
from dotenv import load_dotenv
from groq import Groq
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# --- 1. Keys and connections ---
load_dotenv()
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
WHATSAPP_LINK = os.environ["WHATSAPP_LINK"]
BANK_NAME = os.environ["BANK_NAME"]
ACCOUNT_NAME = os.environ["ACCOUNT_NAME"]
ACCOUNT_NUMBER = os.environ["ACCOUNT_NUMBER"]
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
If the customer just says thank you, okay, alright, noted, or similar after their order is saved,
do NOT call any tool. Just reply warmly, e.g. "You're welcome, Queen! 👑 We'll take it from here on WhatsApp.
If you need anything else, just let us know." 
After saving, reply with ONE short, warm sentence thanking them (e.g. "Thank you Queen, your order is in! 👑").
Do NOT repeat the order, total, bank details or WhatsApp link: the system automatically sends
the order summary, bank details, WhatsApp button and invoice right after your message.

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


def save_order(name, phone, items, address, telegram_user="", user_data=None, customer_text="", chat_id=""):
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
    # Details used for the summary message and the invoice
    order = {
        "name": name, "phone": phone, "address": address, "date": now.strftime("%d %b %Y, %I:%M %p"),
        "items": [(line.split(" x ")[1].split(" = ")[0], int(line.split(" x ")[0])) for line in lines],
        "total": total, "bundles": total_bundles,
    }

    # If nothing changed since the last save, don't save (or send the invoice) again
    last = user_data.get("last_order")
    simple = lambda t: re.sub(r"[^a-z0-9]", "", str(t).lower())     # ignore spaces, commas, capitals
    same_address = last and (simple(last["address"]) in simple(address) or simple(address) in simple(last["address"]))
    same_phone = last and re.sub(r"\D", "", str(last["phone"]))[-7:] == re.sub(r"\D", "", str(phone))[-7:]
    if last and sorted(last["items"]) == sorted(order["items"]) and same_phone and same_address:
        print(f"ℹ️ No change to order {user_data.get('order_id')}, not saved again")
        return "NO CHANGE: this exact order is already saved. Do not call save_order again; just reply kindly."

    if same_row:
        order["id"] = user_data.get("order_id") or f"KC{row:04d}"
        sheet.update(range_name=f"A{row}:K{row}", values=[[
            now.strftime("%Y-%m-%d %H:%M"),
            name, phone, items_text, total_bundles, total, address, telegram_user, "New (updated)", order["id"],
            str(chat_id),
        ]])
        user_data["order_time"] = now
        user_data["order_phone"] = str(phone)
        user_data["last_order"] = order
        print(f"✏️ Order UPDATED ({order['id']}, row {row}): {name} - {items_text} = N{total:,}")
        return f"ORDER UPDATED: {items_text}. New total for hair: N{total:,} (delivery fee not included)."

    # Otherwise add a NEW row, and remember which row it is
    result = sheet.append_row([
        now.strftime("%Y-%m-%d %H:%M"),
        name, phone, items_text, total_bundles, total, address, telegram_user, "New",
        "", str(chat_id),     # J = Order ID (filled in below), K = Chat ID (so we can message them later)
    ])
    match = re.search(r"![A-Z]+(\d+)", str((result or {}).get("updates", {}).get("updatedRange", "")))
    if match:
        row = int(match.group(1))
        order["id"] = f"KC{row:04d}"          # e.g. KC0007 (based on the row number)
        sheet.update(range_name=f"J{row}", values=[[order["id"]]])
        user_data["order_row"] = row
        user_data["order_time"] = now
        user_data["order_phone"] = str(phone)
    else:
        order["id"] = "KC" + now.strftime("%m%d%H%M")
    user_data["order_id"] = order["id"]
    user_data["last_order"] = order
    print(f"✅ Order saved ({order['id']}): {name} - {items_text} = N{total:,}")
    return f"ORDER SAVED: {items_text}. Total for hair: N{total:,} (delivery fee not included)."


# --- 3b. After an order: summary + bank details + WhatsApp button + PDF invoice ---
def whatsapp_link_with_order(order):
    """A WhatsApp link that opens a chat with the order summary already typed in."""
    lines = "\n".join(f"- {qty} x {product}" for product, qty in order["items"])
    text = (
        f"Hello Kikiscurls and Co! 👋\n"
        f"I just placed an order on Telegram.\n\n"
        f"Order ID: {order['id']}\n"
        f"Name: {order['name']}\n"
        f"Phone: {order['phone']}\n"
        f"Address: {order['address']}\n\n"
        f"Items:\n{lines}\n\n"
        f"Total for hair: N{order['total']:,}\n\n"
        f"Please send the delivery fee. I'll send my payment receipt here."
    )
    return WHATSAPP_LINK.split("?")[0] + "?text=" + quote(text)


def order_summary_text(order):
    lines = "\n".join(f"• {qty} x {product} = N{PRICES[product] * qty:,}" for product, qty in order["items"])
    return (
        f"🧾 ORDER SUMMARY ({order['id']})\n\n"
        f"{lines}\n\n"
        f"Total for hair: N{order['total']:,}\n"
        f"Delivery fee: to be confirmed on WhatsApp\n\n"
        f"💳 PAYMENT DETAILS (bank transfer)\n"
        f"Bank: {BANK_NAME}\n"
        f"Account name: {ACCOUNT_NAME}\n"
        f"Account number: {ACCOUNT_NUMBER}\n\n"
        f"👇 Tap the button below to send your order to us on WhatsApp. "
        f"We'll confirm your delivery fee, then send your payment receipt there with your Order ID."
    )


def make_invoice(order):
    """Builds a one-page PDF invoice and returns it as bytes."""
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    left, right = 50, width - 50
    y = height - 60

    # Header
    pdf.setFont("Helvetica-Bold", 20)
    pdf.drawString(left, y, "Kikiscurls and Co")
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawRightString(right, y, "INVOICE")
    y -= 18
    pdf.setFont("Helvetica", 10)
    pdf.drawString(left, y, "Braiding hair extensions  |  Online store  |  Nationwide delivery")
    y -= 30

    # Invoice info and customer
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(left, y, "Billed to:")
    pdf.drawRightString(right, y, f"Invoice no: {order['id']}")
    pdf.setFont("Helvetica", 11)
    for i, text in enumerate([order["name"], order["phone"], order["address"]]):
        pdf.drawString(left, y - 16 * (i + 1), str(text)[:70])
    pdf.drawRightString(right, y - 16, f"Date: {order['date']}")
    pdf.drawRightString(right, y - 32, "Status: UNPAID")
    y -= 80

    # Items table
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(left, y, "Item")
    pdf.drawRightString(330, y, "Bundles")
    pdf.drawRightString(430, y, "Price/bundle")
    pdf.drawRightString(right, y, "Amount")
    y -= 6
    pdf.line(left, y, right, y)
    pdf.setFont("Helvetica", 11)
    for product, qty in order["items"]:
        y -= 20
        pdf.drawString(left, y, product)
        pdf.drawRightString(330, y, str(qty))
        pdf.drawRightString(430, y, f"N{PRICES[product]:,}")
        pdf.drawRightString(right, y, f"N{PRICES[product] * qty:,}")
    y -= 10
    pdf.line(left, y, right, y)

    # Totals
    y -= 22
    pdf.setFont("Helvetica", 11)
    pdf.drawRightString(430, y, "Subtotal (hair):")
    pdf.drawRightString(right, y, f"N{order['total']:,}")
    y -= 18
    pdf.drawRightString(430, y, "Delivery fee:")
    pdf.drawRightString(right, y, "To be confirmed")
    y -= 22
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawRightString(430, y, "Total due (excl. delivery):")
    pdf.drawRightString(right, y, f"N{order['total']:,}")

    # Payment details
    y -= 50
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(left, y, "Payment details (bank transfer only)")
    pdf.setFont("Helvetica", 11)
    for i, text in enumerate([f"Bank: {BANK_NAME}", f"Account name: {ACCOUNT_NAME}",
                              f"Account number: {ACCOUNT_NUMBER}"]):
        pdf.drawString(left, y - 16 * (i + 1), text)
    y -= 80
    pdf.setFont("Helvetica-Oblique", 10)
    pdf.drawString(left, y, f"Please send your payment receipt on WhatsApp with your invoice number ({order['id']}).")
    pdf.drawString(left, y - 14, "Delivery: 2-3 business days within Lagos. Other states confirmed on WhatsApp.")
    pdf.drawString(left, y - 28, "Thank you for shopping with Kikiscurls and Co!")

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


async def send_order_extras(update: Update, order):
    """Sends the summary with bank details, a WhatsApp button, and the PDF invoice."""
    button = InlineKeyboardMarkup([[InlineKeyboardButton(
        "📲 Send my order on WhatsApp", url=whatsapp_link_with_order(order))]])
    await update.message.reply_text(order_summary_text(order), reply_markup=button)
    try:
        await update.message.reply_document(
            document=make_invoice(order), filename=f"Invoice-{order['id']}.pdf",
            caption=f"Your invoice for order {order['id']} 🧾",
        )
    except Exception as e:
        print("Invoice error:", e)


def clean(text):
    # Telegram shows markdown symbols as plain text, so remove them
    return (text or "").replace("**", "").replace("__", "").replace("`", "").strip()


# --- 4. Ask the AI, and run any tools it asks for ---
def ask_ai(history, telegram_user, user_data, customer_text="", chat_id=""):
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
                                            telegram_user, user_data, customer_text, chat_id)
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
    context.user_data.pop("order_id", None)
    context.user_data.pop("last_order", None)
    context.user_data.pop("last_sent", None)
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
            reply, notes = ask_ai(history[-14:], telegram_user, context.user_data, customer_text,
                                  update.effective_chat.id)
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

    # If an order was just saved or updated: send summary, bank details, WhatsApp button and invoice
    # (but never send the exact same summary twice)
    order = context.user_data.get("last_order")
    if notes and order:
        key = (order["id"], tuple(sorted(order["items"])), re.sub(r"[^a-z0-9]", "", str(order["address"]).lower()))
        if context.user_data.get("last_sent") != key:
            await send_order_extras(update, order)
            context.user_data["last_sent"] = key


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


# --- 5b. Watch the sheet: when you change Status, message the customer automatically ---
STATUS_MESSAGES = {
    "paid": "Hey Queen 👑 Payment received for order {id}! 🎉 Thank you. "
            "Your order is now being processed and we'll let you know when it's on its way.",
    "shipped": "Good news, Queen! 🚚 Your order {id} is on its way to you. "
               "Delivery takes 2-3 business days within Lagos; other states as confirmed on WhatsApp.",
    "delivered": "Your order {id} has been delivered! 💇🏾‍♀️ Thank you for shopping with Kikiscurls and Co. "
                 "We'd love to see your hairstyle, so feel free to share a picture with us on WhatsApp: " + WHATSAPP_LINK,
}


async def check_status_updates(context: ContextTypes.DEFAULT_TYPE):
    """Runs every minute. Columns: I = Status, J = Order ID, K = Chat ID, L = Notified (last status we messaged)."""
    try:
        rows = sheet.get_all_values()
    except Exception as e:
        print("Could not read sheet:", e)
        return
    for number, row in enumerate(rows[1:], start=2):     # skip the header row
        row = row + [""] * (12 - len(row))               # make sure columns I to L exist
        status = row[8].strip().lower()
        order_id, chat_id, notified = row[9], row[10].strip(), row[11].strip().lower()
        if status in STATUS_MESSAGES and chat_id and notified != status:
            try:
                await context.bot.send_message(chat_id=int(chat_id),
                                               text=STATUS_MESSAGES[status].format(id=order_id or "your order"))
                sheet.update(range_name=f"L{number}", values=[[status.capitalize()]])
                print(f"📣 Told {row[1]} their order {order_id} is {status}")
            except Exception as e:
                print(f"Could not message {row[1]} about order {order_id}:", e)


# --- 6. Start the bot ---
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    media = (filters.PHOTO | filters.VIDEO | filters.VOICE | filters.AUDIO
             | filters.Document.ALL | filters.Sticker.ALL)
    app.add_handler(MessageHandler(media, handle_media))
    app.job_queue.run_repeating(check_status_updates, interval=60, first=10)   # check the sheet every minute
    print("Bot is running (with Google Sheets orders)... press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()