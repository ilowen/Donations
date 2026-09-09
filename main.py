import os
import uuid
import datetime
import hmac
import hashlib
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from yoomoney import Quickpay
from supabase import create_client, Client
import gspread
from google.oauth2.service_account import Credentials
import uvicorn


# ============================================================
# SUPABASE
# ============================================================

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Таблица депозитов — оставляем прямо в коде.
DEPOSITS_TABLE = "deposits"
DEPOSIT_USER_COLUMN = "username"
DEPOSIT_BALANCE_COLUMN = "balance"
DEPOSIT_UPDATED_COLUMN = "updated_at"

supabase_client: Client | None = None

if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("✅ Supabase initialized", flush=True)
    except Exception as e:
        print(f"❌ Supabase init error: {e}", flush=True)
else:
    print("⚠️ SUPABASE_URL / SUPABASE_KEY не настроены", flush=True)


# ============================================================
# APP
# ============================================================

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# YOOMONEY
# ============================================================

YOOMONEY_WALLET = os.environ.get(
    "YOOMONEY_WALLET",
    "КОШЕЛЕК_НЕ_НАСТРОЕН"
)

# Секрет из настроек HTTP-уведомлений YooMoney.
YOOMONEY_SECRET = os.environ.get("YOOMONEY_SECRET", "")

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
GOOGLE_SECRET_FILE = "learned-pact-242010-54a8a1daf93f.json"


# ============================================================
# IN-MEMORY ORDERS
# ============================================================

DONATIONS_DB = {}
PROCESSED_ORDERS = set()


class DonationOrder(BaseModel):
    username: str
    message: str
    amount: int


# ============================================================
# YOOMONEY SIGN — по документации YooMoney
# ============================================================

def verify_yoomoney_sign(parsed_data):
    """
    YooMoney:
    1. берем все параметры уведомления, кроме sign;
    2. сортируем ключи по алфавиту;
    3. URL-кодируем значения UTF-8;
    4. соединяем как key=value через &;
    5. считаем HMAC-SHA256 с секретным ключом;
    6. сравниваем HEX с параметром sign.
    """

    if not YOOMONEY_SECRET:
        print("❌ YOOMONEY_SECRET не задан", flush=True)
        return False

    received_sign = parsed_data.get("sign", [""])[0]

    if not received_sign:
        print("❌ В webhook отсутствует sign", flush=True)
        return False

    prepared = []

    for key in sorted(parsed_data.keys()):
        if key == "sign":
            continue

        value_list = parsed_data.get(key, [""])
        value = value_list[0] if value_list else ""

        # RFC 3986: кодируем именно ЗНАЧЕНИЕ.
        encoded_value = quote(
            value,
            safe="-_.~"
        )

        prepared.append(
            f"{key}={encoded_value}"
        )

    sign_string = "&".join(prepared)

    calculated_sign = hmac.new(
        YOOMONEY_SECRET.encode("utf-8"),
        sign_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(
        calculated_sign.lower(),
        received_sign.lower()
    ):
        print("❌ YOOMONEY SIGN НЕ СОВПАЛ", flush=True)
        print(f"   Получен: {received_sign}", flush=True)
        print(f"   Расчитан: {calculated_sign}", flush=True)
        return False

    print("✅ YOOMONEY SIGN OK", flush=True)
    return True


# ============================================================
# GOOGLE SHEETS
# ============================================================

def write_to_google_sheet(username, amount, message, order_id=None):
    if not GOOGLE_SHEET_ID:
        print(
            "⚠️ GOOGLE_SHEET_ID не настроен в Environment",
            flush=True
        )
        return False

    if not os.path.exists(GOOGLE_SECRET_FILE):
        print(
            f"⚠️ Секретный файл {GOOGLE_SECRET_FILE} не найден",
            flush=True
        )
        return False

    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]

        creds = Credentials.from_service_account_file(
            GOOGLE_SECRET_FILE,
            scopes=scopes,
        )

        client = gspread.authorize(creds)
        sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1

        current_time = datetime.datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        sheet.append_row([
            current_time,
            username,
            amount,
            message,
            order_id or "",
        ])

        print(
            "📊 Строка успешно записана в Google Таблицу!",
            flush=True
        )
        return True

    except Exception as e:
        print(
            f"❌ ОШИБКА ЗАПИСИ В GOOGLE ТАБЛИЦУ: {e}",
            flush=True
        )
        return False


