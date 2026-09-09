import os
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs

import gspread
from google.oauth2.service_account import Credentials

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
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

# ============================================================
# SUPABASE TABLE
# ============================================================

DEPOSITS_TABLE = "deposits"

DEPOSIT_USER_COLUMN = "username"
DEPOSIT_BALANCE_COLUMN = "balance"
DEPOSIT_UPDATED_COLUMN = "updated_at"

# ============================================================
# GOOGLE SHEETS
# ============================================================

GOOGLE_CREDENTIALS_FILE = (
    "learned-pact-242010-54a8a1daf93f.json"
)


# ============================================================
# SUPABASE CLIENT
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
# ORDERS
# ============================================================

DONATIONS_DB = {}

# Защита от повторного webhook
PROCESSED_ORDERS = set()


# ============================================================
# MODELS
# ============================================================

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

        sheet = gc.open_by_key(
            GOOGLE_SHEET_ID
        ).sheet1

        now = datetime.now(
            timezone.utc
        ).isoformat()

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
        print(
            f"❌ Ошибка Google Sheets: {e}"
        )


# ============================================================
# ADD MONEY TO DEPOSIT
# ============================================================

def add_to_deposit(
    username: str,
    amount: float
):
    """
    Получает текущий balance и прибавляет amount.

    Например:

    balance = 1000
    amount = 500

    result = 1500
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
            .select(
                f"{DEPOSIT_BALANCE_COLUMN}"
            )
            .eq(
                DEPOSIT_USER_COLUMN,
                username
            )
            .limit(1)
            .execute()
        )

        if not result.data:
            return False, (
                f"Пользователь '{username}' "
                f"не найден в таблице "
                f"{DEPOSITS_TABLE}"
            )

        current_balance = float(
            result.data[0].get(
                DEPOSIT_BALANCE_COLUMN
            ) or 0
        )

        # ----------------------------------------------------
        # ПРИБАВЛЯЕМ сумму
        # ----------------------------------------------------

        new_balance = (
            current_balance + float(amount)
        )

        # ----------------------------------------------------
        # UPDATE balance + updated_at
        # ----------------------------------------------------

        update_data = {
            DEPOSIT_BALANCE_COLUMN: new_balance,
            DEPOSIT_UPDATED_COLUMN:
                datetime.now(
                    timezone.utc
                ).isoformat()
        }

        (
            supabase_client
            .table(DEPOSITS_TABLE)
            .update(update_data)
            .eq(
                DEPOSIT_USER_COLUMN,
                username
            )
            .execute()
        )

        # ----------------------------------------------------
        # Проверяем, что действительно записалось
        # ----------------------------------------------------

        verify = (
            supabase_client
            .table(DEPOSITS_TABLE)
            .select(
                f"{DEPOSIT_BALANCE_COLUMN},"
                f"{DEPOSIT_UPDATED_COLUMN}"
            )
            .eq(
                DEPOSIT_USER_COLUMN,
                username
            )
            .limit(1)
            .execute()
        )

        if not verify.data:
            return False, (
                "Не удалось проверить UPDATE deposits"
            )

        saved_balance = float(
            verify.data[0].get(
                DEPOSIT_BALANCE_COLUMN
            ) or 0
        )

        print(
            f"✅ DEPOSIT UPDATE: "
            f"{username}: "
            f"{current_balance} + {amount} "
            f"= {saved_balance}"
        )

        return True, saved_balance

    except Exception as e:

        print(
            f"❌ Ошибка UPDATE deposits: {e}"
        )

        return False, str(e)


# ============================================================
# PAYMENT PAGE
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def home():

    html = """
<!DOCTYPE html>
<html lang="ru">

