<div dir="rtl" align="right">

فرمان‌ها را از ریشهٔ پروژه اجرا کن. مدل و منابع در `config.toml` و اطلاعات تلگرام در `.env` تنظیم می‌شوند.

**۱. نصب اولیه و آماده‌سازی دیتابیس**

<div dir="ltr" align="left">

```sh
uv sync --locked
uv run alembic upgrade head
```

</div>

**۲. روشن کردن مدل در ترمینال اول**

<div dir="ltr" align="left">

```sh
uv run news-intel serve-model
```

</div>

**۳. روشن کردن زمان‌بند در ترمینال دوم**

<div dir="ltr" align="left">

```sh
uv run news-intel schedule --limit 20
```

</div>

مدل و زمان‌بند را روشن نگه دار. `--limit 20` سقف تحلیل و اعلان در هر نوبت است. ساعت‌ها را در `schedule.times` تغییر بده و زمان‌بند را دوباره راه بینداز.

**۴. بررسی اجراها و خبرهای مرتبط**

<div dir="ltr" align="left">

```sh
uv run news-intel runs --limit 5
uv run news-intel review --related
```

</div>

`scheduler_running: true` یعنی زمان‌بند روشن است. برای یک اجرای فوری، `uv run news-intel run --limit 1` می‌تواند حداکثر یک اعلان واقعی بفرستد.

</div>

<div dir="rtl" align="right">

## معماری و مسیر داده

### ۱. شروع نوبت و تعیین بازه

