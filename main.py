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
GROQ_API_KEY = os.getenv('GROQ_API_KEY')  # API Key untuk Groq

# Feature toggles
BUY_LIRA_ACTIVE = True
SELL_LIRA_ACTIVE = True

# Fee configuration
ADMIN_FEE = 5000  # 5,000 IDR admin fee

# Conversation states
(WAITING_BUY_AMOUNT, WAITING_BUY_NAME, WAITING_BUY_IBAN, WAITING_BUY_CONFIRMATION,
 WAITING_SELL_AMOUNT, WAITING_SELL_NAME, WAITING_SELL_ACCOUNT, WAITING_SELL_CONFIRMATION,
 AI_CHAT_STATE) = range(9)  # Tambahan state untuk AI chat

# Google Sheets setup
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SERVICE_ACCOUNT_FILE = 'lirakubot.json'
SPREADSHEET_NAME = 'DATA LIRAKU.ID'

# AI Agent Knowledge Base
KNOWLEDGE_BASE_FILE = 'knowledge_base.json'

def load_knowledge_base():
    """Load knowledge base from JSON file"""
    try:
        with open(KNOWLEDGE_BASE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning(f"Knowledge base file {KNOWLEDGE_BASE_FILE} not found")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing knowledge base JSON: {e}")
        return []

def find_answer_in_knowledge_base(user_question):
    """Search for answer in knowledge base"""
    knowledge_base = load_knowledge_base()
    user_question_lower = user_question.lower()
    
    # Simple keyword matching
    for item in knowledge_base:
        question_lower = item['question'].lower()
        
        # Check if user question contains significant words from knowledge base question
        question_words = question_lower.split()
        matching_words = 0
        
        for word in question_words:
            if len(word) > 2 and word in user_question_lower:  # Only count words longer than 2 chars
                matching_words += 1
        
        # If more than 2 words match or specific keywords found
        if matching_words >= 2 or any(keyword in user_question_lower for keyword in 
            ['cash', 'tunai', 'atm', 'tarik', 'cairkan', 'papara', 'enpara', 'ziraat']):
            return item['answer']
    
    return None

async def ask_groq_ai(question, context=""):
    """Ask Groq AI for answer"""
    if not GROQ_API_KEY:
        return "❌ AI service tidak tersedia saat ini."
    
    try:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }
        
        system_prompt = """Kamu adalah asisten LiraKuBot yang membantu penukaran mata uang IDR ke TRY (Lira Turki) dan sebaliknya. 
        Jawab dengan singkat maksimal 5 baris. Fokus pada informasi praktis tentang:
        - Cara menggunakan uang Lira di Turki
        - Tips transaksi di bank/ATM Turki
        - Informasi umum tentang mata uang Lira
        - Panduan praktis untuk turis di Turki
        
        Jangan berikan nasihat finansial atau investasi. Kalau ditanya tentang kurs/rate, arahkan untuk menggunakan menu bot."""
        
        data = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question}
            ],
            "model": "llama3-8b-8192",
            "temperature": 0.7,
            "max_tokens": 300
        }
        
        response = requests.post(url, headers=headers, json=data, timeout=10)
        
        if response.status_code == 200:
            result = response.json()
            return result['choices'][0]['message']['content'].strip()
        else:
            logger.error(f"Groq API error: {response.status_code} - {response.text}")
            return "❌ Maaf, AI sedang tidak dapat menjawab. Silakan coba lagi atau hubungi @lirakuid"
            
    except Exception as e:
        logger.error(f"Error calling Groq API: {e}")
        return "❌ Terjadi kesalahan. Silakan coba lagi atau hubungi @lirakuid"

