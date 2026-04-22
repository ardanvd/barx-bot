# راهنمای راه‌اندازی مستقل BARX در GitHub Actions

برای اینکه کانال بدون نیاز به Manus و به‌صورت ۲۴/۷ کار کند، این مراحل را انجام دهید:

1. یک حساب در **GitHub.com** بسازید.
2. یک Repository جدید و **Private** (خصوصی) بسازید.
3. تمام فایل‌های این پوشه را در آن آپلود کنید.
4. به بخش **Settings > Secrets and variables > Actions** بروید.
5. دو Secret جدید بسازید:
   - `TELEGRAM_BOT_TOKEN`: توکن بات شما
   - `TELEGRAM_CHANNEL`: آیدی کانال (مثلاً @barxexchange)
6. تمام! گیت‌هاب هر ۱۵ دقیقه اسکریپت را اجرا می‌کند.
