import os
import uuid
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs

import gspread
from google.oauth2.service_account import Credentials

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from yoomoney import Quickpay

import uvicorn


# ============================================================
# ENV
# ============================================================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

YOOMONEY_WALLET = os.getenv("YOOMONEY_WALLET")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

# Таблица депозитов в Supabase
DEPOSITS_TABLE = "deposits"

# Колонки таблицы deposits
DEPOSIT_USER_COLUMN = "username"
DEPOSIT_BALANCE_COLUMN = "balance"
DEPOSIT_UPDATED_COLUMN = "updated_at"

# Google service account
GOOGLE_CREDENTIALS_FILE = "learned-pact-242010-54a8a1daf93f.json"


# ============================================================
# SUPABASE
# ============================================================

supabase_client: Client | None = None

if SUPABASE_URL and SUPABASE_KEY:
    supabase_client = create_client(
        SUPABASE_URL,
        SUPABASE_KEY
    )


# ============================================================
# FASTAPI
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
# TEMPORARY ORDERS
# ============================================================

DONATIONS_DB = {}

# Защита от повторной обработки webhook в рамках жизни процесса
PROCESSED_ORDERS = set()


class DonationOrder(BaseModel):
    username: str
    message: str
    amount: float


# ============================================================
# GOOGLE SHEETS
# ============================================================

def write_to_google_sheet(
    order_id: str,
    username: str,
    amount: float,
    message: str,
    status: str
):
    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]

        credentials = Credentials.from_service_account_file(
            GOOGLE_CREDENTIALS_FILE,
            scopes=scopes
        )

        gc = gspread.authorize(credentials)

        sheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1

        now = datetime.now(timezone.utc).isoformat()

        sheet.append_row([
            now,
            order_id,
            username,
            amount,
            message,
            status
        ])

        print(
            f"✅ Google Sheets: "
            f"{username} / {amount} / {status}"
        )

    except Exception as e:
        print(f"❌ Ошибка Google Sheets: {e}")


# ============================================================
# DEPOSIT UPDATE
# ============================================================

def add_to_deposit(
    username: str,
    amount: float
):
    """
    Читаем текущий баланс и прибавляем к нему сумму платежа.

    Пример:

    balance = 1000
    amount = 500

    new balance = 1500
    """

    if supabase_client is None:
        return False, "Supabase не настроен"

    try:
        # ----------------------------------------------------
        # Получаем текущий баланс
        # ----------------------------------------------------

        result = (
            supabase_client
            .table(DEPOSITS_TABLE)
            .select(DEPOSIT_BALANCE_COLUMN)
            .eq(DEPOSIT_USER_COLUMN, username)
            .limit(1)
            .execute()
        )

        if not result.data:
            return False, (
                f"Счет пользователя '{username}' "
                f"не найден в {DEPOSITS_TABLE}"
            )

        current_balance = float(
            result.data[0].get(DEPOSIT_BALANCE_COLUMN) or 0
        )

        # ----------------------------------------------------
        # ПРИБАВЛЯЕМ сумму платежа
        # ----------------------------------------------------

        new_balance = current_balance + float(amount)

        # ----------------------------------------------------
        # Обновляем баланс + дату изменения
        # ----------------------------------------------------

        update_data = {
            DEPOSIT_BALANCE_COLUMN: new_balance,
            DEPOSIT_UPDATED_COLUMN:
                datetime.now(timezone.utc).isoformat()
        }

        update_result = (
            supabase_client
            .table(DEPOSITS_TABLE)
            .update(update_data)
            .eq(DEPOSIT_USER_COLUMN, username)
            .execute()
        )

        if not update_result.data:
            return False, (
                "UPDATE deposits не вернул "
                "измененную запись"
            )

        print(
            f"✅ DEPOSIT UPDATE: "
            f"{username}: "
            f"{current_balance} + {amount} = {new_balance}"
        )

        return True, new_balance

    except Exception as e:
        print(f"❌ Ошибка UPDATE deposits: {e}")

        return False, str(e)


# ============================================================
# HOME PAGE
# ============================================================

