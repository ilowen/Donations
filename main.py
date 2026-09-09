import os
import uuid
import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs

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
# CONFIG
# ============================================================

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

YOOMONEY_WALLET = os.environ.get(
    "YOOMONEY_WALLET",
    "КОШЕЛЕК_НЕ_НАСТРОЕН"
)

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")

# Таблица текущих депозитов
DEPOSITS_TABLE = os.environ.get("DEPOSITS_TABLE", "deposits")

# Названия колонок в deposits
DEPOSIT_USER_COLUMN = os.environ.get(
    "DEPOSIT_USER_COLUMN",
    "username"
)

DEPOSIT_BALANCE_COLUMN = os.environ.get(
    "DEPOSIT_BALANCE_COLUMN",
    "balance"
)

# Файл Google Service Account
GOOGLE_SECRET_FILE = os.environ.get(
    "GOOGLE_SECRET_FILE",
    "learned-pact-242010-54a8a1daf93f.json"
)


# ============================================================
# SUPABASE
# ============================================================

supabase: Client | None = None

if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(
            SUPABASE_URL,
            SUPABASE_KEY
        )

        print("✅ Supabase client initialized", flush=True)

    except Exception as e:
        print(
            f"❌ Ошибка инициализации Supabase: {e}",
            flush=True
        )
else:
    print(
        "⚠️ SUPABASE_URL или SUPABASE_KEY не настроены",
        flush=True
    )


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
# TEMPORARY ORDERS CACHE
# ============================================================

# Здесь НЕ хранится баланс.
#
# Это только временная связка:
#
# order_id -> пользователь / сообщение / сумма / статус
#
# Основной баланс находится в Supabase deposits.

DONATIONS_DB = {}


# ============================================================
# PROCESSED ORDERS
# ============================================================

# Защита от повторной обработки одного webhook
#
# Пока сервис запущен.
#
# Основной журнал операций всё равно находится в Google Sheets.

PROCESSED_ORDERS = set()


# ============================================================
# MODELS
# ============================================================

class DonationOrder(BaseModel):
    username: str
    message: str
    amount: int


# ============================================================
# GOOGLE SHEETS
# ============================================================

def write_to_google_sheet(
    order_id: str,
    username: str,
    amount,
    message: str,
    status: str = "paid"
):
    """
    Добавляет лог платежа в Google Sheets.

    Supabase здесь НЕ заменяется Google Sheets.
    Supabase = текущий депозит.
    Google Sheets = журнал платежей.
    """

    if not GOOGLE_SHEET_ID:
        print(
            "⚠️ GOOGLE_SHEET_ID не настроен",
            flush=True
        )
        return False

    if not os.path.exists(GOOGLE_SECRET_FILE):
        print(
            f"⚠️ Файл {GOOGLE_SECRET_FILE} не найден",
            flush=True
        )
        return False

    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]

        creds = Credentials.from_service_account_file(
            GOOGLE_SECRET_FILE,
            scopes=scopes
        )

        client = gspread.authorize(creds)

        sheet = client.open_by_key(
            GOOGLE_SHEET_ID
        ).sheet1

        current_time = datetime.datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        row = [
            current_time,
            order_id,
            username,
            amount,
            message,
            status
        ]

        sheet.append_row(row)

        print(
            "📊 Платёж записан в Google Sheets",
            flush=True
        )

        return True

    except Exception as e:
        print(
            f"❌ Ошибка записи в Google Sheets: {e}",
            flush=True
        )

        return False


# ============================================================
# SUPABASE DEPOSIT UPDATE
# ============================================================

