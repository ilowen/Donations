import os
import uuid
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from yoomoney import Client, Quickpay
import uvicorn

app = FastAPI()

# Включаем CORS для фронтенда и OBS виджета
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 🔐 Настройки хостинга Render
YOOMONEY_WALLET = os.environ.get("YOOMONEY_WALLET", "КОШЕЛЕК_НЕ_НАСТРОЕН")
YOOMONEY_TOKEN = os.environ.get("YOOMONEY_TOKEN")

# Инициализируем клиент истории ЮMoney по Хабру
if YOOMONEY_TOKEN:
    try:
        yoomoney_client = Client(YOOMONEY_TOKEN)
        print("🚀 API ЮMoney успешно подключен!")
    except Exception as e:
        print(f"❌ Ошибка токена ЮMoney: {e}")
        yoomoney_client = None
else:
    yoomoney_client = None

# 📦 НАША БАЗА ДАННЫХ В ПАМЯТИ (ИНТЕРНЕТ-МАГАЗИН)
# Связываем ID заказа с "товаром" (Ник + Текст). Формат: { "order_id": {"username": "Ник", "message": "Текст", "status": "pending"} }
DONATIONS_DB = {}

class DonationOrder(BaseModel):
    username: str
    message: str
    amount: int

# =====================================================================
# UI ФРОНТЕНД (GET /) - Форма отправляет fetch и ждет ответа бэкенда
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
                .error { color: red; font-size: 13px; margin-top: 5px; display: none; }
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
                    <div id="errorMsg" class="error">Ошибка сервера.</div>
                </form>
                <p style="color: gray; font-size: 11px; margin-top: 15px;">Донат-сервер: СТАТУС АКТИВЕН 🟢</p>
            </div>

            <script>
                async function sendDonationRequest(event) {
                    event.preventDefault();
                    
                    const submitBtn = document.getElementById('submitBtn');
                    const errorMsg = document.getElementById('errorMsg');
                    
                    submitBtn.innerText = "Создание заказа...";
                    submitBtn.disabled = true;
                    errorMsg.style.display = "none";

                    const payload = {
                        username: document.getElementById('username').value.trim() || 'Аноним',
                        message: document.getElementById('message').value.trim() || 'Без сообщения',
                        amount: parseInt(document.getElementById('amount').value) || 100
                    };

                    try {
                        // 1. Отправляем "товар" на бэкенд
                        const response = await fetch('/create-order', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify(payload)
                        });

                        if (!response.ok) throw new Error("Server error");
                        const data = await response.json();
                        
                        // 2. Бэкенд вернул ID заказа внутри ссылки и сам ID для отслеживания
                        if (data.url && data.order_id) {
                            // Открываем платежку в новом окне, чтобы UI не закрывался и следил за статусом
                            window.open(data.url, '_blank');
                            submitBtn.innerText = "Ожидание оплаты...";
                            
                            // 3. Опрашиваем бэкенд по ID заказа, пока ЮMoney не пришлет хук
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
                            throw new Error("Invalid response");
                        }
                    } catch (err) {
                        console.error(err);
                        submitBtn.innerText = "Поддержать";
                        submitBtn.disabled = false;
                        errorMsg.style.display = "block";
                    }
                }
            </script>
        </body>
    </html>
    """

# =====================================================================
# ГЕНЕРАТОР ЗАКАЗОВ (POST /create-order)
# =====================================================================
@app.post("/create-order")
async def create_order(order: DonationOrder):
    # Генерируем чистый ID заказа (label) по Хабру
    order_id = f"ord_{uuid.uuid4().hex[:12]}"
    
    # 📦 ПРИВЯЗЫВАЕМ ТОВАР К ЗАКАЗУ В ПАМЯТИ
    DONATIONS_DB[order_id] = {
        "username": order.username,
        "message": order.message,
        "amount": order.amount,
        "status": "pending"
    }
    
    # Генерируем ссылку ЮMoney. В label улетает ТОЛЬКО ID заказа
    quickpay = Quickpay(
        receiver=YOOMONEY_WALLET,
        quickpay_form="shop",
        targets="Поддержка стрима",
        paymentType="AC",
        sum=order.amount,
        label=order_id  # Чистый ID заказа уходит в ЮMoney!
    )
    
    # Возвращаем ссылку на оплату и ID заказа обратно в UI
    return JSONResponse(content={"url": quickpay.redirected_url, "order_id": order_id})

# =====================================================================
# ЛОВУШКА ХУКОВ (POST /webhook)
# =====================================================================
@app.post("/webhook")
async def handle_yoomoney_webhook(request: Request):
    form_data = await request.form()
    incoming_label = form_data.get("label") # Сюда прилетит наш ID заказа
    
    if not incoming_label:
        return {"status": "no_label"}

    # Находим заказ в памяти по прилетевшему ID заказа (label)
    if incoming_label in DONATIONS_DB:
        # Меняем статус заказа на оплаченный
        DONATIONS_DB[incoming_label]["status"] = "success"
        
        # Если подключен токен Хабра, можем для верности перепроверить через историю
        if yoomoney_client:
            try:
                history = yoomoney_client.operation_history(label=incoming_label)
                if history and history.operations:
                    DONATIONS_DB[incoming_label]["amount"] = history.operations[0].amount
            except Exception as e:
                print(f"⚠️ Ошибка сверки истории: {e}")

        print(f"🎉 ЗАКАЗ ОПЛАЧЕН! ID: {incoming_label} | Ник: {DONATIONS_DB[incoming_label]['username']} | Текст: {DONATIONS_DB[incoming_label]['message']}")
    else:
        print(f"⚠️ Вебхук для неизвестного ID заказа: {incoming_label}")
        
    return {"status": "ok"}

# =====================================================================
# ПРОВЕРКА СТАТУСА ДЛЯ UI (GET /check-status)
# =====================================================================
@app.get("/check-status")
async def check_status(order_id: str):
    if order_id in DONATIONS_DB and DONATIONS_DB[order_id]["status"] == "success":
        return {"status": "paid"}
    return {"status": "pending"}

# =====================================================================
# РУЧКА ДЛЯ OBS (GET /get-donations)
# =====================================================================
@app.get("/get-donations")
async def get_donations():
    paid_donations = []
    
    # Ищем в памяти все оплаченные заказы, чтобы отдать их на экран
    for order_id, info in list(DONATIONS_DB.items()):
        if info["status"] == "success":
            paid_donations.append({
                "id": order_id,
                "username": info["username"],
                "message": info["message"],
                "amount": info["amount"]
            })
            # Стираем заказ из памяти после выдачи в OBS, чтобы не дублировать
            del DONATIONS_DB[order_id]
            
    return JSONResponse(content=paid_donations)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