<head>

    <meta charset="UTF-8">

    <meta
        name="viewport"
        content="width=device-width, initial-scale=1.0"
    >

    <title>Donation</title>

    <style>

        * {
            box-sizing: border-box;
        }

        body {
            font-family: Arial, sans-serif;
            max-width: 700px;
            margin: 40px auto;
            padding: 20px;
            background: #f5f5f5;
        }

        .container {
            background: white;
            padding: 25px;
            border-radius: 12px;
        }

        h1 {
            margin-top: 0;
        }

        label {
            display: block;
            margin-top: 15px;
            margin-bottom: 5px;
        }

        input,
        textarea,
        button {
            width: 100%;
            padding: 12px;
            font-size: 16px;
        }

        textarea {
            min-height: 100px;
            resize: vertical;
        }

        button {
            margin-top: 20px;
            border: none;
            border-radius: 8px;
            cursor: pointer;
        }

        #result {
            margin-top: 20px;
        }

        .donation {
            background: white;
            padding: 15px;
            margin-top: 10px;
            border-radius: 8px;
        }

    </style>

</head>

<body>

<div class="container">

    <h1>Пополнение депозита</h1>

    <form id="donationForm">

        <label for="username">
            Username
        </label>

        <input
            type="text"
            id="username"
            placeholder="testuser"
            required
        >

        <label for="message">
            Сообщение
        </label>

        <textarea
            id="message"
            placeholder="Комментарий"
        ></textarea>

        <label for="amount">
            Сумма
        </label>

        <input
            type="number"
            id="amount"
            min="1"
            step="0.01"
            placeholder="500"
            required
        >

        <button type="submit">
            Оплатить
        </button>

    </form>

    <div id="result"></div>

    <hr>

    <h2>Последние платежи</h2>

    <div id="donations"></div>

</div>


<script>

const form =
    document.getElementById("donationForm");

const result =
    document.getElementById("result");


// ============================================================
// CREATE PAYMENT
// ============================================================

form.addEventListener(
    "submit",
    async function(event) {

        event.preventDefault();

        result.textContent =
            "Создание платежа...";

        const username =
            document
                .getElementById("username")
                .value
                .trim();

        const message =
            document
                .getElementById("message")
                .value;

        const amount =
            parseFloat(
                document
                    .getElementById("amount")
                    .value
            );

        try {

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
                            username: username,
                            message: message,
                            amount: amount
                        })
                    }
                );

            const data =
                await response.json();

            if (
                data.payment_url
            ) {

                result.innerHTML =
                    '<a href="' +
                    data.payment_url +
                    '" target="_blank">' +
                    'Перейти к оплате' +
                    '</a>';

                // Можно сразу открыть YooMoney
                window.location.href =
                    data.payment_url;

            } else {

                result.textContent =
                    data.error ||
                    "Ошибка создания платежа";
            }

        } catch (error) {

            console.error(error);

            result.textContent =
                "Ошибка соединения с сервером";
        }

    }
);


// ============================================================
// LOAD DONATIONS
// ============================================================

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

            div.className =
                "donation";

            div.innerHTML =
                "<strong>" +
                escapeHtml(
                    donation.username
                ) +
                "</strong>" +
                " — " +
                donation.amount +
                "<br>" +
                escapeHtml(
                    donation.message || ""
                );

            container.appendChild(div);
        }

    } catch (error) {

        console.error(error);

    }
}


// ============================================================
// BASIC HTML ESCAPE
// ============================================================

function escapeHtml(value) {

    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}


// ============================================================

setInterval(
    loadDonations,
    3000
);

loadDonations();

</script>

