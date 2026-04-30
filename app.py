import feedparser
import os
import requests
import time
import re
import smtplib
from email.mime.text import MIMEText
import sqlite3
from groq import Groq
from flask import redirect
from flask import Flask, render_template, request
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

app = Flask(__name__)

SOURCES = {
    "BusinessDay": "https://businessday.ng/feed/",
    "Nairametrics": "https://nairametrics.com/feed/",
    "Reuters Business": "https://feeds.reuters.com/reuters/businessNews",
    "BBC Business": "http://feeds.bbci.co.uk/news/business/rss.xml",
}

new_articles = []

KEYWORDS = [
    "manufacturing",
    "factory",
    "production",
    "industrial",
    "energy",
    "infrastructure",
    "technology",
    "solar",
    "automobile",
    "robot",
    "AI",
    "mining",
    "supply chain",
]

def init_db():
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            url TEXT UNIQUE,
            content TEXT,
            status TEXT DEFAULT 'draft',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("PRAGMA table_info(articles)")
    columns = [column[1] for column in cursor.fetchall()]

    if "created_at" not in columns:
        cursor.execute("ALTER TABLE articles ADD COLUMN created_at TIMESTAMP")
        cursor.execute("""
            UPDATE articles
            SET created_at = CURRENT_TIMESTAMP
            WHERE created_at IS NULL
        """)

    conn.commit()
    conn.close()

def send_email_notification(title):
    try:
        sender = os.getenv("EMAIL_USER")
        password = os.getenv("EMAIL_PASS")
        receiver = os.getenv("EMAIL_TO")

        subject = "🟢 New Draft Ready"
        body = f"""
        🟢 New Drafts Ready

        The system has generated new article drafts:

        {title}

        👉 Open your dashboard to review and approve.
        """
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = receiver

        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(sender, password)
            server.send_message(msg)

        print("Email sent:", title)

    except Exception as e:
        print("Email error:", e)

def save_article(title, url, content):
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()

    try:
        cursor.execute("""
            INSERT INTO articles (title, url, content, created_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        """, (title, url, content))

        conn.commit()

    except sqlite3.IntegrityError:
        print("Already exists, skipping...")

    conn.close()

def update_status(article_id, status):
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()

    cursor.execute(
        "UPDATE articles SET status = ? WHERE id = ?",
        (status, article_id)
    )

    conn.commit()
    conn.close()

def article_exists(url):
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()

    cursor.execute("SELECT id FROM articles WHERE url = ?", (url,))
    result = cursor.fetchone()

    conn.close()

    return result is not None

def auto_generate():
    print("Running auto fetch...")

    articles = fetch_latest_articles()
    new_articles = []
    generated_count = 0
    skipped_count = 0
    error_count = 0

    for item in articles[:3]:
        if article_exists(item["link"]):
            print("Skipping duplicate:", item["title"])
            skipped_count += 1
            continue

        try:
            extracted = extract_article(item["link"])

            ai_article = generate_makers_article(
                extracted["title"],
                extracted["content"]
            )

            formatted_html = format_article_to_html(ai_article)

            save_article(
                extracted["title"],
                item["link"],
                formatted_html
            )

            new_articles.append(extracted["title"])
            generated_count += 1

            print("Saved:", extracted["title"])

            time.sleep(10)

        except Exception as e:
            print("Error:", e)
            error_count += 1
            continue

    print(f"Generated: {generated_count} | Skipped: {skipped_count} | Errors: {error_count}")

    if new_articles:
        send_email_notification(", ".join(new_articles))

