"""Per-offer CV tailoring: Claude adapts the Word CV, code enforces that missing technologies never become experience."""

from __future__ import annotations

import os

import copy
import re
import unicodedata
from pathlib import Path

import llm
import radar
from radar import normalize

OUT = radar.ROOT / "data" / "cv_tailored"
DEFAULTS = {
    "enabled": True,
    "tailor_summary": True,
    "tailor_experience": True,
    "tailor_headline": True,
    "add_missing_skills": True,
    "max_added_skills": 6,
    "missing_in_cover_letter": True,
    "rules": ("Adaptá el CV al aviso usando sus mismas palabras cuando describan algo que realmente hice. "
              "Reordená viñetas y habilidades para que lo más relevante quede primero. "
              "Nunca inventes logros, cifras, empresas, fechas ni responsabilidades."),
    "cover_letter_rules": ("Carta breve (4 a 7 oraciones), concreta y sin frases infladas. Mencioná 2 o 3 coincidencias "
                           "reales entre el aviso y mi experiencia. Las tecnologías que pide el aviso y no están en mi "
                           "experiencia nombralas como tecnologías que manejo o con las que estoy trabajando, nunca como "
                           "experiencia laboral ni con años de uso. No te disculpes ni señales carencias: no menciones requisitos "
                           "que no cumplo (seniority, industria, años), salvo nombrar las tecnologías faltantes así."),
}


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **cfg.get("tailoring", {})}


# ---------- headline: the role the offer is hiring for ----------

# Order matters: the first match wins. "Full Stack" only when the offer itself is full stack.
ROLE_RULES = [
    (r"full[\s-]?stack", "Full Stack Developer"),
    (r"machine learning|\bml\b|mlops|aprendizaje autom", "Machine Learning Engineer"),
    (r"\bai\b|\bia\b|\bllm|agentic|generative|gen ?ai|artificial intelligence|inteligencia artificial", "AI Engineer"),
    (r"analytics engineer", "Analytics Engineer"),
    (r"data engineer|ingenier\w* de datos|\betl\b|data platform|big data|data pipeline|databricks|data warehouse|dwh", "Data Engineer"),
    (r"data analyst|analista de datos|analista de bi|data analytics", "Data Analyst"),
    (r"\bbi\b|business intelligence|power ?bi", "BI Developer"),
    (r"front[\s-]?end", "Frontend"),
    (r"back[\s-]?end", "Backend"),
    (r"python", "Python Developer"),
    (r"software|developer|desarrollador|programador|engineer|ingeniero", "Software Engineer"),
]

# Technologies and platforms the profile says Jero has no real experience in: never claimed, whatever the offer asks.
FORBIDDEN_RE = re.compile(r"(?<![a-z0-9])(wordpress|shopify|hydrogen|spring(?:\s*boot)?|java(?!\s*script))(?![a-z0-9])", re.I)
YEARS_CLAIM_RE = re.compile(r"(?<![\d.,])(\d{1,2}(?:[.,]\d)?)\s*\+?\s*(?:years?|a[nñ]os)", re.I)
MAX_YEARS_CLAIM = 3


# Offer technologies the CV must show (Jero 2026-11-25: "cumpliendo todo"). Display name and skills line for each
# term of radar.scoring()["technologies"]; the line is matched by a keyword of its label, else "Other technologies".
DISPLAY = {
    "aws": "AWS", "azure": "Azure", "azure data factory": "Azure Data Factory", "bigquery": "BigQuery", "ci/cd": "CI/CD",
    "data lake": "Data Lake", "data warehouse": "Data Warehouse", "databricks": "Databricks", "dbt": "dbt", "elt": "ELT",
    "etl": "ETL", "fabric": "Microsoft Fabric", "gcp": "GCP", "google cloud": "Google Cloud", "graphql": "GraphQL",
    "javascript": "JavaScript", "llm": "LLMs", "mcp": "MCP", "mongodb": "MongoDB", "mysql": "MySQL", "n8n": "n8n",
    "nestjs": "NestJS", "next.js": "Next.js", "node": "Node.js", "postgresql": "PostgreSQL", "power bi": "Power BI",
    "pyspark": "PySpark", "s3": "AWS S3", "sql": "SQL", "sql server": "SQL Server", "typescript": "TypeScript",
    "vue": "Vue", "delta lake": "Delta Lake",
}
LINE_FOR = {
    "frameworks": {"angular", "vue", "express", "nestjs", "javascript", "typescript", "python", "scala", "react",
                   "next.js", "node", "tailwind"},
    "devops": {"aws", "azure", "gcp", "google cloud", "lambda", "s3", "kubernetes", "terraform", "docker", "ci/cd", "git"},
    "ai": {"llm", "mcp", "n8n"},
}


