import os
import uuid
from urllib.parse import parse_qs
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from yoomoney import Quickpay
import uvicorn

app = FastAPI()

# Включаем CORS для фронтенда и OBS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 🔐 Настройки кошелька из ENV-переменных Render
YOOMONEY_WALLET = os.environ.get("YOOMONEY_WALLET", "КОШЕЛЕК_НЕ_НАСТРОЕН")

# 📦 База данных в оперативной памяти (ID заказа -> Ник + Сообщение)
DONATIONS_DB = {}

class DonationOrder(BaseModel):
    username: str
    message: str
    amount: int

# UI ФРОНТЕНД (GET /)
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

# ГЕНЕРАТОР ЗАКАЗОВ (POST /create-order)
@app.post("/create-order")
async def create_order(order: DonationOrder):
    order_id = f"ord_{uuid.uuid4().hex[:12]}"
    
    # Сохраняем "товар" в память
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
# ЛОВУШКА ХУКОВ НА ЧИСТЫХ БАЙТАХ (POST /webhook)
# =====================================================================
@app.post("/webhook")
async def handle_yoomoney_webhook(request: Request):
    # 读取原始字节，不再使用 request.form()，彻底摆脱 python-multipart
    body_bytes = await request.body()
    body_str = body_bytes.decode('utf-8')
    
    # Парсим строку параметров формы в удобный словарь Python
    parsed_data = parse_qs(body_str)
    
    # parse_qs возвращает значения списками, забираем первые элементы
    incoming_label = parsed_data.get("label", [None])[0]
    withdraw_amount = parsed_data.get("withdraw_amount", ["0"])[0]
    
    if not incoming_label:
        return {"status": "no_label"}

    # Находим заказ в оперативной памяти по ID (label)
    if incoming_label in DONATIONS_DB:
        DONATIONS_DB[incoming_label]["status"] = "success"
        DONATIONS_DB[incoming_label]["amount"] = withdraw_amount
        
        print(f"\n🎉 ТРУ-АЛЬФА ХУК ОБРАБОТАН НА БАЙТАХ!")
        print(f"ID заказа: {incoming_label}")
        print(f"От кого: {DONATIONS_DB[incoming_label]['username']}")
        print(f"Сумма: {withdraw_amount} руб.")
        print(f"Сообщение: {DONATIONS_DB[incoming_label]['message']}")
        print("=" * 40)
    else:
        print(f"⚠️ Получен вебхук для неизвестного ID заказа: {incoming_label}")
        
    return {"status": "ok"}

# ПРОВЕРКА СТАТУСА ДЛЯ UI (GET /check-status)
@app.get("/check-status")
async def check_status(order_id: str):
    if order_id in DONATIONS_DB and DONATIONS_DB[order_id]["status"] == "success":
        return {"status": "paid"}
    return {"status": "pending"}

# РУЧКА ДЛЯ OBS (GET /get-donations)
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
