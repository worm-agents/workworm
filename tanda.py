"""Tanda de captchas (pedido de Jero, 2026-11-20).

Los formularios que el agente no puede mandar por un captcha o una verificación humana se juntan en una cola. Jero aprieta
«Empezar tanda» en el tablero cuando está en la VM y el worker (servicio workworm-tanda) los va llenando de a uno, con
uno de ventaja: mientras Jero resuelve el captcha de uno, el siguiente se está llenando. Jero nunca escribe un campo.

Dos momentos de captcha, dos modos:
- submit: el captcha está al final (Lever, Greenhouse, HiringRoom, Nubity...). El agente llena todo y Jero resuelve y envía.
- start: la verificación aparece al abrir la oferta o al tocar «Apply» (Cloudflare en Himalayas e Indeed, Paycom, Oracle).
  El worker abre la pestaña, Jero pasa la verificación y toca «Listo»; recién ahí el agente llena (la cookie de la
  verificación queda en el perfil de Chrome) y Jero envía.

Los videos quedan afuera: hay que grabarlos, no es un clic.
Mientras la tanda está activa, el ciclo no postula por formularios, no ordena la pantalla y no reinicia Chrome.
Eventos para las métricas: tanda_start, tanda_ready, tanda_sent, tanda_failed.

Uso del worker: python tanda.py --worker  (lo arranca systemd: workworm-tanda.service)
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

import agent
import radar

HEARTBEAT = radar.ROOT / "data" / "tanda.on"
STOP = radar.ROOT / "data" / "tanda.stop"
AHEAD = 2               # formularios listos (o esperando la verificación) por delante de Jero
IDLE_MINUTES = 30       # sin ninguna acción de Jero, la tanda se pausa sola
VIDEO = re.compile(r"\bvideo|loom|spark ?hire|hirevue|grab(á|a)r", re.I)
# Solo las señales claras de que la verificación está ANTES del formulario. Lo demás arranca en modo submit y, si el
# agente choca con una verificación antes de llegar al formulario, pasa solo a modo start (ver _fill).
START_MODE = re.compile(r"additional verification|performing security verification|just a moment", re.I)
BLOCKED_BEFORE_FORM = re.compile(r"cloudflare|additional verification|security verification|just a moment|"
                                 r"no lleg(u|ué) (al|a) (formulario|bot[oó]n)|verificaci[oó]n de seguridad", re.I)
OPEN_STATES = ("queued", "opening", "checked", "filling", "ready")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _table(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS tanda (
        job_id INTEGER PRIMARY KEY, position INTEGER, mode TEXT, state TEXT, url TEXT, instruction TEXT,
        tabs TEXT DEFAULT '[]', detail TEXT DEFAULT '', updated_at TEXT)""")


def rows() -> list[dict]:
    with agent.connect() as db:
        _table(db)
        return [dict(r) for r in db.execute(
            """SELECT t.*, j.title, j.company, j.score, j.url AS job_url FROM tanda t JOIN jobs j ON j.id=t.job_id
               ORDER BY CASE t.state WHEN 'ready' THEN 0 WHEN 'opening' THEN 1 WHEN 'checked' THEN 2 WHEN 'filling' THEN 3
               WHEN 'queued' THEN 4 ELSE 5 END, t.position""")]


def _set(job_id: int, **fields) -> None:
    fields["updated_at"] = now()
    with agent.connect() as db:
        _table(db)
        db.execute(f"UPDATE tanda SET {', '.join(f'{k}=?' for k in fields)} WHERE job_id=?", (*fields.values(), job_id))


def active() -> bool:
    """El worker está corriendo (latido de menos de 3 minutos)."""
    try:
        return time.time() - HEARTBEAT.stat().st_mtime < 180
    except OSError:
        return False


