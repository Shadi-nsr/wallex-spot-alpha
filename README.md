# Wallex Spot Alpha

نمونه‌ی قابل تحویل برای چالش والکس در هکاتون Agentic AI استارکوچ. این پروژه یک عامل پژوهش بازار و معامله‌ی کاغذی است که با سرمایه‌ی آزمایشی `۵٬۰۰۰٬۰۰۰` تومان کار می‌کند. مرز اجرای سفارش عمداً خاموش است؛ هیچ کلید API، سفارش، خرید، فروش، self-trade یا wash trading در این نسخه وجود ندارد.

این نسخه فقط برای معامله‌ی اسپات طراحی شده است: لوریج همیشه `۱×` است و هر تنظیم لوریج بزرگ‌تر از یک رد می‌شود. ارز مظنه‌ی پروژه `TMN` است و قیمت‌های کندل، سرمایه، ارزش پوزیشن، کارمزد و خروجی بک‌تست همگی به تومان هستند. فهرست کامل بازارهای غیرفعال حساب اعتباری در `app/markets.py` نگه‌داری می‌شود و از `GET /api/markets` برای نمایش در داشبورد ارائه می‌شود.

## چیزی که در دمو دیده می‌شود

عامل برای هر کندل چرخه‌ی `observe → reason → risk_gate → record` را اجرا می‌کند:

- `observe`: کندل OHLCV را می‌گیرد و فقط داده‌ی لازم برای تصمیم را نگه می‌دارد.
- `reason`: استراتژی قطعی `EMA crossover + RSI + ATR` سه شاهد مستقل را ترکیب می‌کند. هر تصمیم امتیاز، شاخص‌ها و دلیل متنی دارد.
- `risk_gate`: اندازه‌ی موقعیت را از بودجه‌ی ریسک و فاصله‌ی حد ضرر محاسبه می‌کند و سقف ارزش موقعیت، سقف افت سرمایه، زیان روزانه و cooldown را اعمال می‌کند.
- `record`: تصمیم در `JSONL` ثبت می‌شود. ابزار `place_order` اصلاً در رجیستری عامل وجود ندارد و آداپتر خام `RawWallexAdapter` هر عملیات را با وضعیت `blocked` برمی‌گرداند.

در حالت دارای endpoint سازگار با OpenAI، کلاس `OpenAICompatiblePlanner` فقط یک پیشنهاد `buy|hold` و thesis می‌گیرد؛ محاسبه‌ی سیگنال و دروازه‌ی ریسک همچنان قطعی و حاکم است. در نبود endpoint یا پاسخ نامعتبر، fallback آفلاین و قابل تکرار اجرا می‌شود.

## اجرا و بازتولید

این پروژه فقط از کتابخانه‌ی استاندارد پایتون استفاده می‌کند و به نصب پکیج نیاز ندارد.

```bash
cd outputs/wallex-strategy
python3 -m unittest discover -s tests -v
python3 -m app.main demo --bars 240 --log data/backtest-decisions.jsonl
python3 -m app.main shadow --bars 240 --log data/shadow-decisions.jsonl
```

برای اتصال اختیاری به `AgentRouter` یا هر endpoint سازگار با OpenAI، کلید را فقط از محیط بده؛ کلید وارد لاگ یا گیت نمی‌شود و دروازه‌ی ریسک همچنان قطعی است. مقدار پیش‌فرض endpoint `https://agentrouter.org/v1/chat/completions` و مدل `gpt-6.1-sol` است و هر دو با متغیر محیطی قابل تغییرند:

```bash
export WALLEX_AI_API_KEY="..."
export WALLEX_AI_MODEL="gpt-6.1-sol"  # یا gpt-6-astra / gpt-5.6-sol
python3 -m app.main shadow --bars 240
```

برای شروع از فایل نمونه استفاده کن:

```bash
cp .env.example .env
```

مقدار واقعی `WALLEX_AI_API_KEY` را در محیط اجرای Liara قرار بده؛ آن را داخل فایل `.env` گیت نکن.

فرمت ورودی، آرایه‌ی JSON یا فایل `JSONL` از کندل‌هاست و کلیدهای `timestamp/open/high/low/close/volume` (یا شکل کوتاه `t/o/h/l/c/v`) را می‌پذیرد:

```bash
python3 -m app.main demo --input candles.json --symbol BTC/TMN --cash 5000000
```

برای اجرای داشبورد محلی:

```bash
python3 -m app.server --host 127.0.0.1 --port 8787
```

