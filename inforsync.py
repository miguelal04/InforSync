import os
import sys
import time
import logging
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from dateutil.parser import isoparse
from dotenv import load_dotenv
from icalendar import Calendar
from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm


# --- Configurações ---

load_dotenv()
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DATABASE_ID = os.getenv("DATABASE_ID")
ICAL_URL = os.getenv("ICAL_URL")

PROJECT_NAME = "Inforestudante"
TZ_NAME = "Europe/Lisbon"
TZ = ZoneInfo(TZ_NAME)
WRITE_DELAY = 0.35  # Notion aceita ~3 pedidos/s

headers = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}

log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# --- Funções auxiliares ---

def normalize_text(text):
    """Remove acentos e converte para minúsculas."""
    text = unicodedata.normalize("NFD", text)
    text = text.encode("ascii", "ignore").decode("utf-8")
    return text.lower().strip()


def fmt_dt(dt):
    """Converte date/datetime do ICS para string canónica (hora de Lisboa, sem offset)."""
    if isinstance(dt, datetime):
        if dt.tzinfo is not None:
            dt = dt.astimezone(TZ)
        return dt.replace(tzinfo=None, second=0, microsecond=0).isoformat(timespec="seconds")
    return dt.isoformat()  # date (dia inteiro): YYYY-MM-DD


def parse_notion_date(value):
    """Converte a data devolvida pelo Notion para a mesma string canónica."""
    if not value:
        return None
    if len(value) == 10:  # dia inteiro
        return value
    return fmt_dt(isoparse(value))


def determine_types(title):
    """Determina os tipos do evento com base no título."""
    types = []
    t = normalize_text(title)
    if "avaliacao" in t or "defesa de trabalhos" in t or "entrega" in t or "(ee)" in t:
        if "defesa de trabalhos" in t:
            types.append("Avaliação")
        elif "entrega" in t:
            types.append("Avaliação")
        elif "(ee)" in t:
            types.append("Época Especial")
        elif "frequencia" in t:
            types.append("Frequência")
        elif "(er)" in t:
            types.append("Exame Recurso")
        elif "(en)" in t:
            types.append("Exame Normal")
        else:
            types.append("Avaliação")
        if "por inscrever" in t:
            types.append("Por inscrever")
    else:
        types.append("Aula")
    return types


def same_types(a, b):
    return {x.lower() for x in a} == {x.lower() for x in b}


def needs_update(page, ev):
    return (
        page["title"] != ev["title"]
        or page["start"] != ev["start"]
        or page["end"] != ev["end"]
        or not same_types(page["types"], ev["types"])
        or page["uid"] != ev["uid"]
    )


# --- Notion API ---

def notion_request(method, url, **kwargs):
    """Pedido com retry para 429/5xx. Levanta exceção se falhar de vez."""
    last_error = None
    for attempt in range(6):
        try:
            r = requests.request(method, url, headers=headers, timeout=30, **kwargs)
        except requests.RequestException as e:
            last_error = e
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 1)))
            continue
        if r.status_code >= 500:
            last_error = f"HTTP {r.status_code}"
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return r
    raise RuntimeError(f"Notion falhou após várias tentativas: {last_error}")


def date_payload(start, end):
    return {
        "start": start,
        "end": end,
        "time_zone": TZ_NAME if "T" in start else None,
    }


def check_schema():
    """Confirma que a base de dados tem as propriedades que o script usa."""
    data = notion_request("GET", f"https://api.notion.com/v1/databases/{DATABASE_ID}").json()
    props = data["properties"]
    expected = {
        "Nome": "title",
        "Date": "date",
        "Project": "select",
        "Type": "multi_select",
        "UID": "rich_text",
    }
    problems = []
    for name, ptype in expected.items():
        if name not in props:
            problems.append(f"falta a propriedade '{name}' (tipo {ptype})")
        elif props[name]["type"] != ptype:
            problems.append(f"'{name}' devia ser do tipo {ptype}, mas é {props[name]['type']}")
    if problems:
        raise RuntimeError(
            "; ".join(problems) + f". Propriedades existentes: {', '.join(props.keys())}"
        )


