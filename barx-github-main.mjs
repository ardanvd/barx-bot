const SOURCE_URL = "https://istanbulexchange.com/";
const CHANNEL = "@barxexchange";

function normalizeDigits(value) {
  return value.replace(/[۰-۹]/g, d => "۰۱۲۳۴۵۶۷۸۹".indexOf(d)).replace(/[٠-٩]/g, d => "٠١٢٣٤٥٦٧٨٩".indexOf(d));
}

function parseRate(html, id) {
  const match = html.match(new RegExp(`<div\\s+id=["']${id}["'][^>]*>([\\s\\S]*?)<\\/div>`, "i"));
  if (!match) throw new Error(`Missing ${id} on source website`);
  const number = Number(normalizeDigits(match[1]).replace(/[^0-9]/g, ""));
  if (!Number.isFinite(number) || number <= 0) throw new Error(`Invalid ${id} value`);
  return number;
}

function formatNumber(value) {
  return new Intl.NumberFormat("fa-IR").format(value);
}

function makeMessage(r) {
  const now = new Intl.DateTimeFormat("fa-IR", { timeZone: "Asia/Tehran", dateStyle: "short", timeStyle: "medium" }).format(new Date());
  return `💱 <b>نرخ ارز</b> 💱\n\n<b>دلار آمریکا</b> 🇺🇸\nخرید: <code>${formatNumber(r.usdBuy)}</code> تومان\nفروش: <code>${formatNumber(r.usdSell)}</code> تومان\n\n<b>یورو</b> 🇪🇺\nخرید: <code>${formatNumber(r.eurBuy)}</code> تومان\nفروش: <code>${formatNumber(r.eurSell)}</code> تومان\n\n<b>لیر ترکیه</b> 🇹🇷\nخرید: <code>${formatNumber(r.tryBuy)}</code> تومان\nفروش: <code>${formatNumber(r.trySell)}</code> تومان\n\n⏰ ${now}`;
}

const sourceResponse = await fetch(SOURCE_URL, { headers: { "User-Agent": "BarxExchangeBot/2.0" } });
if (!sourceResponse.ok) throw new Error(`Source HTTP ${sourceResponse.status}`);
const html = await sourceResponse.text();
const rates = {
  usdSell: parseRate(html, "currency-rate-1"),
  usdBuy: parseRate(html, "currency-rate-2"),
  eurSell: parseRate(html, "currency-rate-3"),
  eurBuy: parseRate(html, "currency-rate-4"),
  trySell: parseRate(html, "currency-rate-5"),
  tryBuy: parseRate(html, "currency-rate-6"),
};
console.log(JSON.stringify({ source: SOURCE_URL, rates }));

if (process.argv.includes("--dry-run")) {
  console.log(makeMessage(rates));
  process.exit(0);
}

const token = process.env.TELEGRAM_BOT_TOKEN;
if (!token) throw new Error("TELEGRAM_BOT_TOKEN secret is missing");
const telegram = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({ chat_id: CHANNEL, text: makeMessage(rates), parse_mode: "HTML" }),
});
const result = await telegram.json();
if (!telegram.ok || !result.ok) throw new Error(`Telegram error: ${JSON.stringify(result)}`);
console.log(`Posted message ${result.result.message_id} to ${CHANNEL}`);
