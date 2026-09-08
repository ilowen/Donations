import os
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from yoomoney import Client
import uvicorn

app = FastAPI()

# Включаем CORS, чтобы HTML-виджет из OBS мог без ошибок забирать донаты
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 🔐 БЕЗОПАСНЫЕ НАСТРОЙКИ ИЗ ENV-ПЕРЕМЕННЫХ RENDER
YOOMONEY_WALLET = os.environ.get("YOOMONEY_WALLET", "КОШЕЛЕК_НЕ_НАСТРОЕН")
YOOMONEY_TOKEN = os.environ.get("YOOMONEY_TOKEN")

# Инициализируем клиента ЮMoney для проверки истории
if YOOMONEY_TOKEN:
    try:
        yoomoney_client = Client(YOOMONEY_TOKEN)
        print("🚀 API ЮMoney успешно подключен!")
    except Exception as e:
        print(f"❌ Ошибка токена ЮMoney: {e}")
        yoomoney_client = None
else:
    print("⚠️ Переменная YOOMONEY_TOKEN не найдена. Сообщения вытягиваться не будут.")
    yoomoney_client = None

# Очередь донатов в оперативной памяти сервера для OBS
DONATIONS_QUEUE = []

# =====================================================================
# 1. ГЛАВНАЯ СТРАНИЦА ОПЛАТЫ (GET /) - Динамическая форма
# =====================================================================
@app.get("/", response_class=HTMLResponse)
async def home_page():
    return f"""
    <html>
        <head>
            <meta charset="UTF-8">
            <title>YooMoney Alpha Donat</title>
            <style>
                body {{ font-family: Arial, sans-serif; max-width: 400px; margin: 50px auto; padding: 20px; background: #f4f4f9; text-align: center; }}
                .card {{ background: white; padding: 30px; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }}
                input, textarea, button {{ width: 100%; padding: 12px; margin: 8px 0; border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box; }}
                button {{ background: #8a2be2; color: white; font-weight: bold; cursor: pointer; border: none; font-size: 16px; }}
                button:hover {{ background: #6a1b9a; }}
                textarea {{ resize: none; height: 80px; }}
                h2 {{ color: #333; }}
            </style>
        </head>
        <body>
            <div class="card">
                <h2>Отправить донат</h2>
                
                <!-- Официальный шлюз Quickpay. Браузер сам перейдет на сайт оплаты -->
                <form action="https://yoomoney.ru" method="POST" onsubmit="prepareDonation(event, this)">
                    
                    <input type="hidden" name="receiver" value="{YOOMONEY_WALLET}">
                    <input type="hidden" name="quickpay-form" value="button">
                    <input type="hidden" name="targets" value="Поддержка стрима">
                    <input type="hidden" name="paymentType" value="AC">
                    
                    <!-- Скрытое поле label, куда упакуем ник и сообщение -->
                    <input type="hidden" name="label" id="yoomoney-label">

                    <input type="text" id="username" placeholder="Ваш никнейм" required maxlength="15">
                    <textarea id="message" placeholder="Текст сообщения..." maxlength="40"></textarea>
                    <input type="number" name="sum" placeholder="Сумма (руб)" min="2" value="100" required>
                    
                    <button type="submit">Поддержать</button>
                </form>
                <p style="color: gray; font-size: 11px; margin-top: 15px;">Донат-сервер: СТАТУС АКТИВЕН 🟢</p>
            </div>

            <script>
                function prepareDonation(event, form) {{
                    const nick = document.getElementById('username').value.trim() || 'Аноним';
                    const msg = document.getElementById('message').value.trim() || 'Без сообщения';
                    
                    // Обрезаем, чтобы гарантированно влезть в лимит label от ЮMoney (64 символа)
                    const cleanNick = nick.substring(0, 15);
                    const cleanMsg = msg.substring(0, 40);
                    
                    document.getElementById('yoomoney-label').value = cleanNick + "|||" + cleanMsg;
                }}
            </script>
        </body>
    </html>
    """

# =====================================================================
# 2. ЛОВУШКА ХУКОВ + ФИЛЬТР ИСТОРИИ ХАБРА (POST /webhook)
# =====================================================================
@app.post("/webhook")
async def handle_yoomoney_webhook(request: Request):
    form_data = await request.form()
    
    # ЮMoney присылает вебхук. Достаем label, который мы сгенерировали
    incoming_label = form_data.get("label")
    
    if not incoming_label:
        return {"status": "no_label"}

    username = "Аноним"
    message = "Без сообщения"
    amount = form_data.get("withdraw_amount", "0")

    # 🔥 ФИШКА ИЗ СТАТЬИ НА ХАБРЕ: Используем историю API для вытягивания точных данных
    if yoomoney_client:
        try:
            # Запрашиваем историю операций конкретно по этому label
            history = yoomoney_client.operation_history(label=incoming_label)
            
            if history and history.operations:
                # Берем подтвержденную операцию из истории кошелька
                operation = history.operations[0]
                amount = operation.amount  # Берем чистую сумму
                
                # Достаем нашу склеенную JS-строку, которая сохранилась в истории ЮMoney
                raw_label = operation.label
                if raw_label and "|||" in raw_label:
                    username, message = raw_label.split("|||", 1)
                elif raw_label:
                    username = raw_label
        except Exception as e:
            print(f"⚠️ Ошибка фильтрации истории через API: {e}")
            # Если API упало, пытаемся распарсить голый входящий label из вебхука
            if "|||" in incoming_label:
                username, message = incoming_label.split("|||", 1)

    print(f"\n🎉 ХАБР-АЛЬФА ХУК ОБРАБОТАН!")
    print(f"От кого: {username} | Сумма: {amount} руб.")
    print(f"Сообщение: {message}")
    print("=" * 40)
    
    # Добавляем данные в оперативку для нашего OBS-виджета
    DONATIONS_QUEUE.append({
        "username": username,
        "amount": amount,
        "message": message
    })
    
    return {"status": "ok"}

# =====================================================================
# 3. РУЧКА ДЛЯ OBS (GET /get-donations)
# =====================================================================
@app.get("/get-donations")
async def get_donations():
    current_donations = list(DONATIONS_QUEUE)
    DONATIONS_QUEUE.clear()
    return JSONResponse(content=current_donations)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
