# کارهای لازم در AVID Cloud

کد Cloud در این مرحله در اختیار نبود. این سند دو بخش دارد:

- **الف:** تغییرهایی که برای کار کردن Agent جدید **الزامی** است.
- **ب:** چک‌لیست ممیزی Cloud که باید روی `AVID_Cloud.zip` اجرا شود.

موارد بخش ب **ادعای وجود ایراد نیستند**. نقطه‌هایی هستند که پرامپت ممیزی پرسیده و پاسخ‌شان فقط با دیدن کد معلوم می‌شود.

## الف. الزامی برای سازگاری با Agent 0.1.0a1

1. **پروتکل sync نسخهٔ ۲** (`docs/AGENT_PROTOCOL.md`). پاسخ باید `protocol`، `agent_id`، `batch_id`، `ack.accepted/rejected` و `directive` داشته باشد. تا این تغییر انجام نشده، Agent هیچ گزارشی را حذف نمی‌کند و هیچ سفارش جدیدی ثبت نمی‌کند.
2. **ذخیرهٔ idempotent با `item_key`:** یکتایی روی `(agent_id, item_key)`، با `ON CONFLICT DO NOTHING`. فقط کلیدهایی که **commit** شده‌اند در `accepted` بیایند. اگر commit موفق شد و پاسخ به Agent نرسید، ارسال مجددِ همان کلیدها نباید اثر تکراری داشته باشد.
3. **بررسی `audience`:** باید دقیقاً برابر origin عمومی همان استقرار باشد. بررسی `agent_id` بدنه در برابر هدر `X-AVID-Agent`.
4. **اعتبارسنجی یکسان اعداد:** همان بازه‌های `avid_core/validation.py` در `validate_strategy` و `set_mode` اعمال شود: رد NaN و Infinity، عدد صحیح برای تعداد، `max_spread` در `(0, 0.05]`، `min_cash_usdt` در `[0, 1e9]`، و `max_daily_usdt ≥ max_order_usdt`. `json.loads` در پایتون NaN را می‌پذیرد؛ باید `parse_constant` رد کند. Agent سیاست نامعتبر را اجرا نمی‌کند و کاربر فقط «directive rejected» می‌بیند.
5. **نمایش فیلدهای جدید** در داشبورد:
   - `state` سفارش؛
   - `accounting` و `reconciliation` (برچسب‌های `AGGREGATE_ONLY` و `INCOMPLETE_RECONCILIATION` آشکار باشند)؛
   - `network`: گزارش testnet هرگز به‌عنوان پول واقعی نشان داده نشود؛
   - `data_quality` عملکرد؛
   - رویدادهای `storage_pressure`، `cloud_bad_ack`، `directive_rejected`، `clock_mismatch`، `other_avid_instance` و `state_recovered_from_ledger`.
6. **`expires_at` در سیاست (اختیاری):** اگر Cloud از دسترس خارج شد، Agent پس از این زمان سفارش جدید نمی‌گذارد.
7. **`agent_id` در پاسخ pair:** حداکثر ۸۰ نویسه از `[A-Za-z0-9_.:-]`.
8. **`local_ceilings.live_allowed`:** ادعای Agent است (مهاجم A2 می‌تواند جعلش کند). در `set_mode` فقط برای هشدار به کاربر استفاده شود، نه به‌عنوان تنها مانع امنیتی. مانع واقعی سمت Agent است.

## ب. چک‌لیست ممیزی Cloud

برای هر بند، برچسب «آسیب‌پذیر / امن / نامشخص» با شاهد ثبت شود.

