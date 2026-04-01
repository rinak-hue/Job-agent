import asyncio
import json
import os
import re
import time
from datetime import datetime
import httpx
from bs4 import BeautifulSoup

# ============================================================
# НАСТРОЙКИ — ИЗМЕНИ ЗДЕСЬ
# ============================================================
TELEGRAM_TOKEN = "8795696345:AAF2fnRFMZ0xajUntVqrwYDbQiAzf9M3Ljs"
TELEGRAM_CHAT_IDS = ["248752467", "142247089"]

KEYWORDS = ["product manager", "продуктовый менеджер", "product owner", "CPO", "head of product"]

# США — города и штаты которые исключаем
EXCLUDE_LOCATIONS = [
    "united states", "usa", "u.s.", "u.s.a",
    "new york", "san francisco", "los angeles", "chicago", "seattle",
    "austin", "boston", "denver", "miami", "atlanta", "bartlett",
    "houston", "dallas", "phoenix", "philadelphia", "san diego",
    "portland", "nashville", "las vegas", "minneapolis", "detroit",
    # Аббревиатуры штатов (с запятой перед ними как в LinkedIn)
    ", al", ", ak", ", az", ", ar", ", ca", ", co", ", ct", ", de",
    ", fl", ", ga", ", hi", ", id", ", il", ", in", ", ia", ", ks",
    ", ky", ", la", ", me", ", md", ", ma", ", mi", ", mn", ", ms",
    ", mo", ", mt", ", ne", ", nv", ", nh", ", nj", ", nm", ", ny",
    ", nc", ", nd", ", oh", ", ok", ", or", ", pa", ", ri", ", sc",
    ", sd", ", tn", ", tx", ", ut", ", vt", ", va", ", wa", ", wv",
    ", wi", ", wy", ", dc"
]

# Как часто проверять (в секундах). 86400 = раз в день
CHECK_INTERVAL = 86400

SEEN_FILE = "seen_jobs.json"
# ============================================================

def is_usa(location: str) -> bool:
    loc = location.lower()
    return any(excl in loc for excl in EXCLUDE_LOCATIONS)

def is_russian(title: str) -> bool:
    return bool(re.search(r'[а-яА-ЯёЁ]', title))

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)

async def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        for chat_id in TELEGRAM_CHAT_IDS:
            try:
                await client.post(url, json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": False
                })
            except Exception as e:
                print(f"Ошибка отправки для {chat_id}: {e}")

def format_job(job: dict) -> str:
    flag = "🇷🇺 " if job.get("is_russian") else ""
    lines = [f"{flag}<b>{job['title']}</b>"]
    if job.get("employer"):
        lines.append(f"🏢 {job['employer']}")
    if job.get("location"):
        lines.append(f"📍 {job['location']}")
    if job.get("salary"):
        lines.append(f"💰 {job['salary']}")
    lines.append(f"🔗 <a href='{job['link']}'>{job['source']}</a>")
    return "\n".join(lines)

async def fetch_hh(seen: set) -> list:
    jobs = []

    searches = [
        # Удалёнка — ищем по всему миру
        {"text": "product manager", "schedule": "remote"},
        {"text": "product owner", "schedule": "remote"},
        {"text": "head of product", "schedule": "remote"},
        {"text": "продуктовый менеджер", "schedule": "remote"},
        {"text": "CPO", "schedule": "remote"},
        # Без ограничения по расписанию — поймём по локации
        {"text": "product manager"},
        {"text": "product owner"},
        {"text": "продуктовый менеджер"},
    ]

    async with httpx.AsyncClient(timeout=20) as client:
        for search in searches:
            try:
                params = {
                    "text": search["text"],
                    "per_page": 50,
                    "order_by": "publication_time",
                    # Убираем search_field — ищем и в описании тоже
                }
                if "schedule" in search:
                    params["schedule"] = search["schedule"]

                resp = await client.get(
                    "https://api.hh.ru/vacancies",
                    params=params,
                    headers={"User-Agent": "job-monitor/1.0"}
                )

                if resp.status_code != 200:
                    print(f"hh.ru {resp.status_code} для '{search['text']}'")
                    continue

                data = resp.json()

                for item in data.get("items", []):
                    job_id = f"hh_{item['id']}"
                    if job_id in seen:
                        continue

                    title = item.get("name", "")
                    employer = item.get("employer", {}).get("name", "")
                    link = item.get("alternate_url", "")
                    salary = item.get("salary")
                    schedule = item.get("schedule", {}).get("name", "")
                    area = item.get("area", {}).get("name", "")

                    location_str = f"{area} · {schedule}".strip(" ·")

                    if is_usa(location_str):
                        continue

                    salary_str = ""
                    if salary:
                        frm = salary.get("from")
                        to = salary.get("to")
                        currency = salary.get("currency", "")
                        if frm and to:
                            salary_str = f"{frm}–{to} {currency}"
                        elif frm:
                            salary_str = f"от {frm} {currency}"
                        elif to:
                            salary_str = f"до {to} {currency}"

                    jobs.append({
                        "id": job_id,
                        "source": "hh.ru",
                        "title": title,
                        "employer": employer,
                        "salary": salary_str,
                        "location": location_str,
                        "link": link,
                        "is_russian": is_russian(title)
                    })
                    seen.add(job_id)

                await asyncio.sleep(0.5)

            except Exception as e:
                print(f"Ошибка hh.ru '{search.get('text')}': {e}")

    return jobs

