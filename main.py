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

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

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

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

YOOMONEY_WALLET = os.environ.get("YOOMONEY_WALLET", "КОШЕЛЕК_НЕ_НАСТРОЕН")
YOOMONEY_SECRET = os.environ.get("YOOMONEY_SECRET", "")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")

DONATIONS_DB = {}
PROCESSED_ORDERS = set()

class DonationOrder(BaseModel):
    username: str
    message: str
    amount: int


def verify_yoomoney_sign(parsed_data: dict) -> bool:
    """Проверка sign по алгоритму YooMoney: все POST-параметры кроме sign,
    сортировка по имени, RFC3986 URL encoding, HMAC-SHA256, HEX."""
    if not YOOMONEY_SECRET:
        print("❌ YOOMONEY_SECRET не настроен", flush=True)
        return False

    received_values = parsed_data.get("sign", [])
    received_sign = received_values[0] if received_values else ""
    if not received_sign:
        print("❌ В webhook отсутствует sign", flush=True)
        return False

    params = {}
    for key, values in parsed_data.items():
        if key == "sign":
            continue
        params[key] = values[0] if values else ""

    prepared = "&".join(
        f"{quote(key, safe='')}={quote(value, safe='-._~')}"
        for key, value in sorted(params.items())
    )

    calculated = hmac.new(
        YOOMONEY_SECRET.encode("utf-8"),
        prepared.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    ok = hmac.compare_digest(calculated.lower(), received_sign.lower())
    print("✅ YooMoney sign OK" if ok else "❌ YooMoney sign INVALID", flush=True)
    return ok


def write_to_google_sheet(username, amount, message, order_id=None):
    """Добавляет строку о пополнении в первый лист Google Sheets."""
    secret_file_path = "learned-pact-242010-54a8a1daf93f.json"

    if not GOOGLE_SHEET_ID:
        print("⚠️ GOOGLE_SHEET_ID не настроен", flush=True)
        return False
    if not os.path.exists(secret_file_path):
        print(f"⚠️ Секретный файл {secret_file_path} не найден", flush=True)
        return False

    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(secret_file_path, scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sheet.append_row([current_time, username, amount, message, order_id or ""])
        print("📊 Строка успешно записана в Google Таблицу!", flush=True)
        return True
    except Exception as e:
        print(f"❌ Ошибка записи в Google Таблицу: {e}", flush=True)
        return False


def add_to_deposit(username, amount):
    """Если пользователя нет — создаёт строку. Если есть — увеличивает balance."""
    if not supabase_client:
        return False, "Supabase не настроен"

    try:
        result = (
            supabase_client.table(DEPOSITS_TABLE)
            .select(DEPOSIT_BALANCE_COLUMN)
            .eq(DEPOSIT_USER_COLUMN, username)
            .limit(1)
            .execute()
        )

        # НОВЫЙ ПОЛЬЗОВАТЕЛЬ: создаём депозит с первой суммой.
        if not result.data:
            initial_balance = float(amount)
            insert_result = (
                supabase_client.table(DEPOSITS_TABLE)
                .insert({
                    DEPOSIT_USER_COLUMN: username,
                    DEPOSIT_BALANCE_COLUMN: initial_balance,
                    DEPOSIT_UPDATED_COLUMN: datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                })
                .execute()
            )

            if not insert_result.data:
                return False, f"Не удалось создать пользователя '{username}' в {DEPOSITS_TABLE}"

            saved_balance = float(
                insert_result.data[0].get(DEPOSIT_BALANCE_COLUMN, initial_balance)
            )
            print(
                f"✅ DEPOSIT CREATE: {username}: 0 + {amount} = {saved_balance}",
                flush=True,
            )
            return True, saved_balance

        # СУЩЕСТВУЮЩИЙ ПОЛЬЗОВАТЕЛЬ: увеличиваем баланс.
        current_balance = float(result.data[0].get(DEPOSIT_BALANCE_COLUMN) or 0)
        new_balance = current_balance + float(amount)

        supabase_client.table(DEPOSITS_TABLE).update({
            DEPOSIT_BALANCE_COLUMN: new_balance,
            DEPOSIT_UPDATED_COLUMN: datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat(),
        }).eq(DEPOSIT_USER_COLUMN, username).execute()

        verify = (
            supabase_client.table(DEPOSITS_TABLE)
            .select(DEPOSIT_BALANCE_COLUMN)
            .eq(DEPOSIT_USER_COLUMN, username)
            .limit(1)
            .execute()
        )
        if not verify.data:
            return False, "Не удалось проверить UPDATE deposits"

        saved_balance = float(verify.data[0].get(DEPOSIT_BALANCE_COLUMN) or 0)
        print(
            f"✅ DEPOSIT UPDATE: {username}: {current_balance} + {amount} = {saved_balance}",
            flush=True,
        )
        return True, saved_balance

    except Exception as e:
        print(f"❌ Ошибка INSERT/UPDATE deposits: {e}", flush=True)
        return False, str(e)


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
            textarea { resize: none; height: 80px; }
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
            <p id="status" style="color: gray; font-size: 11px; margin-top: 15px;">Донат-сервер: СТАТУС АКТИВЕН 🟢</p>
        </div>
        <script>
            async function sendDonationRequest(event) {
                event.preventDefault();
                const submitBtn = document.getElementById('submitBtn');
                const status = document.getElementById('status');
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
                                status.innerText = "Депозит пополнен.";
                            }
                        }, 3000);
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
        content={"url": quickpay.redirected_url, "order_id": order_id}
    )