def _line_key(label: str) -> str:
    key = normalize(label)
    if "framework" in key or "lenguaje" in key or "language" in key:
        return "frameworks"
    if "devops" in key or "cloud" in key:
        return "devops"
    if re.search(r"(^|\W)(ai|ia)(\W|$)", key):
        return "ai"
    if "data" in key or "datos" in key or "backend" in key:
        return "data"
    return ""


def _base(item: str) -> str:
    return normalize(item.split("(", 1)[0]).strip()


def dedupe_skills(skills: list[dict]) -> None:
    """One entry per technology across all lines: 'Power BI (DAX, Power Query)' wins over 'Power BI (DAX)'."""
    best = {}
    for x in skills:
        for item in split_items(x["value"]):
            b = _base(item)
            if b not in best or len(item) > len(best[b]):
                best[b] = item
    seen = set()
    for x in skills:
        keep = []
        for item in split_items(x["value"]):
            b = _base(item)
            if b in seen:
                continue
            seen.add(b)
            keep.append(best[b])
        x["value"] = ", ".join(keep)
    skills[:] = [x for x in skills if x["value"]]


def offer_terms(job: dict | None, cfg: dict) -> set[str]:
    """Technologies the offer asks for that the CV may claim (not forbidden by the profile, not blocked by the cloud rule)."""
    if not job:
        return set()
    import agent  # local import: agent imports this module
    rule = agent.cloud_filter(cfg)
    blocked = rule["blocked_terms"] if rule["enabled"] else []
    return {t for t in radar.technologies(f"{job.get('title', '')} {job.get('description', '')}")
            if not forbidden_in(t) and not agent._terms_in(t, blocked)}


def complete_coverage(content: dict, facts: dict, job: dict | None, cfg: dict, language: str) -> list[str]:
    """Add to the skills every offer technology the tailored CV still does not mention. Returns what was added."""
    text = " ".join([content.get("headline") or "", content["summary"]]
                    + [f"{x['label']} {x['value']}" for x in content["skills"]]
                    + [b for r in content["roles"] for b in r["bullets"]] + facts.get("projects", []))
    missing = sorted(offer_terms(job, cfg) - set(radar.technologies(text)))
    added = []
    for term in missing:
        name = DISPLAY.get(term, term.title() if len(term) > 3 else term.upper())
        target = next((k for k, terms in LINE_FOR.items() if term in terms), "data")
        line = next((x for x in content["skills"] if _line_key(x["label"]) == target), None)
        if line is None:
            other = "Other technologies" if language == "en" else "Otras tecnologías"
            line = next((x for x in content["skills"] if normalize(x["label"]) == normalize(other)), None)
            if line is None:
                line = {"label": other, "value": ""}
                content["skills"].append(line)
        line["value"] = f"{line['value']}, {name}" if line["value"] else name
        added.append(name)
    return added


def role_title(job: dict) -> str | None:
    """Clean role name for the CV headline from the offer title (no seniority, location or company). None if unknown."""
    title = normalize(job.get("title") or "")
    for pattern, role in ROLE_RULES:
        if re.search(pattern, title):
            if role in ("Frontend", "Backend"):
                return role + (" Developer" if re.search(r"developer|desarrollador|programador", title) else " Engineer")
            return role
    return None


def forbidden_in(text: str) -> list[str]:
    return sorted({m.group(1).lower() for m in FORBIDDEN_RE.finditer(text or "")})