def get_saved_articles():
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, title, content, status, created_at
        FROM articles
        ORDER BY id DESC
    """)
    articles = cursor.fetchall()

    conn.close()

    return articles

def format_article_to_html(text):
    # Convert **Heading** to <h3>
    text = re.sub(r"\*\*(.*?)\*\*", r"<h3>\1</h3>", text)

    # Split into paragraphs
    paragraphs = text.split("\n")

    html = ""
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue

        if p.startswith("<h3>"):
            html += p
        else:
            html += f"<p>{p}</p>"

    return html

def extract_article(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }

    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    # Remove unwanted tags
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
        tag.decompose()

    # Try different ways to get title
    title_text = "No title found"

    if soup.find("h1"):
        title_text = soup.find("h1").get_text(strip=True)

    elif soup.find("meta", property="og:title"):
        title_text = soup.find("meta", property="og:title").get("content", "").strip()

    elif soup.find("title"):
        title_text = soup.find("title").get_text(strip=True)

    # Try to target article body first
    article_body = (
        soup.find("article")
        or soup.find("div", {"class": lambda x: x and "content" in x})
        or soup.find("div", {"class": lambda x: x and "post" in x})
        or soup.find("section")
        or soup.find("main")
    )

    paragraphs = []

    if article_body:
        paragraphs = article_body.find_all("p")

    # fallback if empty
    if not paragraphs:
        paragraphs = soup.find_all("p")

    seen = set()
    clean_paragraphs = []

    blocked_phrases = [
        "Share",
        "Join BusinessDay",
        "Open In Whatsapp",
        "Read also",
    ]

    for p in paragraphs:
        text = p.get_text(" ", strip=True)

        if len(text) < 50:
            continue

        if any(phrase.lower() in text.lower() for phrase in blocked_phrases):
            continue

        if text in seen:
            continue

        seen.add(text)
        clean_paragraphs.append(text)

    article_text = "\n\n".join(clean_paragraphs)

    return {
        "title": title_text,
        "content": article_text
    }

def generate_makers_article(title, content):
    prompt = f"""
You are writing for a modern African manufacturing and business publication like Makers.

Rewrite and expand the news into a professional editorial article.

STRICT RULES:
- Do NOT use "Section 1", "Section 2", "Conclusion", or any numbered sections
- Do NOT structure like a report or essay
- Do NOT sound like a textbook
- Do NOT include any author name

WRITING STYLE:
- Calm, factual, and analytical
- Clear and direct language
- No dramatic or emotional words
- No exaggeration
- Write like a business journalist

FORMAT:
- Headline (clean, professional)
- Intro paragraph (what happened + why it matters)
- 3–5 natural paragraphs or sections with meaningful subheadings
- Subheadings should sound like real publication headings (not generic)
- Focus on implications, industry relevance, or economic impact

ADDITIONAL STYLE RULES:
- Avoid repeating the same idea in multiple sections
- Do not over-explain obvious economic effects
- Keep each paragraph focused on a distinct point
- Write like a newsroom article, not a classroom explanation
- Keep it slightly concise and natural, not overly polished

IMPORTANT:
- Each section should feel like a continuation of a story, not a report
- Avoid repeating the same idea
- Keep it readable and not too long

End with:
SEO keywords (comma separated)

INPUT:
Title: {title}

Content:
{content[:4000]}
"""

    response = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[
            {"role": "user", "content": prompt}
        ],
        temperature=0.6,
        max_tokens=700,
    )

    return response.choices[0].message.content

def is_relevant(title):
    title_lower = title.lower()
    return any(keyword.lower() in title_lower for keyword in KEYWORDS)


def fetch_latest_articles():
    articles = []

    for source_name, feed_url in SOURCES.items():
        feed = feedparser.parse(feed_url)

        for entry in feed.entries[:10]:
            title = entry.get("title", "")

            if not is_relevant(title):
                continue

            articles.append({
                "source": source_name,
                "title": title,
                "link": entry.get("link", "#"),
                "published": entry.get("published", "No date")
            })

    return articles

@app.route("/approve/<int:article_id>", methods=["POST"])
def approve(article_id):
    update_status(article_id, "approved")
    return redirect("/")

@app.route("/publish/<int:article_id>", methods=["POST"])
def publish(article_id):
    update_status(article_id, "published")
    return redirect("/")

@app.route("/", methods=["GET", "POST"])
def index():
    article = None
    error = None
    latest_articles = fetch_latest_articles()

    if request.method == "POST":
        url = request.form.get("url")

        try:
            extracted = extract_article(url)

            ai_article = generate_makers_article(
                extracted["title"],
                extracted["content"]
            )

            formatted_html = format_article_to_html(ai_article)

            save_article(
                extracted["title"],
                url,
                formatted_html
            )

            article = {
                "title": extracted["title"],
                "content": formatted_html
            }

        except Exception as e:
            error = f"Could not process article: {e}"

    return render_template(
        "index.html",
        article=article,
        error=error,
        latest_articles=latest_articles,
        saved_articles=get_saved_articles()
    )

if __name__ == "__main__":
    init_db()

    scheduler = BackgroundScheduler()
    scheduler.add_job(func=auto_generate, trigger="interval", minutes=30)
    scheduler.start()

    app.run()
