<p align="center"><img src="docs/assets/avid-logo.svg" alt="AVID" width="320"></p>

<div dir="rtl">

# AVID — متعادل‌ساز وزن‌ثابت قابل‌ممیزی برای نوبیتکس

AVID یک سبد کوچک رمزارز (BTC، PAXG و USDT) را روی وزن‌های هدفی که خودتان انتخاب می‌کنید نگه می‌دارد. در هر چرخه حداکثر **یک** سفارش limit کوچک در بازارهای `<ASSET>USDT` نوبیتکس ثبت می‌کند. طراحی آن «شکست امن» است: هر تصمیم، هر محدودیت، هر وضعیت سفارش و هر رکورد مالی قابل بررسی، بازتولید و ممیزی است.

English: [README.md](README.md) — نسخهٔ انگلیسی مرجع است؛ مستندات فنی در `docs/` به انگلیسی‌اند.

## فهرست

[وضعیت](#وضعیت) · [AVID چیست](#avid-چیست) · [AVID چه نیست](#avid-چه-نیست) · [قابلیت‌ها](#قابلیتها) · [مدل ایمنی](#مدل-ایمنی) · [نحوهٔ کار](#نحوهٔ-کار) · [صرافی و بازارهای پشتیبانی‌شده](#صرافی-و-بازارهای-پشتیبانیشده) · [پیش‌نیازها](#پیشنیازها) · [نصب](#نصب) · [شروع سریع: شبیه‌سازی آفلاین](#شروع-سریع-شبیهسازی-آفلاین) · [نمونهٔ خروجی](#نمونهٔ-خروجی) · [تنظیمات و سقف‌ها](#تنظیمات-و-سقفها) · [از شبیه‌سازی تا معاملهٔ واقعی](#از-شبیهسازی-تا-معاملهٔ-واقعی) · [Agent خصوصی و AVID Cloud](#agent-خصوصی-و-avid-cloud) · [دستورها](#دستورها) · [تست](#تست) · [امنیت](#امنیت) · [مستندات](#مستندات) · [پروژه](#پروژه) · [مجوز و سلب مسئولیت](#مجوز-و-سلب-مسئولیت)

## وضعیت

**نسخهٔ آلفا 0.1.0a2.** آمادهٔ Production نیست و با معاملهٔ واقعی اعتبارسنجی نشده است: تست‌های قرارداد نوبیتکس هنوز روی testnet رسمی اجرا نشده‌اند و AVID Cloud باید پروتکل نسخهٔ ۲ را پیاده کند تا Agent بتواند معامله کند. معاملهٔ واقعی **به‌طور پیش‌فرض خاموش** است. پیش از هر استفاده با پول واقعی [KNOWN_ISSUES.md](KNOWN_ISSUES.md) و [گزارش آمادگی انتشار](docs/audits/RELEASE_READINESS.md) را بخوانید.

## AVID چیست

- `avid_core` — موتور: استراتژی، بررسی‌های ریسک، ماشین حالت صریح سفارش، کلاینت نوبیتکس، دفتر ماندگار، تطبیق حسابداری و شبیه‌ساز آفلاین. به‌تنهایی اجرا می‌شود: `avid-engine`.
- `avid_agent` — Agent خصوصی: موتور را روی **ماشین شما** و زیر **سقف‌های محلی شما** اجرا می‌کند و دستورها را از AVID Cloud می‌گیرد (سرویسی جدا که در این مخزن نیست): `avid-agent`.

کلید API نوبیتکس روی ماشین شما می‌ماند و هرگز به Cloud فرستاده نمی‌شود.

## AVID چه نیست

- ماشین سود نیست. متعادل‌سازی وزن‌ها را نگه می‌دارد، قیمت را پیش‌بینی نمی‌کند و **سود را تضمین نمی‌کند**؛ در بازار روند‌دار ممکن است از نگه‌داشتن ساده عقب بماند.
- ربات فرکانس‌بالا یا آربیتراژ نیست: هر چرخه یک سفارش کوچک (پیش‌فرض هر ۱۰ دقیقه).
- چندصرافی نیست: فقط نوبیتکس.
- امانت‌دار نیست: برداشت، اهرم و سفارش market ندارد.

## قابلیت‌ها

- وزن‌های هدف ثابت با USDT به‌عنوان محور؛ باند نسبی؛ فروش پیش از خرید.
- اندازهٔ دقیق سفارش با `Decimal` روی شبکهٔ صرافی (step، tick، حداقل ارزش سفارش).
- سقف هر سفارش، سقف تعداد و ارزش روزانه، کف نقدینگی و سقف اسپرد.
- ماشین حالت صریح سفارش؛ ثبت ماندگار نیت و clientOrderId پیش از هر ارسال.
- تطبیق در سطح اجرا با تاریخچهٔ معاملات و وضعیت‌های صادقانه.
- شبیه‌ساز آفلاین که **همان موتور واقعی** را روی قیمت‌های مصنوعی ذخیره‌شده اجرا می‌کند.
- خروجی خوانا برای انسان و گزینهٔ `--json` در همه‌جا؛ هر رویداد با شناسه و سطح شدت ثبت می‌شود.
- سرویس پس‌زمینه با systemd `--user`، بدون root؛ توقف، سفارش‌های خود ربات را لغو و نتیجه را گزارش می‌کند.
- بدون وابستگی زمان اجرا (فقط کتابخانهٔ استاندارد؛ `cryptography` اختیاری است و برای سفارش واقعی لازم است).

## مدل ایمنی

هر ویژگی زیر تست دارد ([security/test_findings.py](security/test_findings.py) و [tests/test_fuzz_invariants.py](tests/test_fuzz_invariants.py)):

- در هر لحظه حداکثر یک سفارش ربات؛ کنار سفارش‌های باز خود شما در بازارهای مدیریت‌شده معامله نمی‌کند؛
- هر عدد ریسک اعتبارسنجی می‌شود (NaN و بی‌نهایت پذیرفته نیست)؛ ورودی نامعتبر سفارش جدید را متوقف می‌کند؛
- پاسخی که موتور نمی‌فهمد `UNKNOWN` است: تا روشن شدن آن چیزی آزاد نمی‌شود و سفارش جدیدی ثبت نمی‌شود؛
- سفارشی که پاسخ ثبتش گم شده فقط با clientOrderId پیدا می‌شود و دوباره ارسال نمی‌شود؛
- Cloud فقط می‌تواند سقف‌ها را سخت‌تر کند و هرگز به‌تنهایی معاملهٔ واقعی را روشن نمی‌کند؛
- STOP، حالت آزمایشی، ابطال Agent، فایل تنظیمات خراب، خطای صرافی و حتی دیسک پر، همچنان سفارش‌های خود ربات را لغو می‌کنند؛
- حسابداری هرگز کامل اعلام نمی‌شود وقتی کامل نیست (`RECONCILED` / `AGGREGATE_ONLY` / `INCOMPLETE_RECONCILIATION`).

جزئیات: [docs/RISK_MODEL.md](docs/RISK_MODEL.md) و [docs/SECURITY_ARCHITECTURE.md](docs/SECURITY_ARCHITECTURE.md).

## نحوهٔ کار

در هر چرخه: ابتدا وضعیت سفارش‌های باز روشن می‌شود؛ سپس همهٔ دارایی‌های مدیریت‌شده ارزش‌گذاری می‌شوند، دارایی‌ای که بیشترین فاصله را از باند دارد انتخاب می‌شود، یک سفارش اندازه‌گیری می‌شود، بررسی‌های ریسک اجرا می‌شوند، نیت به‌صورت ماندگار ذخیره می‌شود و سپس ارسال انجام می‌شود. نمودار اجزا در [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) و جریان تصمیم در [docs/STRATEGY_MATH.md](docs/STRATEGY_MATH.md#decision-flow) است.

## صرافی و بازارهای پشتیبانی‌شده

| صرافی | بازارها | شبکه |
|---|---|---|
| نوبیتکس | `BTCUSDT` و `PAXGUSDT` (USDT محور) | اصلی، testnet رسمی (`NOBITEX_NETWORK=testnet`) |

مشخصات بازار (گام مقدار، tick قیمت، حداقل سفارش بیش از ۱۱ تتر) طبق مستندات نوبیتکس است و اگر صرافی آن‌ها را تغییر دهد، با فایل اعتبارسنجی‌شدهٔ `markets.json` قابل بازنویسی است. استفاده از صرافی ثالث ممکن است تابع تحریم‌ها و قوانین محلی باشد.

## پیش‌نیازها

- لینوکس (برای سرویس پس‌زمینه: systemd)، پایتون 3.10 تا 3.13 (ماتریس CI؛ در این انتشار 3.11 تا 3.13 به‌صورت محلی تست شد و 3.10 فقط در CI).
- برای سفارش واقعی: `cryptography` (`sudo apt install -y python3-cryptography` یا extra `[live]`).

## نصب

AVID هنوز روی PyPI نیست. از wheel انتشار یا از سورس:

```bash
python3 -m venv ~/.venvs/avid
~/.venvs/avid/bin/pip install avid_rebalancer-0.1.0a2-py3-none-any.whl     # یا: pip install .
~/.venvs/avid/bin/avid-engine --version
```

Agent به‌عنوان سرویس پس‌زمینه (بدون root): `avid-agent install`. راهنمای کامل، ارتقا و حذف: [docs/INSTALLATION.md](docs/INSTALLATION.md).

## شروع سریع: شبیه‌سازی آفلاین

بدون حساب، بدون کلید و بدون شبکه:

```bash
avid-engine simulate --example btc-usdt
avid-engine simulate --example btc-paxg-usdt --json > sim.json
avid-engine simulate --scenario my-scenario.json
```

شبیه‌سازی با «اجرای آزمایشی زنده» یکی **نیست**: `avid-engine plan` حساب واقعی شما را می‌خواند و نشان می‌دهد موتور الان چه می‌کرد، بدون ارسال یا لغو هیچ سفارشی.

## نمونهٔ خروجی

۲۰۰ تتر، هدف BTC ۵۰٪ / USDT ۵۰٪، اولین گام مسیر قیمت مصنوعی همراه بسته (خروجی واقعی `avid-engine simulate --example btc-usdt`):

</div>

```text
First decision (step 1, 2026-01-05T18:00:00Z)
  Portfolio Value  200.00 USDT (estimated at mid prices)
  Current Weights  btc 0.0000  usdt 1.0000
  Target Weights   btc 0.5000  usdt 0.5000
  Band             ±25% of each target (btc 0.375-0.625, usdt 0.375-0.625)
  Decision         BUY order placed on the simulated exchange
  Order Amount     0.000333 BTC (BUY on BTCUSDT)
  Order Price      59988.0 USDT, limit (resting side); notional 19.98 USDT
  Risk Checks      cash_reserve OK (200.00 free - 20.54 (order + fee reserve + margin) = 179.46 USDT; floor 0); day_lock OK (not locked); orders_per_day OK (0 submitted today, cap 6); daily_notional OK (0.00 committed + 19.98 = 19.98 USDT, cap 100)
  Reason           btc weight 0.000 vs target 0.500
```

<div dir="rtl">

در کل اجرای ۹۶ گامی، موتور ۵ سفارش ثبت می‌کند (۴ اجرا و ۱ به‌دلیل کهنگی لغو) و وقتی وزن BTC داخل باند رسید (۰٫۴۰) معامله را متوقف می‌کند. این‌ها نتایج شبیه‌سازی روی قیمت‌های مصنوعی‌اند: ارزش «تخمینی» هستند نه «تحقق‌یافته» و نشانهٔ بازده آینده نیستند. ریاضیات: [docs/STRATEGY_MATH.md](docs/STRATEGY_MATH.md#6-worked-example-offline-simulation-real-engine-output).

## تنظیمات و سقف‌ها

| سقف | پیش‌فرض | معنا |
|---|---|---|
| `--weights` | `btc=0.34,paxg=0.33,usdt=0.33` | وزن‌های هدف؛ دارایی نام‌برده‌نشده هدف صفر دارد |
| `--band-rel` | 0.25 | معامله وقتی وزن بیش از ۲۵٪ هدف از آن فاصله دارد |
| `--max-order-usdt` | 20 | سقف هر سفارش |
| `--max-orders-per-day` | 6 | تعداد ارسال در روز UTC (لغوشده‌ها هم حساب می‌شوند) |
| `--max-daily-usdt` | 100 | ارزش اجراشده + باز در روز UTC |
| `--min-cash-usdt` | 0 | تتری که خرید باید آزاد بگذارد |
| `--max-spread` | 0.01 | بیشترین اسپرد قابل معامله |

برای Agent همین سقف‌ها در `~/.avid-agent/ceilings.json` هستند (`avid-agent ceilings`) و سیاست Cloud فقط می‌تواند آن‌ها را سخت‌تر کند. ببینید [docs/RISK_MODEL.md](docs/RISK_MODEL.md).

## از شبیه‌سازی تا معاملهٔ واقعی

1. `avid-engine simulate` — آفلاین.
2. `avid-engine check` و `plan` — فقط خواندن از حساب شما.
3. **testnet رسمی** با کلید testnet: `NOBITEX_NETWORK=testnet ... avid-engine run --live --once`.
4. سقف‌های کوچک، سپس `avid-agent ceilings --live yes ...` و سیاست live در AVID Cloud.

سفارش واقعی در حالت API-key به `cryptography` نیاز دارد؛ امضاکنندهٔ پایتون خالص برای سفارش واقعی روی شبکهٔ اصلی رد می‌شود.

## Agent خصوصی و AVID Cloud

</div>

```bash
avid-agent install
avid-agent pair --cloud https://<your AVID Cloud> --code <code>
avid-agent start
avid-agent status
avid-agent stop            # لغو سفارش‌های باز خود ربات و گزارش نتیجه (کد خروج ۳/۴ اگر تأیید نشد)
avid-agent restart         # سفارش‌های باز ربات حفظ می‌شوند
```

<div dir="rtl">

قرارداد Agent ↔ Cloud: [docs/AGENT_PROTOCOL.md](docs/AGENT_PROTOCOL.md). AVID Cloud جزو این مخزن نیست.

## دستورها

`avid-engine`: `check`، `plan`، `simulate`، `run`، `status`، `help`.
`avid-agent`: `install`، `uninstall`، `pair`، `once`، `start`، `stop`، `restart`، `status`، `logs`، `ceilings`، `resolve`، `help` (و `run`، `safe-stop`، `reset-cid`).
همهٔ گزینه‌ها و کدهای خروج: [docs/CLI_REFERENCE.md](docs/CLI_REFERENCE.md).

## تست

</div>

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
AVID_FUZZ_STEPS=10000 AVID_FUZZ_SEEDS=8 python -m pytest -q -m fuzz
```

<div dir="rtl">

هیچ تست خودکاری به حساب واقعی دست نمی‌زند. آنچه برای این انتشار اجرا شد، با نتیجه و پوشش اندازه‌گیری‌شده به تفکیک بخش: [docs/audits/TEST_EVIDENCE.md](docs/audits/TEST_EVIDENCE.md). روش تست: [docs/TESTING.md](docs/TESTING.md).

## امنیت

آسیب‌پذیری را خصوصی گزارش کنید ([SECURITY.md](SECURITY.md)) — اینجا سفارش ناخواسته، توقفی که متوقف نمی‌کند یا رکورد مالی نادرست، مسئلهٔ امنیتی است. هرگز با پول واقعی تست نکنید.

## مستندات

| | |
|---|---|
| [ARCHITECTURE](docs/ARCHITECTURE.md) | اجزا، ماژول‌ها، فایل‌ها، یک چرخه |
| [STRATEGY_MATH](docs/STRATEGY_MATH.md) | وزن، باند، اندازهٔ سفارش، جریان تصمیم |
| [ORDER_LIFECYCLE](docs/ORDER_LIFECYCLE.md) | ماشین حالت سفارش، پاسخ گم‌شده، لغو |
| [RISK_MODEL](docs/RISK_MODEL.md) | سقف‌ها، بررسی‌ها، توقف‌ها، قواعد Fail-Closed |
| [RECONCILIATION](docs/RECONCILIATION.md) | اجرا در برابر fill، وضعیت‌ها |
| [EXCHANGE_ADAPTER](docs/EXCHANGE_ADAPTER.md) | رابط صرافی و قرارداد آن |
| [AGENT_PROTOCOL](docs/AGENT_PROTOCOL.md) | پروتکل نسخهٔ ۲ Agent ↔ Cloud |
| [SECURITY_ARCHITECTURE](docs/SECURITY_ARCHITECTURE.md) | مدل تهدید، کنترل‌ها، بازیابی پس از Crash |
| [INSTALLATION](docs/INSTALLATION.md) · [CLI_REFERENCE](docs/CLI_REFERENCE.md) · [TESTING](docs/TESTING.md) · [TROUBLESHOOTING](docs/TROUBLESHOOTING.md) · [ROADMAP](docs/ROADMAP.md) | |
| [ممیزی پیش از انتشار](docs/audits/PRE_RELEASE_AUDIT.md) · [آمادگی انتشار](docs/audits/RELEASE_READINESS.md) · [یادداشت انتشار](docs/releases/v0.1.0a2.md) | |

## پروژه

[CONTRIBUTING](CONTRIBUTING.md) · [CODE_OF_CONDUCT](CODE_OF_CONDUCT.md) · [SUPPORT](SUPPORT.md) · [GOVERNANCE](GOVERNANCE.md) · [CHANGELOG](CHANGELOG.md) · [KNOWN_ISSUES](KNOWN_ISSUES.md) · [ROADMAP](docs/ROADMAP.md)

## مجوز و سلب مسئولیت

MIT — فایل [LICENSE](LICENSE). این نرم‌افزار مشاورهٔ مالی، سرمایه‌گذاری یا حقوقی نیست. معاملهٔ رمزارز می‌تواند زیان‌ده باشد؛ مسئولیت حساب، سقف‌ها و رعایت قوانین مربوط به شما با خودتان است.

</div>