</body>
</html>
"""

    return HTMLResponse(
        content=html
    )


# ============================================================
# CREATE ORDER
# ============================================================

@app.post("/create-order")
async def create_order(
    order: DonationOrder
):

    if order.amount <= 0:
        return {
            "error": "Сумма должна быть больше 0"
        }

    if not order.username.strip():
        return {
            "error": "Username не указан"
        }

    order_id = (
        "ord_" +
        uuid.uuid4().hex
    )

    # Сохраняем заказ
    DONATIONS_DB[order_id] = {
        "username":
            order.username.strip(),

        "message":
            order.message,

        "amount":
            float(order.amount),

        "status":
            "pending"
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

        payment_url =
            quickpay.redirected_url

        print(
            f"✅ Создан заказ: "
            f"{order_id} / "
            f"{order.username} / "
            f"{order.amount}"
        )

        return {
            "order_id":
                order_id,

            "payment_url":
                payment_url
        }

    except Exception as e:

        print(
            f"❌ Ошибка создания заказа: {e}"
        )

        return {
            "error": str(e)
        }


# ============================================================
# YOOMONEY WEBHOOK
# ============================================================

@app.post("/webhook")
async def webhook(
    request: Request
):

    body = await request.body()

    try:

        # ----------------------------------------------------
        # YooMoney присылает x-www-form-urlencoded
        # ----------------------------------------------------

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

        print(
            f"📩 YooMoney webhook: "
            f"label={label}, "
            f"amount={withdraw_amount}"
        )

        # ----------------------------------------------------

        if not label:

            return {
                "status": "error",
                "message":
                    "label отсутствует"
            }

        # ----------------------------------------------------
        # Ищем заказ
        # ----------------------------------------------------

        order =
            DONATIONS_DB.get(label)

        if not order:

            print(
                f"❌ Заказ не найден: {label}"
            )

            return {
                "status": "error",
                "message":
                    "order not found"
            }

        # ----------------------------------------------------
        # Защита от повторного webhook
        # ----------------------------------------------------

        if (
            label in PROCESSED_ORDERS
            or order.get("status")
                == "success"
        ):

            print(
                f"⚠️ Повторный webhook: "
                f"{label}"
            )

            return {
                "status":
                    "already_processed"
            }

        # ----------------------------------------------------
        # Получаем сумму
        # ----------------------------------------------------

        amount = float(
            withdraw_amount
        )

        if amount <= 0:

            return {
                "status": "error",
                "message":
                    "Некорректная сумма"
            }

        username =
            order["username"]

        message =
            order["message"]

        # ----------------------------------------------------
        # ПОПОЛНЯЕМ DEPOSIT
        # ----------------------------------------------------

        success, balance_or_error =
            add_to_deposit(
                username=username,
                amount=amount
            )

        if not success:

            print(
                f"❌ Не удалось пополнить "
                f"депозит: "
                f"{balance_or_error}"
            )

            return {
                "status": "error",
                "message":
                    balance_or_error
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

        order["status"] =
            "success"

        order["paid_amount"] =
            amount

        order["balance"] =
            balance_or_error

        PROCESSED_ORDERS.add(
            label
        )

        # ----------------------------------------------------

        print(
            f"✅ Платеж обработан: "
            f"{username} +{amount} "
            f"→ balance {balance_or_error}"
        )

        return {
            "status":
                "success",

            "order_id":
                label,

            "username":
                username,

            "amount":
                amount,

            "balance":
                balance_or_error
        }

    except Exception as e:

        print(
            f"❌ Webhook error: {e}"
        )

        return {
            "status":
                "error",

            "message":
                str(e)
        }


# ============================================================
# CHECK STATUS
# ============================================================

@app.get(
    "/check-status/{order_id}"
)
async def check_status(
    order_id: str
):

    order =
        DONATIONS_DB.get(
            order_id
        )

    if not order:

        return {
            "status":
                "not_found"
        }

    return {

        "status":
            order.get("status"),

        "order_id":
            order_id,

        "username":
            order.get("username"),

        "amount":
            order.get("amount"),

        "paid_amount":
            order.get("paid_amount"),

        "balance":
            order.get("balance")
    }


# ============================================================
# GET DONATIONS
# ============================================================

@app.get("/get-donations")
async def get_donations():

    result = []

    for (
        order_id,
        donation
    ) in DONATIONS_DB.items():

        if (
            donation.get("status")
            == "success"
        ):

            result.append({

                "order_id":
                    order_id,

                "username":
                    donation.get(
                        "username"
                    ),

                "amount":
                    donation.get(
                        "paid_amount"
                    ),

                "message":
                    donation.get(
                        "message"
                    ),

                "balance":
                    donation.get(
                        "balance"
                    )
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
                "8000"
            )
        )
    )