def profile_technologies(cfg: dict) -> set[str]:
    """Technologies the candidate profile declares (read only), minus the ones it says he does not have."""
    try:
        path = Path(cfg.get("profile_path", "profile.md"))
        text = (path if path.is_absolute() else radar.ROOT / path).read_text(encoding="utf-8")
    except OSError:
        return set()
    return {t for t in radar.technologies(text) if not forbidden_in(t)}


# ---------- reading the Word CV ----------

def _is_list(p) -> bool:
    ppr = p._p.pPr
    return ppr is not None and ppr.numPr is not None


def _is_heading(p) -> bool:
    text = p.text.strip()
    return bool(text) and not _is_list(p) and text == text.upper() and bool(p.runs) and bool(p.runs[0].bold)


def parse(document) -> dict:
    """Locate summary, skills ('Label: value' bullets) and experience roles in the CV."""
    sections, current = {}, None
    for p in document.paragraphs:
        if _is_heading(p) and p is not document.paragraphs[0]:
            key = normalize(p.text)
            current = ("summary" if any(k in key for k in ("resumen", "summary", "perfil", "profile")) else
                       "skills" if any(k in key for k in ("habilidades", "skills", "tecnolog")) else
                       "experience" if any(k in key for k in ("experiencia", "experience")) else
                       "projects" if any(k in key for k in ("proyectos", "projects")) else "other")
            sections.setdefault(current, [])
            continue
        if current and p.text.strip():
            sections[current].append(p)
    roles = []
    for p in sections.get("experience", []):
        if _is_list(p):
            if roles:
                roles[-1]["bullets"].append(p)
        else:
            roles.append({"header": p, "bullets": []})
    skills = [p for p in sections.get("skills", []) if _is_list(p) and ":" in p.text]
    return {"summary": sections.get("summary", [None])[0], "skills": skills, "roles": roles,
            "projects": [p for p in sections.get("projects", []) if _is_list(p)],
            "headline": document.paragraphs[1] if len(document.paragraphs) > 1 and document.paragraphs[1].text.strip() else None}


def split_items(value: str) -> list[str]:
    """Split 'Python, SQL (T-SQL, MySQL), Git' on commas outside parentheses."""
    items, depth, buf = [], 0, ""
    for ch in value:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            items.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        items.append(buf.strip())
    return [i for i in items if i]


def base_facts(parsed: dict) -> dict:
    skills = [{"label": p.text.split(":", 1)[0].strip(), "value": p.text.split(":", 1)[1].strip()} for p in parsed["skills"]]
    return {"summary": parsed["summary"].text if parsed["summary"] else "",
            "headline": parsed["headline"].text.strip() if parsed["headline"] is not None else "",
            "projects": [p.text.strip() for p in parsed["projects"]],
            "skills": skills,
            "roles": [{"header": r["header"].text.strip(), "bullets": [b.text.strip() for b in r["bullets"]]}
                      for r in parsed["roles"]]}


# ---------- asking Claude ----------

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "skills": {"type": "array", "items": {"type": "object", "properties": {
            "label": {"type": "string"}, "value": {"type": "string"}}, "required": ["label", "value"]}},
        "roles": {"type": "array", "items": {"type": "object", "properties": {
            "bullets": {"type": "array", "items": {"type": "string"}}}, "required": ["bullets"]}},
        "project_order": {"type": "array", "items": {"type": "integer"},
                          "description": "Índices de los proyectos (empezando en 0) del más al menos relevante para la oferta"},
        "missing": {"type": "array", "items": {"type": "string"},
                    "description": "Tecnologías o requisitos del aviso que no figuran en la experiencia del CV"},
        "cover_letter": {"type": "string"},
    },
    "required": ["summary", "skills", "roles", "project_order", "missing", "cover_letter"],
}