# ============================================================
# SUPABASE DEPOSIT
# ============================================================

def add_to_deposit(username, amount):
    if not supabase_client:
        return False, "Supabase не настроен"

    try:
        result = (
            supabase_client
            .table(DEPOSITS_TABLE)
            .select(DEPOSIT_BALANCE_COLUMN)
            .eq(DEPOSIT_USER_COLUMN, username)
            .limit(1)
            .execute()
        )

        if not result.data:
            return (
                False,
                f"Пользователь '{username}' не найден в таблице {DEPOSITS_TABLE}"
            )

        current_balance = float(
            result.data[0].get(DEPOSIT_BALANCE_COLUMN) or 0
        )

        new_balance = current_balance + float(amount)

        update_result = (
            supabase_client
            .table(DEPOSITS_TABLE)
            .update({
                DEPOSIT_BALANCE_COLUMN: new_balance,
                DEPOSIT_UPDATED_COLUMN: datetime.datetime.now(
                    datetime.timezone.utc
                ).isoformat(),
            })
            .eq(DEPOSIT_USER_COLUMN, username)
            .execute()
        )

        # Не используем update_result.data как единственный признак успеха.
        if update_result.data is not None and len(update_result.data) == 0:
            print(
                "⚠️ UPDATE выполнен, но Supabase не вернул строки",
                flush=True
            )

        verify = (
            supabase_client
            .table(DEPOSITS_TABLE)
            .select(
                f"{DEPOSIT_BALANCE_COLUMN},{DEPOSIT_UPDATED_COLUMN}"
            )
            .eq(DEPOSIT_USER_COLUMN, username)
            .limit(1)
            .execute()
        )

        if not verify.data:
            return False, "Не удалось проверить UPDATE deposits"

        saved_balance = float(
            verify.data[0].get(DEPOSIT_BALANCE_COLUMN) or 0
        )

        print(
            f"✅ DEPOSIT UPDATE: {username}: "
            f"{current_balance} + {amount} = {saved_balance}",
            flush=True
        )

        return True, saved_balance

    except Exception as e:
        print(
            f"❌ Ошибка UPDATE deposits: {e}",
            flush=True
        )
        return False, str(e)