def candidates() -> list[dict]:
    """Ofertas en «Tu atención» que solo necesitan un captcha o una verificación (no videos, no cuentas, no datos),
    más los formularios que mandó un reclutador por mail (Rankmi de Petersen, por ejemplo). Sin descartar por puntaje."""
    with agent.connect() as db:
        attention = [dict(r) for r in db.execute(
            """SELECT j.*, a.status AS app_status, a.channel, a.note FROM jobs j JOIN applications a ON a.job_id=j.id
               WHERE j.status='action_needed' ORDER BY j.score DESC""")]
    out = []
    for j in attention:
        note = j.get("note") or ""
        if agent.attention_kind(j) == "finish" and not VIDEO.search(note):
            out.append({"job_id": j["id"], "mode": "start" if START_MODE.search(note) else "submit",
                        "url": "", "instruction": ""})
    for q in agent.recruiter_forms():
        if q["id"] not in {c["job_id"] for c in out}:
            out.append({"job_id": q["id"], "mode": "submit", "url": q["form_url"], "instruction": q["form_note"]})
    return out


def build() -> int:
    """Suma a la cola lo que falte. Lo ya enviado o descartado no vuelve; lo que falló sí, al final."""
    added = 0
    with agent.connect() as db:
        _table(db)
        have = {r["job_id"]: r["state"] for r in db.execute("SELECT job_id, state FROM tanda")}
        last = db.execute("SELECT COALESCE(MAX(position), 0) FROM tanda").fetchone()[0]
        for c in candidates():
            state = have.get(c["job_id"])
            if state in OPEN_STATES or state == "sent":
                continue
            last += 1
            db.execute("""INSERT INTO tanda(job_id, position, mode, state, url, instruction, tabs, detail, updated_at)
                          VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET position=excluded.position,
                          state='queued', mode=excluded.mode, url=excluded.url, instruction=excluded.instruction,
                          tabs='[]', detail='', updated_at=excluded.updated_at""",
                       (c["job_id"], last, c["mode"], "queued", c["url"], c["instruction"], "[]", "", now()))
            added += 1
    return added


# ---------- acciones del tablero ----------

def touch() -> None:
    """Jero hizo algo: la tanda sigue viva otros IDLE_MINUTES."""
    (radar.ROOT / "data" / "tanda.touch").write_text(now(), encoding="utf-8")


def start() -> int:
    if not active():  # una tanda cortada a mitad de un llenado lo deja en «filling»: vuelve a la cola
        with agent.connect() as db:
            _table(db)
            db.execute("UPDATE tanda SET state='queued', detail='' WHERE state IN ('filling', 'checked')")
    added = build()
    STOP.unlink(missing_ok=True)
    touch()
    with agent.connect() as db:
        agent.log_event(db, None, "tanda_start", f"{len([r for r in rows() if r['state'] in OPEN_STATES])} en la cola")
    subprocess.run(["systemctl", "--user", "start", "workworm-tanda.service"], capture_output=True)
    return added


def stop() -> None:
    STOP.write_text(now(), encoding="utf-8")
    subprocess.run(["systemctl", "--user", "stop", "workworm-tanda.service"], capture_output=True)
    HEARTBEAT.unlink(missing_ok=True)


def show(job_id: int) -> None:
    """Trae al frente la pestaña de ese formulario (lo pidió Jero desde el tablero)."""
    touch()
    for tab in json.loads(next((r["tabs"] for r in rows() if r["job_id"] == job_id), "[]") or "[]"):
        try:
            agent._cdp(f"/json/activate/{tab}")
            return
        except (OSError, ValueError):
            continue


def _close_tabs(job_id: int) -> None:
    for tab in json.loads(next((r["tabs"] for r in rows() if r["job_id"] == job_id), "[]") or "[]"):
        try:
            agent._cdp(f"/json/close/{tab}")
        except (OSError, ValueError):
            pass


def checked(job_id: int) -> None:
    """Modo start: Jero pasó la verificación; el worker llena el formulario."""
    touch()
    _set(job_id, state="checked")


def sent(job_id: int) -> None:
    """Jero envió: queda postulada (el acuse por mail lo confirma después) y se pasa al siguiente."""
    touch()
    with agent.connect() as db:
        db.execute("UPDATE applications SET status='sent', channel='form', sent_at=?, last_outbound_at=?, updated_at=? "
                   "WHERE job_id=?", (now(), now(), now(), job_id))
        db.execute("UPDATE jobs SET status='applied', updated_at=? WHERE id=?", (now(), job_id))
        agent.log_event(db, job_id, "tanda_sent", "Enviada en la tanda: el agente llenó el formulario y Jero resolvió el captcha")
    agent.clear_hold(job_id)
    _close_tabs(job_id)
    _set(job_id, state="sent")


