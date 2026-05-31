"""
resume_generator.py – Role-aware PDF resume generation.

This module turns the structured candidate profile into a polished PDF resume
using Playwright's PDF rendering. The content is assembled from role-specific
templates and the candidate's current profile fields.
"""

from __future__ import annotations

import asyncio
import html
import re
from pathlib import Path
from typing import Dict, List

from playwright.async_api import async_playwright

from models import CandidateProfile
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from pypdf import PdfReader, PdfWriter
import io


ROLE_TEMPLATES: Dict[str, Dict[str, object]] = {
    "java developer + c2c": {
        "headline": "Java Developer",
        "summary": (
            "Java backend developer focused on Spring Boot, Hibernate/JPA, REST APIs, "
            "MySQL, and clean service-layer design."
        ),
        "skills": [
            "Java", "Spring Boot", "Hibernate/JPA", "REST APIs", "MySQL",
            "Maven", "Git & GitHub", "Docker", "JWT Authentication",
        ],
        "projects": [
            {
                "title": "Employee Management System",
                "points": [
                    "Manage employee records and departments",
                    "Attendance and leave management",
                    "Role-based authentication",
                ],
            },
            {
                "title": "Online Banking System",
                "points": [
                    "Secure account and transaction management",
                    "Fund transfer functionality",
                    "Transaction history tracking",
                ],
            },
            {
                "title": "Job Portal Backend API",
                "points": [
                    "Recruiter and candidate management",
                    "Job posting and application APIs",
                    "Resume upload support",
                ],
            },
        ],
    },
    "business analyst + c2c": {
        "headline": "Business Analyst",
        "summary": (
            "Business analyst with a strong requirements, process mapping, and reporting background "
            "across stakeholder-facing projects."
        ),
        "skills": [
            "SQL", "Excel", "Power BI", "Tableau", "Jira", "Confluence",
            "BPMN", "UML", "Agile/Scrum", "Requirement Gathering",
        ],
        "projects": [
            {
                "title": "HR Recruitment Process Analysis",
                "points": [
                    "Documented recruitment workflows",
                    "Created user stories and BRD",
                    "Identified process improvements",
                ],
            },
            {
                "title": "Hospital Management Analysis",
                "points": [
                    "Mapped patient management processes",
                    "Performed gap analysis",
                    "Prepared requirement documents",
                ],
            },
            {
                "title": "E-commerce KPI Dashboard",
                "points": [
                    "Analyzed sales performance",
                    "Created business dashboards",
                    "Generated actionable insights",
                ],
            },
        ],
    },
    "data analyst + c2c": {
        "headline": "Data Analyst",
        "summary": (
            "Data analyst experienced in SQL, dashboards, and exploratory data analysis "
            "with a focus on actionable reporting."
        ),
        "skills": [
            "Python", "SQL", "Excel", "Power BI", "Tableau",
            "Pandas", "NumPy", "Statistics", "Data Visualization",
        ],
        "projects": [
            {
                "title": "Netflix Data Analysis",
                "points": [
                    "Analyzed content trends and genres",
                    "Created interactive dashboards",
                    "Visualized key insights",
                ],
            },
            {
                "title": "Sales Forecasting Dashboard",
                "points": [
                    "Predicted future sales trends",
                    "Built KPI monitoring dashboards",
                    "Analyzed revenue growth",
                ],
            },
            {
                "title": "HR Attrition Analysis",
                "points": [
                    "Studied employee turnover patterns",
                    "Generated department-wise insights",
                    "Created analytical reports",
                ],
            },
        ],
    },
    "frontend developer": {
        "headline": "Frontend Developer",
        "summary": (
            "Frontend developer focused on modern interfaces, responsive layouts, and clean component-driven UI."
        ),
        "skills": [
            "HTML5", "CSS3", "JavaScript", "TypeScript", "React.js",
            "Next.js", "Tailwind CSS", "Redux", "Git & GitHub",
        ],
        "projects": [
            {
                "title": "AI Resume Builder",
                "points": [
                    "Dynamic resume generation",
                    "Multiple resume templates",
                    "PDF export functionality",
                ],
            },
            {
                "title": "E-commerce Frontend",
                "points": [
                    "Responsive product catalog",
                    "Cart and wishlist features",
                    "REST API integration",
                ],
            },
            {
                "title": "Task Management App",
                "points": [
                    "Create and manage tasks",
                    "Drag-and-drop interface",
                    "Progress tracking dashboard",
                ],
            },
        ],
    },
    "web developer": {
        "headline": "Web Developer",
        "summary": (
            "Web developer experienced with full-stack web application workflows and REST integrations."
        ),
        "skills": [
            "HTML5", "CSS3", "JavaScript", "React.js", "Node.js",
            "Express.js", "MongoDB", "MySQL", "REST APIs", "Git & GitHub",
        ],
        "projects": [
            {
                "title": "LinkedIn Job Portal",
                "points": [
                    "Job posting and application system",
                    "Resume upload functionality",
                    "Role-based authentication",
                ],
            },
            {
                "title": "College Event Management System",
                "points": [
                    "Event registration platform",
                    "QR-based attendance tracking",
                    "Admin dashboard",
                ],
            },
            {
                "title": "Freelancer Marketplace",
                "points": [
                    "Project posting system",
                    "Proposal management",
                    "User reviews and ratings",
                ],
            },
        ],
    },
}