# ============================================================
# 1. UI
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def home_page():
    return """
    <html>
        <head>
            <meta charset="UTF-8">
            <title>YooMoney True Order Donat</title>
            <style>
                body { font-family: Arial, sans-serif; max-width: 400px; margin: 50px auto; padding: 20px; background: #f4f4f9; text-align: center; }
                .card { background: white; padding: 30px; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
                input, textarea, button { width: 100%; padding: 12px; margin: 8px 0; border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box; }
                button { background: #8a2be2; color: white; font-weight: bold; cursor: pointer; border: none; font-size: 16px; }
                button:hover { background: #6a1b9a; }
                textarea { resize: none; height: 80px; }
                h2 { color: #333; }
            </style>
        </head>
        <body>
            <div class="card">
                <h2>Отправить донат</h2>
                <form id="donationForm" onsubmit="sendDonationRequest(event)">
                    <input type="text" id="username" placeholder="Ваш никнейм" required maxlength="20">
                    <textarea id="message" placeholder="Текст сообщения доната..." maxlength="200"></textarea>
                    <input type="number" id="amount" placeholder="Сумма (руб)" min="2" value="100" required>
                    <button type="submit" id="submitBtn">Поддержать</button>
                </form>
                <p style="color: gray; font-size: 11px; margin-top: 15px;">Донат-сервер: СТАТУС АКТИВЕН 🟢</p>
            </div>

            <script>
                async function sendDonationRequest(event) {
                    event.preventDefault();
                    const submitBtn = document.getElementById('submitBtn');
                    submitBtn.innerText = "Создание заказа...";
                    submitBtn.disabled = true;

                    const payload = {
                        username: document.getElementById('username').value.trim() || 'Аноним',
                        message: document.getElementById('message').value.trim() || 'Без сообщения',
                        amount: parseInt(document.getElementById('amount').value) || 100
                    };

                    try {
                        const response = await fetch('/create-order', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify(payload)
                        });

                        const data = await response.json();
                        if (data.url && data.order_id) {
                            window.open(data.url, '_blank');
                            submitBtn.innerText = "Ожидание оплаты...";

                            const interval = setInterval(async () => {
                                const statusResp = await fetch(`/check-status?order_id=${data.order_id}`);
                                const statusData = await statusResp.json();
                                if (statusData.status === 'paid') {
                                    clearInterval(interval);
                                    submitBtn.innerText = `Успешно оплачено! 🎉`;
                                    submitBtn.style.background = "#2e7d32";
                                }
                            }, 3000);
                        } else {
                            submitBtn.innerText = "Ошибка создания заказа";
                            submitBtn.disabled = false;
                        }
                    } catch (err) {
                        console.error(err);
                        submitBtn.innerText = "Поддержать";
                        submitBtn.disabled = false;
                    }
                }
            </script>
        </body>
    </html>
    """


# ============================================================
# 2. CREATE ORDER
# ============================================================

@app.post("/create-order")
async def create_order(order: DonationOrder):
    order_id = f"ord_{uuid.uuid4().hex[:12]}"

    DONATIONS_DB[order_id] = {
        "username": order.username,
        "message": order.message,
        "amount": order.amount,
        "status": "pending",
    }

    quickpay = Quickpay(
        receiver=YOOMONEY_WALLET,
        quickpay_form="shop",
        targets="Поддержка стрима",
        paymentType="AC",
        sum=order.amount,
        label=order_id,
    )

    return JSONResponse(
        content={
            "url": quickpay.redirected_url,
            "order_id": order_id,
        }
    )


# ============================================================
# 3. YOOMONEY WEBHOOK
# ============================================================

