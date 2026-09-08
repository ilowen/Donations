import os
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from yoomoney import Client, Quickpay
import uvicorn

app = FastAPI()

# Включаем CORS, чтобы фронтенд мог слать fetch-запросы на бэкенд без блокировок
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 🔐 Читаем кошелек и токен из переменных окружения хостинга Render
# (В репозитории данные не светятся)
YOOMONEY_WALLET = os.environ.get("YOOMONEY_WALLET", "КОШЕЛЕК_НЕ_НАСТРОЕН")
YOOMONEY_TOKEN = os.environ.get("YOOMONEY_TOKEN")

# Инициализируем клиент истории ЮMoney
if YOOMONEY_TOKEN:
    try:
        yoomoney_client = Client(YOOMONEY_TOKEN)
        print("🚀 API ЮMoney успешно подключен!")
    except Exception as e:
        print(f"❌ Ошибка токена ЮMoney: {e}")
        yoomoney_client = None
else:
    print("⚠️ Переменная YOOMONEY_TOKEN не найдена.")
    yoomoney_client = None

# Очередь донатов в оперативной памяти сервера для OBS виджета
DONATIONS_QUEUE = []

# Модель данных, которую мы ждем от UI через fetch
class DonationOrder(BaseModel):
    username: str
    message: str
    amount: int

# =====================================================================
# 1. ИНТЕРФЕЙС (GET /) - Красивый UI с fetch-запросом
# =====================================================================
@app.get("/", response_class=HTMLResponse)
async def home_page():
    return """
    <html>
        <head>
            <meta charset="UTF-8">
            <title>YooMoney True Alpha Donat</title>
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
                
                <!-- Форма БЕЗ атрибутов action и method, управляется чисто через JS -->
                <form id="donationForm" onsubmit="sendDonationRequest(event)">
                    <input type="text" id="username" placeholder="Ваш никнейм" required maxlength="15">
                    <textarea id="message" placeholder="Текст сообщения..." maxlength="40"></textarea>
                    <input type="number" id="amount" placeholder="Сумма (руб)" min="2" value="100" required>
                    
                    <button type="submit" id="submitBtn">Поддержать</button>
                    <div id="errorMsg" class="error">Ошибка сервера. Попробуйте позже.</div>
                </form>
                <p style="color: gray; font-size: 11px; margin-top: 15px;">Донат-сервер: СТАТУС АКТИВЕН 🟢</p>
            </div>

            <script>
                async function sendDonationRequest(event) {
                    event.preventDefault(); // Стопаем стандартную отправку страницы
                    
                    const submitBtn = document.getElementById('submitBtn');
                    const errorMsg = document.getElementById('errorMsg');
                    
                    submitBtn.innerText = "Создание заказа...";
                    submitBtn.disabled = true;
                    errorMsg.style.display = "none";

                    // 1. Собираем данные из полей UI
                    const payload = {
                        username: document.getElementById('username').value.trim() || 'Аноним',
                        message: document.getElementById('message').value.trim() || 'Без сообщения',
                        amount: parseInt(document.getElementById('amount').value) || 100
                    };

                    try {
                        // 2. Пуляем асинхронный POST-запрос на ТВОЙ бэкенд /create-order
                        const response = await fetch('/create-order', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify(payload)
                        });

                        if (!response.ok) throw new Error("Server error");

                        // 3. Получаем сгенерированную бэкендом ссылку ЮMoney
                        const data = await response.json();
                        
                        // 4. Мягко перенаправляем зрителя на шлюз оплаты ЮMoney
                        if (data.url) {
                            window.location.href = data.url;
                        } else {
                            throw new Error("No URL returned");
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
# 2. БЭКЕНД-РОУТ: ПРИЕМ ЗАПРОСА И СОЗДАНИЕ ССЫЛКИ (POST /create-order)
# =====================================================================
@app.post("/create-order")
async def create_order(order: DonationOrder):
    # Чистим и обрезаем строки под лимиты label (64 символа)
    clean_nick = order.username[:15]
    clean_msg = order.message[:40]
    
    # Склеиваем ник и сообщение в одну строчку для label
    raw_label = f"{clean_nick}|||{clean_msg}"
    
    # Вызываем Quickpay из пакета yoomoney, он сам соберет легитимную ссылку
    quickpay = Quickpay(
        receiver=YOOMONEY_WALLET,
        quickpay_form="shop",
        targets="Поддержка стрима",
        paymentType="AC",  # Карты и SberPay
        sum=order.amount,
        label=raw_label
    )
    
    # Возвращаем готовую ссылку ЮMoney обратно в UI в формате JSON
    return JSONResponse(content={"url": quickpay.redirected_url})

# =====================================================================
# 3. ЛОВУШКА ХУКОВ + ФИЛЬТР ИСТОРИИ (POST /webhook)
# =====================================================================
@app.post("/webhook")
async def handle_yoomoney_webhook(request: Request):
    form_data = await request.form()
    incoming_label = form_data.get("label")
    
    if not incoming_label:
        return {"status": "no_label"}

    username = "Аноним"
    message = "Без сообщения"
    amount = form_data.get("withdraw_amount", "0")

    if yoomoney_client:
        try:
            # Вытягиваем через API историю конкретно по этому label
            history = yoomoney_client.operation_history(label=incoming_label)
            if history and history.operations:
                operation = history.operations[0]
                amount = operation.amount
                raw_label = operation.label
                
                if raw_label and "|||" in raw_label:
                    username, message = raw_label.split("|||", 1)
                elif raw_label:
                    username = raw_label
        except Exception as e:
            print(f"⚠️ Ошибка API ЮMoney при чтении истории: {e}")
            if "|||" in incoming_label:
                username, message = incoming_label.split("|||", 1)

    print(f"\n🎉 ТРУ-АЛЬФА ХУК ОБРАБОТАН!")
    print(f"От кого: {username} | Сумма: {amount} руб. | Текст: {message}")
    print("=" * 40)
    
    # Закидываем в очередь для OBS
    DONATIONS_QUEUE.append({
        "username": username,
        "amount": amount,
        "message": message
    })
    
    return {"status": "ok"}

# =====================================================================
# 4. РУЧКА ДЛЯ OBS (GET /get-donations)
# =====================================================================
@app.get("/get-donations")
async def get_donations():
    current_donations = list(DONATIONS_QUEUE)
    DONATIONS_QUEUE.clear()
    return JSONResponse(content=current_donations)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