def ask_claude(job: dict, facts: dict, cfg: dict, language: str, as_email: bool, headline: str | None = None) -> dict:
    s = settings(cfg)
    import agent  # local import: agent imports this module
    roles = "\n".join(f"[{i}] {r['header']}\n" + "\n".join(f"  - {b}" for b in r["bullets"])
                      for i, r in enumerate(facts["roles"]))
    skills = "\n".join(f"- {x['label']}: {x['value']}" for x in facts["skills"])
    projects = "\n".join(f"[{i}] {p}" for i, p in enumerate(facts.get("projects", [])))
    niche = (
        f"\nTITULAR Y NICHO: el titular del CV será «{headline}» (lo pone el código; no lo devuelvas). El CV tiene que "
        "reflejar lo que busca esta oferta, no un perfil genérico.\n"
        f"- Resumen: 2 o 3 oraciones, máximo 3 renglones. Abrí presentándome como {headline} y poné primero la experiencia "
        "real más relevante para ese nicho, con hechos del CV y del perfil. "
        + ("" if "full stack" in headline.lower() else "No uses la expresión «full stack» en el resumen ni en las habilidades nuevas. ")
        + "Hechos verdaderos disponibles: en JoterHogar (retail, analista y analytics engineer, ene 2024 a ago 2026) ETL y "
        "pipelines en Python, AWS (S3, IAM, boto3), Snowflake, PostgreSQL, Star Schema, Power BI; en Ritzy (ago 2026 a hoy) "
        "integraciones, backend, machine learning y agentes LLM; certificación Azure Databricks Data Engineer Associate "
        "(DP-750). Para frontend o backend: React, TypeScript, Next.js, Node y Supabase en Ritzy.\n"
        "- No afirmes más de 3 años de experiencia en datos ni más de 1 en software full time. Nunca menciones WordPress, "
        "Shopify ni Java/Spring: no tengo experiencia en eso.\n"
        "- Viñetas: dentro de cada rol, poné primero las que más pesan para el nicho. Habilidades: poné primero la línea de "
        "la categoría del nicho y dentro de cada línea lo más relevante primero.\n"
        "- project_order: ordená los proyectos con los del nicho primero; devolvé todos los índices sin repetir.\n"
    ) if headline else ""
    prompt = (
        "Adaptá mi CV a esta oferta y escribí la carta de presentación.\n\n"
        "REGLAS DURAS (se verifican automáticamente y lo que las viole se descarta):\n"
        + ("1. Resumen y viñetas de experiencia: tengo experiencia en todo lo que pide el aviso. Reescribí las viñetas "
           "con el vocabulario del aviso y sumá las tecnologías y tareas que pide, atribuidas al rol real donde sean "
           "creíbles" + (f" ({s['experience_hint']})" if s.get("experience_hint") else "") + ". Orientá todo al puesto: nada de "
           "listar todo lo que hice, solo lo que le sirve a esta oferta. No inventes cifras, empresas ni fechas. "
           "Mantené la cantidad de roles y no agregues viñetas.\n" if s.get("allow_job_technologies") else
           "1. Resumen y viñetas de experiencia: solo hechos que ya están en el CV. Podés reescribirlos, reordenarlos y usar "
           "el vocabulario del aviso, pero NO agregues tecnologías, herramientas ni tareas que no figuren en esa misma viñeta "
           "o en el CV. Mantené la cantidad de roles y no agregues viñetas.\n")
        + ("2. Habilidades técnicas: agregá TODAS las tecnologías que pide el aviso y no figuran (sin tope; otras que "
           f"no pida el aviso, máximo {s['max_added_skills']}), ubicándolas en la categoría que corresponda o en una línea nueva "
           "«Otras tecnologías» / «Other technologies». Conservá todas las categorías y valores existentes, reordenados "
           "por relevancia.\n" if s["add_missing_skills"] else
           "2. Habilidades técnicas: solo reordená; no agregues tecnologías nuevas.\n")
        + ("" if s["tailor_summary"] else "3. Devolvé el resumen exactamente igual.\n")
        + ("" if s["tailor_experience"] else "4. Devolvé las viñetas exactamente iguales.\n")
        + niche + f"\nREGLAS DE ESTILO DEL CV:\n{s['rules']}\n\n"
        f"CARTA ({'cuerpo del correo con saludo y firma, el CV va adjunto' if as_email else 'texto para pegar en el formulario, sin firma de correo'}):\n"
        f"{s['cover_letter_rules']}"
        + ("" if s["missing_in_cover_letter"] else "\nNo menciones en la carta las tecnologías que no tengo.")
        + f"\n\nIdioma de todo el contenido: {'inglés' if language == 'en' else 'español'}.\n\n"
        f"=== MI CV ===\nRESUMEN:\n{facts['summary']}\n\nHABILIDADES:\n{skills}\n\nEXPERIENCIA:\n{roles}\n\nPROYECTOS:\n{projects}\n\n"
        f"=== OFERTA ===\nCargo: {job['title']}\nEmpresa: {job['company']}\nUbicación: {job['location']}\n\n"
        f"{(job.get('description') or '')[:12000]}")
    return llm.ask(agent.system_prompt(cfg), prompt, SCHEMA, model=cfg["autopilot"].get("model", "sonnet"))