@app.get("/")
async def home():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Donations</title>

        <style>
            body {
                font-family: Arial, sans-serif;
                max-width: 900px;
                margin: 40px auto;
                padding: 20px;
            }

            input, textarea, button {
                width: 100%;
                padding: 10px;
                margin-top: 8px;
                margin-bottom: 15px;
                box-sizing: border-box;
            }

            button {
                cursor: pointer;
            }

            .donation {
                border: 1px solid #ddd;
                padding: 12px;
                margin-top: 10px;
                border-radius: 8px;
            }
        </style>
    </head>

    <body>

        <h1>Donation</h1>

        <form id="donationForm">

            <label>Username</label>
            <input
                type="text"
                id="username"
                required
            >

            <label>Message</label>
            <textarea
                id="message"
            ></textarea>

            <label>Amount</label>
            <input
                type="number"
                id="amount"
                step="0.01"
                min="1"
                required
            >

            <button type="submit">
                Donate
            </button>

        </form>

        <div id="result"></div>

        <h2>Donations</h2>

        <div id="donations"></div>

        <script>

        const form = document.getElementById(
            "donationForm"
        );

        const result = document.getElementById(
            "result"
        );

        form.addEventListener(
            "submit",
            async (event) => {

                event.preventDefault();

                const username =
                    document.getElementById(
                        "username"
                    ).value;

                const message =
                    document.getElementById(
                        "message"
                    ).value;

                const amount =
                    parseFloat(
                        document.getElementById(
                            "amount"
                        ).value
                    );

                const response =
                    await fetch(
                        "/create-order",
                        {
                            method: "POST",
                            headers: {
                                "Content-Type":
                                    "application/json"
                            },

                            body: JSON.stringify({
                                username,
                                message,
                                amount
                            })
                        }
                    );

                const data =
                    await response.json();

                if (data.payment_url) {

                    result.innerHTML =
                        `<a href="${data.payment_url}"
                           target="_blank">
                           Оплатить
                         </a>`;

                } else {

                    result.textContent =
                        data.error || "Ошибка";

                }
            }
        );


        async function loadDonations() {

            try {

                const response =
                    await fetch(
                        "/get-donations"
                    );

                const data =
                    await response.json();

                const container =
                    document.getElementById(
                        "donations"
                    );

                container.innerHTML = "";

                for (
                    const donation of data
                ) {

                    const div =
                        document.createElement(
                            "div"
                        );

                    div.className = "donation";

                    div.innerHTML = `
                        <strong>
                            ${donation.username}
                        </strong>

                        — ${donation.amount}

                        <br>

                        ${donation.message}
                    `;

                    container.appendChild(div);
                }

            } catch (error) {

                console.error(error);

            }
        }


        setInterval(
            loadDonations,
            3000
        );

        loadDonations();

        </script>

    </body>
    </html>
    """


# ============================================================
# CREATE ORDER
# ============================================================

@app.post("/create-order")
async def create_order(order: DonationOrder):

    order_id = "ord_" + uuid.uuid4().hex

    DONATIONS_DB[order_id] = {
        "username": order.username,
        "message": order.message,
        "amount": order.amount,
        "status": "pending"
    }

    try:

        quickpay = Quickpay(
            receiver=YOOMONEY_WALLET,
            quickpay_form="shop",
            targets=order.message,
            paymentType="AC",
            sum=order.amount,
            label=order_id
        )

        payment_url = quickpay.redirected_url

        return {
            "order_id": order_id,
            "payment_url": payment_url
        }

    except Exception as e:

        print(f"❌ Ошибка создания заказа: {e}")

        return {
            "error": str(e)
        }


# ============================================================
# YOOMONEY WEBHOOK
# ============================================================

@app.post("/webhook")
async def webhook(request: Request):

    body = await request.body()

    try:

        form_data = parse_qs(
            body.decode("utf-8")
        )

        label = form_data.get(
            "label",
            [None]
        )[0]

        withdraw_amount = form_data.get(
            "withdraw_amount",
            ["0"]
        )[0]

        if not label:

            return {
                "status": "error",
                "message": "label отсутствует"
            }


        # ----------------------------------------------------
        # Проверяем заказ
        # ----------------------------------------------------

        order = DONATIONS_DB.get(label)

        if not order:

            print(
                f"❌ Заказ не найден: {label}"
            )

            return {
                "status": "error",
                "message": "order not found"
            }


        # ----------------------------------------------------
        # Защита от повторного webhook
        # ----------------------------------------------------

        if (
            label in PROCESSED_ORDERS
            or order.get("status") == "success"
        ):

            print(
                f"⚠️ Повторный webhook: {label}"
            )

            return {
                "status": "already_processed"
            }


        amount = float(withdraw_amount)

        username = order["username"]
        message = order["message"]


        # ----------------------------------------------------
        # ПОПОЛНЯЕМ DEPOSIT
        # ----------------------------------------------------

        success, balance_or_error = add_to_deposit(
            username=username,
            amount=amount
        )

        if not success:

            print(
                f"❌ Не удалось пополнить депозит: "
                f"{balance_or_error}"
            )

            return {
                "status": "error",
                "message": balance_or_error
            }


        # ----------------------------------------------------
        # Пишем платеж в Google Sheets
        # ----------------------------------------------------

        write_to_google_sheet(
            order_id=label,
            username=username,
            amount=amount,
            message=message,
            status="success"
        )


        # ----------------------------------------------------
        # Отмечаем заказ обработанным
        # ----------------------------------------------------

        order["status"] = "success"
        order["paid_amount"] = amount
        order["balance"] = balance_or_error

        PROCESSED_ORDERS.add(label)


        print(
            f"✅ Платеж обработан: "
            f"{username} +{amount}; "
            f"баланс = {balance_or_error}"
        )


        return {
            "status": "success",
            "order_id": label,
            "username": username,
            "amount": amount,
            "balance": balance_or_error
        }

    except Exception as e:

        print(f"❌ Webhook error: {e}")

        return {
            "status": "error",
            "message": str(e)
        }


# ============================================================
# CHECK STATUS
# ============================================================

@app.get("/check-status/{order_id}")
async def check_status(order_id: str):

    order = DONATIONS_DB.get(order_id)

    if not order:

        return {
            "status": "not_found"
        }

    return {
        "status": order.get("status"),
        "order_id": order_id,
        "username": order.get("username"),
        "amount": order.get("amount"),
        "paid_amount": order.get("paid_amount"),
        "balance": order.get("balance")
    }


# ============================================================
# GET DONATIONS
# ============================================================

@app.get("/get-donations")
async def get_donations():

    result = []

    for order_id, donation in DONATIONS_DB.items():

        if donation.get("status") == "success":

            result.append({
                "order_id": order_id,
                "username": donation.get("username"),
                "amount": donation.get("paid_amount"),
                "message": donation.get("message"),
                "balance": donation.get("balance")
            })

    return result


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                8000
            )
        )
    )
