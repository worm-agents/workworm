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
AHEAD = 3               # formularios listos (o esperando la verificación) por delante de Jero (Jero 20/11: «me abrió solo 3»)
IDLE_MINUTES = 45       # sin ninguna acción de Jero (panel o captchas resueltos), la tanda se pausa sola
FRESH_MINUTES = 15      # un formulario listo hace más que esto se revisa en su pestaña (las sesiones de los sitios vencen)
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
    tabs = json.loads(next((r["tabs"] for r in rows() if r["job_id"] == job_id), "[]") or "[]")
    for tab in tabs:
        try:
            agent._cdp(f"/json/activate/{tab}")
            break
        except (OSError, ValueError):
            continue
    scroll_to_captcha(tabs)


def tab_info(tabs_json: str) -> str:
    """«sitio · título» de la primera pestaña abierta del formulario, para que Jero sepa cuál es."""
    ids = json.loads(tabs_json or "[]")
    for t in agent.vm_tabs():
        if t["id"] in ids:
            host = radar.urlparse(t["url"]).netloc.replace("www.", "")
            return f"{host} · {t['title'][:60]}"
    return ""


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


FILL_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": ["sent", "ready", "verification", "failed"]},
        "detail": {"type": "string"},
    },
    "required": ["outcome", "detail"],
}

SCROLL_JS = """() => {
  const sel = ['iframe[src*="hcaptcha"]', 'iframe[src*="recaptcha"]', 'iframe[src*="turnstile"]', 'iframe[title*="captcha" i]',
               '.h-captcha', '.g-recaptcha', '.cf-turnstile', 'button[type=submit]', 'input[type=submit]'];
  for (const s of sel) { const e = document.querySelector(s); if (e) { e.scrollIntoView({block: 'center'}); return s; } }
  return '';
}"""


def scroll_to_captcha(tab_ids: list[str]) -> str:
    """Deja la pestaña con el captcha (o el botón de enviar) en el medio de la pantalla. Jero 20/11: «no me dejó
    scrolleado en el captcha»."""
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{agent.ASSIST_PORT}", timeout=15000)
            for ctx in browser.contexts:
                for page in ctx.pages:
                    session = ctx.new_cdp_session(page)
                    target = session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
                    session.detach()
                    if target in tab_ids:
                        return page.evaluate(SCROLL_JS) or ""
    except Exception as exc:  # nunca frena la tanda
        return f"error: {exc}"
    return ""


def _tab_url(tab_ids: list[str]) -> tuple[str, str]:
    for t in agent.vm_tabs():
        if t["id"] in tab_ids:
            return t["url"], t["title"]
    return "", ""


