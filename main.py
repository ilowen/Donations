import os
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
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

# 📦 Очередь донатов в оперативной памяти сервера
DONATIONS_QUEUE = []

# =====================================================================
# 1. ГЛАВНАЯ СТРАНИЦА ОПЛАТЫ (GET /) - Форма для зрителя и пинг UptimeRobot
# =====================================================================
@app.get("/", response_class=HTMLResponse)
async def home_page():
    return """
    <html>
        <head>
            <meta charset="UTF-8">
            <title>YooMoney Alpha Donat</title>
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
                
                <!-- Официальный шлюз ЮMoney Quickpay (Прием Карт и SberPay) -->
                <form action="https://yoomoney.ru" method="POST" onsubmit="prepareDonation(event, this)">
                    
                    <!-- ⚠️ ЗАМЕНИ НА СВОЙ НОМЕР КОШЕЛЬКА ЮMONEY (15 ЦИФР) -->
                    <input type="hidden" name="receiver" value="41001XXXXXXXXXXXX">
                    <input type="hidden" name="quickpay-form" value="button">
                    <input type="hidden" name="targets" value="Поддержка стрима">
                    <input type="hidden" name="paymentType" value="AC">
                    
                    <!-- Скрытое поле label, куда JS упакует ник и сообщение -->
                    <input type="hidden" name="label" id="yoomoney-label">

                    <!-- Поля формы -->
                    <input type="text" id="username" placeholder="Ваш никнейм" required maxlength="20">
                    <textarea id="message" placeholder="Текст сообщения..." maxlength="150"></textarea>
                    <input type="number" name="sum" placeholder="Сумма (руб)" min="2" value="100" required>
                    
                    <button type="submit">Поддержать</button>
                </form>
                <p style="color: gray; font-size: 11px; margin-top: 15px;">Донат-сервер: СТАТУС АКТИВЕН 🟢</p>
            </div>

            <script>
                function prepareDonation(event, form) {
                    const nick = document.getElementById('username').value.trim() || 'Аноним';
                    const msg = document.getElementById('message').value.trim() || 'Без сообщения';
                    
                    // Ограничиваем длину сообщения, чтобы вся строка влезла в лимит label (64 символа)
                    // ЮMoney обрезает label, если он длиннее 64 символов. 
                    // Если нужно длинное сообщение — придется возвращаться к схеме с ID и базой в памяти.
                    const cleanNick = nick.substring(0, 15);
                    const cleanMsg = msg.substring(0, 40);
                    
                    document.getElementById('yoomoney-label').value = cleanNick + "|||" + cleanMsg;
                }
            </script>
        </body>
    </html>
    """

# =====================================================================
# 2. ЛОВУШКА ХУКОВ (POST /webhook) - Сюда ЮMoney шлет скрытый POST-запрос
# =====================================================================
@app.post("/webhook")
async def handle_yoomoney_webhook(request: Request):
    form_data = await request.form()
    
    # Достаем нашу склеенную строку из поля label
    raw_data = form_data.get("label", "Аноним|||Без сообщения")
    amount = form_data.get("withdraw_amount", "0")
    
    # Распиливаем строку обратно на Ник и Сообщение
    if "|||" in raw_data:
        username, message = raw_data.split("|||", 1)
    else:
        username, message = raw_data, "Без сообщения"
        
    print(f"\n🎉 АЛЬФА-ХУК ПОЙМАН!")
    print(f"От кого: {username} | Сумма: {amount} руб.")
    print(f"Сообщение: {message}")
    print("=" * 40)
    
    # Складываем в оперативку для виджета OBS
    DONATIONS_QUEUE.append({
        "username": username,
        "amount": amount,
        "message": message
    })
    
    return {"status": "ok"}

# =====================================================================
# 3. РУЧКА ДЛЯ OBS ВИТДЖЕТА (GET /get-donations) - Отдает накопленные донаты
# =====================================================================
@app.get("/get-donations")
async def get_donations():
    current_donations = list(DONATIONS_QUEUE)
    DONATIONS_QUEUE.clear() # Очищаем очередь, чтобы не крутить по кругу
    return JSONResponse(content=current_donations)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