`news-intel schedule` ساعت‌ها را از `config.toml` می‌خواند. `publication_window` بازهٔ انتشار را از **بعد از ساعت قبلی تا خود ساعت فعلی** می‌سازد. مثلاً اگر دو نوبت متوالی ۱۷:۴۵ و ۱۸:۰۰ باشند، نوبت ۱۸:۰۰ خبرهای منتشرشده در همان بازه را برای تحلیل و اعلان در نظر می‌گیرد. سپس `Workflow.run` مراحل واکشی و پردازش را اجرا می‌کند. [فرمان زمان‌بند](app/cli.py#L244) · [محاسبهٔ بازه](app/scheduler.py#L41) · [خط لولهٔ اصلی](app/pipelines/workflow.py#L105)

### ۲. خواندن فید و ذخیرهٔ خبر

منبع فعلی فید عمومی **GOV.UK / FCDO** است. `build_source` آداپتور `RssSource` را می‌سازد. `RssSource.fetch` فید Atom را با `HttpFetcher` می‌گیرد و با `feedparser` از هر ورودی عنوان، لینک، چکیده، زمان و زبان را استخراج می‌کند. اگر فید فقط زمان به‌روزرسانی داشته باشد، همان زمان در فیلد `published_at` قرار می‌گیرد. [انتخاب آداپتور](app/sources/registry.py#L19) · [خواندن فید](app/sources/rss.py#L33)

در تنظیم فعلی، عنوان و چکیده از خود فید می‌آیند. وقتی `fetch_full_text` برای منبع فعال باشد، `ArticleExtractor.resolve` می‌تواند متن صفحهٔ خبر را هم استخراج کند. `CollectionStore.save` نشانی استاندارد و هش محتوا را برای تشخیص تکرار بررسی می‌کند و خبر و نسخهٔ متن را در `articles` و `article_revisions` ثبت می‌کند. [استخراج متن](app/extractors/article.py#L35) · [ذخیره و تشخیص تکرار](app/storage/collection.py#L159)

### ۳. انتخاب خبر و آماده‌کردن ورودی مدل

`AnalysisStore.candidate_ids` نسخه‌های واجد شرایط را از SQLite انتخاب می‌کند. منبع باید برای تحلیل فعال باشد. اجرای زمان‌بندی‌شده بازهٔ انتشار را اعمال می‌کند و نسخه‌هایی را که با همین مدل و تنظیمات نتیجهٔ معتبر دارند کنار می‌گذارد. `--limit` سقف تعداد خبرهای انتخاب‌شده در هر نوبت است. هر بار یک نسخه به `LLMClassifier.classify` داده می‌شود. [انتخاب خبرها](app/storage/analysis.py#L40) · [پردازش یک‌به‌یک](app/pipelines/analysis.py#L32)

مدل عنوان، زبان و متن کامل در صورت دسترسی را می‌گیرد. اگر متن کامل در دسترس نباشد، چکیدهٔ فید استفاده می‌شود. برای متن طولانی، بخشی از ابتدا و انتها تا سقف تنظیم‌شده فرستاده می‌شود. ورودی یک خبر نمونه دربارهٔ فردو به این شکل است. [ساخت ورودی](app/intelligence/classifier.py#L66)

<div dir="ltr" align="left">

```json
{
  "title": "IAEA inspectors report new activity at Fordow",
  "summary": null,
  "body_segments": [],
  "body_truncated": false,
  "language": "en"
}
```

</div>

### ۴. درخواست به Qwen و خواندن پاسخ

در تنظیم فعلی، `ChatCompletionsAPI.complete` یک درخواست `POST` به `http://127.0.0.1:18080/v1/chat/completions` می‌فرستد. دستورهای دسته‌بندی در پیام `system` و JSON خبر در پیام `user` قرار می‌گیرند. درخواست شامل نام مدل، سقف توکن خروجی و `response_format` با JSON Schema سخت‌گیرانه است. پرامپت از مدل می‌خواهد ارتباط را با شاهدی از خود متن توضیح بدهد. به همین دلیل اشاره به فردو هم می‌تواند به برچسب `indirect` منجر شود. [ساخت درخواست](app/intelligence/llm_api.py#L71) · [پرامپت تحلیل](app/intelligence/prompts/iran_relevance_v1.txt)

`ChatCompletionsAPI` متن پاسخ را از `choices[0].message.content` برمی‌دارد. `Assessment.model_validate_json` ساختار و مقدارهای پاسخ را اعتبارسنجی می‌کند. مدل یکی از چهار برچسب `direct` (مستقیم)، `indirect` (غیرمستقیم)، `unrelated` (نامرتبط) و `uncertain` (نامشخص) را می‌دهد. `_verify_evidence` بررسی می‌کند که شاهدها در ورودی آمده باشند. اگر مدل خبر را مرتبط بداند و شاهد قابل تأیید ندهد، نتیجه `uncertain` و نیازمند بازبینی می‌شود. JSON نامعتبر به‌صورت خطای تحلیل ثبت می‌شود. این یک نمونهٔ توضیحی از خروجی معتبر است. [قرارداد پاسخ](app/intelligence/contracts.py#L18) · [بررسی شواهد](app/intelligence/classifier.py#L122)

<div dir="ltr" align="left">

```json
{
  "relevance": "indirect",
  "confidence": 0.87,
  "topic": "nuclear_program",
  "reason": "اشاره به تأسیسات فردو ارتباط خبر با ایران را نشان می‌دهد.",
  "evidence": ["Fordow"],
  "related_entities": ["Fordow"],
  "summary_fa": "بازرسان آژانس از فعالیت تازه در فردو گزارش دادند.",
  "needs_review": false
}
```

</div>

### ۵. ثبت نتیجه و ارسال تلگرام

`AnalysisStore.complete` نتیجه را در `classifications` ذخیره می‌کند و آن را به نسخهٔ خبر پیوند می‌دهد. نام مدل، نسخهٔ پرامپت و تعداد توکن‌ها در صورت دسترسی همراه نتیجه ثبت می‌شوند. پس از ثبت، `after_saved` ارسال همان خبر را بررسی می‌کند. در حالت پیش‌فرض، `direct` و `indirect` واجد شرایط اعلان‌اند. پیام فارسی با عنوان، خلاصه، دلیل ارتباط و لینک خبر به تلگرام می‌رود. پس از تأیید تلگرام، وضعیت `sent` و شناسهٔ پیام در `notifications` ثبت می‌شوند. هر خبر برای کانال تلگرام یک رکورد اعلان دارد تا ارسال تکراری رخ ندهد. [ذخیرهٔ تحلیل](app/storage/analysis.py#L115) · [ارسال بعد از ثبت](app/pipelines/workflow.py#L139) · [درخواست تلگرام](app/notifications/telegram.py#L40) · [ثبت اعلان](app/storage/notifications.py#L158)

`uv run news-intel runs --limit 1` بازه و نتیجهٔ آخرین نوبت را نشان می‌دهد. `uv run news-intel review --revision REVISION_ID` جزئیات یک خبر و شاهد ثبت‌شدهٔ مدل را نمایش می‌دهد.

</div>


## تنظیمات و واکشی

SQLite در `data/iran_news.db` ساخته می‌شود. فرمان `status` ساعت‌ها، رسانه‌های تعریف‌شده، نسخهٔ migration و تعداد رکوردهای هر جدول را نشان می‌دهد.

برای اجرای دستی یک یا چند منبع مشخص، از گزینهٔ `--source` استفاده کن.

```powershell
uv run news-intel collect --source bbc --source dw
```

فرمان `collect` برای هر منبع تعداد خبرهای دریافتی، مقاله‌ها و نسخه‌های جدید و موارد تکراری را چاپ می‌کند. خرابی یک منبع در `fetch_logs` ثبت می‌شود و جمع‌آوری از بقیه ادامه پیدا می‌کند. اگر دست‌کم یک منبع ناموفق باشد، فرمان بعد از گزارش همهٔ منابع با کد خروج ۱ پایان می‌یابد.

تنظیمات پیش‌فرض در [defaults.toml](app/config/defaults.toml) قرار دارند. برای تغییر زمان‌ها، یک `config.toml` در ریشه بساز و بخش زیر را در آن بگذار. منطقهٔ زمانی یا رسانه‌هایی که در فایل محلی نیامده‌اند از تنظیمات پیش‌فرض خوانده می‌شوند.

```toml
[schedule]
times = ["08:00", "11:00", "14:00", "17:00", "20:00"]
```

با `uv run news-intel --config custom.toml status` می‌توانی فایل تنظیمات دیگری را بررسی کنی. برای تغییر مسیر پایگاه داده یا سطح لاگ، [.env.example](.env.example) را به `.env` کپی کن. تنظیمات محیطی بر `.env` اولویت دارند. مسیر تأییدشدهٔ این نسخه SQLite است. اجرای PostgreSQL به نصب درایور و آزمون migration در آن محیط نیاز دارد.

زمان انتظار شبکه، سقف تعداد تلاش و حداقل فاصلهٔ درخواست‌ها برای هر میزبان در `config.toml` قابل تنظیم‌اند. پاسخ‌های موقت HTTP و timeout دوباره امتحان می‌شوند و `Retry-After` ناشر رعایت می‌شود.

```toml
[collection]
timeout_seconds = 15
attempts = 3
min_interval_seconds = 1
```

### فیدها و API

| رسانه | نشانی | قالب فید |
| --- | --- | --- |
| BBC News | [World RSS](https://feeds.bbci.co.uk/news/world/rss.xml) | RSS 2.0 |
| The Guardian | [World RSS](https://www.theguardian.com/world/rss) | RSS 2.0 |
| Al Jazeera English | [English RSS](https://www.aljazeera.com/xml/rss/all.xml) | RSS 2.0 |
| Deutsche Welle | [English RSS](https://rss.dw.com/rdf/rss-en-all) | RSS 1.0 / RDF |

هر چهار نشانی در بررسی مستقیم HTTP با XML معتبر پاسخ دادند. در سه فید اول، تاریخ در `pubDate` است. فید DW از فیلدهای نام‌فضادار `date`، `language` و `contentID` استفاده می‌کند.

Guardian فعلاً در تنظیمات پیش‌فرض غیرفعال است. API رسمی آن از جست‌وجوی صفحه‌بندی‌شده و دریافت بدنه با `show-fields=body` پشتیبانی می‌کند. برای استفاده در این پروژه، باید مجوز مناسب برای جمع‌آوری، تحلیل ماشینی و نگه‌داری داده وجود داشته باشد. کلید API دسترسی به سرویس را فراهم می‌کند و مجوز تحلیل ماشینی را جداگانه باید بررسی کرد. [مستندات API](https://open-platform.theguardian.com/documentation/) · [Content API Explorer](https://explorer.capi.gutools.co.uk/) · [شرایط Open Platform](https://www.theguardian.com/open-platform/terms-and-conditions) · [شرایط وب‌سایت و RSS](https://www.theguardian.com/help/terms-of-service)

### استخراج متن و تشخیص تکرار

حالت پیش‌فرض برای منابع فعال، دریافت عنوان و خلاصه از RSS است. برای رسانه‌ای که دریافت و نگه‌داری متن آن برای کاربردت مجاز است، [defaults.toml](app/config/defaults.toml) را به `config.toml` کپی کن و در بخش همان رسانه `fetch_full_text = true` بگذار. تعریف `[[sources]]` در فایل محلی جای فهرست پیش‌فرض را می‌گیرد.

مسیر استخراج HTML، قواعد `robots.txt` و نشانه‌های محدودیت دسترسی را بررسی می‌کند و پس از دریافت صفحه، متن اصلی را با Trafilatura جدا می‌کند. وقتی خلاصه در دسترس باشد، حالت جایگزین `partial` است. اگر فقط عنوان موجود باشد، رکورد با وضعیت `failed` ثبت می‌شود. [مستندات Trafilatura](https://trafilatura.readthedocs.io/en/latest/usage-python.html) · [پروتکل robots.txt](https://www.rfc-editor.org/rfc/rfc9309.html)

برای استفادهٔ مجاز از Guardian، `enabled = true` و `GUARDIAN_API_KEY` را در تنظیمات مربوطه قرار بده. در صورت وجود کلید، آداپتور ابتدا API رسمی را می‌خواند. برای دریافت بدنه از همان API، `fetch_full_text = true` را فعال کن. اگر دسترسی مجاز فقط به RSS داری، تنظیم منبع را روی فید نگه دار و کلید وارد نکن.

پارامترهای شناخته‌شدهٔ رهگیری از URL استاندارد حذف می‌شوند و لینک اولیهٔ ناشر حفظ می‌شود. بازنشر با لینک تازه در همان رسانه با لینک canonical یا محتوای دقیقِ دارای شواهد کافی شناسایی می‌شود. تغییر عنوان یا متن، نسخهٔ تازه می‌سازد. در حالت RSS، خلاصه در هش محتوا به کار می‌رود. خبرهای رسانه‌های مختلف مستقل ذخیره می‌شوند.

## تحلیل ارتباط خبر با ایران

برای تنظیم تحلیل، اگر `config.toml` نداری، [defaults.toml](app/config/defaults.toml) را به این نام در ریشهٔ پروژه کپی کن. در بخش `[[sources]]` هر رسانه‌ای که مجوز پردازش محتوایش در مدل انتخابی را داری، `allow_classification = true` را تنظیم کن. فقط نسخه‌های ذخیره‌شدهٔ منابع فعال با این گزینه برای تحلیل انتخاب می‌شوند.

### اجرای مدل محلی روی ویندوز و لینوکس

فایل GGUF مدل **Qwen3.5 9B Q5_K_S** از پوشهٔ `data/local-ollama/models` با llama.cpp اجرا می‌شود. `serve-model` نام مدل و نشانی را از `[classification]` در `config.toml` می‌خواند. نشانی فعلی `http://127.0.0.1:18080/v1` است. روی لینوکس، نسخهٔ متناسب با معماری سرور از `llama-server` را در `PATH` قرار بده یا مسیر آن را با `--server` بده. در ویندوز، باینری موجود در `data/local-llama/runtime` هم شناسایی می‌شود. [نسخهٔ llama.cpp](https://github.com/ggml-org/llama.cpp/releases/tag/b12359)

```sh
uv run news-intel serve-model
```

این فرمان سرویس مدل را در همان ترمینال نگه می‌دارد. پس از آماده‌شدن مدل، فرمان `schedule` را در یک ترمینال دیگر اجرا کن. در لینوکس، حالت پیش‌فرض CPU است. برای اجرای صریح روی CPU از `--gpu-layers 0` استفاده کن. روی سرور دارای GPU سازگار، `--gpu-layers` و در صورت نیاز `--device` را مطابق باینری و سخت‌افزارت تنظیم کن. اگر فایل GGUF را در مسیری دیگر قرار دادی، مسیر آن را با `--model-file` وارد کن. [گزینه‌های llama-server](https://github.com/ggml-org/llama.cpp/blob/b12359/tools/server/README.md)

### ارائه‌دهنده‌های دیگر

این نمونه برای سرویس دارای Chat Completions و پشتیبانی از Structured Outputs است. آدرس پایه و نام مدل را مطابق ارائه‌دهندهٔ خودت بگذار و کلید سرویس را با نام `LLM_API_KEY` در `.env` قرار بده. [مستندات Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) · [مدل GPT-4.1 mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini)

```toml
[classification]
provider = "openai"
protocol = "openai_compatible"
base_url = "https://api.openai.com/v1"
model = "gpt-4.1-mini"
```

برای Ollama محلی، از تنظیمات زیر با مدلی که روی سیستم بارگذاری شده استفاده کن. این مسیر به کلید API نیاز ندارد. [مستندات خروجی ساختاریافتهٔ Ollama](https://docs.ollama.com/capabilities/structured-outputs)

```toml
[classification]
provider = "ollama"
protocol = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen3:8b"
```

```powershell
uv run alembic upgrade head
uv run news-intel analyze --limit 5
uv run news-intel analyze --retry-failed --limit 5
```

برای تحلیل همهٔ موارد واجد شرایط، گزینهٔ `--limit` را بردار. اجرای دوباره با همان تنظیمات، نسخه‌هایی را که نتیجهٔ معتبر دارند کنار می‌گذارد. تغییر مدل، نسخهٔ اختیاری `model_version` یا پرامپت، اجرای مستقل می‌سازد. گزینهٔ `--retry-failed` تلاش‌های ناموفق را هم در صف می‌آورد. خطاهای سراسری مانند محدودیت نرخ و احراز هویت اجرای فعلی را متوقف می‌کنند.

پرامپت پایه در [iran_relevance_v1.txt](app/intelligence/prompts/iran_relevance_v1.txt) قرار دارد. با `prompt_path` می‌توانی راهنمای مورد اعتماد پروژه را از فایلی با مسیر نسبی به TOML به پرامپت پایه اضافه کنی. نسخهٔ ثبت‌شده از محتوای پرامپت به دست می‌آید. سقف متن و توکن خروجی نیز با `max_body_chars` و `max_output_tokens` قابل تنظیم‌اند.

مدل برای هر نسخه یک برچسب `direct`، `indirect`، `unrelated` یا `uncertain`، دلیل، شواهد و خلاصهٔ فارسی برمی‌گرداند. برای نمونه، «IAEA inspectors report new activity at Fordow» می‌تواند به‌دلیل اشاره به فردو غیرمستقیم تشخیص داده شود. متن خبر در پیام داده قرار می‌گیرد و برنامه برای مدل ابزاری تعریف نمی‌کند. خروجی با JSON Schema و Pydantic بررسی می‌شود و شواهد باید در بخش‌های ارسال‌شده از خبر پیدا شوند. ارتباط مثبتِ بدون شاهد معتبر به `uncertain` با نیاز به بازبینی تبدیل می‌شود. امتیاز `confidence` برآورد مدل است.

نتیجه در `classifications` و وضعیت هر تلاش در `analysis_jobs` ذخیره می‌شود. نمونه‌های مصنوعی آزمون در [tests/fixtures](tests/fixtures) قرار دارند.

## اعلان تلگرام

برای ساخت بات، در گفت‌وگو با BotFather دستور `/newbot` را اجرا کن. توکن را با نام `TELEGRAM_BOT_TOKEN` در فایل `.env` قرار بده. برای چت خصوصی، ابتدا در خود بات `/start` را بزن. شناسهٔ مقصد از `message.chat.id` در پاسخ `getUpdates` به دست می‌آید و در `.env` با نام `TELEGRAM_CHAT_ID` قرار می‌گیرد. برای کانال، می‌توان نام عمومی با قالب `@channelname` را وارد کرد و به بات اجازهٔ ارسال پست داد. [راهنمای ساخت بات](https://core.telegram.org/bots/tutorial) · [getUpdates](https://core.telegram.org/bots/api#getupdates) · [sendMessage](https://core.telegram.org/bots/api#sendmessage)

```powershell
uv run alembic upgrade head
uv run news-intel notify --limit 1
```

`notify` تازه‌ترین نتیجه برای نسخهٔ فعلی هر خبر را بررسی می‌کند. برای خبر مستقیم یا غیرمستقیمِ منابع فعال و مجاز، پیام فارسی با خلاصه، دلیل ارتباط، دسته‌بندی، تاریخ به وقت تنظیم‌شده و لینک مقاله می‌فرستد. با `send_uncertain = true` خبرهای نامشخص هم با عنوان «نیازمند بررسی» وارد صف می‌شوند.

در `schedule` و `run`، هر خبر به‌محض ثبت نتیجهٔ تحلیل برای ارسال بررسی می‌شود و سپس تحلیل خبر بعدی ادامه پیدا می‌کند.

```toml
[notifications]
send_uncertain = false
timeout_seconds = 15
```

هر مقاله یک رکورد اعلان تلگرام دارد. شناسهٔ پیام و زمان ارسال بعد از تأیید تلگرام ثبت می‌شوند. تلاش‌های ناموفقِ قابل بازیابی در اجراهای بعدی، با رعایت زمان اعلام‌شده از تلگرام، دوباره امتحان می‌شوند. اگر خطای مقصد یا مجوز را برطرف کردی، `uv run news-intel notify --retry-failed` را اجرا کن.

اگر پاسخ ارسال گم شود، ممکن است پیام به مقصد رسیده باشد. این موارد با وضعیت `unknown` و شناسهٔ مقاله در گزارش `unknown_articles` دیده می‌شوند. پس از بررسی تاریخچهٔ چت، برای تلاش دوبارهٔ همان مقاله از `uv run news-intel notify --retry-unknown ARTICLE_ID` استفاده کن.

## فرمان‌های خط لوله

هر ساعت یکتا در `schedule.times` یک نوبت روزانه است. `status` فهرست کامل ساعت‌ها و `schedule --preview` پنج نوبت بعدی را نشان می‌دهد. روی لینوکس، مدل و زمان‌بند را می‌توان با دو سرویس `systemd` بالا آورد.

هر نوبت زمان‌بندی‌شده خبرها را بر اساس زمان انتشار، از **بعد از نوبت قبلی تا خود ساعت نوبت فعلی** تحلیل می‌کند و اعلان همان بازه را می‌فرستد. برای نمونه، نوبت `17:33` بازهٔ `(17:00, 17:33]` را بررسی می‌کند و نوبت `18:00` از بعد از `17:33` شروع می‌شود. بازه در `report.window` در تاریخچهٔ `runs` دیده می‌شود. خبرهای خارج از بازه یا بدون تاریخ انتشار برای پردازش دستی در آرشیو می‌مانند.

برای اجرای کامل به بخش `[classification]`، یک منبع فعال با `allow_classification = true` برای استفادهٔ مجاز، و تنظیمات تلگرام در `.env` نیاز است. پیش‌نمایش زمان‌ها بدون این پیش‌نیازها کار می‌کند.

اگر هنگام `alembic upgrade head` خطای `database is locked` دیدی و فایل SQLite در DB Browser باز است، در آن برنامه **Close Database** را بزن و migration را دوباره اجرا کن.

| فرمان | کاربرد |
| --- | --- |
| `collect --metadata-only` | ذخیرهٔ تیتر و چکیدهٔ RSS یا API، بدون درخواست متن کامل |
| `extract --limit 20` | تلاش دوباره برای متن کامل نسخه‌های ناقص منابع مجاز به استخراج |
| `analyze` و `notify` | اجرای مستقل تحلیل و اعلان |
| `run --limit 20` | یک اجرای کامل با سقف ۲۰ تحلیل و ۲۰ اعلان در این نوبت |
| `retry --limit 20` | تلاش دوباره برای استخراج ناقص، تحلیل‌های شکست‌خورده و خطاهای قطعی ارسال |
| `status` | نمایش زمان‌بندی، منابع و وضعیت پایگاه داده |
| `runs --limit 10` | نمایش وضعیت فعال زمان‌بند و ده اجرای اخیر |
| `review --related` | نمایش خبرهایی که مدل مستقیم یا غیرمستقیم به ایران مرتبط دانسته است |

در `runs`، `pipeline_running` وضعیت پردازش فعلی را نشان می‌دهد. `trigger: scheduled` برای نوبت خودکار و `trigger: manual` برای اجرای دستی است. جدول `pipeline_runs` زمان شروع و پایان، نتیجهٔ تحلیل و تعداد اعلان‌های ارسال‌شده را ثبت می‌کند.

هر فرمان خط لوله روی همان پایگاه داده قفل می‌گیرد. اگر نوبت دیگری در حال اجرا باشد، فرمان تازه با کد ۳ خارج می‌شود. اگر زمان‌بند در نوبت بعدی هنوز مشغول باشد، همان نوبت رد می‌شود.