def _fill(job: dict, cfg: dict) -> None:
    """Llena (o sigue llenando) el formulario. A diferencia de assist_form:
    - si en el último paso no hay captcha ni verificación, ENVÍA el agente (Jero 20/11: «por qué me lo pidió, si pudo
      haberlo hecho él», Indeed Smart Apply);
    - si aparece un captcha a mitad del formulario (JPMorgan, email → Next), frena con outcome=verification y, cuando Jero lo
      resuelve y toca «Listo», sigue EN ESA MISMA PESTAÑA;
    - usa autopilot.tanda_model (sonnet por defecto): con haiku quedaban campos y casillas vacías (JPMorgan, AirTM, Humana)."""
    job_id = job["job_id"]
    _set(job_id, state="filling", detail="El agente está llenando el formulario")
    info = radar.job(job_id)
    with agent.connect() as db:
        app_row = db.execute("SELECT body, cv_path FROM applications WHERE job_id=?", (job_id,)).fetchone()
    letter = (app_row["body"] if app_row else "").split("\n\nRespuestas del formulario:")[0]
    language = agent.application_language(info, cfg, "")
    cv = agent.cv_for(cfg, language)
    cv_path = app_row["cv_path"] if app_row and app_row["cv_path"] and agent.Path(app_row["cv_path"]).exists() \
        else str(agent.upload_copy(cv))
    old_tabs = json.loads(job.get("tabs") or "[]")
    tab_url, tab_title = _tab_url(old_tabs) if job["state"] == "checked" else ("", "")
    where = (f"YA HAY UNA PESTAÑA ABIERTA con esta postulación (URL: {tab_url} · título: {tab_title}). Seleccionala con "
             "browser_tabs y SEGUÍ DESDE DONDE ESTÁ: el candidato acaba de resolver ahí una verificación o un captcha. "
             "No abras otra pestaña ni vuelvas a empezar.\n" if tab_url else
             f"Abrí {job.get('url') or info['url']} en una PESTAÑA NUEVA (no toques ni cierres las otras) y pulsá "
             "«Apply/Postularme»; si te lleva a la web de la empresa, seguí ahí.\n")
    prompt = (
        f"Postulá a esta oferta en nombre de {cfg['candidate_name']} usando el navegador.\n"
        f"Oferta: {info['title']} — {info['company']}\nCV para subir (ruta absoluta): {cv_path}\n" + where +
        (f"Es un formulario que mandó el reclutador por mail: {job['instruction']}\n" if job.get("instruction") else "") +
        f"Carta de presentación:\n{letter}\n\nTexto del CV:\n{agent.cv_texts(cfg).get(cv['id'], '')}\n\n"
        "Completá TODOS los campos y pasos: obligatorios y opcionales, listas, radios, casillas de consentimiento de la "
        "postulación (privacidad, términos, avisos por SMS o mail sobre esta postulación: marcalas, el candidato las acepta), "
        "ubicación actual, pretensión, preguntas de selección. En los campos con autocompletar (ubicación, ciudad, "
        "universidad) elegí una opción de la lista, no dejes solo el texto escrito. Antes de terminar, sacá un "
        "browser_snapshot y recorré el formulario de arriba abajo: si queda algún campo vacío o un mensaje de error en "
        "rojo, corregilo.\n\n"
        "Al llegar al último paso, con todo cargado:\n"
        "- Si NO hay captcha ni verificación humana a la vista, ENVIÁ vos (pulsá el botón final UNA vez), confirmá que "
        "aparezca el mensaje de envío exitoso y devolvé outcome=sent.\n"
        "- Si hay un captcha o verificación (hCaptcha, reCAPTCHA, Cloudflare, «no soy un robot»), NO lo resuelvas ni envíes: "
        "scrolleá hasta que el captcha y el botón de enviar queden en el medio de la pantalla y devolvé outcome=ready.\n"
        "- Si un captcha o verificación aparece ANTES de terminar (al abrir, en el primer paso, al tocar Next), frená ahí "
        "sin resolverlo, dejá esa pestaña en ese punto y devolvé outcome=verification.\n"
        "- Si no se puede (el aviso cerró, pide crear una cuenta con contraseña, falta un dato que el perfil no tiene), "
        "devolvé outcome=failed y decí por qué.\n"
        "No cierres el navegador ni la pestaña del formulario. En detail: qué quedó cargado y qué falta, corto."
        + agent.FORM_FACTS + agent.personal_form_rules())
    before = set(agent.open_tab_ids())
    config = radar.ROOT / "data" / "mcp_assist.json"
    config.write_text(json.dumps({"mcpServers": {"playwright": {"command": "npx", "args": [
        "-y", agent.PLAYWRIGHT_MCP, "--cdp-endpoint", f"http://127.0.0.1:{agent.ASSIST_PORT}",
        "--allow-unrestricted-file-access", "--output-dir", str(radar.ROOT / "data" / "pw-output")]}}}), encoding="utf-8")
    try:
        result = agent.llm.ask(agent.system_prompt(cfg), prompt, FILL_SCHEMA,
                               model=cfg["autopilot"].get("tanda_model", "sonnet"),
                               timeout=int(cfg["autopilot"].get("form_timeout_minutes", 15)) * 60,
                               extra=["--mcp-config", str(config), "--strict-mcp-config", "--allowedTools", "mcp__playwright",
                                      "--disallowedTools", "mcp__playwright__browser_close"])
    except Exception as exc:  # el worker no se cae por un formulario
        result = {"outcome": "failed", "detail": f"{type(exc).__name__}: {exc}"}
    tabs = [t for t in agent.open_tab_ids() if t not in before] or [t for t in old_tabs if t in agent.open_tab_ids()]
    outcome, detail = result["outcome"], result.get("detail", "")[:1500]
    if outcome == "sent":
        with agent.connect() as db:
            db.execute("UPDATE applications SET status='sent', channel='form', sent_at=?, last_outbound_at=?, updated_at=? "
                       "WHERE job_id=?", (now(), now(), now(), job_id))
            db.execute("UPDATE jobs SET status='applied', updated_at=? WHERE id=?", (now(), job_id))
            agent.log_event(db, job_id, "applied", "Formulario enviado por el agente en la tanda (no había captcha)")
        agent.clear_hold(job_id)
        _set(job_id, state="sent", tabs=json.dumps(tabs), detail="Lo envió el agente (no había captcha). " + detail)
        _close_tabs(job_id)
    elif outcome == "ready":
        agent.set_hold(job_id, int(cfg["autopilot"].get("hold_minutes", 180)), tabs=tabs)
        scroll_to_captcha(tabs)
        _set(job_id, state="ready", tabs=json.dumps(tabs), detail=detail)
        with agent.connect() as db:
            agent.log_event(db, job_id, "tanda_ready", detail[:500])
    elif outcome == "verification":
        agent.set_hold(job_id, int(cfg["autopilot"].get("hold_minutes", 180)), tabs=tabs)
        scroll_to_captcha(tabs)
        _set(job_id, mode="start", state="opening", tabs=json.dumps(tabs),
             detail="Apareció una verificación en el medio: resolvela en esa pestaña y tocá «Listo»; el agente sigue ahí. "
                    + detail)
    else:
        _set(job_id, state="failed", tabs=json.dumps(tabs), detail=detail)
        with agent.connect() as db:
            agent.log_event(db, job_id, "tanda_failed", "El agente no lo pudo dejar listo: " + detail[:400])