def normalize_role(role: str) -> str:
    return re.sub(r"\s+", " ", (role or "").strip().lower())


def _split_lines(text: str) -> List[str]:
    return [line.strip("- \t") for line in (text or "").splitlines() if line.strip("- \t")]


def _dedupe(items: List[str]) -> List[str]:
    seen = set()
    result = []
    for item in items:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def build_resume_html(candidate: CandidateProfile) -> str:
    role_key = normalize_role(candidate.target_role)
    template = ROLE_TEMPLATES.get(role_key, ROLE_TEMPLATES["java developer + c2c"])

    skills = _dedupe(_split_lines(candidate.technical_skills.replace(",", "\n")) or list(template["skills"]))
    if not skills:
        skills = list(template["skills"])

    project_text = _split_lines(candidate.projects)
    role_projects = template["projects"]

    project_blocks = []
    if project_text and len(project_text) >= 3:
        for idx, title in enumerate(project_text[:3], start=1):
            project_blocks.append({"title": title, "points": ["Tailored to the selected role", "Edit this section before sharing", "Generated from your current profile"]})
    else:
        project_blocks = role_projects  # type: ignore[assignment]

    name = html.escape(candidate.name or "Prashant Gupta")
    email = html.escape(candidate.email or "")
    phone = html.escape(candidate.phone or "")
    location = html.escape(candidate.location or "")
    linkedin = html.escape(candidate.linkedin_url or "")
    headline = html.escape(str(template["headline"]))
    summary = html.escape(str(template["summary"]))
    target_role = html.escape(candidate.target_role or str(template["headline"]))

    skills_html = "".join(f"<span class='pill'>{html.escape(skill)}</span>" for skill in skills)
    projects_html = "".join(
        """
        <section class='project-card'>
          <h3>{title}</h3>
          <ul>{points}</ul>
        </section>
        """.format(
            title=html.escape(str(block["title"])),
            points="".join(f"<li>{html.escape(point)}</li>" for point in block["points"]),
        )
        for block in project_blocks
    )

    return f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset='utf-8' />
  <style>
    @page {{ size: A4; margin: 18mm; }}
    body {{ font-family: Arial, Helvetica, sans-serif; color: #111827; margin: 0; }}
    .resume {{ border: 1px solid #d7deea; border-radius: 18px; overflow: hidden; }}
    .header {{ background: linear-gradient(135deg, #0f172a, #1e3a8a); color: white; padding: 26px 28px; }}
    .header h1 {{ margin: 0; font-size: 28px; }}
    .header .role {{ margin-top: 6px; font-size: 15px; opacity: 0.9; }}
    .header .meta {{ margin-top: 12px; font-size: 12px; line-height: 1.7; opacity: 0.95; }}
    .body {{ padding: 22px 28px 26px; }}
    .section {{ margin-bottom: 18px; }}
    .section h2 {{ margin: 0 0 10px; font-size: 14px; text-transform: uppercase; letter-spacing: 1px; color: #1d4ed8; }}
    .summary {{ font-size: 13px; line-height: 1.7; color: #334155; }}
    .pills {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    .pill {{ background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe; padding: 6px 10px; border-radius: 999px; font-size: 11px; font-weight: 700; }}
    .project-card {{ border: 1px solid #dbe4f0; border-radius: 14px; padding: 12px 14px; margin-bottom: 10px; }}
    .project-card h3 {{ margin: 0 0 8px; font-size: 13.5px; color: #0f172a; }}
    .project-card ul {{ margin: 0; padding-left: 18px; color: #475569; font-size: 12.5px; line-height: 1.6; }}
    .footer-note {{ margin-top: 10px; font-size: 11px; color: #64748b; }}
  </style>
</head>
<body>
  <div class='resume'>
    <div class='header'>
      <h1>{name}</h1>
      <div class='role'>{headline} | {target_role}</div>
      <div class='meta'>
        {email}<br />{phone}<br />{location}<br />{linkedin}
      </div>
    </div>
    <div class='body'>
      <div class='section'>
        <h2>Professional Summary</h2>
        <div class='summary'>{summary}</div>
      </div>
      <div class='section'>
        <h2>Technical Skills</h2>
        <div class='pills'>{skills_html}</div>
      </div>
      <div class='section'>
        <h2>Projects</h2>
        {projects_html}
      </div>
      <div class='footer-note'>Generated from the selected role and your current profile.</div>
    </div>
  </div>
</body>
</html>
"""


async def generate_resume_pdf(candidate: CandidateProfile, output_path: str) -> str:
    html_content = build_resume_html(candidate)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1240, "height": 1754})
        try:
            await page.set_content(html_content, wait_until="domcontentloaded")
            await page.pdf(path=output_path, format="A4", print_background=True, margin={"top": "0mm", "right": "0mm", "bottom": "0mm", "left": "0mm"})
        finally:
            await browser.close()
    return output_path


def _draw_overlay_pdf(candidate: CandidateProfile) -> bytes:
    """Create a transparent overlay PDF placing text at fixed coordinates.

    Coordinates are in points with origin at bottom-left. We target A4.
    This overlay will be merged on top of the template PDF to place dynamic
    text into the template's reserved spaces.
    """
    buffer = io.BytesIO()
    width, height = A4  # points
    c = canvas.Canvas(buffer, pagesize=A4)

    name = candidate.name or ""
    headline = (candidate.target_role or "").upper()
    contact = ", ".join(filter(None, [candidate.phone or "", candidate.email or ""]))
    location = candidate.location or ""
    skills = candidate.technical_skills or ""
    projects = candidate.projects or ""

    # Place name near top-left region of template
    c.setFont("Helvetica-Bold", 20)
    c.setFillColorRGB(0.06, 0.06, 0.06)
    c.drawString(48, height - 96, name)

    # Headline / target role under name
    c.setFont("Helvetica", 11)
    c.setFillColorRGB(0.17, 0.38, 0.89)
    c.drawString(48, height - 118, headline)

    # Contact block on right side of header
    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0.13, 0.13, 0.13)
    c.drawRightString(width - 48, height - 90, contact)
    c.drawRightString(width - 48, height - 106, location)

    # Skills pill area — render as wrapped lines starting beneath header
    c.setFont("Helvetica", 9)
    y = height - 150
    max_width = width - 96
    for line in skills.split("\n")[:6]:
        if not line.strip():
            continue
        c.drawString(48, y, line.strip())
        y -= 14

    # Projects — place in main body left column
    c.setFont("Helvetica-Bold", 10)
    y = height - 300
    for block in (projects.split("\n") if projects else [])[:3]:
        if not block.strip():
            continue
        c.drawString(48, y, block.strip())
        y -= 14

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer.read()


def generate_resume_using_template(candidate: CandidateProfile, output_path: str) -> str:
    """Generate a PDF by merging a filled transparent overlay onto the
    `data/harshibar_s_resume__2_.pdf` template. Falls back to HTML-rendered
    resume when template or libraries are not available.
    """
    tpl_path = Path(__file__).resolve().parents[1] / "data" / "harshibar_s_resume__2_.pdf"
    if not tpl_path.exists():
        # fallback to Playwright-rendered HTML
        import asyncio

        return asyncio.get_event_loop().run_until_complete(generate_resume_pdf(candidate, output_path))

    overlay_bytes = _draw_overlay_pdf(candidate)

    # Merge overlay onto template
    tpl = PdfReader(str(tpl_path))
    overlay = PdfReader(io.BytesIO(overlay_bytes))
    writer = PdfWriter()

    base_page = tpl.pages[0]
    base_page.merge_page(overlay.pages[0])
    writer.add_page(base_page)

    with open(output_path, "wb") as fh:
        writer.write(fh)

    return output_path


def generate_resume(candidate: CandidateProfile, output_path: str) -> str:
    """Public entry: prefer template merge, fallback to HTML-rendered PDF."""
    try:
        return generate_resume_using_template(candidate, output_path)
    except Exception:
        import asyncio

        return asyncio.get_event_loop().run_until_complete(generate_resume_pdf(candidate, output_path))