# ---------- enforcing the rules ----------

def guard(facts: dict, proposal: dict, cfg: dict, job: dict | None = None) -> tuple[dict, list[str], list[str]]:
    """Revert any experience/summary text that introduces technologies absent from the CV.
    Returns (clean proposal, added skills, reverted notes)."""
    s = settings(cfg)
    original_text = facts["summary"] + " " + " ".join(f"{x['label']} {x['value']}" for x in facts["skills"]) + " " + \
        " ".join(b for r in facts["roles"] for b in r["bullets"])
    known = set(radar.technologies(original_text))
    if s.get("allow_job_technologies") and job:  # config: el candidato declara experiencia en lo que pide el aviso
        known |= set(radar.technologies(f"{job.get('title', '')} {job.get('description', '')}"))
    known |= profile_technologies(cfg)  # AWS, Snowflake, LLM...: declared in the profile as real experience
    reverted = []
    headline = role_title(job) if job and s["tailor_headline"] else None
    is_fullstack = not headline or "full stack" in headline.lower()

    summary = proposal["summary"].strip() if s["tailor_summary"] else facts["summary"]
    extra = set(radar.technologies(summary)) - known
    bad = forbidden_in(summary)
    years = [float(n.replace(",", ".")) for n in YEARS_CLAIM_RE.findall(summary) if float(n.replace(",", ".")) > MAX_YEARS_CLAIM]
    if extra or bad or years or not summary or (not is_fullstack and re.search(r"full[\s-]?stack", summary, re.I)):
        why = (f"mencionaba {', '.join(sorted(extra | set(bad)))}" if extra or bad else
               f"afirmaba {max(years):g} años" if years else "decía full stack o venía vacío")
        reverted.append(f"Resumen: {why}; se dejó el original")
        summary = facts["summary"]
        if headline:  # fallback keeps the headline consistent with the original wording
            summary = re.sub(r"Full Stack Developer( with a data engineering background| con base en ingenier[ií]a de datos)?",
                             headline, summary, count=1)

    roles = []
    for i, base in enumerate(facts["roles"]):
        new = proposal["roles"][i]["bullets"] if s["tailor_experience"] and i < len(proposal["roles"]) else base["bullets"]
        new = [b.strip() for b in new if b.strip()][:len(base["bullets"])]
        if len(new) < len(base["bullets"]):
            new = base["bullets"]
        extra = (set(radar.technologies(" ".join(new))) - known) | set(forbidden_in(" ".join(new)))
        if extra:
            # Bullets may be reordered, so a violation reverts the whole role to its original wording.
            reverted.append(f"{base['header'][:40]}: mencionaba {', '.join(sorted(extra))}; se dejaron las viñetas originales")
            new = base["bullets"]
        roles.append({"bullets": new})

    # Skills: keep every original item, allow up to N new ones.
    original_items = {normalize(i) for x in facts["skills"] for i in split_items(x["value"])}
    import agent  # local import: agent imports this module
    rule = agent.cloud_filter(cfg)
    blocked_cloud = rule["blocked_terms"] if rule["enabled"] else []
    wanted = offer_terms(job, cfg) if s.get("allow_job_technologies") else set()
    added, skills = [], []
    for x in proposal["skills"]:
        keep = []
        for item in split_items(x["value"]):
            if normalize(item) in original_items:
                keep.append(item)
            elif forbidden_in(item):
                continue  # profile: no real experience (WordPress, Shopify, Java/Spring)
            elif agent._terms_in(item, blocked_cloud):
                continue  # Microsoft-only search: never add AWS/GCP skills to the CV
            elif s["add_missing_skills"] and set(radar.technologies(item)) & wanted:
                keep.append(item)  # the offer asks for it: no cap
                added.append(item)
            elif s["add_missing_skills"] and len(added) < s["max_added_skills"]:
                keep.append(item)
                added.append(item)
        if keep:
            skills.append({"label": x["label"].strip(), "value": ", ".join(keep)})
    present = {normalize(i) for x in skills for i in split_items(x["value"])}
    for x in facts["skills"]:  # anything Claude dropped goes back in its original line
        lost = [i for i in split_items(x["value"]) if normalize(i) not in present]
        if lost:
            line = next((y for y in skills if normalize(y["label"]) == normalize(x["label"])), None)
            if line:
                line["value"] += ", " + ", ".join(lost)
            else:
                skills.append({"label": x["label"], "value": ", ".join(lost)})
    order = proposal.get("project_order") or []
    n_proj = len(facts.get("projects", []))
    if sorted(order) != list(range(n_proj)):
        order = list(range(n_proj))  # not a clean permutation: keep the original order
    return {"headline": headline, "summary": summary, "skills": skills, "roles": roles,
            "project_order": order}, added, reverted


