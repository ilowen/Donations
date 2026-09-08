import os
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from yoomoney import Client
import uvicorn

app = FastAPI()

# Включаем CORS для виджета OBS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 🔐 БЕЗОПАСНОЕ ЧТЕНИЕ ТОКЕНА ИЗ ОКРУЖЕНИЯ (В репозитории пусто)
YOOMONEY_TOKEN = os.environ.get("YOOMONEY_TOKEN")

# Инициализируем клиент ЮMoney
if YOOMONEY_TOKEN:
    try:
        yoomoney_client = Client(YOOMONEY_TOKEN)
        print("🚀 API Клиент ЮMoney успешно инициализирован!")
    except Exception as e:
        print(f"❌ Ошибка авторизации токена ЮMoney: {e}")
        yoomoney_client = None
else:
    print("⚠️ Переменная окружения YOOMONEY_TOKEN не найдена! Проверьте настройки хостинга.")
    yoomoney_client = None

# Списки для управления донатами в памяти
DONATIONS_QUEUE = []
PROCESSED_OPERATIONS = set()

# Простая главная страница для UptimeRobot
@app.get("/", response_class=HTMLResponse)
async def home_page():
    return """
    <html>
        <head><title>YooMoney API Server</title></head>
        <body style="font-family: Arial; text-align: center; padding-top: 100px; background: #f4f4f9;">
            <h1>API Сервер Донатов запущен 🟢</h1>
            <p>Статус: Работает через защищенные переменные окружения.</p>
        </body>
    </html>
    """

# Ручка для OBS виджета: опрашивает кошелек и выдает донаты
@app.get("/get-donations")
async def get_donations():
    if yoomoney_client:
        try:
            # Запрашиваем историю входящих переводов
            history = yoomoney_client.operation_history(type="deposition")
            
            if history and history.operations:
                # Проверяем последние операции
                for op in reversed(history.operations[:3]):
                    op_id = op.operation_id
                    
                    if op_id not in PROCESSED_OPERATIONS:
                        # При первом запуске просто запоминаем старые транзакции, чтобы не спамить
                        if not PROCESSED_OPERATIONS:
                            PROCESSED_OPERATIONS.add(op_id)
                            continue
                            
                        # Вытягиваем детальную инфу (сообщение)
                        details = yoomoney_client.operation_details(op_id)
                        msg = getattr(details, "message", None) or getattr(details, "comment", None) or "Без сообщения"
                        amount = op.amount
                        
                        print(f"🎉 Новый донат: {amount} руб. | Текст: {msg}")
                        
                        DONATIONS_QUEUE.append({
                            "username": "Перевод",
                            "amount": amount,
                            "message": msg
                        })
                        PROCESSED_OPERATIONS.add(op_id)
        except Exception as e:
            print(f"⚠️ Ошибка чтения истории кошелька: {e}")

    current_donations = list(DONATIONS_QUEUE)
    DONATIONS_QUEUE.clear()
    return JSONResponse(content=current_donations)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
