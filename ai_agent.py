"""
HireWire — Gemini AI Integration
Uses the google-genai SDK with gemini-2.5-flash to analyze
scraped projects and generate Telegram-ready Arabic reports.
"""

import json
from google import genai

from config import GEMINI_API_KEY, GEMINI_MODEL, logger
from models import Project


# ---------------------------------------------------------------------------
# Initialize Gemini Client
# ---------------------------------------------------------------------------
_client: genai.Client | None = None


def _get_client() -> genai.Client:
    """Lazy-initialize the Gemini client."""
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
        logger.info("[AI Agent] Gemini client initialized with model: %s", GEMINI_MODEL)
    return _client


# ---------------------------------------------------------------------------
# Project Serialization
# ---------------------------------------------------------------------------
def _projects_to_text(projects: list[Project]) -> str:
    """Convert Project dataclasses to a structured text for the AI prompt."""
    items = []
    for p in projects:
        # Platform-specific label
        source_label = {
            "mostaql": "مستقل",
            "nafezly": "نفذلي",
            "pph": "PeoplePerHour",
        }.get(p.source, p.source)

        item = {
            "title": p.title,
            "url": p.url,
            "source": source_label,
            "description": p.description[:500] if p.description else "غير متوفر",
            "budget": p.budget or "غير محدد",
            "skills": p.skills or [],
            "proposals": p.proposals_count or "غير معروف",
            "client_name": p.client.name or "غير معروف",
            "client_hiring_rate": (
                f"{p.client.hiring_rate}%"
                if p.client.hiring_rate >= 0
                else "غير متوفر"
            ),
            "client_projects": p.client.total_projects or 0,
            "client_country": p.client.country or "",
            "time_posted": p.time_posted or "",
        }
        items.append(item)
    return json.dumps(items, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Main Analysis Function
# ---------------------------------------------------------------------------
def analyze_projects(projects: list[Project], criteria: str) -> str | None:
    """
    Send serious projects to Gemini for analysis and report generation.

    Args:
        projects: List of Project objects that passed the hiring rate filter.
        criteria: Arabic string describing what kind of projects to look for.

    Returns:
        Telegram-formatted Arabic report string, or None if no matches / error.
    """
    if not projects:
        logger.info("[AI Agent] No projects to analyze.")
        return None

    client = _get_client()
    projects_text = _projects_to_text(projects)

    prompt = f"""أنت خبير تحليل مشاريع عمل حر محترف. مهمتك تحليل المشاريع التالية وإنشاء تقرير مفصّل.

📋 المشاريع المطلوب تحليلها:
{projects_text}

🎯 معايير الاختيار:
"{criteria}"

⚠️ قواعد التنسيق الصارمة (مهمة جداً):
- اكتب نصاً عادياً مع إيموجي.
- يُسمح فقط بوسوم: <b>عريض</b> و <i>مائل</i> و <a href="URL">رابط</a>
- ❌ ممنوع تماماً: <h1> <h2> <h3> <p> <div> <span> <br> <ul> <ol> <li> <table> <hr> أو أي وسم HTML آخر.
- استخدم سطراً جديداً (Enter) بدلاً من <br>.
- استخدم الإيموجي • بدلاً من <li>.
- لا تخترع أي روابط — استخدم فقط الـ URL الموجود في بيانات المشروع.

📐 النموذج المطلوب (اتبعه بالضبط):

<b>🚀 تقرير المشاريع الجديدة</b>
━━━━━━━━━━━━━━━━

<b>1️⃣ اسم المشروع</b>
🌐 المنصة: مستقل / نفذلي / PeoplePerHour
💰 الميزانية: $500 أو £45/hr (Hourly)
👤 العميل: اسم العميل
📊 نسبة التوظيف: 85%
📝 الوصف: ملخص قصير للمشروع
🔗 <a href="URL">رابط المشروع</a>

━━━━━━━━━━━━━━━━

<b>2️⃣ اسم المشروع التالي</b>
...

━━━━━━━━━━━━━━━━
<i>📊 إجمالي: X مشروع من Y</i>

📝 التعليمات الإضافية:
1. حلل كل مشروع وحدد مدى تطابقه مع المعايير.
2. رتّب المشاريع حسب الأهمية (الأنسب أولاً).
3. اذكر المنصة (مستقل/نفذلي/PeoplePerHour) لكل مشروع.
4. أبرز العملاء الجادين (نسبة توظيف عالية أو مشاريع مكتملة كثيرة).
5. انسخ الميزانية كما هي من البيانات بدون تغيير.
6. إذا لم يتطابق أي مشروع، اكتب "لا توجد مشاريع مطابقة حالياً."
"""

    try:
        logger.info("[AI Agent] Analyzing %d projects with %s...", len(projects), GEMINI_MODEL)

        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )

        report = response.text.strip() if response.text else None

        if report and len(report) > 20:
            logger.info("[AI Agent] Report generated successfully (%d chars)", len(report))
            return report
        else:
            logger.warning("[AI Agent] AI returned empty or too-short response.")
            return None

    except Exception as exc:
        logger.error("[AI Agent] Gemini API error: %s", exc, exc_info=True)

        # Retry once on transient errors
        try:
            logger.info("[AI Agent] Retrying once...")
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
            )
            report = response.text.strip() if response.text else None
            if report and len(report) > 20:
                logger.info("[AI Agent] Retry successful (%d chars)", len(report))
                return report
        except Exception as retry_exc:
            logger.error("[AI Agent] Retry also failed: %s", retry_exc)

        return None
