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
import uvicorn


# ============================================================
# SUPABASE
# ============================================================

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Таблица и колонки оставлены ЖЕСТКО в коде.
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
        print(f"❌ Ошибка инициализации Supabase: {e}", flush=True)
else:
    print("⚠️ SUPABASE_URL или SUPABASE_KEY не настроены", flush=True)


app = FastAPI()


# Включаем CORS, чтобы HTML-виджет и сайт могли общаться с бэкендом
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

# Секретное слово из настроек HTTP-уведомлений YooMoney.
YOOMONEY_SECRET = os.environ.get("YOOMONEY_SECRET", "")


# База в памяти для связки ID заказа со зрителем.
DONATIONS_DB = {}

# Защита от повторной обработки одной операции.
PROCESSED_OPERATIONS = set()


class DonationOrder(BaseModel):
    username: str
    message: str
    amount: int


# ============================================================
# SUPABASE: ПОПОЛНЕНИЕ ДЕПОЗИТА
# ============================================================

def add_to_deposit(username, amount):
    """
    Читаем текущий balance пользователя из deposits,
    прибавляем сумму платежа и сохраняем новый balance.
    """

    if not supabase_client:
        return False, "Supabase не настроен"

    try:
        # Получаем текущий баланс.
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

        # Обновляем баланс и время изменения.
        update_result = (
            supabase_client
            .table(DEPOSITS_TABLE)
            .update({
                DEPOSIT_BALANCE_COLUMN: new_balance,
                DEPOSIT_UPDATED_COLUMN: datetime.datetime.now(
                    datetime.timezone.utc
                ).isoformat()
            })
            .eq(DEPOSIT_USER_COLUMN, username)
            .execute()
        )

        # У некоторых версий supabase-py UPDATE может вернуть пустой data,
        # даже когда запрос выполнен. Поэтому окончательно проверяем SELECT.
        if update_result.data is not None and len(update_result.data) == 0:
            print(
                "⚠️ Supabase UPDATE не вернул строки, проверяем SELECT",
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
        print(f"❌ Ошибка UPDATE deposits: {e}", flush=True)
        return False, str(e)


# ============================================================
# YOOMONEY SIGN
# ============================================================

def verify_yoomoney_sign(parsed_data):
    """
    Проверяет параметр sign по актуальной схеме YooMoney:
    - исключить sign;
    - отсортировать параметры по имени;
    - URL-кодировать значения;
    - собрать key=value через &;
    - HMAC-SHA256 с секретным ключом.
    """

    if not YOOMONEY_SECRET:
        print("❌ YOOMONEY_SECRET не настроен", flush=True)
        return False

    received_list = parsed_data.get("sign", [])
    received_sign = received_list[0] if received_list else ""

    if not received_sign:
        print("❌ YooMoney webhook без sign", flush=True)
        return False

    values = {}

    for key, value_list in parsed_data.items():
        if key == "sign":
            continue

        values[key] = value_list[0] if value_list else ""

    prepared = "&".join(
        f"{quote(key, safe='')}={quote(values[key], safe='')}"
        for key in sorted(values)
    )

    calculated_sign = hmac.new(
        YOOMONEY_SECRET.encode("utf-8"),
        prepared.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    valid = hmac.compare_digest(
        calculated_sign.lower(),
        received_sign.lower()
    )

    print(
        "✅ YooMoney sign OK" if valid else "❌ YooMoney sign INVALID",
        flush=True
    )

    return valid


# =====================================================================
# 1. UI ФРОНТЕНД (GET /)
# =====================================================================
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
                                try {
                                    const statusResp = await fetch(`/check-status?order_id=${data.order_id}`);
                                    const statusData = await statusResp.json();

                                    if (statusData.status === 'paid') {
                                        clearInterval(interval);
                                        submitBtn.innerText = `Успешно оплачено! 🎉`;
                                        submitBtn.style.background = "#2e7d32";
                                    }
                                } catch (e) {
                                    console.error(e);
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


# =====================================================================
# 2. ГЕНЕРАТОР ЗАКАЗОВ (POST /create-order)
# =====================================================================
@app.post("/create-order")
async def create_order(order: DonationOrder):
    order_id = f"ord_{uuid.uuid4().hex[:12]}"

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

    print(
        f"✅ Создан заказ {order_id}: {order.username}, {order.amount} руб.",
        flush=True
    )

    return JSONResponse(
        content={
            "url": quickpay.redirected_url,
            "order_id": order_id
        }
    )


# =====================================================================
# 3. YOOMONEY WEBHOOK (POST /webhook)
# =====================================================================
@app.post("/webhook")
async def handle_yoomoney_webhook(request: Request):
    body_bytes = await request.body()
    body_str = body_bytes.decode("utf-8")

    parsed_data = parse_qs(body_str, keep_blank_values=True)

    print(
        f"📩 YooMoney webhook: {parsed_data}",
        flush=True
    )

    # Проверяем подпись до начисления денег.
    if not verify_yoomoney_sign(parsed_data):
        return JSONResponse(
            status_code=403,
            content={"status": "invalid_signature"}
        )

    notification_type = parsed_data.get("notification_type", [""])[0]
    operation_id = parsed_data.get("operation_id", [""])[0]

    labels_list = parsed_data.get("label", [])
    incoming_label = labels_list[0] if labels_list else None

    # amount — сумма, которая зачислена на кошелек получателя.
    amounts_list = parsed_data.get("amount", ["0"])
    incoming_amount = amounts_list[0] if amounts_list else "0"

    # Оставляем fallback для совместимости со старым кодом.
    if incoming_amount in (None, "", "0", "0.00"):
        amounts_list = parsed_data.get("withdraw_amount", ["0"])
        incoming_amount = amounts_list[0] if amounts_list else "0"

    unaccepted = parsed_data.get("unaccepted", ["false"])[0]

    if notification_type not in ("p2p-incoming", "card-incoming"):
        print(
            f"⚠️ Неизвестный notification_type: {notification_type}",
            flush=True
        )
        return {"status": "ignored"}

    if unaccepted == "true":
        print("⚠️ Платеж unaccepted=true, не зачисляем", flush=True)
        return {"status": "unaccepted"}

    if not operation_id:
        print("⚠️ Webhook без operation_id", flush=True)
        return {"status": "no_operation_id"}

    if not incoming_label:
        print("⚠️ Получен вебхук без поля label", flush=True)
        return {"status": "no_label"}

    # Защита от повторного webhook.
    if operation_id in PROCESSED_OPERATIONS:
        print(
            f"⚠️ Повторный webhook operation_id={operation_id}",
            flush=True
        )
        return {
            "status": "ok",
            "already_processed": True
        }

    # Ищем заказ.
    if incoming_label not in DONATIONS_DB:
        print(
            f"⚠️ Получен вебхук для неизвестного ID заказа: {incoming_label}",
            flush=True
        )
        return {"status": "ok"}

    if DONATIONS_DB[incoming_label]["status"] == "success":
        PROCESSED_OPERATIONS.add(operation_id)
        return {
            "status": "ok",
            "already_processed": True
        }

    try:
        amount = float(incoming_amount)
    except (TypeError, ValueError):
        print(
            f"❌ Некорректная сумма в webhook: {incoming_amount}",
            flush=True
        )
        return {"status": "bad_amount"}

    if amount <= 0:
        print(f"❌ Сумма платежа <= 0: {amount}", flush=True)
        return {"status": "bad_amount"}

    # Проверяем, что пришло не меньше суммы заказа.
    expected_amount = float(DONATIONS_DB[incoming_label]["amount"])
    if amount < expected_amount:
        print(
            f"❌ Сумма меньше заказа: ожидалось {expected_amount}, получено {amount}",
            flush=True
        )
        return {"status": "amount_mismatch"}

    user = DONATIONS_DB[incoming_label]["username"]
    msg = DONATIONS_DB[incoming_label]["message"]

    print("\n🎉 ТРУ-АЛЬФА ХУК ОБРАБОТАН НА БАЙТАХ!", flush=True)
    print(
        f"ID заказа: {incoming_label} | От кого: {user} | Сумма: {amount} руб.",
        flush=True
    )
    print(f"Сообщение: {msg}", flush=True)
    print("=" * 40, flush=True)

    # ========================================================
    # Пополняем депозит в Supabase.
    # ========================================================
    success, result = add_to_deposit(user, amount)

    if not success:
        print(
            f"❌ Депозит НЕ пополнен: {result}",
            flush=True
        )

        # 500 заставит YooMoney повторить уведомление.
        return JSONResponse(
            status_code=500,
            content={
                "status": "deposit_update_error",
                "message": result
            }
        )

    # Помечаем заказ обработанным только ПОСЛЕ успешного UPDATE.
    DONATIONS_DB[incoming_label]["status"] = "success"
    DONATIONS_DB[incoming_label]["amount"] = amount
    DONATIONS_DB[incoming_label]["balance"] = result
    DONATIONS_DB[incoming_label]["operation_id"] = operation_id

    PROCESSED_OPERATIONS.add(operation_id)

    print(
        f"✅ Платеж зачислен: {user} + {amount} руб. | Новый баланс: {result}",
        flush=True
    )

    return {
        "status": "ok",
        "order_id": incoming_label,
        "username": user,
        "amount": amount,
        "balance": result
    }


# =====================================================================
# 4. ПРОВЕРКА СТАТУСА ДЛЯ UI (GET /check-status)
# =====================================================================
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


# =====================================================================
# HEALTH
# =====================================================================
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "supabase": supabase_client is not None,
        "yoomoney_wallet": bool(YOOMONEY_WALLET),
        "yoomoney_secret": bool(YOOMONEY_SECRET)
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
