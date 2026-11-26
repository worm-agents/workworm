"""Métricas de WorkWorm por día y por cambio (pedido de Jero, 2026-11-20: «quiero ver qué tanto ganamos o perdemos a
medida que hacemos cambios»).

- Cada intento de postulación termina en un resultado (enviada, captcha, video, cuenta, falta dato, navegador...).
  Se sacan de la tabla events, que ya registraba todo desde el 23/10.
- Las respuestas se cuentan por cohorte: las postulaciones enviadas en una semana y qué les pasó después (acuse,
  respuesta, rechazo, entrevista), porque las respuestas llegan días después del envío.
- Los cambios están en data/changes.json; cada arreglo nuevo suma una línea (python metrics.py --change "título" "detalle").

Uso: python metrics.py            resumen en texto (para Pendant o una sesión de Claude)
     python metrics.py --change T D  anota un cambio con fecha de hoy
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

import radar

CHANGES = radar.ROOT / "data" / "changes.json"

# Orden en que se muestran; los que no son intentos de postular van al final.
OUTCOMES = ["enviada", "enviada en tanda", "captcha", "video", "cuenta o login", "falta dato", "navegador",
            "otro error", "descartada al leer", "te postulaste vos"]
ATTEMPTS = {"enviada", "enviada en tanda", "captcha", "video", "cuenta o login", "falta dato", "navegador", "otro error"}
SENT = {"enviada", "enviada en tanda"}

CAPTCHA = re.compile(r"captcha|cloudflare|turnstile|verificaci[oó]n (adicional|humana)|additional verification|"
                     r"no soy un robot|not a robot|soy humano|human", re.I)
VIDEO = re.compile(r"\bvideo|loom|spark ?hire|hirevue", re.I)
ACCOUNT = re.compile(r"cuenta|account|registr|sign ?up|iniciar sesi|login|log in|contraseña|password", re.I)


def local_day(at: str) -> date | None:
    t = radar.parse_time(at)
    return t.astimezone(radar.LOCAL_TZ).date() if t and hasattr(radar, "LOCAL_TZ") else (t.date() if t else None)


def classify(kind: str, detail: str) -> str | None:
    """Resultado de un intento de postular a partir de un evento, o None si el evento no es un intento."""
    detail = detail or ""
    if kind == "applied":
        return "te postulaste vos" if "Te postulaste vos" in detail else "enviada"
    if kind == "tanda_sent":
        return "enviada en tanda"
    if kind == "login_required":
        return "cuenta o login"
    if kind != "status":
        return None
    if detail.startswith("Movida a «Requiere tu atención»: CUENTA"):
        return "cuenta o login"
    if detail.startswith("Movida a «Requiere tu atención»: Formulario sin enviar"):
        return "falta dato"
    if detail.startswith("Movida a «Requiere tu atención»: No se pudo postular"):
        if VIDEO.search(detail):
            return "video"
        if CAPTCHA.search(detail):
            return "captcha"
        if ACCOUNT.search(detail):
            return "cuenta o login"
        return "otro error"
    if detail.startswith("Movida a «Nueva»: Reintento: el navegador"):
        return "navegador"
    if detail.startswith("Movida a «Descartada por el agente»"):
        return "descartada al leer"
    return None


def daily(days: int = 60) -> dict[date, Counter]:
    """{día: Counter(resultado)} de los últimos `days` días (hora de Argentina)."""
    since = (datetime.now() - timedelta(days=days)).date()
    out: dict[date, Counter] = defaultdict(Counter)
    with radar.connect() as db:
        for r in db.execute("SELECT at, kind, detail FROM events WHERE kind IN "
                            "('applied','tanda_sent','login_required','status')"):
            outcome = classify(r["kind"], r["detail"])
            day = local_day(r["at"])
            if outcome and day and day >= since:
                out[day][outcome] += 1
    return dict(sorted(out.items()))


def summarize(counter: Counter) -> dict:
    attempts = sum(v for k, v in counter.items() if k in ATTEMPTS)
    sent = sum(v for k, v in counter.items() if k in SENT)
    return {"intentos": attempts, "enviadas": sent, "tasa": (sent / attempts) if attempts else None,
            "captcha": counter.get("captcha", 0), "video": counter.get("video", 0),
            "cuenta": counter.get("cuenta o login", 0), "falta_dato": counter.get("falta dato", 0),
            "navegador": counter.get("navegador", 0)}


RESPONSE_KINDS = ("reply", "question", "availability_request", "interview_proposed", "interview_confirmed", "rejection",
                  "other")


def cohorts() -> list[dict]:
    """Por semana de envío: cuántas se mandaron y qué les pasó después."""
    with radar.connect() as db:
        apps = [dict(r) for r in db.execute(
            """SELECT a.job_id, a.sent_at, a.interview_at, a.channel, j.status FROM applications a JOIN jobs j ON j.id=a.job_id
               WHERE a.status='sent' AND a.sent_at != ''""")]
        inbound = defaultdict(set)
        for r in db.execute("SELECT job_id, kind FROM messages WHERE direction='in' AND job_id IS NOT NULL"):
            inbound[r["job_id"]].add(r["kind"])
    weeks: dict[date, Counter] = defaultdict(Counter)
    for a in apps:
        day = local_day(a["sent_at"])
        if not day:
            continue
        week = day - timedelta(days=day.weekday())
        kinds = inbound.get(a["job_id"], set())
        c = weeks[week]
        c["enviadas"] += 1
        c["a mano"] += a["channel"] == "manual"
        c["con acuse"] += "auto_ack" in kinds
        c["con respuesta"] += bool(kinds & set(RESPONSE_KINDS)) and "rejection" not in kinds
        c["rechazos"] += "rejection" in kinds or a["status"] == "rejected"
        c["entrevistas"] += bool(a["interview_at"]) or a["status"] in ("interview", "offer") or bool(
            kinds & {"interview_proposed", "interview_confirmed"})
    return [{"semana": w, **c} for w, c in sorted(weeks.items())]


def changes() -> list[dict]:
    try:
        items = json.loads(CHANGES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        items = []
    return sorted(items, key=lambda c: c["date"])


def add_change(title: str, detail: str = "", when: str = "") -> None:
    items = changes()
    items.append({"date": when or date.today().isoformat(), "title": title, "detail": detail})
    CHANGES.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


def before_after(window: int = 7) -> list[dict]:
    """Para cada cambio: los `window` días antes contra los `window` días desde ese día (o hasta hoy)."""
    by_day = daily(120)
    today = date.today()
    rows = []
    for ch in changes():
        d = date.fromisoformat(ch["date"])
        before, after = Counter(), Counter()
        for day, c in by_day.items():
            if d - timedelta(days=window) <= day < d:
                before.update(c)
            elif d <= day < d + timedelta(days=window):
                after.update(c)
        b, a = summarize(before), summarize(after)
        rows.append({"fecha": ch["date"], "cambio": ch["title"],
                     "días después": min(window, (today - d).days + 1),
                     "intentos antes": b["intentos"], "intentos después": a["intentos"],
                     "enviadas antes": b["enviadas"], "enviadas después": a["enviadas"],
                     "tasa antes": b["tasa"], "tasa después": a["tasa"],
                     "captcha antes": b["captcha"], "captcha después": a["captcha"]})
    return rows


def pct(x) -> str:
    return "—" if x is None else f"{x:.0%}"


def text_report(days: int = 14) -> str:
    lines = [f"WorkWorm, últimos {days} días (intentos → enviadas, tasa, motivos)"]
    for day, c in daily(days).items():
        s = summarize(c)
        if not s["intentos"] and not c:
            continue
        reasons = ", ".join(f"{k} {v}" for k, v in c.most_common() if k not in SENT)
        lines.append(f"{day:%a %d/%m}: {s['intentos']} → {s['enviadas']} ({pct(s['tasa'])}). {reasons}")
    lines.append("")
    lines.append("Por semana de envío (enviadas / acuse / respuesta / rechazo / entrevista):")
    for c in cohorts():
        lines.append(f"{c['semana']:%d/%m}: {c.get('enviadas', 0)} / {c.get('con acuse', 0)} / {c.get('con respuesta', 0)}"
                     f" / {c.get('rechazos', 0)} / {c.get('entrevistas', 0)}")
    lines.append("")
    lines.append("Cambios (7 días antes → después: enviadas, tasa):")
    for r in before_after():
        lines.append(f"{r['fecha']} {r['cambio']}: {r['enviadas antes']} → {r['enviadas después']} "
                     f"({pct(r['tasa antes'])} → {pct(r['tasa después'])}), {r['días después']} días medidos")
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--change":
        add_change(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
        print("anotado")
    else:
        print(text_report(int(sys.argv[1]) if len(sys.argv) > 1 else 14))