def get_existing_events():
    """
    Devolve lista de todas as páginas do projeto. Se a leitura falhar,
    a exceção propaga-se e o script aborta (nunca segue com dados parciais).
    """
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    pages = []
    cursor = None
    while True:
        payload = {
            "page_size": 100,
            "filter": {"property": "Project", "select": {"equals": PROJECT_NAME}},
        }
        if cursor:
            payload["start_cursor"] = cursor
        data = notion_request("POST", url, json=payload).json()

        for page in data["results"]:
            props = page["properties"]
            title = "".join(t["plain_text"] for t in props["Nome"]["title"])
            date_info = props["Date"]["date"] or {}
            uid = "".join(t["plain_text"] for t in props["UID"]["rich_text"]).strip()
            pages.append({
                "page_id": page["id"],
                "title": title,
                "start": parse_notion_date(date_info.get("start")),
                "end": parse_notion_date(date_info.get("end")),
                "types": [t["name"] for t in props["Type"]["multi_select"]],
                "uid": uid,
            })

        logger.info(f"Notion: {len(pages)} páginas lidas...")
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return pages


def build_properties(ev, include_project=False):
    props = {
        "Nome": {"title": [{"text": {"content": ev["title"]}}]},
        "Date": {"date": date_payload(ev["start"], ev["end"])},
        "Type": {"multi_select": [{"name": t} for t in ev["types"]]},
        "UID": {"rich_text": [{"text": {"content": ev["uid"]}}]},
    }
    if include_project:
        props["Project"] = {"select": {"name": PROJECT_NAME}}
    return props


def add_event(ev):
    body = {
        "parent": {"database_id": DATABASE_ID},
        "properties": build_properties(ev, include_project=True),
    }
    try:
        notion_request("POST", "https://api.notion.com/v1/pages", json=body)
        return True
    except Exception as e:
        logger.error(f"Erro ao criar '{ev['title']}': {e}")
        return False


def update_event(page_id, ev):
    body = {"properties": build_properties(ev)}
    try:
        notion_request("PATCH", f"https://api.notion.com/v1/pages/{page_id}", json=body)
        return True
    except Exception as e:
        logger.error(f"Erro ao atualizar '{ev['title']}': {e}")
        return False


def archive_event(page_id, title):
    try:
        notion_request("PATCH", f"https://api.notion.com/v1/pages/{page_id}", json={"archived": True})
        return True
    except Exception as e:
        logger.error(f"Erro ao arquivar '{title}': {e}")
        return False


# --- ICS ---

def load_ics_events():
    r = requests.get(ICAL_URL, timeout=30)
    r.raise_for_status()
    cal = Calendar.from_ical(r.content)

    events = {}
    for comp in cal.walk("VEVENT"):
        title = str(comp.get("summary", "")).strip()
        uid = str(comp.get("uid", "")).strip()
        rec = comp.get("recurrence-id")
        if rec is not None:
            uid = f"{uid}#{fmt_dt(rec.dt)}"
        if not uid:
            # Sem UID: cria um identificador estável a partir de título + início
            uid = f"noid-{normalize_text(title)}-{fmt_dt(comp.get('dtstart').dt)}"

        s = comp.get("dtstart").dt
        e_prop = comp.get("dtend")
        e = e_prop.dt if e_prop is not None else None
        if e is not None and not isinstance(e, datetime):
            e = e - timedelta(days=1)  # DTEND de dia inteiro é exclusivo no ICS

        start = fmt_dt(s)
        end = fmt_dt(e) if e is not None else None
        if end == start:
            end = None

        events[uid] = {
            "uid": uid,
            "title": title,
            "start": start,
            "end": end,
            "types": determine_types(title),
        }
    return events


# --- Sincronização ---

