import os
import uuid
import json
import datetime
from urllib.parse import parse_qs
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from yoomoney import Quickpay
import uvicorn


# Supabase configuration - will be checked at runtime
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")


app = FastAPI()


# Включаем CORS, чтобы HTML-виджет из OBS и сайт могли общаться с бэкендом
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 🔐 Читаем настройки из обычных переменных окружения Render
YOOMONEY_WALLET = os.environ.get("YOOMONEY_WALLET", "КОШЕЛЕК_НЕ_НАСТРОЕН")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")


# База данных в оперативной памяти для связки ID заказа со зрителем
DONATIONS_DB = {}


class DonationOrder(BaseModel):
    username: str
    message: str
    amount: int


# Функция записи в Google Таблицу (резервная)
def write_to_google_sheet(username, amount, message):
    """
    Записывает запись в Google Sheets (используется только если Supabase недоступен).
    """
    secret_file_path = "learned-pact-242010-54a8a1daf93f.json"
    
    if not GOOGLE_SHEET_ID:
        print("⚠️ Переменная GOOGLE_SHEET_ID не настроена в Environment!", flush=True)
        return
        
    if not os.path.exists(secret_file_path):
        print(f"⚠️ Секретный файл {secret_file_path} не найден в корне проекта!", flush=True)
        return
        
    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        
        # Авторизуемся под видом нашего сервисного аккаунта напрямую из секретного файла
        creds = Credentials.from_service_account_file(secret_file_path, scopes=scopes)
        client = gspread.authorize(creds)
        
        # Открываем таблицу по ID и берем первый лист
        sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
        
        # Текущее время
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Дописываем строку в конец таблицы: [Время, Ник, Сумма, Сообщение]
        sheet.append_row([current_time, username, amount, message])
        print("📊 Строка успешно записана в Google Таблицу через Secret File!", flush=True)
    except Exception as e:
        print(f"❌ КРИТИЧЕСКАЯ ОШИБКА ЗАПИСИ В GOOGLE ТАБЛИЦУ: {e}", flush=True)


# Новая функция для вставки в Supabase
def insert_into_supabase(order_id, username, message, amount, supabase_client=None):
    """
    Вставляет запись о заполнении депозита в Supabase.
    """
    # Проверяем, что supabase_client установлен и имеет метод query
    if not supabase_client or not hasattr(supabase_client, 'query'):
        print("⚠️ Supabase не настроен или не инициализирован", flush=True)
        return False
    
    try:
        query = """
        INSERT INTO donations (id, username, message, amount, created_at)
        VALUES ($1, $2, $3, $4, NOW())
        """
        result = supabase_client.query(query, [order_id, username, message, amount])
        print(f"✅ Запись успешно вставлена в Supabase (ID: {order_id})", flush=True)
        return True
    except Exception as e:
        print(f"❌ Ошибка при вставке в Supabase: {e}", flush=True)
        return False


# =====================================================================
# 1. UI ФРОНТЕНД (GET /) - Форма отправляет fetch и ждет ответ бэкенда
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
                                const statusResp = await fetch(`/check-status?order_id=${data.order_id}`);
                                const statusData = await statusResp.json();
                                if (statusData.status === 'paid') {
                                    clearInterval(interval);
                                    submitBtn.innerText = `Успешно оплачено! 🎉`;
                                    submitBtn.style.background = "#2e7d32";
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
    
    return JSONResponse(content={"url": quickpay.redirected_url, "order_id": order_id})


# =====================================================================
# 3. ЛОВУШКА ХУКОВ НА ЧИСТЫХ БАЙТАХ (POST /webhook)
# =====================================================================
@app.post("/webhook")
async def handle_yoomoney_webhook(request: Request):
    body_bytes = await request.body()
    body_str = body_bytes.decode('utf-8')
    
    parsed_data = parse_qs(body_str)
    
    labels_list = parsed_data.get("label", [])
    incoming_label = labels_list[0] if labels_list else None
    
    amounts_list = parsed_data.get("withdraw_amount", ["0"])
    withdraw_amount = amounts_list[0] if amounts_list else "0"
    
    if not incoming_label:
        print("⚠️ Получен вебхук без поля label", flush=True)
        return {"status": "no_label"}
    
    if incoming_label in DONATIONS_DB:
        DONATIONS_DB[incoming_label]["status"] = "success"
        DONATIONS_DB[incoming_label]["amount"] = withdraw_amount
        
        user = DONATIONS_DB[incoming_label]["username"]
        msg = DONATIONS_DB[incoming_label]["message"]

        print(f"\n🎉 ТРУ-АЛЬФА ХУК ОБРАБОТАН НА БАЙТАХ!", flush=True)
        print(f"ID заказа: {incoming_label} | От кого: {user} | Сумма: {withdraw_amount} руб.", flush=True)
        print(f"Сообщение: {msg}", flush=True)
        print("=" * 40, flush=True)
        
        # Записываем в Supabase вместо Google Sheets
        success = insert_into_supabase(incoming_label, user, msg, withdraw_amount, supabase_client)
        if success:
            print("✅ Запись в Supabase успешна", flush=True)
        else:
            print("❌ Не удалось записать в Supabase", flush=True)
    else:
        print(f"⚠️ Получен вебхук для неизвестного ID заказа: {incoming_label}", flush=True)
        
    return {"status": "ok"}


# =====================================================================
# 4. ПРОВЕРКА СТАТУСА ДЛЯ UI (GET /check-status)
# =====================================================================
@app.get("/check-status")
async def check_status(order_id: str = None):
    if not order_id:
        return {"status": "pending"}
    if order_id in DONATIONS_DB and DONATIONS_DB[order_id]["status"] == "success":
        return {"status": "paid"}
    return {"status": "pending"}


# =====================================================================
# 5. РУЧКА ДЛЯ OBS (GET /get-donations)
# =====================================================================
@app.get("/get-donations")
async def get_donations():
    paid_donations = []
    for order_id, info in list(DONATIONS_DB.items()):
        if info["status"] == "success":
            paid_donations.append({
                "id": order_id,
                "username": info["username"],
                "message": info["message"],
                "amount": info["amount"]
            })
            del DONATIONS_DB[order_id]
    return JSONResponse(content=paid_donations)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