def add_to_deposit(
    username: str,
    amount
):
    """
    Пополняет существующий депозит пользователя.

    ВАЖНО:
    - RPC НЕ используется.
    - transaction table НЕ используется.
    - создаётся/изменяется только текущий balance.
    - ничего не удаляется.

    Логика:

        SELECT текущий balance
              ↓
        balance + amount
              ↓
        UPDATE deposits
    """

    if supabase is None:
        print(
            "❌ Supabase client не инициализирован",
            flush=True
        )
        return False, None

    try:
        amount_decimal = Decimal(str(amount))

    except (InvalidOperation, ValueError):
        print(
            f"❌ Некорректная сумма: {amount}",
            flush=True
        )
        return False, None

    if amount_decimal <= 0:
        print(
            f"❌ Сумма должна быть > 0: {amount}",
            flush=True
        )
        return False, None

    try:
        # ----------------------------------------------------
        # Получаем текущий счёт
        # ----------------------------------------------------

        result = (
            supabase
            .table(DEPOSITS_TABLE)
            .select(DEPOSIT_BALANCE_COLUMN)
            .eq(DEPOSIT_USER_COLUMN, username)
            .limit(1)
            .execute()
        )

        if not result.data:
            print(
                f"❌ Депозит пользователя не найден: {username}",
                flush=True
            )

            return False, None

        current_value = result.data[0].get(
            DEPOSIT_BALANCE_COLUMN,
            0
        )

        try:
            current_balance = Decimal(
                str(current_value or 0)
            )

        except (InvalidOperation, ValueError):
            print(
                f"❌ Некорректный текущий balance: "
                f"{current_value}",
                flush=True
            )

            return False, None

        # ----------------------------------------------------
        # Новый баланс
        # ----------------------------------------------------

        new_balance = current_balance + amount_decimal

        # Если в БД numeric — передаём число,
        # а не Decimal object.
        new_balance_value = float(new_balance)

        print(
            f"💰 {username}: "
            f"{current_balance} + {amount_decimal} "
            f"= {new_balance}",
            flush=True
        )

        # ----------------------------------------------------
        # UPDATE deposits
        # ----------------------------------------------------

        update_result = (
            supabase
            .table(DEPOSITS_TABLE)
            .update({
                DEPOSIT_BALANCE_COLUMN: new_balance_value
            })
            .eq(DEPOSIT_USER_COLUMN, username)
            .execute()
        )

        # ----------------------------------------------------
        # Проверяем, что UPDATE действительно прошёл
        # ----------------------------------------------------

        if not update_result.data:
            print(
                "❌ Supabase не вернул обновлённую запись",
                flush=True
            )

            return False, None

        # Проверяем фактическое значение
        updated_balance = update_result.data[0].get(
            DEPOSIT_BALANCE_COLUMN
        )

        if updated_balance is None:
            print(
                "❌ Supabase UPDATE выполнен, "
                "но balance не вернулся",
                flush=True
            )

            return False, None

        print(
            f"✅ Депозит обновлён: "
            f"{username} = {updated_balance}",
            flush=True
        )

        return True, updated_balance

    except Exception as e:

        print(
            f"❌ Ошибка UPDATE deposits: {e}",
            flush=True
        )

        return False, None


