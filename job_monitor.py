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
TELEGRAM_CHAT_ID = "248752467"

# Ключевые слова для поиска
KEYWORDS = ["product manager", "продуктовый менеджер", "PM", "product owner", "CPO"]

# Локации
LOCATIONS = ["remote", "удалённо", "serbia", "сербия", "europe", "европа", "cyprus", "кипр", "белград"]

# Как часто проверять вакансии (в секундах). 3600 = каждый час
CHECK_INTERVAL = 86400

# Файл для хранения уже отправленных вакансий
SEEN_FILE = "seen_jobs.json"
# ============================================================

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
        await client.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        })

def is_relevant(title: str, description: str = "") -> bool:
    text = (title + " " + description).lower()
    has_keyword = any(kw.lower() in text for kw in KEYWORDS)
    has_location = any(loc.lower() in text for loc in LOCATIONS) or "remote" in text or "удалён" in text
    return has_keyword and has_location

async def fetch_hh(seen: set) -> list:
    jobs = []
    queries = ["product+manager", "product+owner"]
    areas = "0"  # все регионы

    async with httpx.AsyncClient(timeout=15) as client:
        for query in queries:
            try:
                # Используем публичный API hh.ru
                url = f"https://api.hh.ru/vacancies?text={query}&area={areas}&per_page=20&order_by=publication_time"
                resp = await client.get(url, headers={"User-Agent": "job-monitor/1.0"})
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

                    # Проверяем релевантность по локации
                    location_text = (area + " " + schedule).lower()
                    location_ok = (
                        any(loc.lower() in location_text for loc in LOCATIONS)
                        or "remote" in location_text
                        or "удалённ" in location_text
                        or "дистанционн" in location_text
                    )

                    if not location_ok:
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
                        "location": f"{area} · {schedule}",
                        "link": link
                    })
                    seen.add(job_id)

            except Exception as e:
                print(f"Ошибка hh.ru: {e}")

    return jobs

async def fetch_linkedin(seen: set) -> list:
    """LinkedIn через публичный RSS/поиск без авторизации"""
    jobs = []
    queries = ["product+manager+remote", "product+manager+serbia", "product+manager+europe"]

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for query in queries:
            try:
                url = f"https://www.linkedin.com/jobs/search/?keywords={query}&f_WT=2&sortBy=DD"
                resp = await client.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
                })
                soup = BeautifulSoup(resp.text, "html.parser")
                cards = soup.find_all("div", class_=re.compile("job-search-card"))

                for card in cards[:10]:
                    try:
                        title_el = card.find("h3")
                        company_el = card.find("h4")
                        link_el = card.find("a", href=True)
                        location_el = card.find("span", class_=re.compile("location"))

                        if not title_el or not link_el:
                            continue

                        title = title_el.get_text(strip=True)
                        company = company_el.get_text(strip=True) if company_el else ""
                        link = link_el["href"].split("?")[0]
                        location = location_el.get_text(strip=True) if location_el else ""

                        job_id = f"li_{link.split('/')[-1]}"
                        if job_id in seen:
                            continue

                        if not is_relevant(title, location):
                            continue

                        jobs.append({
                            "id": job_id,
                            "source": "LinkedIn",
                            "title": title,
                            "employer": company,
                            "salary": "",
                            "location": location,
                            "link": link
                        })
                        seen.add(job_id)

                    except Exception:
                        continue

            except Exception as e:
                print(f"Ошибка LinkedIn: {e}")

    return jobs

def format_job(job: dict) -> str:
    lines = [f"<b>{job['title']}</b>"]
    if job["employer"]:
        lines.append(f"🏢 {job['employer']}")
    if job["location"]:
        lines.append(f"📍 {job['location']}")
    if job["salary"]:
        lines.append(f"💰 {job['salary']}")
    lines.append(f"🔗 <a href='{job['link']}'>{job['source']}</a>")
    return "\n".join(lines)

async def run_check():
    seen = load_seen()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Проверяю вакансии...")

    all_jobs = []
    hh_jobs = await fetch_hh(seen)
    li_jobs = await fetch_linkedin(seen)
    all_jobs = hh_jobs + li_jobs

    print(f"Найдено новых: {len(all_jobs)}")

    if all_jobs:
        header = f"🔍 <b>Новые вакансии Product Manager</b> — {datetime.now().strftime('%d.%m.%Y %H:%M')}\nНайдено: {len(all_jobs)}\n"
        await send_telegram(header)

        for job in all_jobs:
            await send_telegram(format_job(job))
            await asyncio.sleep(0.5)
    else:
        print("Новых вакансий нет")

    save_seen(seen)

async def main():
    await send_telegram("✅ <b>Job Monitor запущен!</b>\nБуду присылать вакансии Product Manager каждый час.\n\nИщу: hh.ru + LinkedIn\nЛокации: Remote, Сербия, Европа, Кипр")
    
    while True:
        await run_check()
        print(f"Следующая проверка через {CHECK_INTERVAL // 60} минут")
        await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