async def handle_ai_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle AI question from user"""
    user_question = update.message.text.strip()
    
    # Check if user wants to exit AI chat
    if user_question.lower() in ['/menu', '/start', 'menu', 'keluar', 'exit', 'stop']:
        context.user_data.pop('ai_chat_active', None)
        welcome_message = (
            "🔙 **Kembali ke Menu Utama**\n\n"
            "💚 **Selamat datang di LiraKuBot!**\n\n"
            "✅ Proses cepat & aman\n"
            "✅ Langsung kirim ke IBAN\n"
            "✅ Lebih hemat dibanding beli di bandara & bank\n\n"
            "Silakan pilih menu:"
        )
        await update.message.reply_text(
            welcome_message,
            reply_markup=get_main_keyboard(),
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    
    # Show typing indicator
    await update.message.chat.send_action(action="typing")
    
    # First check knowledge base
    kb_answer = find_answer_in_knowledge_base(user_question)
    
    if kb_answer:
        answer = f"💡 **Dari Knowledge Base:**\n\n{kb_answer}"
    else:
        # Ask Groq AI
        ai_answer = await ask_groq_ai(user_question)
        answer = f"🤖 **AI Assistant:**\n\n{ai_answer}"
    
    # Add footer with options
    answer += "\n\n💬 Tanya lagi atau ketik /menu untuk kembali ke menu utama"
    
    await update.message.reply_text(
        answer,
        parse_mode='Markdown'
    )
    
    return AI_CHAT_STATE

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
        [InlineKeyboardButton("🤖 Tanya AI Assistant", callback_data="ask_ai")],  # New AI button
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

def format_currency(amount, currency='IDR'):
    """Format currency display"""
    if currency == 'IDR':
        return f"Rp{amount:,.0f}".replace(',', '.')
    elif currency == 'TRY':
        return f"₺{amount:,.2f}".replace(',', '.')
    return f"{amount:,.2f}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    # Clear any active AI chat session
    context.user_data.pop('ai_chat_active', None)
    
    welcome_message = (
        "💚 **Selamat datang di LiraKuBot!**\n\n"
        "✅ Proses cepat & aman\n"
        "✅ Langsung kirim ke IBAN\n"
        "✅ Lebih hemat dibanding beli di bandara & bank\n\n"
        "Silakan pilih menu:"
    )

    await update.message.reply_text(
        welcome_message,
        reply_markup=get_main_keyboard(),
        parse_mode='Markdown'
    )
    return ConversationHandler.END

async def tanya_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start AI chat session with /tanya command"""
    context.user_data['ai_chat_active'] = True
    
    ai_intro = (
        "🤖 **AI Assistant LiraKuBot**\n\n"
        "Halo! Saya bisa membantu menjawab pertanyaan tentang:\n"
        "• Cara menggunakan Lira di Turki\n"
        "• Tips transaksi di bank/ATM Turki\n"
        "• Panduan praktis untuk turis\n"
        "• Informasi umum mata uang Lira\n\n"
        "💬 **Silakan tanya apa saja!**\n"
        "Ketik /menu untuk kembali ke menu utama"
    )
    
    await update.message.reply_text(
        ai_intro,
        parse_mode='Markdown'
    )
    return AI_CHAT_STATE

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks"""
    query = update.callback_query
    await query.answer()

    if query.data == "main_menu":
        # Clear any active AI chat session
        context.user_data.pop('ai_chat_active', None)
        
        welcome_message = (
            "💚 **Selamat datang di LiraKuBot!**\n\n"
            "✅ Proses cepat & aman\n"
            "✅ Langsung kirim ke IBAN\n"
            "✅ Lebih hemat dibanding beli di bandara & bank\n\n"
            "Silakan pilih menu:"
        )
        await query.edit_message_text(
            welcome_message,
            reply_markup=get_main_keyboard(),
            parse_mode='Markdown'
        )
        return ConversationHandler.END

    elif query.data == "ask_ai":
        # Start AI chat session
        context.user_data['ai_chat_active'] = True
        
        ai_intro = (
            "🤖 **AI Assistant LiraKuBot**\n\n"
            "Halo! Saya bisa membantu menjawab pertanyaan tentang:\n"
            "• Cara menggunakan Lira di Turki\n"
            "• Tips transaksi di bank/ATM Turki\n"
            "• Panduan praktis untuk turis\n"
            "• Informasi umum mata uang Lira\n\n"
            "💬 **Silakan tanya apa saja!**\n"
            "Ketik /menu untuk kembali ke menu utama"
        )
        
        await query.edit_message_text(
            ai_intro,
            parse_mode='Markdown'
        )
        return AI_CHAT_STATE

    elif query.data == "buy_lira":
        if not BUY_LIRA_ACTIVE:
            await query.edit_message_text(
                "❌ Maaf, pembelian Lira sedang tidak tersedia.",
                reply_markup=get_back_menu_keyboard()
            )
            return ConversationHandler.END

        await query.edit_message_text(
            "💸 **Beli Lira (IDR ke TRY)**\n\n"
            "Masukkan nominal dalam Rupiah yang ingin dikonversi ke Lira Turki.\n"
            "Minimal pembelian: Rp100.000\n\n"
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

        await query.edit_message_text(
            "💵 **Jual Lira (TRY ke IDR)**\n\n"
            "Masukkan jumlah Lira Turki yang ingin dijual.\n\n"
            "Contoh: 100",
            reply_markup=get_back_menu_keyboard(),
            parse_mode='Markdown'
        )
        return WAITING_SELL_AMOUNT

    elif query.data == "simulation":
        await show_simulation(query)

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

async def handle_back_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle back navigation"""
    query = update.callback_query
    current_state = context.user_data.get('current_state', None)

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
        await query.edit_message_text(
            welcome_message,
            reply_markup=get_main_keyboard(),
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
        f"*Simulasi di atas belum termasuk biaya admin*\n"
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

    # Show confirmation with admin fee
    amount = context.user_data['buy_amount_idr']
    estimated_try = context.user_data['buy_estimated_try']
    total_payment = amount + ADMIN_FEE

    context.user_data['buy_total_payment'] = total_payment

    confirmation_message = (
        "📋 **Konfirmasi Detail Pembelian**\n\n"
        f"👤 **Nama:** {context.user_data['buy_name']}\n"
        f"🏦 **IBAN:** `{iban}`\n"
        f"💸 **Nominal konversi:** {format_currency(amount)}\n"
        f"🇹🇷 **TRY yang diterima:** ₺{estimated_try:.2f}\n"
        f"💼 **Biaya admin:** {format_currency(ADMIN_FEE)}\n"
        f"💰 **Total pembayaran:** {format_currency(total_payment)}\n\n"
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

        # Get exchange rate
        base_rate = get_exchange_rate('TRY', 'IDR')
        if not base_rate:
            await update.message.reply_text(
                "❌ Gagal mengambil data kurs. Silakan coba lagi.",
                reply_markup=get_back_menu_keyboard()
            )
            return WAITING_SELL_AMOUNT

        # Calculate dengan margin 2.5% (hidden from user)
        estimated_idr_gross = amount * base_rate * 0.975

        # Store in context
        context.user_data['sell_amount_try'] = amount
        context.user_data['sell_estimated_idr_gross'] = estimated_idr_gross
        context.user_data['current_state'] = 'sell_name'

        await update.message.reply_text(
            f"💰 **Estimasi Konversi**\n\n"
            f"🇹🇷 Lira: ₺{amount:,.2f}\n"
            f"💵 Estimasi IDR: {format_currency(estimated_idr_gross)}\n\n"
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

    # Show confirmation with admin fee
    amount = context.user_data['sell_amount_try']
    estimated_idr_gross = context.user_data['sell_estimated_idr_gross']
    estimated_idr_net = estimated_idr_gross - ADMIN_FEE

    # Check if result is positive
    if estimated_idr_net <= 0:
        await update.message.reply_text(
            "❌ Jumlah Lira terlalu kecil. Setelah dikurangi biaya admin, nilai akan negatif.\n"
            "Silakan masukkan jumlah yang lebih besar.",
            reply_markup=get_back_menu_keyboard()
        )
        context.user_data['current_state'] = 'sell_account'
        return WAITING_SELL_ACCOUNT

    context.user_data['sell_estimated_idr_net'] = estimated_idr_net

    confirmation_message = (
        "📋 **Konfirmasi Detail Penjualan**\n\n"
        f"👤 **Nama:** {context.user_data['sell_name']}\n"
        f"🏦 **Rekening:** `{account}`\n"
        f"🪙 **TRY yang dikirim:** ₺{amount:,.2f}\n"
        f"💵 **IDR sebelum potongan:** {format_currency(estimated_idr_gross)}\n"
        f"💼 **Biaya admin:** {format_currency(ADMIN_FEE)}\n"
        f"💰 **IDR yang diterima:** {format_currency(estimated_idr_net)}\n\n"
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
        total_payment = context.user_data['buy_total_payment']
        iban = context.user_data['buy_iban']

        payment_message = (
            "💳 **Detail Pembayaran**\n\n"
            f"👤 **Nama:** {context.user_data['buy_name']}\n"
            f"🏦 **IBAN:** `{iban}`\n"
            f"🇹🇷 **TRY yang diterima:** ₺{estimated_try:.2f}\n"
            f"💰 **Total pembayaran:** {format_currency(total_payment)}\n\n"
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
        estimated_idr_net = context.user_data['sell_estimated_idr_net']
        account = context.user_data['sell_account']

        transfer_message = (
            "💸 **Detail Transfer Lira**\n\n"
            f"👤 **Nama:** {context.user_data['sell_name']}\n"
            f"🏦 **Rekening Anda:** `{account}`\n"
            f"🪙 **TRY yang dikirim:** ₺{amount:,.2f}\n"
            f"💰 **IDR yang diterima:** {format_currency(estimated_idr_net)}\n\n"
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
    if not all(key in context.user_data for key in ['buy_name', 'buy_iban', 'buy_amount_idr', 'buy_estimated_try', 'buy_total_payment']):
        await query.edit_message_text(
            "❌ Data transaksi tidak lengkap. Silakan mulai transaksi baru.",
            reply_markup=get_main_keyboard()
        )
        return

    # Prepare transaction data
    now = datetime.now()
    transaction_data = [
        now.strftime('%Y-%m-%d %H:%M:%S'),
        context.user_data.get('buy_name', ''),
        context.user_data.get('buy_iban', ''),
        context.user_data.get('buy_total_payment', 0),
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
        f"💸 **Nominal konversi:** {format_currency(context.user_data.get('buy_amount_idr', 0))}\n"
        f"💼 **Biaya admin:** {format_currency(ADMIN_FEE)}\n"
        f"💰 **Total pembayaran:** {format_currency(context.user_data.get('buy_total_payment', 0))}\n"
        f"🇹🇷 **TRY Dikirim:** ₺{context.user_data.get('buy_estimated_try', 0):.2f}\n"
        f"📊 **Margin tersembunyi:** 2.5% dari konversi\n"
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
        f"💰 Jumlah: {format_currency(context.user_data.get('buy_total_payment', 0))}\n\n"
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
    if not all(key in context.user_data for key in ['sell_name', 'sell_account', 'sell_amount_try', 'sell_estimated_idr_net']):
        await query.edit_message_text(
            "❌ Data transaksi tidak lengkap. Silakan mulai transaksi baru.",
            reply_markup=get_main_keyboard()
        )
        return

    # Prepare transaction data
    now = datetime.now()
    transaction_data = [
        now.strftime('%Y-%m-%d %H:%M:%S'),
        context.user_data.get('sell_name', ''),
        context.user_data.get('sell_account', ''),
        round(context.user_data.get('sell_estimated_idr_net', 0)),
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
        f"🪙 **TRY Dikirim:** ₺{context.user_data.get('sell_amount_try', 0):,.2f}\n"
        f"💵 **IDR gross (dengan margin):** {format_currency(context.user_data.get('sell_estimated_idr_gross', 0))}\n"
        f"💼 **Biaya admin:** {format_currency(ADMIN_FEE)}\n"
        f"💰 **IDR yang diterima user:** {format_currency(context.user_data.get('sell_estimated_idr_net', 0))}\n"
        f"📊 **Margin tersembunyi:** 2.5% dari konversi\n"
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
        f"💰 **IDR yang akan diterima:** {format_currency(context.user_data.get('sell_estimated_idr_net', 0))}\n\n"
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
    await update.message.reply_text(
        "❌ Transaksi dibatalkan.",
        reply_markup=get_main_keyboard()
    )
    return ConversationHandler.END

def main():
    """Main function to run the bot"""
    try:
        # Validate environment variables
        if not BOT_TOKEN:
            raise ValueError("BOT_TOKEN tidak ditemukan dalam environment variables")
        if not EXCHANGE_API_KEY:
            raise ValueError("EXCHANGE_API_KEY tidak ditemukan dalam environment variables")
        if not ADMIN_CHAT_ID:
            logger.warning("ADMIN_CHAT_ID tidak ditemukan, notifikasi admin tidak akan dikirim")
        if not GROQ_API_KEY:
            logger.warning("GROQ_API_KEY tidak ditemukan, AI Assistant tidak akan berfungsi")

        logger.info("Initializing bot application...")

        # Create application with error handling
        try:
            application = Application.builder().token(BOT_TOKEN).build()
        except Exception as e:
            logger.error(f"Error creating application: {e}")
            # Try alternative method
            from telegram.ext import ApplicationBuilder
            application = ApplicationBuilder().token(BOT_TOKEN).build()

        # Add conversation handler for AI chat
        ai_conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler('tanya', tanya_command),
                CallbackQueryHandler(button_handler, pattern="^ask_ai$")
            ],
            states={
                AI_CHAT_STATE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ai_question),
                    CommandHandler('menu', start),
                    CommandHandler('start', start)
                ]
            },
            fallbacks=[
                CommandHandler('cancel', cancel),
                CommandHandler('menu', start),
                CommandHandler('start', start)
            ],
            allow_reentry=True
        )

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

        # Add handlers in priority order
        application.add_handler(CommandHandler("start", start))
        application.add_handler(ai_conv_handler, group=0)  # AI handler dengan group 0 (prioritas tinggi)
        application.add_handler(buy_conv_handler, group=1)
        application.add_handler(sell_conv_handler, group=2)
        application.add_handler(CallbackQueryHandler(button_handler), group=3)

        # Start keep alive server before polling (IMPORTANT!)
        print("🌐 Starting keep-alive server...")
        keep_alive()

        # Start polling with error handling
        print("🤖 LiraKuBot is starting...")
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