@app.post("/webhook")
async def handle_yoomoney_webhook(request: Request):
    body_bytes = await request.body()

    try:
        body_str = body_bytes.decode("utf-8")
    except UnicodeDecodeError:
        print("❌ Некорректный UTF-8 webhook", flush=True)
        return JSONResponse(
            status_code=400,
            content={"status": "bad_encoding"}
        )

    parsed_data = parse_qs(
        body_str,
        keep_blank_values=True,
    )

    print(
        f"📩 YooMoney webhook: {parsed_data}",
        flush=True
    )

    # Проверяем подлинность уведомления ПО ДОКУМЕНТАЦИИ YooMoney.
    if not verify_yoomoney_sign(parsed_data):
        return JSONResponse(
            status_code=403,
            content={"status": "invalid_signature"}
        )

    # --------------------------------------------------------
    # Параметры POST YooMoney
    # --------------------------------------------------------

    labels_list = parsed_data.get("label", [])
    incoming_label = labels_list[0] if labels_list else None

    # amount — сумма операции, которая пришла на кошелек получателя.
    amounts_list = parsed_data.get("amount", ["0"])
    amount_raw = amounts_list[0] if amounts_list else "0"

    operation_id = parsed_data.get(
        "operation_id", [""]
    )[0]

    notification_type = parsed_data.get(
        "notification_type", [""]
    )[0]

    unaccepted = parsed_data.get(
        "unaccepted", ["false"]
    )[0]

    if notification_type not in (
        "p2p-incoming",
        "card-incoming",
    ):
        print(
            f"⚠️ Неизвестный тип уведомления: {notification_type}",
            flush=True
        )
        return {"status": "ok"}

    if unaccepted == "true":
        print("⚠️ Платеж unaccepted", flush=True)
        return {"status": "unaccepted"}

    if not incoming_label:
        print(
            "⚠️ Получен вебхук без поля label",
            flush=True
        )
        return {"status": "no_label"}

    if incoming_label not in DONATIONS_DB:
        print(
            f"⚠️ Получен вебхук для неизвестного ID заказа: {incoming_label}",
            flush=True
        )
        return {"status": "ok"}

    if (
        incoming_label in PROCESSED_ORDERS
        or DONATIONS_DB[incoming_label]["status"] == "success"
    ):
        print(
            f"⚠️ Повторный webhook для уже обработанного заказа: {incoming_label}",
            flush=True
        )
        return {
            "status": "ok",
            "already_processed": True,
        }

    try:
        amount = float(amount_raw)
    except (TypeError, ValueError):
        print(
            f"❌ Некорректная сумма в webhook: {amount_raw}",
            flush=True
        )
        return {"status": "bad_amount"}

    if amount <= 0:
        print(
            f"❌ Сумма платежа <= 0: {amount}",
            flush=True
        )
        return {"status": "bad_amount"}

    user = DONATIONS_DB[incoming_label]["username"]
    msg = DONATIONS_DB[incoming_label]["message"]

    print(
        "\n🎉 YOOMONEY WEBHOOK ПОДТВЕРЖДЕН\n"
        f"ID заказа: {incoming_label}\n"
        f"Operation ID: {operation_id}\n"
        f"От кого: {user}\n"
        f"Сумма зачисления: {amount} руб.\n"
        f"Сумма заказа: {DONATIONS_DB[incoming_label]['amount']} руб.\n"
        f"Сообщение: {msg}\n"
        + "=" * 40,
        flush=True,
    )

    # --------------------------------------------------------
    # Пополняем депозит на amount.
    # Никакого сравнения с суммой заказа.
    # --------------------------------------------------------

    success, result = add_to_deposit(
        user,
        amount,
    )

    if not success:
        print(
            f"❌ Депозит НЕ пополнен: {result}",
            flush=True
        )

        # Ошибка Supabase -> 500, чтобы YooMoney мог повторить уведомление.
        return JSONResponse(
            status_code=500,
            content={
                "status": "deposit_update_error",
                "message": result,
            },
        )

    # --------------------------------------------------------
    # Пишем успешное пополнение в Google Sheets.
    # В Excel/Sheets уходит фактически зачисленная сумма amount.
    # --------------------------------------------------------

    write_to_google_sheet(
        user,
        amount,
        msg,
        incoming_label,
    )

    # --------------------------------------------------------
    # Отмечаем заказ обработанным.
    # --------------------------------------------------------

    DONATIONS_DB[incoming_label]["status"] = "success"
    DONATIONS_DB[incoming_label]["amount"] = amount
    DONATIONS_DB[incoming_label]["balance"] = result
    DONATIONS_DB[incoming_label]["operation_id"] = operation_id

    # Сохраняем и order_id, и operation_id для защиты от повтора.
    PROCESSED_ORDERS.add(incoming_label)
    if operation_id:
        PROCESSED_ORDERS.add(operation_id)

    print(
        f"✅ Платеж зачислен: {user} + {amount} руб. | Новый баланс: {result}",
        flush=True,
    )

    return {
        "status": "ok",
        "order_id": incoming_label,
        "username": user,
        "amount": amount,
        "balance": result,
    }


# ============================================================
# 4. CHECK STATUS
# ============================================================

@app.get("/check-status")
async def check_status(order_id: str = None):
    if not order_id:
        return {"status": "pending"}

    if (
        order_id in DONATIONS_DB
        and DONATIONS_DB[order_id]["status"] == "success"
    ):
        return {"status": "paid"}

    return {"status": "pending"}


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
    )