# ============================================================
# HOME PAGE
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def home_page():

    return """
    <html>
        <head>
            <meta charset="UTF-8">
            <title>YooMoney True Order Donat</title>

            <style>
                body {
                    font-family: Arial, sans-serif;
                    max-width: 400px;
                    margin: 50px auto;
                    padding: 20px;
                    background: #f4f4f9;
                    text-align: center;
                }

                .card {
                    background: white;
                    padding: 30px;
                    border-radius: 10px;
                    box-shadow:
                        0 4px 6px rgba(0,0,0,0.1);
                }

                input,
                textarea,
                button {
                    width: 100%;
                    padding: 12px;
                    margin: 8px 0;
                    border: 1px solid #ccc;
                    border-radius: 6px;
                    box-sizing: border-box;
                }

                button {
                    background: #8a2be2;
                    color: white;
                    font-weight: bold;
                    cursor: pointer;
                    border: none;
                    font-size: 16px;
                }

                button:hover {
                    background: #6a1b9a;
                }

                textarea {
                    resize: none;
                    height: 80px;
                }

                h2 {
                    color: #333;
                }
            </style>
        </head>

        <body>

            <div class="card">

                <h2>Отправить донат</h2>

                <form
                    id="donationForm"
                    onsubmit="sendDonationRequest(event)"
                >

                    <input
                        type="text"
                        id="username"
                        placeholder="Ваш никнейм"
                        required
                        maxlength="20"
                    >

                    <textarea
                        id="message"
                        placeholder="Текст сообщения доната..."
                        maxlength="200"
                    ></textarea>

                    <input
                        type="number"
                        id="amount"
                        placeholder="Сумма (руб)"
                        min="2"
                        value="100"
                        required
                    >

                    <button
                        type="submit"
                        id="submitBtn"
                    >
                        Поддержать
                    </button>

                </form>

                <p
                    style="
                        color: gray;
                        font-size: 11px;
                        margin-top: 15px;
                    "
                >
                    Донат-сервер: СТАТУС АКТИВЕН 🟢
                </p>

            </div>


            <script>

                async function sendDonationRequest(event) {

                    event.preventDefault();

                    const submitBtn =
                        document.getElementById('submitBtn');

                    submitBtn.innerText =
                        "Создание заказа...";

                    submitBtn.disabled = true;


                    const payload = {

                        username:
                            document
                            .getElementById('username')
                            .value
                            .trim() || 'Аноним',

                        message:
                            document
                            .getElementById('message')
                            .value
                            .trim() || 'Без сообщения',

                        amount:
                            parseInt(
                                document
                                .getElementById('amount')
                                .value
                            ) || 100
                    };


                    try {

                        const response =
                            await fetch(
                                '/create-order',
                                {
                                    method: 'POST',

                                    headers: {
                                        'Content-Type':
                                            'application/json'
                                    },

                                    body:
                                        JSON.stringify(payload)
                                }
                            );


                        const data =
                            await response.json();


                        if (
                            data.url &&
                            data.order_id
                        ) {

                            window.open(
                                data.url,
                                '_blank'
                            );


                            submitBtn.innerText =
                                "Ожидание оплаты...";


                            const interval =
                                setInterval(
                                    async () => {

                                        const statusResp =
                                            await fetch(
                                                `/check-status?order_id=${data.order_id}`
                                            );

                                        const statusData =
                                            await statusResp.json();


                                        if (
                                            statusData.status
                                            === 'paid'
                                        ) {

                                            clearInterval(
                                                interval
                                            );

                                            submitBtn.innerText =
                                                `Успешно оплачено! 🎉`;

                                            submitBtn.style.background =
                                                "#2e7d32";
                                        }

                                    },
                                    3000
                                );
                        }

                    } catch (err) {

                        console.error(err);

                        submitBtn.innerText =
                            "Поддержать";

                        submitBtn.disabled = false;
                    }
                }

            </script>

        </body>
    </html>
    """


# ============================================================
# CREATE ORDER
# ============================================================

@app.post("/create-order")
async def create_order(order: DonationOrder):

    order_id = (
        f"ord_{uuid.uuid4().hex[:12]}"
    )

    DONATIONS_DB[order_id] = {

        "username": order.username,

        "message": order.message,

        "amount": order.amount,

        "status": "pending"
    }


    quickpay = Quickpay(

        receiver=YOOMONEY_WALLET,

        quickpay_form="shop",

        targets="Поддержка стрима",

        paymentType="AC",

        sum=order.amount,

        label=order_id
    )


    return JSONResponse(
        content={
            "url": quickpay.redirected_url,
            "order_id": order_id
        }
    )


# ============================================================
# YOOMONEY WEBHOOK
# ============================================================

