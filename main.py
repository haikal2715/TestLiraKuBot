import os
import logging
import asyncio
import requests
import threading
import json
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from dotenv import load_dotenv
from http.server import HTTPServer, BaseHTTPRequestHandler

# Flask imports for keep alive
try:
    from flask import Flask
    FLASK_AVAILABLE = True
except ImportError:
    print("⚠️ Flask not found. Install with: pip install flask")
    FLASK_AVAILABLE = False

try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import (
        Application, CommandHandler, CallbackQueryHandler, 
        MessageHandler, filters, ContextTypes, ConversationHandler
    )
except ImportError as e:
    print(f"❌ Error importing telegram libraries: {e}")
    print("💡 Coba install ulang dengan: pip install --upgrade python-telegram-bot==20.3")
    exit(1)

# Import Google Sheets dependencies
try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    print("⚠️ Google Sheets dependencies not found. Install with: pip install gspread google-auth")
    gspread = None
    Credentials = None

# Load environment variables
load_dotenv()

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
BOT_TOKEN = os.getenv('BOT_TOKEN')
EXCHANGE_API_KEY = os.getenv('EXCHANGE_API_KEY')
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID')
ADMIN_IBAN = os.getenv('ADMIN_IBAN', 'TR1234567890123456789012345')
OWNER_USER_ID = int(os.getenv('OWNER_USER_ID', '0'))  # Add owner user ID to env

# Feature toggles
BUY_LIRA_ACTIVE = True
SELL_LIRA_ACTIVE = True

# Stock management (stored in memory, you can persist to file or database)
STOCK = {
    'lira': 0.0,    # TRY stock
    'rupiah': 2500000  # IDR stock - initial 2.5M
}

# File to persist stock data
STOCK_FILE = 'stock_data.json'

# Conversation states
(WAITING_BUY_AMOUNT, WAITING_BUY_NAME, WAITING_BUY_IBAN, WAITING_BUY_CONFIRMATION,
 WAITING_SELL_AMOUNT, WAITING_SELL_NAME, WAITING_SELL_ACCOUNT, WAITING_SELL_CONFIRMATION,
 WAITING_STOCK_UPDATE_CURRENCY, WAITING_STOCK_UPDATE_AMOUNT) = range(10)

# Google Sheets setup
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SERVICE_ACCOUNT_FILE = 'lirakubot.json'
SPREADSHEET_NAME = 'DATA LIRAKU.ID'

def load_stock():
    """Load stock from file"""
    global STOCK
    try:
        if os.path.exists(STOCK_FILE):
            with open(STOCK_FILE, 'r') as f:
                STOCK = json.load(f)
                logger.info(f"Stock loaded: {STOCK}")
    except Exception as e:
        logger.error(f"Error loading stock: {e}")
        # Use default stock if loading fails
        STOCK = {'lira': 0.0, 'rupiah': 2500000}

def save_stock():
    """Save stock to file"""
    try:
        with open(STOCK_FILE, 'w') as f:
            json.dump(STOCK, f)
        logger.info(f"Stock saved: {STOCK}")
    except Exception as e:
        logger.error(f"Error saving stock: {e}")

def update_stock(currency, amount):
    """Update stock and save to file"""
    global STOCK
    if currency in STOCK:
        STOCK[currency] += amount
        if STOCK[currency] < 0:
            STOCK[currency] = 0
        save_stock()

def check_stock_availability(currency, amount):
    """Check if stock is sufficient for transaction"""
    return STOCK.get(currency, 0) >= amount

# Flask app for keep alive
if FLASK_AVAILABLE:
    app = Flask(__name__)
    
    @app.route('/')
    def home():
        return "LiraKuBot is alive!"
    
    @app.route('/health')
    def health():
        return {
            "status": "healthy",
            "bot": "LiraKuBot",
            "timestamp": datetime.now().isoformat(),
            "uptime": "running"
        }
    
    def keep_alive():
        """Start Flask server in a separate thread for Replit keep-alive"""
        def run():
            # Use different port for Replit
            port = int(os.getenv('PORT', 8080))
            try:
                app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
            except Exception as e:
                logger.error(f"Flask server error: {e}")
        
        # Start Flask in daemon thread
        server_thread = threading.Thread(target=run, daemon=True)
        server_thread.start()
        logger.info(f"🌐 Flask keep-alive server started on port {os.getenv('PORT', 8080)}")
        print(f"🌐 Flask keep-alive server started - LiraKuBot is alive!")
        
        return server_thread
else:
    def keep_alive():
        logger.warning("Flask not available, keep-alive server not started")
        print("⚠️ Flask not available, keep-alive server not started")
        return None

def get_google_sheets_client():
    """Initialize Google Sheets client"""
    try:
        if not gspread or not Credentials:
            logger.warning("Google Sheets dependencies not available")
            return None

        creds = Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )
        return gspread.authorize(creds)
    except Exception as e:
        logger.error(f"Error initializing Google Sheets: {e}")
        return None

def get_exchange_rate(from_currency='IDR', to_currency='TRY'):
    """Get exchange rate from exchangerate-api"""
    try:
        url = f"https://v6.exchangerate-api.com/v6/{EXCHANGE_API_KEY}/pair/{from_currency}/{to_currency}"
        response = requests.get(url, timeout=10)
        data = response.json()

        if data['result'] == 'success':
            return float(data['conversion_rate'])
        else:
            logger.error(f"Exchange rate API error: {data}")
            return None
    except Exception as e:
        logger.error(f"Error fetching exchange rate: {e}")
        return None

def save_to_sheets(transaction_data):
    """Save transaction to Google Sheets"""
    try:
        gc = get_google_sheets_client()
        if not gc:
            logger.warning("Google Sheets not available, skipping save")
            return True  # Return True to not block the process

        sheet = gc.open(SPREADSHEET_NAME).sheet1

        # Add headers if sheet is empty
        if not sheet.get_all_records():
            headers = ['Waktu', 'Nama', 'IBAN/Rekening', 'IDR', 'TRY', 'Status', 'Username', 'User ID', 'Jenis']
            sheet.append_row(headers)

        sheet.append_row(transaction_data)
        return True
    except Exception as e:
        logger.error(f"Error saving to sheets: {e}")
        return True  # Return True to not block the process

def save_transaction(transaction_data):
    """Save transaction - wrapper function"""
    return save_to_sheets(transaction_data)