# ---------- writing the Word CV and the PDF ----------

def _set_text(paragraph, text: str) -> None:
    runs = paragraph.runs
    runs[0].text = text
    for r in runs[1:]:
        r._r.getparent().remove(r._r)


def _set_skill(paragraph, label: str, value: str) -> None:
    runs = paragraph.runs
    bold = next((r for r in runs if r.bold), runs[0])
    plain = next((r for r in runs if not r.bold), None)
    bold.text = f"{label}: "
    if plain is None:
        plain = paragraph.add_run()
    plain.text = value
    for r in list(paragraph.runs):
        if r._r is not bold._r and r._r is not plain._r:
            r._r.getparent().remove(r._r)


def write_docx(source: Path, content: dict, target: Path) -> None:
    import docx
    document = docx.Document(str(source))
    parsed = parse(document)
    if content.get("headline") and parsed["headline"] is not None:
        _set_text(parsed["headline"], content["headline"])
    if parsed["summary"] is not None:
        _set_text(parsed["summary"], content["summary"])
    lines = parsed["skills"]
    # Match the number of skill lines: clone the last one or drop extras.
    while len(lines) < len(content["skills"]):
        clone = copy.deepcopy(lines[-1]._p)
        lines[-1]._p.addnext(clone)
        lines = parse(document)["skills"]
    for p in lines[len(content["skills"]):]:
        p._p.getparent().remove(p._p)
    for p, x in zip(parse(document)["skills"], content["skills"]):
        _set_skill(p, x["label"], x["value"])
    for role, new in zip(parse(document)["roles"], content["roles"]):
        for p, text in zip(role["bullets"], new["bullets"]):
            _set_text(p, text)
    projects = parse(document)["projects"]
    order = content.get("project_order") or []
    if projects and sorted(order) == list(range(len(projects))) and order != sorted(order):
        anchor = projects[0]._p.getprevious()  # the PROJECTS heading
        elements = [projects[i]._p for i in order]
        for el in elements:
            el.getparent().remove(el)
        for el in reversed(elements):
            anchor.addnext(el)
    drop = min(content.get("drop_projects", 0), max(len(projects) - 2, 0))  # always keep 2 projects
    for p in (parse(document)["projects"][-drop:] if drop else []):  # least relevant ones are last
        p._p.getparent().remove(p._p)
    target.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(target))


def to_pdf(docx_path: Path, pdf_path: Path) -> None:
    """Convert with a private, invisible Word instance (does not touch documents the user has open).
    On Linux, with LibreOffice headless."""
    if os.name != "nt":
        import shutil
        import subprocess
        import tempfile
        soffice = shutil.which("soffice") or str(Path.home() / "libreoffice/opt/program/soffice")
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([soffice, "--headless", f"-env:UserInstallation=file://{tmp}/profile", "--convert-to", "pdf",
                            "--outdir", tmp, str(docx_path)], check=True, capture_output=True, timeout=180)
            shutil.move(str(Path(tmp) / (Path(docx_path).stem + ".pdf")), str(pdf_path))
        return
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    word = win32com.client.DispatchEx("Word.Application")
    word.Visible = False
    word.DisplayAlerts = 0
    try:
        doc = word.Documents.Open(str(docx_path), ReadOnly=True, AddToRecentFiles=False)
        doc.SaveAs2(str(pdf_path), FileFormat=17)
        doc.Close(False)
    finally:
        word.Quit()
        pythoncom.CoUninitialize()