`GET /health` وضعیت خاموش بودن اجرا را می‌دهد، `GET /api/config` تنظیمات و ابزارها را برمی‌گرداند، `GET /api/markets` بازارهای غیرفعال حساب اعتباری را می‌دهد، `POST /api/backtest` بک‌تست می‌گیرد و `POST /api/shadow/candle` یک تصمیم کاغذی ثبت می‌کند. برای نمایش رابط، فایل `app/static/index.html` را با یک وب‌سرور محلی باز کن؛ API برای دمو کافی است.

## منطق معامله و کنترل ریسک

- سرمایه‌ی شروع: `۵٬۰۰۰٬۰۰۰` تومان.
- همه‌ی قیمت‌ها و خروجی‌های مالی با ارز مظنه‌ی تومان (`TMN`) هستند؛ نماد پیش‌فرض `BTC/TMN` است.
- ریسک هر معامله: یک درصد equity؛ اندازه‌ی پوزیشن برابر است با `risk_budget / (ATR × stop_multiplier)` و با سقف ۲۵٪ equity محدود می‌شود.
- حد ضرر: `۲ × ATR`؛ حد سود: `۳ × ATR`؛ کارمزد و لغزش در بک‌تست لحاظ می‌شوند.
- سقف افت سرمایه: ۱۲٪؛ سقف زیان روزانه: ۳٪؛ بعد از نقض، circuit breaker فعال می‌شود.
- یک موقعیت هم‌زمان در بازار اسپات، با لوریج ثابت `۱×` و بدون martingale؛ بعد از خروج cooldown اعمال می‌شود.
- اگر در یک کندل هم حد ضرر و هم حد سود لمس شوند، حد ضرر اولویت دارد (فرض محافظه‌کارانه).

این‌ها پیشنهاد سرمایه‌گذاری نیستند و نتیجه‌ی بک‌تست تضمین آینده نیست. داده‌ی ورودی باید timestamp مرتب و OHLC معتبر داشته باشد.

## معماری

```text
OHLCV JSON/stream
       │
       ▼
Candle parser ──► Strategy (EMA/RSI/ATR) ──► Planner (offline/optional AI)
       │                                               │
       └──────────────► RiskManager / circuit breakers ┘
                                      │
                                      ▼
                           Decision JSONL + dashboard
                                      │
                         [execution boundary: blocked]
```

## معیارهای تحویل

- **عملکرد معاملاتی:** equity curve، بازده، max drawdown، win rate، کارمزد و معاملات بسته‌شده در خروجی بک‌تست.
- **منطق:** شاخص‌ها، امتیاز و دلیل هر تصمیم در لاگ قابل بازبینی است.
- **استفاده از AI:** planner با قرارداد مشخص قابل اتصال به مدل سازگار با OpenAI است؛ مدل فقط پیشنهاد می‌دهد و ابزارهای deterministic تصمیم نهایی را محدود می‌کنند.
- **مدیریت ریسک:** position sizing، stop/take، سقف موقعیت، daily loss و drawdown circuit breaker.
- **تکرارپذیری:** synthetic candles ثابت، seed ضمنیِ بدون تصادفی‌بودن، تست‌های `unittest` و اجرای بدون کلید خارجی.
- **بک‌تست/Shadow:** هر دو مسیر در CLI و API موجودند و در مسیر Shadow تعداد سفارش ارسالی همیشه صفر است.

## ساختار فایل

- `app/engine.py`: مدل کندل، استراتژی، مدیریت ریسک اسپات، بک‌تست، Shadow و آداپتر خاموش.
- `app/markets.py`: نرمال‌سازی نمادها، سیاست اسپات/تومان و فهرست بازارهای غیرفعال حساب اعتباری.
- `app/agent.py`: رجیستری ابزار، حلقه‌ی عامل و planner آفلاین/اختیاری.
- `app/main.py`: CLI بازتولیدپذیر.
- `app/server.py`: API محلی بدون credential.
- `app/static/index.html`: رابط دمو.
- `tests/test_engine.py`: تست‌های invariant، تکرارپذیری و ممنوعیت اجرا.

## سناریوی ویدئوی پنج‌دقیقه‌ای

۱. اجرای `python3 -m app.server` و نمایش نوار سبز «۰ سفارش ارسال‌شده». ۲. اجرای بک‌تست و نمایش بازده، max drawdown و کارمزد. ۳. باز کردن `data/backtest-decisions.jsonl` و نشان دادن یک تصمیم با `EMA`, `RSI`, `ATR`, مقدار ریسک و دلیل متنی. ۴. فرستادن چند کندل به `/api/shadow/candle` و نمایش اینکه تصمیم ثبت می‌شود اما آداپتر اجرا `blocked` می‌ماند. ۵. تغییر `--risk` یا `--max-position` و اجرای دوباره برای نشان دادن اثر کنترل ریسک.