@app.post("/webhook")
async def handle_yoomoney_webhook(request: Request):
    body_bytes = await request.body()
    body_str = body_bytes.decode("utf-8")
    parsed_data = parse_qs(body_str, keep_blank_values=True)

    print(f"📩 YooMoney webhook: {parsed_data}", flush=True)

    # Подлинность webhook проверяем по sign.
    if not verify_yoomoney_sign(parsed_data):
        return JSONResponse(
            status_code=403,
            content={"status": "invalid_signature"},
        )

    incoming_label = parsed_data.get("label", [""])[0]

    # ВНИМАНИЕ: amount — сумма операции, поступившая получателю.
    # Здесь намеренно НЕТ проверки against order amount.
    amount_raw = parsed_data.get("amount", ["0"])[0]

    if not incoming_label:
        print("⚠️ Получен вебхук без поля label", flush=True)
        return {"status": "no_label"}

    if incoming_label not in DONATIONS_DB:
        print(
            f"⚠️ Получен вебхук для неизвестного ID заказа: {incoming_label}",
            flush=True,
        )
        return {"status": "ok"}

    if incoming_label in PROCESSED_ORDERS:
        print(
            f"⚠️ Повторный webhook для уже обработанного заказа: {incoming_label}",
            flush=True,
        )
        return {"status": "ok", "already_processed": True}

    try:
        amount = float(amount_raw)
    except (TypeError, ValueError):
        print(f"❌ Некорректная сумма в webhook: {amount_raw}", flush=True)
        return {"status": "bad_amount"}

    if amount <= 0:
        return {"status": "bad_amount"}

    user = DONATIONS_DB[incoming_label]["username"]
    msg = DONATIONS_DB[incoming_label]["message"]

    print(
        f"\n🎉 ПЛАТЁЖ: {incoming_label} | {user} | {amount} руб.",
        flush=True,
    )

    # Сначала депозит.
    success, result = add_to_deposit(user, amount)
    if not success:
        print(f"❌ Депозит НЕ пополнен: {result}", flush=True)
        return {
            "status": "deposit_update_error",
            "message": result,
        }

    # После успешного депозита — запись в Excel/Google Sheets.
    write_to_google_sheet(
        user,
        amount,
        msg,
        incoming_label,
    )

    DONATIONS_DB[incoming_label]["status"] = "success"
    DONATIONS_DB[incoming_label]["amount"] = amount
    DONATIONS_DB[incoming_label]["balance"] = result
    PROCESSED_ORDERS.add(incoming_label)

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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