def idle() -> bool:
    try:
        last = (radar.ROOT / "data" / "tanda.touch").stat().st_mtime
    except OSError:
        return True
    return time.time() - last > IDLE_MINUTES * 60


PAGE_JS = r"""() => {
  const filled = s => Array.from(document.querySelectorAll(s)).some(e => (e.value || '').length > 20);
  const token = filled('textarea[name="h-captcha-response"]') || filled('textarea[name="g-recaptcha-response"]')
             || filled('input[name="cf-turnstile-response"]');
  const text = (document.title || '') + ' ' + (document.body ? document.body.innerText.slice(0, 6000) : '');
  const cloudflare = /just a moment|un momento|verifying you are human|verificando que|additional verification|security verification/i.test(text);
  const success = /thank you for (applying|your application|your interest)|application (has been |was )?(submitted|received)|we('ve| have) received your application|gracias por (postularte|tu postulaci|aplicar|tu mensaje)|postulaci[oó]n (fue |ha sido )?(enviada|recibida)|mensaje ha sido enviado|successfully (submitted|applied)/i.test(text);
  const captcha = !!document.querySelector('iframe[src*="hcaptcha"], iframe[src*="recaptcha"], iframe[src*="turnstile"], iframe[src*="challenges.cloudflare"], .h-captcha, .g-recaptcha, .cf-turnstile');
  return {token, cloudflare, captcha, success};
}"""