def failed(job_id: int, why: str = "") -> None:
    """No salió (el sitio dio error, el formulario quedó mal...). Sigue en «Tu atención»; vuelve a la cola en otra tanda."""
    touch()
    with agent.connect() as db:
        agent.log_event(db, job_id, "tanda_failed", why or "Jero marcó que no salió")
    agent.clear_hold(job_id)
    _close_tabs(job_id)
    _set(job_id, state="failed", detail=why or "No salió")


def skip(job_id: int) -> None:
    """Saltear por ahora (queda al final de la cola de la próxima tanda)."""
    touch()
    agent.clear_hold(job_id)
    _close_tabs(job_id)
    _set(job_id, state="skipped")


# ---------- worker ----------

def _open_for_check(job: dict) -> list[str]:
    """Modo start: abre la oferta (o el link) en una pestaña en segundo plano para que Jero pase la verificación."""
    before = set(agent.open_tab_ids())
    agent.cdp_open(job["url"] or radar.job(job["job_id"])["url"])
    time.sleep(2)
    return [t for t in agent.open_tab_ids() if t not in before]


def _fill(job: dict, cfg: dict) -> None:
    _set(job["job_id"], state="filling", detail="El agente está llenando el formulario")
    try:
        result = agent.assist_form(job["job_id"], cfg, url=job.get("url") or "", instruction=job.get("instruction") or "")
    except Exception as exc:  # el worker no se cae por un formulario
        result = {"ready": False, "detail": f"{type(exc).__name__}: {exc}"}
    if job.get("tabs") and job["state"] == "checked":
        _close_tabs(job["job_id"])  # la pestaña de la verificación ya no hace falta
    if result.get("ready"):
        holds = [h for h in agent.active_holds() if h["job_id"] == job["job_id"]]
        tabs = holds[0]["tabs"] if holds else []
        _set(job["job_id"], state="ready", tabs=json.dumps(tabs), detail=result["detail"][:1500])
        with agent.connect() as db:
            agent.log_event(db, job["job_id"], "tanda_ready", result["detail"][:500])
    elif BLOCKED_BEFORE_FORM.search(result.get("detail", "")) and job["mode"] != "start":
        # Había una verificación antes del formulario: que la pase Jero y después se llena.
        _set(job["job_id"], mode="start", state="queued", detail="Tiene verificación al abrir: pasa a modo «Listo»")
    else:
        _set(job["job_id"], state="failed", detail=result.get("detail", "")[:1500])
        with agent.connect() as db:
            agent.log_event(db, job["job_id"], "tanda_failed", "El agente no lo pudo dejar listo: " + result.get("detail", "")[:400])


def idle() -> bool:
    try:
        last = (radar.ROOT / "data" / "tanda.touch").stat().st_mtime
    except OSError:
        return True
    return time.time() - last > IDLE_MINUTES * 60


def worker() -> None:
    cfg = radar.config()
    agent.ensure_browser()
    while not STOP.exists() and not idle():
        HEARTBEAT.write_text(now(), encoding="utf-8")
        items = rows()
        waiting = [r for r in items if r["state"] in ("ready", "opening")]
        # Lo que Jero ya verificó va primero: él está esperando ese.
        todo = next((r for r in items if r["state"] == "checked"), None)
        if not todo and len(waiting) < AHEAD:
            todo = next((r for r in items if r["state"] == "queued"), None)
        if not todo:
            time.sleep(5)
            continue
        if todo["state"] == "queued" and todo["mode"] == "start":
            tabs = _open_for_check(todo)
            _set(todo["job_id"], state="opening", tabs=json.dumps(tabs),
                 detail="Pasá la verificación en esa pestaña y tocá «Listo» en el tablero")
            continue
        _fill(todo, cfg)
    HEARTBEAT.unlink(missing_ok=True)


if __name__ == "__main__":
    if "--worker" in sys.argv:
        worker()
    else:
        print(f"agregadas {build()}")
        for r in rows():
            print(r["state"], r["mode"], r["job_id"], r["company"], "·", r["title"])