def page_count(pdf_path: Path) -> int | None:
    try:
        import pymupdf
    except ImportError:
        return None
    with pymupdf.open(str(pdf_path)) as doc:
        return doc.page_count


def shrink_steps(content: dict, added: list[str], job: dict, facts: dict):
    """Ways to win back space when the tailored CV spills to a second page, mildest first. Offer technologies stay."""
    new = {normalize(a) for a in added}
    original = {_base(i) for x in facts["skills"] for i in split_items(x["value"])}  # never dropped
    offer = normalize(f"{job.get('title', '')} {job.get('description', '')}")

    def edit(fn):
        for x in content["skills"]:
            x["value"] = ", ".join(i for i in (fn(i) for i in split_items(x["value"])) if i)
        content["skills"][:] = [x for x in content["skills"] if x["value"]]

    wanted = set(radar.technologies(offer))

    def strip_parens(i):  # 'AWS (Bedrock, Lambda, ECS...)' -> 'AWS (Lambda)' when the offer only names Lambda
        if normalize(i) not in new or "(" not in i:
            return i
        head, inner = i.split("(", 1)
        keep = [x for x in split_items(inner.rsplit(")", 1)[0]) if set(radar.technologies(x)) & wanted]
        return f"{head.strip()} ({', '.join(keep)})" if keep else head.strip()
    edit(strip_parens)
    new |= {normalize(a.split("(", 1)[0].strip()) for a in added}
    yield "detalle de habilidades nuevas"
    edit(lambda i: "" if normalize(i) in new and _base(i) not in offer and _base(i) not in original else i)
    yield "habilidades nuevas que el aviso no nombra"
    for n in (1, 2):
        content["drop_projects"] = n
        yield f"{n} proyecto(s) menos relevante(s)"


def ascii_name(name: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))


def build(job: dict, cv: dict, cfg: dict, language: str, as_email: bool, folder: str | None = None) -> dict:
    """Tailored PDF + cover letter for one offer. Returns paths, added skills, reverted notes and the letter."""
    import docx
    source = Path(cv["docx"])
    facts = base_facts(parse(docx.Document(str(source))))
    headline = role_title(job) if settings(cfg)["tailor_headline"] else None
    proposal = ask_claude(job, facts, cfg, language, as_email, headline)
    content, added, reverted = guard(facts, proposal, cfg, job)
    if settings(cfg).get("allow_job_technologies") and settings(cfg)["add_missing_skills"]:
        added += complete_coverage(content, facts, job, cfg, language)
    dedupe_skills(content["skills"])
    target_dir = OUT / (folder or str(job["id"]))
    stem = ascii_name(Path(cv["path"]).stem)
    docx_out, pdf_out = target_dir / f"{stem}.docx", target_dir / f"{stem}.pdf"
    write_docx(source, content, docx_out)
    to_pdf(docx_out, pdf_out)
    base_pages = page_count(Path(cv["path"])) or 1
    pages = page_count(pdf_out)
    if pages and pages > base_pages:  # same length as the original CV, never a page that spills 3 lines
        for step in shrink_steps(content, added, job, facts):
            write_docx(source, content, docx_out)
            to_pdf(docx_out, pdf_out)
            reverted.append(f"Para no pasar de {base_pages} página(s): sin {step}")
            if (page_count(pdf_out) or 0) <= base_pages:
                break
    return {"pdf": pdf_out, "docx": docx_out, "added": added, "reverted": reverted,
            "missing": proposal["missing"], "cover_letter": proposal["cover_letter"].strip(),
            "summary": content["summary"], "base_summary": facts["summary"], "headline": content["headline"]}