def _watch_once(seen_challenge: dict) -> None:
    """Mira las pestañas que esperan a Jero. Así no tiene que volver al tablero (Jero 20/11: hacía los captchas en las
    pestañas y la tanda seguía esperando su «Listo»):
    - opening: si ya hay token de captcha, o había Cloudflare y ya no está → el agente sigue (checked);
    - ready: si la página dice que la postulación salió → enviada."""
    from playwright.sync_api import sync_playwright
    waiting = {}
    for r in rows():
        if r["state"] in ("opening", "ready"):
            for t in json.loads(r["tabs"] or "[]"):
                waiting[t] = r
    if not waiting:
        return
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{agent.ASSIST_PORT}", timeout=15000)
        for ctx in browser.contexts:
            for page in ctx.pages:
                try:
                    session = ctx.new_cdp_session(page)
                    target = session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
                    session.detach()
                    r = waiting.get(target)
                    if not r:
                        continue
                    state = page.evaluate(PAGE_JS)
                except Exception:
                    continue
                job_id = r["job_id"]
                if state["cloudflare"]:
                    seen_challenge[job_id] = True
                if r["state"] == "ready" and state["success"]:
                    touch()
                    sent(job_id)
                elif (r["state"] == "opening" and not state["cloudflare"] and (state["token"] or not state["captcha"])
                      and (datetime.now(timezone.utc) - radar.parse_time(r["updated_at"])).total_seconds() > 15):
                    touch()
                    _set(job_id, state="checked", detail="Pasaste la verificación: el agente sigue en esa pestaña")


def _watcher() -> None:
    """Hilo aparte: latido de la tanda (el llenado puede tardar varios minutos) y detección de lo que hace Jero."""
    seen: dict = {}
    while not STOP.exists() and not idle():
        HEARTBEAT.write_text(now(), encoding="utf-8")
        try:
            _watch_once(seen)
        except Exception:
            pass
        time.sleep(8)


def worker() -> None:
    import threading
    agent.ensure_browser()
    threading.Thread(target=_watcher, daemon=True).start()
    while not STOP.exists() and not idle():
        try:
            _step()
        except Exception as exc:  # p. ej. «database is locked» mientras corre el ciclo: reintenta en 10 s
            agent.debug_log(None, f"tanda: {type(exc).__name__}: {exc}")
            time.sleep(10)
    HEARTBEAT.unlink(missing_ok=True)


def _step() -> None:
    cfg = radar.config()  # cada vuelta: las reglas nuevas de config.json valen sin reiniciar
    if True:
        HEARTBEAT.write_text(now(), encoding="utf-8")
        items = rows()
        waiting = [r for r in items if r["state"] in ("ready", "opening")]
        # Lo que Jero ya verificó va primero: él está esperando ese.
        todo = next((r for r in items if r["state"] == "checked"), None)
        if not todo and len(waiting) < AHEAD:
            todo = next((r for r in items if r["state"] == "queued"), None)
        if not todo:
            # Mientras espera a Jero, mantiene frescos los listos: los formularios vencen (Humana «No Activity», Paycom
            # vuelve a «Start Application», Lever rechaza «error verifying your application»). Jero 20/11.
            stale = [r for r in items if r["state"] == "ready" and r["tabs"] not in ("", "[]")
                     and radar.parse_time(r["updated_at"])
                     and (datetime.now(timezone.utc) - radar.parse_time(r["updated_at"])).total_seconds() > FRESH_MINUTES * 60]
            if stale:
                _fill({**stale[0], "state": "checked",
                       "instruction": (stale[0].get("instruction") or "") + " Este formulario ya estaba cargado hace un rato: "
                       "revisá que siga todo (CV adjunto, cada campo); si la sesión venció o la página se vació, tocá "
                       "Continue o recargá y volvé a llenarlo."}, cfg)
                return
            time.sleep(5)
            return
        if todo["state"] == "queued" and todo["mode"] == "start":
            tabs = _open_for_check(todo)
            _set(todo["job_id"], state="opening", tabs=json.dumps(tabs),
                 detail="Pasá la verificación en esa pestaña y tocá «Listo» en el tablero")
            return
        _fill(todo, cfg)


if __name__ == "__main__":
    if "--worker" in sys.argv:
        worker()
    else:
        print(f"agregadas {build()}")
        for r in rows():
            print(r["state"], r["mode"], r["job_id"], r["company"], "·", r["title"])