async def fetch_linkedin_with_period(seen: set, period_seconds: int = 86400) -> list:
    jobs = []

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        try:
            url = f"https://www.linkedin.com/jobs/search/?keywords=product+manager&f_WT=2&f_TPR=r{period_seconds}&sortBy=DD"
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            })

            soup = BeautifulSoup(resp.text, "html.parser")
            cards = soup.find_all("div", class_=re.compile("job-search-card|base-card"))

            for card in cards[:30]:
                try:
                    title_el = card.find("h3")
                    company_el = card.find("h4")
                    link_el = card.find("a", href=True)
                    location_el = card.find("span", class_=re.compile("location|job-search-card__location"))

                    if not title_el or not link_el:
                        continue

                    title = title_el.get_text(strip=True)
                    company = company_el.get_text(strip=True) if company_el else ""
                    link = link_el["href"].split("?")[0]
                    location = location_el.get_text(strip=True) if location_el else ""

                    if is_usa(location):
                        continue

                    job_id = f"li_{abs(hash(link))}"
                    if job_id in seen:
                        continue

                    jobs.append({
                        "id": job_id,
                        "source": "LinkedIn",
                        "title": title,
                        "employer": company,
                        "salary": "",
                        "location": location,
                        "link": link,
                        "is_russian": is_russian(title)
                    })
                    seen.add(job_id)

                except Exception:
                    continue

        except Exception as e:
            print(f"Ошибка LinkedIn: {e}")

    return jobs

async def send_jobs(jobs: list):
    if jobs:
        jobs.sort(key=lambda j: (0 if j.get("is_russian") else 1))
        header = (
            f"🔍 <b>Вакансии Product Manager</b> — {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
            f"Найдено: {len(jobs)}\n"
            f"🇷🇺 На русском: {sum(1 for j in jobs if j.get('is_russian'))}\n"
        )
        await send_telegram(header)
        for job in jobs:
            await send_telegram(format_job(job))
            await asyncio.sleep(0.5)
    else:
        await send_telegram("🤷 Новых вакансий не найдено")

async def run_check():
    seen = load_seen()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Проверяю вакансии...")

    hh_jobs = await fetch_hh(seen)
    li_jobs = await fetch_linkedin_with_period(seen, 86400)
    all_jobs = hh_jobs + li_jobs

    print(f"hh.ru: {len(hh_jobs)}, LinkedIn: {len(li_jobs)}")
    await send_jobs(all_jobs)
    save_seen(seen)

async def run_refresh():
    await send_telegram("🔄 Обновляю подборку за последние 4 дня...")
    if os.path.exists(SEEN_FILE):
        os.remove(SEEN_FILE)

    seen = set()
    hh_jobs = await fetch_hh(seen)
    li_jobs = await fetch_linkedin_with_period(seen, 345600)  # 4 дня
    all_jobs = hh_jobs + li_jobs

    await send_jobs(all_jobs)
    save_seen(seen)

async def poll_commands():
    offset = None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                params = {"timeout": 10}
                if offset:
                    params["offset"] = offset

                resp = await client.get(url, params=params)
                data = resp.json()

                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    text = msg.get("text", "")

                    if chat_id in TELEGRAM_CHAT_IDS and text == "/refresh":
                        print(f"Команда /refresh от {chat_id}")
                        asyncio.create_task(run_refresh())

            except Exception as e:
                print(f"Ошибка polling: {e}")
                await asyncio.sleep(5)

async def main():
    await send_telegram(
        "✅ <b>Job Monitor запущен!</b>\n"
        "Ищу вакансии Product Manager на hh.ru и LinkedIn.\n"
        f"Проверка раз в {CHECK_INTERVAL // 3600} ч.\n\n"
        "Команды:\n/refresh — подборка за 4 дня прямо сейчас"
    )

    async def check_loop():
        while True:
            await run_check()
            print(f"Следующая проверка через {CHECK_INTERVAL // 3600} ч.")
            await asyncio.sleep(CHECK_INTERVAL)

    await asyncio.gather(
        check_loop(),
        poll_commands()
    )

if __name__ == "__main__":
    asyncio.run(main())