def build_plan(ics_events, pages):
    """Calcula tudo o que é preciso fazer, sem escrever nada."""
    by_uid = {}
    legacy = {}
    for p in pages:
        if p["uid"]:
            by_uid.setdefault(p["uid"], []).append(p)
        else:
            legacy_key = f"{normalize_text(p['title'])}__{p['start']}__{p['end']}"
            legacy.setdefault(legacy_key, []).append(p)

    actions = []  # (tipo, page_id, evento_ou_titulo)
    unchanged = 0
    archive = []

    for uid, ev in ics_events.items():
        candidates = by_uid.pop(uid, [])

        if not candidates:
            # Tenta adotar uma página antiga (sem UID) com o mesmo título+datas
            legacy_key = f"{normalize_text(ev['title'])}__{ev['start']}__{ev['end']}"
            if legacy.get(legacy_key):
                candidates = [legacy[legacy_key].pop(0)]

        if not candidates:
            actions.append(("add", None, ev))
            continue

        keep, extras = candidates[0], candidates[1:]
        archive.extend(("archive", p["page_id"], p["title"]) for p in extras)  # duplicados

        if needs_update(keep, ev):
            actions.append(("update", keep["page_id"], ev))
        else:
            unchanged += 1

    # O que sobrou já não existe no ICS (ou é duplicado antigo sem UID)
    for group in by_uid.values():
        archive.extend(("archive", p["page_id"], p["title"]) for p in group)
    for group in legacy.values():
        archive.extend(("archive", p["page_id"], p["title"]) for p in group)

    return actions + archive, unchanged


def main():
    t0 = time.time()

    logger.info("A ler o calendário ICS...")
    try:
        ics_events = load_ics_events()
    except Exception as e:
        logger.error(f"Erro ao recuperar/processar ICS: {e}")
        return 1
    logger.info(f"ICS: {len(ics_events)} eventos.")

    if not ics_events:
        logger.error("ICS sem eventos; a abortar para não apagar tudo no Notion.")
        return 1

    logger.info("A ler a base de dados do Notion...")
    try:
        check_schema()
        pages = get_existing_events()
    except Exception as e:
        logger.error(f"Erro ao ler o Notion; a abortar sem alterar nada: {e}")
        return 1

    actions, unchanged = build_plan(ics_events, pages)
    n_add = sum(1 for a in actions if a[0] == "add")
    n_upd = sum(1 for a in actions if a[0] == "update")
    n_arc = sum(1 for a in actions if a[0] == "archive")
    logger.info(
        f"Plano: {n_add} a adicionar | {n_upd} a atualizar | {n_arc} a arquivar | {unchanged} sem alterações"
    )

    added, updated, deleted, failed = [], [], [], 0
    labels = {"add": "A adicionar", "update": "A atualizar", "archive": "A arquivar"}

    if actions:
        with logging_redirect_tqdm():
            bar = tqdm(actions, unit="ev", ncols=100, bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]")
            for kind, page_id, payload in bar:
                title = payload["title"] if isinstance(payload, dict) else payload
                bar.set_description(f"{labels[kind]}: {title[:45]:<45}")

                if kind == "add":
                    ok = add_event(payload)
                    if ok:
                        added.append(title)
                elif kind == "update":
                    ok = update_event(page_id, payload)
                    if ok:
                        updated.append(title)
                else:
                    ok = archive_event(page_id, title)
                    if ok:
                        deleted.append(title)

                if not ok:
                    failed += 1
                time.sleep(WRITE_DELAY)

    logger.info(
        f"RESUMO - Mantidos: {unchanged} | Atualizados: {len(updated)} | "
        f"Adicionados: {len(added)} | Eliminados: {len(deleted)} | "
        f"Falhados: {failed} | Tempo: {time.time() - t0:.2f}s"
    )
    for label, items in (("Adicionados", added), ("Atualizados", updated), ("Eliminados", deleted)):
        for title in items:
            logger.info(f"  {label}: {title}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())