@app.post("/webhook")
async def handle_yoomoney_webhook(
    request: Request
):

    body_bytes = await request.body()

    body_str = body_bytes.decode(
        "utf-8",
        errors="replace"
    )

    parsed_data = parse_qs(body_str)


    # --------------------------------------------------------
    # YooMoney label
    # --------------------------------------------------------

    labels_list = parsed_data.get(
        "label",
        []
    )

    incoming_label = (
        labels_list[0]
        if labels_list
        else None
    )


    # --------------------------------------------------------
    # YooMoney amount
    # --------------------------------------------------------

    amounts_list = parsed_data.get(
        "withdraw_amount",
        ["0"]
    )

    withdraw_amount = (
        amounts_list[0]
        if amounts_list
        else "0"
    )


    # --------------------------------------------------------
    # Проверяем order_id
    # --------------------------------------------------------

    if not incoming_label:

        print(
            "⚠️ Получен webhook без label",
            flush=True
        )

        return JSONResponse(
            status_code=400,
            content={
                "status": "no_label"
            }
        )


    # --------------------------------------------------------
    # Проверяем существующий заказ
    # --------------------------------------------------------

    if incoming_label not in DONATIONS_DB:

        print(
            "⚠️ Получен webhook для "
            f"неизвестного заказа: {incoming_label}",
            flush=True
        )

        return JSONResponse(
            status_code=400,
            content={
                "status": "unknown_order"
            }
        )


    order = DONATIONS_DB[
        incoming_label
    ]


    # --------------------------------------------------------
    # Защита от повторного webhook
    # --------------------------------------------------------

    if (
        order["status"] == "success"
        or incoming_label in PROCESSED_ORDERS
    ):

        print(
            f"ℹ️ Повторный webhook: "
            f"{incoming_label}",
            flush=True
        )

        return {
            "status": "already_processed"
        }


    # --------------------------------------------------------
    # Данные заказа
    # --------------------------------------------------------

    user = order["username"]

    msg = order["message"]


    try:

        paid_amount = Decimal(
            str(withdraw_amount)
        )

    except (InvalidOperation, ValueError):

        print(
            f"❌ Некорректная сумма YooMoney: "
            f"{withdraw_amount}",
            flush=True
        )

        return JSONResponse(
            status_code=400,
            content={
                "status": "bad_amount"
            }
        )


    if paid_amount <= 0:

        print(
            f"❌ Некорректная сумма: "
            f"{paid_amount}",
            flush=True
        )

        return JSONResponse(
            status_code=400,
            content={
                "status": "bad_amount"
            }
        )


    print(
        "\n🎉 ПОЛУЧЕН ПЛАТЁЖ",
        flush=True
    )

    print(
        f"Order ID: {incoming_label}",
        flush=True
    )

    print(
        f"User: {user}",
        flush=True
    )

    print(
        f"Amount: {paid_amount} RUB",
        flush=True
    )

    print(
        f"Message: {msg}",
        flush=True
    )


    # ========================================================
    # 1. UPDATE DEPOSIT
    # ========================================================

    deposit_success, new_balance = (
        add_to_deposit(
            username=user,
            amount=paid_amount
        )
    )


    if not deposit_success:

        print(
            "❌ Депозит НЕ обновлён. "
            "Excel тоже пока НЕ пишем.",
            flush=True
        )

        # Возвращаем ошибку, чтобы YooMoney мог
        # повторить webhook.
        return JSONResponse(
            status_code=500,
            content={
                "status": "deposit_update_failed"
            }
        )


    # ========================================================
    # 2. GOOGLE SHEETS LOG
    # ========================================================

    sheet_success = write_to_google_sheet(

        order_id=incoming_label,

        username=user,

        amount=str(paid_amount),

        message=msg,

        status="paid"
    )


    if not sheet_success:

        print(
            "⚠️ Депозит уже обновлён, "
            "но запись в Google Sheets не удалась.",
            flush=True
        )

    else:

        print(
            "✅ Лог платежа записан в Excel/Google Sheets",
            flush=True
        )


    # ========================================================
    # 3. MARK ORDER AS PROCESSED
    # ========================================================

    order["status"] = "success"

    order["amount"] = str(
        paid_amount
    )

    order["balance"] = new_balance

    PROCESSED_ORDERS.add(
        incoming_label
    )


    print(
        f"✅ ВСЁ ГОТОВО: "
        f"{user} получил +{paid_amount} "
        f"депозита. Новый баланс: {new_balance}",
        flush=True
    )

    print(
        "=" * 50,
        flush=True
    )


    return {
        "status": "ok",

        "order_id":
            incoming_label,

        "username":
            user,

        "amount":
            str(paid_amount),

        "balance":
            new_balance,

        "sheet_logged":
            sheet_success
    }


# ============================================================
# CHECK STATUS
# ============================================================

@app.get("/check-status")
async def check_status(
    order_id: str = None
):

    if not order_id:

        return {
            "status": "pending"
        }


    if (
        order_id in DONATIONS_DB
        and
        DONATIONS_DB[order_id]["status"]
        == "success"
    ):

        return {
            "status": "paid"
        }


    return {
        "status": "pending"
    }


# ============================================================
# GET DONATIONS
# ============================================================

@app.get("/get-donations")
async def get_donations():

    paid_donations = []


    for order_id, info in DONATIONS_DB.items():

        if info["status"] == "success":

            paid_donations.append({

                "id":
                    order_id,

                "username":
                    info["username"],

                "message":
                    info["message"],

                "amount":
                    info["amount"]
            })


    # ========================================================
    # ВАЖНО:
    #
    # НИКАКОГО:
    #
    # del DONATIONS_DB[order_id]
    #
    # Здесь больше нет.
    #
    # GET только читает данные.
    # ========================================================

    return JSONResponse(
        content=paid_donations
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            8000
        )
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port
    )
