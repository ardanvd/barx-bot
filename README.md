# 🚀 BARX Exchange Bot

ربات خودکار انتشار نرخ ارز در کانال تلگرام `@barxexchange`.

هر ۳۰ دقیقه یک‌بار قیمت‌های دلار، یورو و لیر ترکیه را از کانال‌های مرجع می‌خواند و در کانال منتشر می‌کند.

---

## راه‌اندازی (یک‌بار برای همیشه)

### قدم ۱ — ساخت repo در GitHub

1. به [github.com](https://github.com) بروید و وارد حساب خود شوید.
2. روی **New repository** کلیک کنید.
3. نام repo را `barx-bot` بگذارید.
4. گزینه **Private** را انتخاب کنید.
5. روی **Create repository** کلیک کنید.

### قدم ۲ — آپلود فایل‌ها

1. در صفحه repo جدید، روی **uploading an existing file** کلیک کنید.
2. فایل‌های زیر را drag & drop کنید:
   - `main.py`
   - `requirements.txt`
   - `barx_live_state.json`
   - `barx_live_monitor.log`
3. پوشه `.github/workflows/barx.yml` را هم آپلود کنید:
   - روی **Create new file** کلیک کنید.
   - نام فایل را `.github/workflows/barx.yml` بنویسید.
   - محتوای فایل `barx.yml` را کپی و پیست کنید.
4. روی **Commit changes** کلیک کنید.

### قدم ۳ — تنظیم توکن بات (Secret)

1. در repo، به **Settings** بروید.
2. از منوی سمت چپ، **Secrets and variables** → **Actions** را انتخاب کنید.
3. روی **New repository secret** کلیک کنید.
4. نام: `TELEGRAM_BOT_TOKEN`
5. مقدار: توکن بات تلگرام خود را وارد کنید.
6. روی **Add secret** کلیک کنید.

### قدم ۴ — فعال‌سازی Actions

1. به تب **Actions** در repo بروید.
2. اگر پیامی درباره فعال‌سازی Workflows دیدید، روی **I understand my workflows, go ahead and enable them** کلیک کنید.
3. تمام. از این لحظه هر ۳۰ دقیقه ربات اجرا می‌شود.

---

## سیاست انتشار

| پارامتر | مقدار |
|---|---|
| ساعت کاری | ۹:۰۰ صبح تا ۱۲:۰۰ شب (تهران) |
| تواتر | هر ۳۰ دقیقه |
| پایان معاملات | اعلام خودکار در نیمه‌شب |
| منبع دلار | `@dollar_tehran3bze` (۷۵٪) + `@tahran_sabza` (۲۵٪) |
| منبع یورو | `@navasanchannel` |
| محاسبه لیر | دلار تهران ÷ نرخ دلار ترکیه |
| اسپرد دلار/یورو | ۱,۰۰۰ تومان |
| اسپرد لیر | ۱۰۰ تومان |