| بند پرامپت | موضوع | چه چیزی باید دیده شود |
|---|---|---|
| ۱ | brute-force کد اتصال (۶۰ بیت، ۱۰ دقیقه، ۵ کد، ۲۰ تلاش در ساعت برای هر IP) | با N نشانی IP، احتمال موفقیت ≈ N·20·(10/60)·5 / 2^60؛ حتی با یک میلیون IP ناچیز است. ولی سقف سراسری (نه فقط برای هر IP) و ثبت حسابرسی تلاش‌ها بررسی شود |
| ۲–۳ | شمارش کاربر و قفل عمدی حساب | پاسخ یکسان و زمان یکسان (`DUMMY_HASH`)؛ 409 در ثبت‌نام یک کانال شمارش است و می‌شود با «ایمیل تأیید ارسال شد» جایگزینش کرد؛ قفل بر اساس حساب + IP، نه فقط حساب |
| ۴ | race در TOTP | `UPDATE users SET mfa_last_step=? WHERE id=? AND mfa_last_step < ?` و بررسی rowcount (اتمیک) |
| ۵ | نشست | `AVID_SECURE_COOKIES=0` در `AVID_ENV=production` باید باعث خطای شروع شود؛ پس از تغییر رمز یا خاموش کردن MFA همهٔ نشست‌ها باطل شوند |
| ۶ | CSRF | فقط POST با `application/json`؛ هیچ GET تغییردهنده؛ نبود هدر `Access-Control-Allow-*` |
| ۷ | MFA step-up | به‌ویژه `create_pairing` (اتصال Agent تازه به حساب پول‌دار) و `change_password` |
| ۹–۱۴ | IDOR، نقش‌ها، نشت داده در admin | برای هر endpoint و هر شناسه از tenant دیگر، 404 یکسان و بدون اثر؛ ستون‌های `password_hash`، `mfa_secret` و `csrf` هرگز در خروجی نیایند |
| ۱۶ | race در pair | `SELECT … FOR UPDATE` روی PostgreSQL، یا update شرطی روی `pairing_hash` |
| ۱۷ | Agent بدخواه | `_ingest` فقط در سبدِ همان `agent_id` درج کند؛ سقف ردیف در روز؛ `trade_fills` جعلی فقط داشبورد خودش را خراب کند، نه `admin.overview` یا دیگران؛ رندر امن همهٔ متن‌ها (XSS) |
| ۲۰ | `LIKE ?` | escape کردن `%` و `_` (اثر: جستجوی گسترده‌تر، نه تزریق) |
| ۲۱ | JSON | NaN و `1e400` در `_d()` و `validate_strategy` (همان بند الف-۴) |
| ۲۲–۲۵ | بدنه، Host، مسیر فایل، webhook | timeout در uvicorn برای slowloris؛ `*` در `AVID_ALLOWED_HOSTS` در production رد شود؛ allowlist فونت‌ها؛ SSRF از `AVID_ALERT_WEBHOOK` |
| ۲۶–۳۰ | SPA | همهٔ `innerHTML`ها با `esc()`؛ CSP بدون inline script؛ `frame-ancestors 'none'` |
| ۳۱–۳۲ | scrypt و AES-GCM | `N=2^14` برای ۲۰۲۶ کم است (OWASP: `N=2^17, r=8, p=1`)، با ارتقای hash هنگام ورود؛ گذاشتن `user_id` در AAD تا جابه‌جاییِ راز دو کاربر کشف شود؛ برخورد با راز متن‌ساده |
| ۴۶–۴۹ | migration، یکتایی، رشد داده | قفل migration (advisory lock در PostgreSQL)؛ پوشش `cleanup` برای `agent_nonces`، `rate_limits` و `notifications` |
| ۵۰ | پشتیبان | `openssl enc -aes-256-cbc` بدون MAC است و فایل `.sha256` جدا جلوی دستکاری عمدی را نمی‌گیرد. از `age` یا `gpg`، یا از HMAC کلیددار استفاده شود |
| ۵۱–۵۳ | استقرار | `POSTGRES_PASSWORD` با Docker secret (`POSTGRES_PASSWORD_FILE`)؛ ایمیج پایه با digest؛ `pip install --require-hashes` |
| ۵۵ | CI | actionها با SHA (نمونه در `.github/workflows/ci.yml` همین مخزن)؛ اجرای `pip-audit`؛ hash فونت‌ها در `get-fonts.sh` |
| ۵۷ | DoS | scrypt با رمز ۲۵۶ نویسه؛ سقف سراسری ورود، نه فقط برای هر IP |
| ۶۰–۶۱ | حریم خصوصی و حذف حساب | چه چیزی در `audit_events`، `invoices` و پشتیبان‌ها باقی می‌ماند |

**پس از اجرا:** نتیجه به `docs/audits/AUDIT_RESPONSE_0.1.0a1_FA.md` (بخش ۶) اضافه شود و تست‌های اثبات Cloud در `security/test_findings.py` قرار بگیرند.
