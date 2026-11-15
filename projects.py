"""Armado automatico de proyectos propios cuando una oferta pide una tecnologia que el perfil no tiene.

Solo recibe nombres de tecnologias (nunca el texto del aviso, que no es de confianza). Corre Claude Code con
Sonnet en esfuerzo medio, arma un prototipo chico que funciona, lo sube a un repo publico del usuario y lo anota
en profile.md. Tope diario en config.json -> projects.max_per_day.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import radar

ROOT = radar.ROOT
STATE = ROOT / "data" / "projects.json"
WORKDIR = Path.home() / "Documents" / "workworm-projects"
SAFE_TECH = re.compile(r"^[A-Za-z0-9 .+#/_-]{1,40}$")

SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "repo_url": {"type": "string"},
        "name": {"type": "string"},
        "summary": {"type": "string", "description": "Una oracion en espanol: que hace y con que stack"},
        "reason": {"type": "string"},
    },
    "required": ["ok", "repo_url", "name", "summary", "reason"],
}

def _owner() -> str:
    return radar.personal("projects", {}).get("github_owner", "")


def _identity() -> dict:
    p = radar.personal("projects", {})
    cfg = radar.config()
    forbidden = p.get("forbidden_accounts", "")
    return {"name": cfg.get("candidate_name", ""), "owner": p.get("github_owner", ""),
            "git_name": p.get("git_name", p.get("github_owner", "")),
            "git_email": p.get("git_email", cfg.get("candidate_email", "")),
            "forbidden": f"Nunca uses cuentas, mails ni tokens de {forbidden}. " if forbidden else ""}


PROMPT = (
    "Arma un proyecto chico y real en {tech} para el portfolio de {name} (GitHub: {owner}).\n"
    "Reglas:\n"
    "- Tiene que funcionar de verdad: corrolo y comproba que arranca y hace lo que dice el README. Tests solo si salen "
    "baratos; no son obligatorios.\n"
    "- Idea concreta y util (una API, CLI o app chica con 3 a 5 funciones), no un hola mundo. Unos 150 a 400 lineas.\n"
    "- README en espanol, honesto: describe lo que hace y como correrlo. Dice que es un proyecto de practica.\n"
    "- Directorio de trabajo: {dir}. Crea ahi una carpeta nueva. Usa solo herramientas ya instaladas o instalables "
    "con el gestor del lenguaje; si falta un toolchain grande y no podes correrlo, devolve ok=false con el motivo.\n"
    "- git init en main, commits con GIT_AUTHOR_NAME/GIT_COMMITTER_NAME={git_name} y "
    "GIT_AUTHOR_EMAIL/GIT_COMMITTER_EMAIL={git_email}, sin mencionar a Claude en commits ni README.\n"
    "- Subilo con `gh repo create {owner}/<nombre> --public --source . --push`.\n"
    "- Si el proyecto necesita un servicio externo (hosting, base de datos, deploy: Vercel, Supabase, Cloud Run, "
    "etc.), podes crear la cuenta vos, solo en su plan gratuito y con {git_email} (o el login de GitHub de "
    "{owner}). {forbidden}Nunca cargues una tarjeta ni actives un plan pago o "
    "un trial que pida tarjeta; si el servicio la pide, no lo uses y resolvelo sin el servicio o devolve ok=false. "
    "Si piden CAPTCHA o un codigo que no podes leer, frenate igual. Los tokens van a ~/.workworm/accounts/ "
    "(nunca dentro del repo ni en commits ni en el README) y anota ahi que cuenta creaste y donde.\n"
    "- No toques nada fuera de {dir} y ~/.workworm/accounts/. No leas ni uses archivos personales.\n"
    "Devolve ok, repo_url, name y summary."
)


def settings(cfg: dict) -> dict:
    s = {"enabled": False, "max_per_day": 2, "model": "sonnet", "effort": "medium", "timeout_minutes": 25}
    s.update(cfg.get("projects") or {})
    return s


def _state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"built": []}


def _save(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def clean(tech: str) -> str:
    tech = (tech or "").strip()
    return tech if SAFE_TECH.match(tech) else ""


def existing(tech: str) -> dict | None:
    """A project already built for that technology (so it is never built twice)."""
    key = tech.lower()
    for p in _state()["built"]:
        if p["tech"].lower() == key and p.get("repo_url"):
            return p
    return None


def built_today() -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    return sum(1 for p in _state()["built"] if p["at"][:10] == today)


def build(tech: str, cfg: dict) -> dict | None:
    """Build and publish a prototype for `tech`. Returns the project record or None."""
    s = settings(cfg)
    tech = clean(tech)
    if not s["enabled"] or not tech:
        return None
    if (found := existing(tech)):
        return found
    if built_today() >= s["max_per_day"]:
        return None
    exe = shutil.which("claude")
    if not exe:
        return None
    WORKDIR.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "-p", "--model", s["model"], "--effort", s["effort"], "--output-format", "json",
           "--no-session-persistence", "--strict-mcp-config", "--json-schema", json.dumps(SCHEMA),
           "--permission-mode", "acceptEdits", "--allowedTools", "Bash Read Write Edit Glob Grep",
           "--add-dir", str(WORKDIR)]
    try:
        done = subprocess.run(cmd, input=PROMPT.format(tech=tech, dir=WORKDIR, **_identity()), capture_output=True, text=True,
                              encoding="utf-8", cwd=str(WORKDIR), timeout=s["timeout_minutes"] * 60,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        result = json.loads(done.stdout)
        out = result.get("structured_output") or {}
    except (subprocess.TimeoutExpired, ValueError, OSError):
        out = {}
    record = {"tech": tech, "at": datetime.now(timezone.utc).isoformat(), "ok": bool(out.get("ok")),
              "repo_url": out.get("repo_url", ""), "name": out.get("name", ""),
              "summary": out.get("summary", ""), "reason": out.get("reason", "")}
    state = _state()
    state["built"].append(record)
    _save(state)
    if not record["ok"] or not record["repo_url"].startswith(f"https://github.com/{_owner()}/"):
        return None
    _add_to_profile(record)
    return record


def _add_to_profile(p: dict) -> None:
    path = ROOT / "profile.md"
    text = path.read_text(encoding="utf-8")
    line = f"  - {p['name']} ({datetime.now().strftime('%b %Y').lower()}): {p['summary']} {p['repo_url']}\n"
    marker = "  - ScreenOCR (UTN)"
    text = text.replace(marker, line + marker, 1) if marker in text else text + "\n" + line
    path.write_text(text, encoding="utf-8")