def get_main_keyboard():
    """Create main menu keyboard"""
    keyboard = [
        [InlineKeyboardButton("💸 Beli Lira", callback_data="buy_lira")],
        [InlineKeyboardButton("💵 Jual Lira", callback_data="sell_lira")],
        [InlineKeyboardButton("💱 Lihat Simulasi Kurs", callback_data="simulation")],
        [InlineKeyboardButton("📊 Cek Stok", callback_data="check_stock")],
        [InlineKeyboardButton("👤 Kontak Admin", callback_data="contact_admin")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_owner_keyboard():
    """Create owner menu keyboard"""
    keyboard = [
        [InlineKeyboardButton("💸 Beli Lira", callback_data="buy_lira")],
        [InlineKeyboardButton("💵 Jual Lira", callback_data="sell_lira")],
        [InlineKeyboardButton("💱 Lihat Simulasi Kurs", callback_data="simulation")],
        [InlineKeyboardButton("📊 Cek Stok", callback_data="check_stock")],
        [InlineKeyboardButton("⚙️ Update Stok", callback_data="update_stock")],
        [InlineKeyboardButton("👤 Kontak Admin", callback_data="contact_admin")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_back_menu_keyboard():
    """Create back and menu keyboard"""
    keyboard = [
        [InlineKeyboardButton("🔙 Kembali", callback_data="back")],
        [InlineKeyboardButton("🏠 Menu Utama", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_confirmation_keyboard():
    """Create confirmation keyboard"""
    keyboard = [
        [InlineKeyboardButton("✅ Data Sudah Benar", callback_data="confirm_transaction")],
        [InlineKeyboardButton("🔙 Kembali", callback_data="back")],
        [InlineKeyboardButton("🏠 Menu Utama", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_payment_keyboard():
    """Create payment confirmation keyboard"""
    keyboard = [
        [InlineKeyboardButton("✅ Saya sudah bayar", callback_data="payment_sent")],
        [InlineKeyboardButton("🔙 Kembali", callback_data="back")],
        [InlineKeyboardButton("🏠 Menu Utama", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_stock_update_keyboard():
    """Create stock update keyboard"""
    keyboard = [
        [InlineKeyboardButton("💰 Update Stok Rupiah", callback_data="update_rupiah")],
        [InlineKeyboardButton("🇹🇷 Update Stok Lira", callback_data="update_lira")],
        [InlineKeyboardButton("🔙 Kembali", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def format_currency(amount, currency='IDR'):
    """Format currency display"""
    if currency == 'IDR':
        return f"Rp{amount:,.0f}".replace(',', '.')
    elif currency == 'TRY':
        return f"₺{amount:,.2f}".replace(',', '.')
    return f"{amount:,.2f}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    user = update.effective_user
    
    welcome_message = (
        "💚 **Selamat datang di LiraKuBot!**\n\n"
        "✅ Proses cepat & aman\n"
        "✅ Langsung kirim ke IBAN\n"
        "✅ Lebih hemat dibanding beli di bandara & bank\n\n"
        "Silakan pilih menu:"
    )

    # Show different keyboard for owner
    keyboard = get_owner_keyboard() if user.id == OWNER_USER_ID else get_main_keyboard()
    
    await update.message.reply_text(
        welcome_message,
        reply_markup=keyboard,
        parse_mode='Markdown'
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks"""
    query = update.callback_query
    await query.answer()
    user = query.from_user

    if query.data == "main_menu":
        welcome_message = (
            "💚 **Selamat datang di LiraKuBot!**\n\n"
            "✅ Proses cepat & aman\n"
            "✅ Langsung kirim ke IBAN\n"
            "✅ Lebih hemat dibanding beli di bandara & bank\n\n"
            "Silakan pilih menu:"
        )
        
        keyboard = get_owner_keyboard() if user.id == OWNER_USER_ID else get_main_keyboard()
        
        await query.edit_message_text(
            welcome_message,
            reply_markup=keyboard,
            parse_mode='Markdown'
        )
        return ConversationHandler.END

    elif query.data == "buy_lira":
        if not BUY_LIRA_ACTIVE:
            await query.edit_message_text(
                "❌ Maaf, pembelian Lira sedang tidak tersedia.",
                reply_markup=get_back_menu_keyboard()
            )
            return ConversationHandler.END

        # Check rupiah stock
        if STOCK['rupiah'] <= 0:
            await query.edit_message_text(
                "❌ Maaf, stok Rupiah sedang habis. Silakan coba lagi nanti atau hubungi admin.",
                reply_markup=get_back_menu_keyboard()
            )
            return ConversationHandler.END

        # Calculate available stock info
        idr_to_try_rate = get_exchange_rate('IDR', 'TRY')
        if idr_to_try_rate:
            max_try_available = STOCK['rupiah'] * idr_to_try_rate * 0.975
            stock_info = f"\n📊 **Stok tersedia:** {format_currency(STOCK['rupiah'])} (≈ ₺{max_try_available:,.2f})"
        else:
            stock_info = f"\n📊 **Stok tersedia:** {format_currency(STOCK['rupiah'])}"

        await query.edit_message_text(
            "💸 **Beli Lira (IDR ke TRY)**\n\n"
            "Masukkan nominal dalam Rupiah yang ingin dikonversi ke Lira Turki.\n"
            "Minimal pembelian: Rp100.000\n"
            f"{stock_info}\n\n"
            "Contoh: 500000",
            reply_markup=get_back_menu_keyboard(),
            parse_mode='Markdown'
        )
        return WAITING_BUY_AMOUNT

    elif query.data == "sell_lira":
        if not SELL_LIRA_ACTIVE:
            await query.edit_message_text(
                "❌ Maaf, penjualan Lira sedang tidak tersedia.",
                reply_markup=get_back_menu_keyboard()
            )
            return ConversationHandler.END

        # Show available lira stock
        stock_info = f"\n📊 **Stok Lira tersedia:** ₺{STOCK['lira']:,.2f}"

        await query.edit_message_text(
            "💵 **Jual Lira (TRY ke IDR)**\n\n"
            "Masukkan jumlah Lira Turki yang ingin dijual.\n"
            f"{stock_info}\n\n"
            "Contoh: 100",
            reply_markup=get_back_menu_keyboard(),
            parse_mode='Markdown'
        )
        return WAITING_SELL_AMOUNT

    elif query.data == "simulation":
        await show_simulation(query)

    elif query.data == "check_stock":
        await show_stock_info(query)

    elif query.data == "update_stock":
        if user.id != OWNER_USER_ID:
            await query.edit_message_text(
                "❌ Anda tidak memiliki akses untuk fitur ini.",
                reply_markup=get_back_menu_keyboard()
            )
            return ConversationHandler.END

        await query.edit_message_text(
            f"⚙️ **Update Stok**\n\n"
            f"📊 **Stok saat ini:**\n"
            f"💰 Rupiah: {format_currency(STOCK['rupiah'])}\n"
            f"🇹🇷 Lira: ₺{STOCK['lira']:,.2f}\n\n"
            f"Pilih mata uang yang ingin diupdate:",
            reply_markup=get_stock_update_keyboard(),
            parse_mode='Markdown'
        )

    elif query.data == "update_rupiah":
        if user.id != OWNER_USER_ID:
            await query.edit_message_text(
                "❌ Anda tidak memiliki akses untuk fitur ini.",
                reply_markup=get_back_menu_keyboard()
            )
            return ConversationHandler.END

        context.user_data['update_currency'] = 'rupiah'
        await query.edit_message_text(
            f"💰 **Update Stok Rupiah**\n\n"
            f"Stok saat ini: {format_currency(STOCK['rupiah'])}\n\n"
            f"Masukkan jumlah untuk **MENAMBAH** stok (gunakan angka negatif untuk mengurangi):\n"
            f"Contoh: 1000000 (menambah 1 juta)\n"
            f"Contoh: -500000 (mengurangi 500 ribu)",
            reply_markup=get_back_menu_keyboard(),
            parse_mode='Markdown'
        )
        return WAITING_STOCK_UPDATE_AMOUNT

    elif query.data == "update_lira":
        if user.id != OWNER_USER_ID:
            await query.edit_message_text(
                "❌ Anda tidak memiliki akses untuk fitur ini.",
                reply_markup=get_back_menu_keyboard()
            )
            return ConversationHandler.END

        context.user_data['update_currency'] = 'lira'
        await query.edit_message_text(
            f"🇹🇷 **Update Stok Lira**\n\n"
            f"Stok saat ini: ₺{STOCK['lira']:,.2f}\n\n"
            f"Masukkan jumlah untuk **MENAMBAH** stok (gunakan angka negatif untuk mengurangi):\n"
            f"Contoh: 1000 (menambah 1000 Lira)\n"
            f"Contoh: -500 (mengurangi 500 Lira)",
            reply_markup=get_back_menu_keyboard(),
            parse_mode='Markdown'
        )
        return WAITING_STOCK_UPDATE_AMOUNT

    elif query.data == "contact_admin":
        contact_message = (
            "👤 **Kontak Admin**\n\n"
            "📱 Telegram: @lirakuid\n"
            "📞 WhatsApp: 087773834406"
        )
        await query.edit_message_text(
            contact_message,
            reply_markup=get_back_menu_keyboard(),
            parse_mode='Markdown'
        )

    elif query.data == "confirm_transaction":
        await handle_transaction_confirmation(update, context)

    elif query.data == "payment_sent":
        await handle_payment_confirmation(update, context)

    elif query.data == "sell_sent":
        await handle_sell_confirmation(update, context)

    elif query.data == "back":
        await handle_back_navigation(update, context)

async def handle_stock_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle stock update input"""
    try:
        amount = float(update.message.text.replace(',', '.'))
        currency = context.user_data.get('update_currency')
        
        if not currency:
            await update.message.reply_text(
                "❌ Error: mata uang tidak ditemukan. Silakan mulai ulang.",
                reply_markup=get_back_menu_keyboard()
            )
            return ConversationHandler.END

        old_stock = STOCK[currency]
        new_stock = old_stock + amount
        
        if new_stock < 0:
            await update.message.reply_text(
                f"❌ Error: Stok tidak bisa negatif.\n"
                f"Stok saat ini: {format_currency(old_stock) if currency == 'rupiah' else f'₺{old_stock:,.2f}'}\n"
                f"Pengurangan maksimal: {format_currency(old_stock) if currency == 'rupiah' else f'₺{old_stock:,.2f}'}",
                reply_markup=get_back_menu_keyboard(),
                parse_mode='Markdown'
            )
            return WAITING_STOCK_UPDATE_AMOUNT

        # Update stock
        STOCK[currency] = new_stock
        save_stock()

        currency_symbol = "💰" if currency == 'rupiah' else "🇹🇷"
        old_formatted = format_currency(old_stock) if currency == 'rupiah' else f"₺{old_stock:,.2f}"
        new_formatted = format_currency(new_stock) if currency == 'rupiah' else f"₺{new_stock:,.2f}"
        change_formatted = format_currency(amount) if currency == 'rupiah' else f"₺{amount:,.2f}"

        success_message = (
            f"✅ **Stok {currency_symbol} berhasil diupdate!**\n\n"
            f"📊 **Perubahan:**\n"
            f"Stok lama: {old_formatted}\n"
            f"Perubahan: {'+' if amount >= 0 else ''}{change_formatted}\n"
            f"Stok baru: {new_formatted}\n\n"
            f"📅 Update: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
        )

        await update.message.reply_text(
            success_message,
            reply_markup=get_back_menu_keyboard(),
            parse_mode='Markdown'
        )

        # Clear user data
        context.user_data.clear()
        return ConversationHandler.END

    except ValueError:
        await update.message.reply_text(
            "❌ Format angka tidak valid. Masukkan angka saja.\n"
            "Contoh: 1000000 atau -500000",
            reply_markup=get_back_menu_keyboard()
        )
        return WAITING_STOCK_UPDATE_AMOUNT

async def show_stock_info(query):
    """Show current stock information"""
    # Get exchange rates for conversion info
    idr_to_try_rate = get_exchange_rate('IDR', 'TRY')
    try_to_idr_rate = get_exchange_rate('TRY', 'IDR')

    stock_message = f"📊 **Informasi Stok**\n\n"
    
    # Rupiah stock info
    stock_message += f"💰 **Stok Rupiah:** {format_currency(STOCK['rupiah'])}\n"
    if idr_to_try_rate and STOCK['rupiah'] > 0:
        equivalent_try = STOCK['rupiah'] * idr_to_try_rate * 0.975
        stock_message += f"   ≈ ₺{equivalent_try:,.2f} (setelah margin)\n\n"
    else:
        stock_message += "\n"

    # Lira stock info
    stock_message += f"🇹🇷 **Stok Lira:** ₺{STOCK['lira']:,.2f}\n"
    if try_to_idr_rate and STOCK['lira'] > 0:
        equivalent_idr = STOCK['lira'] * try_to_idr_rate * 0.975
        stock_message += f"   ≈ {format_currency(equivalent_idr)} (setelah margin)\n\n"
    else:
        stock_message += "\n"

    stock_message += f"⏰ **Update:** {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
    stock_message += f"💹 **Margin:** 2.5% tersembunyi dalam kurs"

    await query.edit_message_text(
        stock_message,
        reply_markup=get_back_menu_keyboard(),
        parse_mode='Markdown'
    )

async def handle_back_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle back navigation"""
    query = update.callback_query
    current_state = context.user_data.get('current_state', None)
    user = query.from_user

    if current_state == 'buy_amount':
        await query.edit_message_text(
            "💸 **Beli Lira (IDR ke TRY)**\n\n"
            "Masukkan nominal dalam Rupiah yang ingin dikonversi ke Lira Turki.\n"
            "Minimal pembelian: Rp100.000\n\n"
            "Contoh: 500000",
            reply_markup=get_back_menu_keyboard(),
            parse_mode='Markdown'
        )
        return WAITING_BUY_AMOUNT
    elif current_state == 'buy_name':
        await query.edit_message_text(
            "💸 **Beli Lira (IDR ke TRY)**\n\n"
            "Masukkan nominal dalam Rupiah yang ingin dikonversi ke Lira Turki.\n"
            "Minimal pembelian: Rp100.000\n\n"
            "Contoh: 500000",
            reply_markup=get_back_menu_keyboard(),
            parse_mode='Markdown'
        )
        context.user_data['current_state'] = 'buy_amount'
        return WAITING_BUY_AMOUNT
    elif current_state == 'buy_iban':
        await query.edit_message_text(
            f"💰 **Estimasi Konversi**\n\n"
            f"💸 Nominal: {format_currency(context.user_data.get('buy_amount_idr', 0))}\n"
            f"🇹🇷 Estimasi TRY: ₺{context.user_data.get('buy_estimated_try', 0):.2f}\n\n"
            f"Masukkan nama lengkap sesuai IBAN Anda:",
            reply_markup=get_back_menu_keyboard(),
            parse_mode='Markdown'
        )
        context.user_data['current_state'] = 'buy_name'
        return WAITING_BUY_NAME
    elif current_state == 'buy_confirmation':
        await query.edit_message_text(
            f"👤 Nama: **{context.user_data.get('buy_name', '')}**\n\n"
            f"Masukkan IBAN Turki Anda (format: TR + 24 angka)\n"
            f"Contoh: `TR123456789012345678901234`",
            reply_markup=get_back_menu_keyboard(),
            parse_mode='Markdown'
        )
        context.user_data['current_state'] = 'buy_iban'
        return WAITING_BUY_IBAN
    elif current_state == 'sell_amount':
        await query.edit_message_text(
            "💵 **Jual Lira (TRY ke IDR)**\n\n"
            "Masukkan jumlah Lira Turki yang ingin dijual.\n\n"
            "Contoh: 100",
            reply_markup=get_back_menu_keyboard(),
            parse_mode='Markdown'
        )
        return WAITING_SELL_AMOUNT
    elif current_state == 'sell_name':
        await query.edit_message_text(
            "💵 **Jual Lira (TRY ke IDR)**\n\n"
            "Masukkan jumlah Lira Turki yang ingin dijual.\n\n"
            "Contoh: 100",
            reply_markup=get_back_menu_keyboard(),
            parse_mode='Markdown'
        )
        context.user_data['current_state'] = 'sell_amount'
        return WAITING_SELL_AMOUNT
    elif current_state == 'sell_account':
        await query.edit_message_text(
            f"💰 **Estimasi Konversi**\n\n"
            f"🇹🇷 Lira: ₺{context.user_data.get('sell_amount_try', 0):,.2f}\n"
            f"💵 Estimasi IDR: {format_currency(context.user_data.get('sell_estimated_idr_gross', 0))}\n\n"
            f"Masukkan nama lengkap Anda:",
            reply_markup=get_back_menu_keyboard(),
            parse_mode='Markdown'
        )
        context.user_data['current_state'] = 'sell_name'
        return WAITING_SELL_NAME
    elif current_state == 'sell_confirmation':
        await query.edit_message_text(
            f"👤 Nama: **{context.user_data.get('sell_name', '')}**\n\n"
            "Masukkan nomor rekening bank Indonesia Anda.\n"
            "Format: [Nama Bank] - [Nomor Rekening]\n"
            "Contoh: `BCA - 1234567890`",
            reply_markup=get_back_menu_keyboard(),
            parse_mode='Markdown'
        )
        context.user_data['current_state'] = 'sell_account'
        return WAITING_SELL_ACCOUNT
    else:
        # Default back to main menu
        welcome_message = (
            "💚 **Selamat datang di LiraKuBot!**\n\n"
            "✅ Proses cepat & aman\n"
            "✅ Langsung kirim ke IBAN\n"
            "✅ Lebih hemat dibanding beli di bandara & bank\n\n"
            "Silakan pilih menu:"
        )
        
        keyboard = get_owner_keyboard() if user.id == OWNER_USER_ID else get_main_keyboard()
        
        await query.edit_message_text(
            welcome_message,
            reply_markup=keyboard,
            parse_mode='Markdown'
        )
        return ConversationHandler.END

async def show_simulation(query):
    """Show exchange rate simulation"""
    idr_to_try_rate = get_exchange_rate('IDR', 'TRY')
    try_to_idr_rate = get_exchange_rate('TRY', 'IDR')

    if not idr_to_try_rate or not try_to_idr_rate:
        await query.edit_message_text(
            "❌ Gagal mengambil data kurs. Silakan coba lagi.",
            reply_markup=get_back_menu_keyboard()
        )
        return

    # Calculate simulation values with 2.5% margin (hidden from user)
    simulation_message = (
        "💱 **Simulasi Tukar IDR ke TRY**\n"
        f"💸 Rp100.000 → 🇹🇷 ₺{(100000 * idr_to_try_rate * 0.975):.2f}\n"
        f"💸 Rp500.000 → 🇹🇷 ₺{(500000 * idr_to_try_rate * 0.975):.2f}\n"
        f"💸 Rp1.000.000 → 🇹🇷 ₺{(1000000 * idr_to_try_rate * 0.975):.2f}\n\n"
        "💱 **Simulasi Tukar TRY ke IDR**\n"
        f"🇹🇷 ₺100 → {format_currency(100 * try_to_idr_rate * 0.975)}\n"
        f"🇹🇷 ₺500 → {format_currency(500 * try_to_idr_rate * 0.975)}\n"
        f"🇹🇷 ₺1.000 → {format_currency(1000 * try_to_idr_rate * 0.975)}\n\n"
        f"*Update: {datetime.now().strftime('%H:%M %d/%m/%Y')}*"
    )

    await query.edit_message_text(
        simulation_message,
        reply_markup=get_back_menu_keyboard(),
        parse_mode='Markdown'
    )

async def handle_buy_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle buy amount input"""
    try:
        amount = int(update.message.text.replace('.', '').replace(',', ''))

        if amount < 100000:
            await update.message.reply_text(
                "❌ Minimal pembelian adalah Rp100.000\n"
                "Silakan masukkan nominal yang valid.",
                reply_markup=get_back_menu_keyboard()
            )
            return WAITING_BUY_AMOUNT

        # Check stock availability
        if amount > STOCK['rupiah']:
            await update.message.reply_text(
                f"❌ Stok tidak mencukupi!\n"
                f"Stok tersedia: {format_currency(STOCK['rupiah'])}\n"
                f"Nominal diminta: {format_currency(amount)}\n\n"
                f"Silakan masukkan nominal yang lebih kecil atau hubungi admin.",
                reply_markup=get_back_menu_keyboard(),
                parse_mode='Markdown'
            )
            return WAITING_BUY_AMOUNT

        # Get exchange rate
        base_rate = get_exchange_rate('IDR', 'TRY')
        if not base_rate:
            await update.message.reply_text(
                "❌ Gagal mengambil data kurs. Silakan coba lagi.",
                reply_markup=get_back_menu_keyboard()
            )
            return WAITING_BUY_AMOUNT

        # Calculate TRY with 2.5% margin (hidden from user)
        estimated_try = amount * base_rate * 0.975

        # Store in context
        context.user_data['buy_amount_idr'] = amount
        context.user_data['buy_estimated_try'] = estimated_try
        context.user_data['current_state'] = 'buy_name'

        await update.message.reply_text(
            f"💰 **Estimasi Konversi**\n\n"
            f"💸 Nominal: {format_currency(amount)}\n"
            f"🇹🇷 Estimasi TRY: ₺{estimated_try:.2f}\n\n"
            f"Masukkan nama lengkap sesuai IBAN Anda:",
            reply_markup=get_back_menu_keyboard(),
            parse_mode='Markdown'
        )
        return WAITING_BUY_NAME

    except ValueError:
        await update.message.reply_text(
            "❌ Format nominal tidak valid. Masukkan angka saja.\n"
            "Contoh: 500000",
            reply_markup=get_back_menu_keyboard()
        )
        return WAITING_BUY_AMOUNT

async def handle_buy_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle buy name input"""
    name = update.message.text.strip()

    if len(name) < 2:
        await update.message.reply_text(
            "❌ Nama terlalu pendek. Masukkan nama lengkap yang valid.",
            reply_markup=get_back_menu_keyboard()
        )
        return WAITING_BUY_NAME

    context.user_data['buy_name'] = name
    context.user_data['current_state'] = 'buy_iban'

    await update.message.reply_text(
        f"👤 Nama: **{name}**\n\n"
        f"Masukkan IBAN Turki Anda (format: TR + 24 angka)\n"
        f"Contoh: `TR123456789012345678901234`",
        reply_markup=get_back_menu_keyboard(),
        parse_mode='Markdown'
    )
    return WAITING_BUY_IBAN

async def handle_buy_iban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle buy IBAN input"""
    iban = update.message.text.strip().upper().replace(' ', '')

    # IBAN validation
    if not iban.startswith('TR'):
        await update.message.reply_text(
            "❌ IBAN harus dimulai dengan 'TR' untuk Turki.\n"
            "Contoh: `TR123456789012345678901234`",
            reply_markup=get_back_menu_keyboard(),
            parse_mode='Markdown'
        )
        return WAITING_BUY_IBAN
    
    if len(iban) < 24:
        await update.message.reply_text(
            f"❌ IBAN terlalu pendek.\n"
            f"📏 Panjang saat ini: {len(iban)} karakter\n"
            f"📏 Minimal: 24 karakter\n"
            f"📏 Standar Turki: 26 karakter (TR + 24 angka)\n\n"
            f"Contoh: `TR123456789012345678901234`",
            reply_markup=get_back_menu_keyboard(),
            parse_mode='Markdown'
        )
        return WAITING_BUY_IBAN
    
    if len(iban) > 28:
        await update.message.reply_text(
            f"❌ IBAN terlalu panjang.\n"
            f"📏 Panjang saat ini: {len(iban)} karakter\n"
            f"📏 Maksimal: 28 karakter\n"
            f"📏 Standar Turki: 26 karakter (TR + 24 angka)\n\n"
            f"Contoh: `TR123456789012345678901234`",
            reply_markup=get_back_menu_keyboard(),
            parse_mode='Markdown'
        )
        return WAITING_BUY_IBAN
    
    if not iban[2:].isdigit():
        await update.message.reply_text(
            "❌ IBAN harus berupa 'TR' diikuti angka saja.\n"
            "Tidak boleh ada huruf setelah 'TR'.\n\n"
            f"Contoh: `TR123456789012345678901234`",
            reply_markup=get_back_menu_keyboard(),
            parse_mode='Markdown'
        )
        return WAITING_BUY_IBAN

    context.user_data['buy_iban'] = iban
    context.user_data['current_state'] = 'buy_confirmation'

    # Show confirmation (no admin fee anymore)
    amount = context.user_data['buy_amount_idr']
    estimated_try = context.user_data['buy_estimated_try']

    confirmation_message = (
        "📋 **Konfirmasi Detail Pembelian**\n\n"
        f"👤 **Nama:** {context.user_data['buy_name']}\n"
        f"🏦 **IBAN:** `{iban}`\n"
        f"💸 **Total pembayaran:** {format_currency(amount)}\n"
        f"🇹🇷 **TRY yang diterima:** ₺{estimated_try:.2f}\n\n"
        f"Apakah data sudah benar?"
    )

    await update.message.reply_text(
        confirmation_message,
        reply_markup=get_confirmation_keyboard(),
        parse_mode='Markdown'
    )
    return WAITING_BUY_CONFIRMATION

async def handle_sell_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle sell amount input"""
    try:
        amount = float(update.message.text.replace(',', '.'))

        if amount <= 0:
            await update.message.reply_text(
                "❌ Jumlah harus lebih dari 0.\n"
                "Silakan masukkan jumlah yang valid.",
                reply_markup=get_back_menu_keyboard()
            )
            return WAITING_SELL_AMOUNT

        # Check if we have enough lira stock (for selling, we need lira stock)
        if amount > STOCK['lira']:
            await update.message.reply_text(
                f"❌ Stok Lira tidak mencukupi!\n"
                f"Stok tersedia: ₺{STOCK['lira']:,.2f}\n"
                f"Jumlah diminta: ₺{amount:,.2f}\n\n"
                f"Silakan masukkan jumlah yang lebih kecil atau hubungi admin.",
                reply_markup=get_back_menu_keyboard(),
                parse_mode='Markdown'
            )
            return WAITING_SELL_AMOUNT

        # Get exchange rate
        base_rate = get_exchange_rate('TRY', 'IDR')
        if not base_rate:
            await update.message.reply_text(
                "❌ Gagal mengambil data kurs. Silakan coba lagi.",
                reply_markup=get_back_menu_keyboard()
            )
            return WAITING_SELL_AMOUNT

        # Calculate with margin 2.5% (hidden from user)
        estimated_idr = amount * base_rate * 0.975

        # Store in context
        context.user_data['sell_amount_try'] = amount
        context.user_data['sell_estimated_idr'] = estimated_idr
        context.user_data['current_state'] = 'sell_name'

        await update.message.reply_text(
            f"💰 **Estimasi Konversi**\n\n"
            f"🇹🇷 Lira: ₺{amount:,.2f}\n"
            f"💵 Estimasi IDR: {format_currency(estimated_idr)}\n\n"
            f"Masukkan nama lengkap Anda:",
            reply_markup=get_back_menu_keyboard(),
            parse_mode='Markdown'
        )
        return WAITING_SELL_NAME

    except ValueError:
        await update.message.reply_text(
            "❌ Format jumlah tidak valid. Masukkan angka saja.\n"
            "Contoh: 100 atau 100.50",
            reply_markup=get_back_menu_keyboard()
        )
        return WAITING_SELL_AMOUNT

async def handle_sell_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle sell name input"""
    name = update.message.text.strip()

    if len(name) < 2:
        await update.message.reply_text(
            "❌ Nama terlalu pendek. Masukkan nama lengkap yang valid.",
            reply_markup=get_back_menu_keyboard()
        )
        return WAITING_SELL_NAME

    context.user_data['sell_name'] = name
    context.user_data['current_state'] = 'sell_account'

    await update.message.reply_text(
        f"👤 Nama: **{name}**\n\n"
        "Masukkan nomor rekening bank Indonesia Anda.\n"
        "Format: [Nama Bank] - [Nomor Rekening]\n"
        "Contoh: `BCA - 1234567890`",
        reply_markup=get_back_menu_keyboard(),
        parse_mode='Markdown'
    )
    return WAITING_SELL_ACCOUNT

async def handle_sell_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle sell account input"""
    account = update.message.text.strip()

    if len(account) < 5 or '-' not in account:
        await update.message.reply_text(
            "❌ Format rekening tidak valid.\n"
            "Format: [Nama Bank] - [Nomor Rekening]\n"
            "Contoh: `BCA - 1234567890`",
            reply_markup=get_back_menu_keyboard(),
            parse_mode='Markdown'
        )
        return WAITING_SELL_ACCOUNT

    context.user_data['sell_account'] = account
    context.user_data['current_state'] = 'sell_confirmation'

    # Show confirmation (no admin fee)
    amount = context.user_data['sell_amount_try']
    estimated_idr = context.user_data['sell_estimated_idr']

    confirmation_message = (
        "📋 **Konfirmasi Detail Penjualan**\n\n"
        f"👤 **Nama:** {context.user_data['sell_name']}\n"
        f"🏦 **Rekening:** `{account}`\n"
        f"🪙 **TRY yang dikirim:** ₺{amount:,.2f}\n"
        f"💰 **IDR yang diterima:** {format_currency(estimated_idr)}\n\n"
        f"Apakah data sudah benar?"
    )

    await update.message.reply_text(
        confirmation_message,
        reply_markup=get_confirmation_keyboard(),
        parse_mode='Markdown'
    )
    return WAITING_SELL_CONFIRMATION

async def handle_transaction_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle transaction confirmation"""
    query = update.callback_query
    current_state = context.user_data.get('current_state')

    if current_state == 'buy_confirmation':
        # Show payment details for buy transaction
        amount = context.user_data['buy_amount_idr']
        estimated_try = context.user_data['buy_estimated_try']
        iban = context.user_data['buy_iban']

        payment_message = (
            "💳 **Detail Pembayaran**\n\n"
            f"👤 **Nama:** {context.user_data['buy_name']}\n"
            f"🏦 **IBAN:** `{iban}`\n"
            f"🇹🇷 **TRY yang diterima:** ₺{estimated_try:.2f}\n"
            f"💰 **Total pembayaran:** {format_currency(amount)}\n\n"
            f"💳 **Transfer ke:**\n"
            f"🏦 Bank: BCA\n"
            f"💳 Rekening: `7645257260`\n"
            f"👤 a.n. Muhammad Haikal Sutanto\n\n"
            f"Setelah transfer, klik tombol di bawah:"
        )

        await query.edit_message_text(
            payment_message,
            reply_markup=get_payment_keyboard(),
            parse_mode='Markdown'
        )

    elif current_state == 'sell_confirmation':
        # Show transfer details for sell transaction
        amount = context.user_data['sell_amount_try']
        estimated_idr = context.user_data['sell_estimated_idr']
        account = context.user_data['sell_account']

        transfer_message = (
            "💸 **Detail Transfer Lira**\n\n"
            f"👤 **Nama:** {context.user_data['sell_name']}\n"
            f"🏦 **Rekening Anda:** `{account}`\n"
            f"🪙 **TRY yang dikirim:** ₺{amount:,.2f}\n"
            f"💰 **IDR yang diterima:** {format_currency(estimated_idr)}\n\n"
            f"🏦 **Kirim Lira ke IBAN Admin:**\n"
            f"`{ADMIN_IBAN}`\n\n"
            f"Setelah mengirim, klik tombol di bawah:"
        )

        keyboard = [
            [InlineKeyboardButton("✅ Saya sudah kirim", callback_data="sell_sent")],
            [InlineKeyboardButton("🔙 Kembali", callback_data="back")],
            [InlineKeyboardButton("🏠 Menu Utama", callback_data="main_menu")]
        ]

        await query.edit_message_text(
            transfer_message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )

    return ConversationHandler.END

async def handle_payment_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle payment confirmation"""
    query = update.callback_query
    user = query.from_user

    # Check if we have the necessary data
    if not all(key in context.user_data for key in ['buy_name', 'buy_iban', 'buy_amount_idr', 'buy_estimated_try']):
        await query.edit_message_text(
            "❌ Data transaksi tidak lengkap. Silakan mulai transaksi baru.",
            reply_markup=get_main_keyboard()
        )
        return

    # Update stock - reduce rupiah
    amount_idr = context.user_data['buy_amount_idr']
    update_stock('rupiah', -amount_idr)
    logger.info(f"Stock updated: Reduced {amount_idr} IDR, remaining: {STOCK['rupiah']}")

    # Prepare transaction data
    now = datetime.now()
    transaction_data = [
        now.strftime('%Y-%m-%d %H:%M:%S'),
        context.user_data.get('buy_name', ''),
        context.user_data.get('buy_iban', ''),
        context.user_data.get('buy_amount_idr', 0),
        round(context.user_data.get('buy_estimated_try', 0), 2),
        'Menunggu Konfirmasi',
        user.username or '',
        str(user.id),
        'Beli Lira'
    ]

    # Save transaction
    save_success = save_transaction(transaction_data)

    # Send notification to admin (show margin details for admin)
    admin_message = (
        "🔔 **PESANAN MASUK - Beli Lira**\n\n"
        f"👤 **Nama:** {context.user_data.get('buy_name', '')}\n"
        f"🆔 **Username:** @{user.username or 'Tidak ada'}\n"
        f"🆔 **User ID:** {user.id}\n"
        f"🏦 **IBAN:** `{context.user_data.get('buy_iban', '')}`\n"
        f"💰 **Total pembayaran:** {format_currency(context.user_data.get('buy_amount_idr', 0))}\n"
        f"🇹🇷 **TRY Dikirim:** ₺{context.user_data.get('buy_estimated_try', 0):.2f}\n"
        f"📊 **Margin tersembunyi:** 2.5% dari konversi\n"
        f"📦 **Stok Rupiah tersisa:** {format_currency(STOCK['rupiah'])}\n"
        f"⏰ **Waktu:** {now.strftime('%d/%m/%Y %H:%M:%S')}\n"
        f"💾 **Status Simpan:** {'✅ Berhasil' if save_success else '❌ Gagal'}\n\n"
        f"**Silakan verifikasi pembayaran dan proses transaksi ini.**"
    )

    try:
        if ADMIN_CHAT_ID:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=admin_message,
                parse_mode='Markdown'
            )
            logger.info(f"Admin notification sent for buy transaction from user {user.id}")
        else:
            logger.warning("ADMIN_CHAT_ID not configured, admin notification not sent")
    except Exception as e:
        logger.error(f"Error sending admin notification: {e}")

    # Send confirmation to user (no margin mentioned)
    await query.edit_message_text(
        "✅ **Konfirmasi Pembayaran Diterima!**\n\n"
        "Terima kasih! Transaksi Anda sedang diproses.\n"
        "Admin akan segera memverifikasi pembayaran dan mengirim Lira ke IBAN Anda.\n\n"
        f"🏦 **Detail Transfer Anda:**\n"
        f"💳 Rekening: `7645257260` (BCA)\n"
        f"👤 a.n. Muhammad Haikal Sutanto\n"
        f"💰 Jumlah: {format_currency(context.user_data.get('buy_amount_idr', 0))}\n\n"
        f"🇹🇷 **IBAN Tujuan:** `{context.user_data.get('buy_iban', '')}`\n"
        f"₺ **TRY yang akan diterima:** ₺{context.user_data.get('buy_estimated_try', 0):.2f}\n\n"
        "📱 **Estimasi Waktu Proses:** 5-15 menit\n"
        "💬 **Jika ada pertanyaan:** @lirakuid\n\n"
        "Kami akan mengirim notifikasi setelah transfer selesai.",
        reply_markup=get_back_menu_keyboard(),
        parse_mode='Markdown'
    )

    # Clear user data
    context.user_data.clear()

async def handle_sell_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle sell confirmation"""
    query = update.callback_query
    user = query.from_user

    # Check if we have the necessary data
    if not all(key in context.user_data for key in ['sell_name', 'sell_account', 'sell_amount_try', 'sell_estimated_idr']):
        await query.edit_message_text(
            "❌ Data transaksi tidak lengkap. Silakan mulai transaksi baru.",
            reply_markup=get_main_keyboard()
        )
        return

    # Update stock - add lira (user is sending lira to us), reduce lira from available stock
    amount_try = context.user_data['sell_amount_try']
    update_stock('lira', -amount_try)  # Reduce because user is taking from our stock
    logger.info(f"Stock updated: Reduced {amount_try} TRY, remaining: {STOCK['lira']}")

    # Prepare transaction data
    now = datetime.now()
    transaction_data = [
        now.strftime('%Y-%m-%d %H:%M:%S'),
        context.user_data.get('sell_name', ''),
        context.user_data.get('sell_account', ''),
        round(context.user_data.get('sell_estimated_idr', 0)),
        context.user_data.get('sell_amount_try', 0),
        'Menunggu Konfirmasi',
        user.username or '',
        str(user.id),
        'Jual Lira'
    ]

    # Save transaction
    save_success = save_transaction(transaction_data)

    # Send notification to admin (show margin details for admin)
    admin_message = (
        "🔔 **PESANAN MASUK - Jual Lira**\n\n"
        f"👤 **Nama:** {context.user_data.get('sell_name', '')}\n"
        f"🆔 **Username:** @{user.username or 'Tidak ada'}\n"
        f"🆔 **User ID:** {user.id}\n"
        f"🏦 **Rekening:** `{context.user_data.get('sell_account', '')}`\n"
        f"🪙 **TRY dari user:** ₺{context.user_data.get('sell_amount_try', 0):,.2f}\n"
        f"💰 **IDR yang diterima user:** {format_currency(context.user_data.get('sell_estimated_idr', 0))}\n"
        f"📊 **Margin tersembunyi:** 2.5% dari konversi\n"
        f"📦 **Stok Lira tersisa:** ₺{STOCK['lira']:,.2f}\n"
        f"🏦 **IBAN Admin:** `{ADMIN_IBAN}`\n"
        f"⏰ **Waktu:** {now.strftime('%d/%m/%Y %H:%M:%S')}\n"
        f"💾 **Status Simpan:** {'✅ Berhasil' if save_success else '❌ Gagal'}\n\n"
        f"**Silakan cek penerimaan Lira dan proses transfer IDR.**"
    )

    try:
        if ADMIN_CHAT_ID:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=admin_message,
                parse_mode='Markdown'
            )
            logger.info(f"Admin notification sent for sell transaction from user {user.id}")
        else:
            logger.warning("ADMIN_CHAT_ID not configured, admin notification not sent")
    except Exception as e:
        logger.error(f"Error sending admin notification: {e}")

    # Send confirmation to user (no margin mentioned)
    await query.edit_message_text(
        "✅ **Konfirmasi Pengiriman Diterima!**\n\n"
        "Terima kasih! Transaksi Anda sedang diproses.\n"
        "Admin akan segera memverifikasi penerimaan Lira dan mengirim Rupiah ke rekening Anda.\n\n"
        f"🏦 **IBAN Admin (tujuan kirim Lira):**\n"
        f"`{ADMIN_IBAN}`\n"
        f"🪙 **TRY yang Anda kirim:** ₺{context.user_data.get('sell_amount_try', 0):,.2f}\n\n"
        f"🏦 **Rekening Anda (tujuan IDR):**\n"
        f"`{context.user_data.get('sell_account', '')}`\n"
        f"💰 **IDR yang akan diterima:** {format_currency(context.user_data.get('sell_estimated_idr', 0))}\n\n"
        "📱 **Estimasi Waktu Proses:** 5-15 menit\n"
        "💬 **Jika ada pertanyaan:** @lirakuid\n\n"
        "Kami akan mengirim notifikasi setelah transfer selesai.",
        reply_markup=get_back_menu_keyboard(),
        parse_mode='Markdown'
    )

    # Clear user data
    context.user_data.clear()

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel conversation"""
    user = update.effective_user
    keyboard = get_owner_keyboard() if user.id == OWNER_USER_ID else get_main_keyboard()
    
    await update.message.reply_text(
        "❌ Transaksi dibatalkan.",
        reply_markup=keyboard
    )
    return ConversationHandler.END

def main():
    """Main function to run the bot"""
    try:
        # Load stock data on startup
        load_stock()
        
        # Validate environment variables
        if not BOT_TOKEN:
            raise ValueError("BOT_TOKEN tidak ditemukan dalam environment variables")
        if not EXCHANGE_API_KEY:
            raise ValueError("EXCHANGE_API_KEY tidak ditemukan dalam environment variables")
        if not ADMIN_CHAT_ID:
            logger.warning("ADMIN_CHAT_ID tidak ditemukan, notifikasi admin tidak akan dikirim")
        if not OWNER_USER_ID:
            logger.warning("OWNER_USER_ID tidak ditemukan, fitur owner tidak akan tersedia")

        logger.info("Initializing bot application...")

        # Create application with error handling
        try:
            application = Application.builder().token(BOT_TOKEN).build()
        except Exception as e:
            logger.error(f"Error creating application: {e}")
            # Try alternative method
            from telegram.ext import ApplicationBuilder
            application = ApplicationBuilder().token(BOT_TOKEN).build()

        # Add conversation handler for buy lira
        buy_conv_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(button_handler, pattern="^buy_lira$")],
            states={
                WAITING_BUY_AMOUNT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buy_amount),
                    CallbackQueryHandler(button_handler, pattern="^(back|main_menu)$")
                ],
                WAITING_BUY_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buy_name),
                    CallbackQueryHandler(button_handler, pattern="^(back|main_menu)$")
                ],
                WAITING_BUY_IBAN: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buy_iban),
                    CallbackQueryHandler(button_handler, pattern="^(back|main_menu)$")
                ],
                WAITING_BUY_CONFIRMATION: [
                    CallbackQueryHandler(button_handler, pattern="^(confirm_transaction|back|main_menu)$")
                ],
            },
            fallbacks=[
                CommandHandler('cancel', cancel),
                CallbackQueryHandler(button_handler, pattern="^(back|main_menu)$")
            ],
            allow_reentry=True
        )

        # Add conversation handler for sell lira
        sell_conv_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(button_handler, pattern="^sell_lira$")],
            states={
                WAITING_SELL_AMOUNT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_sell_amount),
                    CallbackQueryHandler(button_handler, pattern="^(back|main_menu)$")
                ],
                WAITING_SELL_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_sell_name),
                    CallbackQueryHandler(button_handler, pattern="^(back|main_menu)$")
                ],
                WAITING_SELL_ACCOUNT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_sell_account),
                    CallbackQueryHandler(button_handler, pattern="^(back|main_menu)$")
                ],
                WAITING_SELL_CONFIRMATION: [
                    CallbackQueryHandler(button_handler, pattern="^(confirm_transaction|back|main_menu)$")
                ],
            },
            fallbacks=[
                CommandHandler('cancel', cancel),
                CallbackQueryHandler(button_handler, pattern="^(back|main_menu)$")
            ],
            allow_reentry=True
        )

        # Add conversation handler for stock updates (owner only)
        stock_conv_handler = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(button_handler, pattern="^update_rupiah$"),
                CallbackQueryHandler(button_handler, pattern="^update_lira$")
            ],
            states={
                WAITING_STOCK_UPDATE_AMOUNT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_stock_update),
                    CallbackQueryHandler(button_handler, pattern="^(back|main_menu)$")
                ],
            },
            fallbacks=[
                CommandHandler('cancel', cancel),
                CallbackQueryHandler(button_handler, pattern="^(back|main_menu)$")
            ],
            allow_reentry=True
        )

        # Add handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(buy_conv_handler)
        application.add_handler(sell_conv_handler)
        application.add_handler(stock_conv_handler)
        application.add_handler(CallbackQueryHandler(button_handler))

        # Start keep alive server before polling (IMPORTANT!)
        print("🌐 Starting keep-alive server...")
        keep_alive()

        # Start polling with error handling
        print("🤖 LiraKuBot is starting...")
        print(f"📊 Initial stock: Rupiah: {format_currency(STOCK['rupiah'])}, Lira: ₺{STOCK['lira']:,.2f}")
        logger.info("Bot started successfully")

        # Use run_polling with proper parameters
        application.run_polling(
            timeout=30,
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES
        )

    except Exception as e:
        logger.error(f"Critical error starting bot: {e}")
        print(f"❌ Error starting bot: {e}")
        return False

class HealthCheckHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler for health checks (fallback for Render)"""
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'LiraKuBot is running!')

    def log_message(self, format, *args):
        # Disable HTTP server logging
        return

def start_http_server():
    """Start simple HTTP server for Render health checks (fallback)"""
    port = int(os.getenv('PORT', 10000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    logger.info(f"HTTP server starting on port {port}")
    server.serve_forever()

if __name__ == '__main__':
    # Check deployment environment
    if os.getenv('RENDER'):
        # For Render deployment - use HTTP server
        print("🔧 Detected Render environment")
        http_thread = threading.Thread(target=start_http_server, daemon=True)
        http_thread.start()
        logger.info("HTTP server started for Render")
    elif os.getenv('REPLIT_DB_URL') or os.getenv('REPL_ID'):
        # For Replit deployment - use Flask server
        print("🔧 Detected Replit environment")
        logger.info("Using Flask keep-alive server for Replit")
    else:
        # Local or other deployment
        print("🔧 Local/Other deployment detected")

    # Start the bot
    main()
