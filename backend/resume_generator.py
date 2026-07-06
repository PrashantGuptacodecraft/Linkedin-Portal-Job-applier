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

try:
    from .models import CandidateProfile
except ImportError:
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

    name = html.escape(candidate.first_name + " " + candidate.last_name if candidate.first_name else "Candidate Name")
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

    First draws white rectangles to blank out the template's dynamic content
    areas, then draws the candidate's information in the cleared zones.
    Coordinates are in points with origin at bottom-left. We target A4.
    """
    buffer = io.BytesIO()
    width, height = A4  # 595.27 x 841.89 points
    c = canvas.Canvas(buffer, pagesize=A4)

    # ── Resolve role template ────────────────────────────────────────────
    role_key = normalize_role(candidate.target_role)
    template = ROLE_TEMPLATES.get(role_key, ROLE_TEMPLATES["java developer + c2c"])

    name = (candidate.first_name + " " + candidate.last_name).strip() if candidate.first_name else "Candidate Name"
    headline = (candidate.target_role or str(template["headline"])).upper()
    email = candidate.email or ""
    phone = candidate.phone or ""
    location = candidate.location or ""
    linkedin = candidate.linkedin_url or ""
    summary = str(template["summary"])

    # Skills
    skills_raw = candidate.technical_skills or ""
    skills_list = _dedupe(
        _split_lines(skills_raw.replace(",", "\n"))
    ) or list(template["skills"])

    # Projects
    project_text = _split_lines(candidate.projects)
    if project_text and len(project_text) >= 3:
        role_projects = [
            {"title": t, "points": ["Role-tailored feature", "Customized implementation"]}
            for t in project_text[:3]
        ]
    else:
        role_projects = template["projects"]

    # Contact line
    contact_parts = [p for p in [phone, email] if p]
    contact_line = "  |  ".join(contact_parts)
    location_line = "  |  ".join([p for p in [location, linkedin] if p])

    # ── Zone definitions (x, y_top, w, h) – areas to blank with white ────
    # Header zone: name, headline, contact (top ~80pt of page)
    header_zone = (0, height - 130, width, 130)
    # Skills zone: below header, above projects
    skills_zone = (30, height - 310, width - 60, 150)
    # Projects zone: main body area
    projects_zone = (30, height - 660, width - 60, 350)

    # ── Step 1: Blank the dynamic zones with white ───────────────────────
    c.setFillColorRGB(1, 1, 1)  # white
    for (x, y, w, h) in [header_zone, skills_zone, projects_zone]:
        c.rect(x, y, w, h, fill=1, stroke=0)

    # ── Step 2: Draw the header ──────────────────────────────────────────
    # Background gradient effect (dark blue header)
    c.setFillColorRGB(0.059, 0.090, 0.165)  # #0f172a dark navy
    c.rect(0, height - 120, width, 120, fill=1, stroke=0)

    # Name
    c.setFont("Helvetica-Bold", 22)
    c.setFillColorRGB(1, 1, 1)
    c.drawString(40, height - 42, name)

    # Headline / target role
    c.setFont("Helvetica", 11)
    c.setFillColorRGB(0.85, 0.90, 1.0)
    c.drawString(40, height - 62, headline)

    # Contact info
    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0.8, 0.85, 0.95)
    c.drawString(40, height - 82, contact_line)
    if location_line:
        c.drawString(40, height - 96, location_line)

    # ── Step 3: Professional Summary ─────────────────────────────────────
    y_cursor = height - 150

    c.setFont("Helvetica-Bold", 11)
    c.setFillColorRGB(0.11, 0.31, 0.83)  # #1d4ed8 blue
    c.drawString(40, y_cursor, "PROFESSIONAL SUMMARY")
    y_cursor -= 18

    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0.2, 0.25, 0.33)
    # Wrap the summary text
    max_line_width = width - 80
    words = summary.split()
    line = ""
    for word in words:
        test_line = f"{line} {word}".strip()
        if c.stringWidth(test_line, "Helvetica", 9) < max_line_width:
            line = test_line
        else:
            c.drawString(40, y_cursor, line)
            y_cursor -= 14
            line = word
    if line:
        c.drawString(40, y_cursor, line)
        y_cursor -= 14

    # ── Step 4: Technical Skills ─────────────────────────────────────────
    y_cursor -= 10

    c.setFont("Helvetica-Bold", 11)
    c.setFillColorRGB(0.11, 0.31, 0.83)
    c.drawString(40, y_cursor, "TECHNICAL SKILLS")
    y_cursor -= 18

    # Render skills as pills/tags in a wrapped row
    c.setFont("Helvetica-Bold", 8)
    x_cursor = 40
    pill_height = 16
    pill_padding = 8
    pill_gap = 6
    row_gap = 6

    for skill in skills_list:
        text_width = c.stringWidth(skill, "Helvetica-Bold", 8)
        pill_width = text_width + pill_padding * 2

        # Check if we need to wrap to next line
        if x_cursor + pill_width > width - 40:
            x_cursor = 40
            y_cursor -= pill_height + row_gap

        # Draw pill background
        c.setFillColorRGB(0.94, 0.96, 1.0)  # #eff6ff light blue bg
        c.roundRect(x_cursor, y_cursor - 4, pill_width, pill_height, 8, fill=1, stroke=0)

        # Draw pill border
        c.setStrokeColorRGB(0.75, 0.86, 0.99)  # #bfdbfe
        c.setLineWidth(0.5)
        c.roundRect(x_cursor, y_cursor - 4, pill_width, pill_height, 8, fill=0, stroke=1)

        # Draw skill text
        c.setFillColorRGB(0.11, 0.31, 0.83)
        c.drawString(x_cursor + pill_padding, y_cursor, skill)

        x_cursor += pill_width + pill_gap

    y_cursor -= pill_height + 20

    # ── Step 5: Projects ─────────────────────────────────────────────────
    c.setFont("Helvetica-Bold", 11)
    c.setFillColorRGB(0.11, 0.31, 0.83)
    c.drawString(40, y_cursor, "PROJECTS")
    y_cursor -= 18

    for proj in role_projects[:3]:
        if y_cursor < 80:
            break  # don't overflow the page

        # Project title
        c.setFont("Helvetica-Bold", 10)
        c.setFillColorRGB(0.06, 0.09, 0.16)
        c.drawString(48, y_cursor, str(proj["title"]))
        y_cursor -= 16

        # Project bullet points
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.28, 0.33, 0.42)
        for point in proj["points"][:4]:
            if y_cursor < 60:
                break
            c.drawString(58, y_cursor, f"•  {point}")
            y_cursor -= 13

        y_cursor -= 8  # gap between projects

    # ── Footer note ──────────────────────────────────────────────────────
    c.setFont("Helvetica", 7)
    c.setFillColorRGB(0.39, 0.45, 0.55)
    c.drawString(40, 30, "Generated resume — tailored from the selected role and candidate profile.")

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer.read()


async def generate_resume_using_template(candidate: CandidateProfile, output_path: str) -> str:
    """Generate a PDF by merging a filled transparent overlay onto the
    `data/harshibar_s_resume__2_.pdf` template. Falls back to HTML-rendered
    resume when template or libraries are not available.
    """
    tpl_path = Path(__file__).resolve().parents[1] / "data" / "harshibar_s_resume__2_.pdf"
    if not tpl_path.exists():
        # fallback to Playwright-rendered HTML
        return await generate_resume_pdf(candidate, output_path)

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


async def generate_resume(candidate: CandidateProfile, output_path: str) -> str:
    """Public entry: prefer template merge, fallback to HTML-rendered PDF."""
    try:
        return await generate_resume_using_template(candidate, output_path)
    except Exception:
        return await generate_resume_pdf(candidate, output_path)
