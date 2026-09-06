import requests
import os
from icalendar import Calendar
from datetime import datetime, date
from dateutil.parser import isoparse
from dotenv import load_dotenv
import unicodedata
import time
import logging


# --- Configurações ---

load_dotenv()
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DATABASE_ID = os.getenv("DATABASE_ID")
ICAL_URL = os.getenv("ICAL_URL")

headers = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28"
}


# --- Configuração de Logging ---

log_file = os.path.join(os.path.dirname(__file__), "sync.log")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# --- Funções auxiliares ---

def normalize_datetime(dt):     # Converte para datetime sem timezone, truncando segundos e microssegundos
    if isinstance(dt, datetime):
        return dt.replace(tzinfo=None, second=0, microsecond=0)
    elif isinstance(dt, date):
        return datetime.combine(dt, datetime.min.time())
    return dt

def normalize_text(text):       # Remove acentos e converte para minúsculas
    text = unicodedata.normalize('NFD', text)
    text = text.encode('ascii', 'ignore').decode('utf-8')
    return text.lower().strip()

def determine_types(title):     # Determina os tipos do evento baseado no título
    types = []
    title_norm = normalize_text(title)
    if "avaliacao" in title_norm or "defesa de trabalhos" in title_norm or "entrega" in title_norm or "(ee)" in title_norm:
        if "defesa de trabalhos" in title_norm:
            types.append("Avaliação")
        elif "entrega" in title_norm:
            types.append("Avaliação")
        elif "(ee)" in title_norm:
            types.append("Época Especial")
        elif "frequencia" in title_norm:
            types.append("Frequência")
        elif "(er)" in title_norm:
            types.append("Exame Recurso")
        elif "(en)" in title_norm:
            types.append("Exame Normal")
        else:
            types.append("Avaliação")
        if "por inscrever" in title_norm:
            types.append("Por inscrever")
    else:
        types.append("Aula")
    return types

def types_changed(existing_types, new_types):
    return set(map(str.lower, existing_types)) != set(map(str.lower, new_types))

def dates_changed(existing_start, existing_end, new_start, new_end):
    return existing_start != new_start or existing_end != new_end


# --- Notion API ---

def get_existing_events():
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    events = {}
    has_more = True
    next_cursor = None
    while has_more:
        payload = {"page_size": 100}
        if next_cursor:
            payload["start_cursor"] = next_cursor
        try:
            response = requests.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as e:
            print(f"ALERTA - Erro ao recuperar eventos: {e}")
            break
        for page in data["results"]:
            try:
                project = page["properties"]["Project"]["select"]["name"] if page["properties"]["Project"]["select"] else ""
                if project != "Inforestudante":
                    continue
                title = page["properties"]["Nome"]["title"][0]["text"]["content"] if page["properties"]["Nome"]["title"] else ""
                date_info = page["properties"]["Date"]["date"]
                start = isoparse(date_info["start"]) if date_info and date_info.get("start") else None
                end = isoparse(date_info["end"]) if date_info and date_info.get("end") else None
                types = [t["name"] for t in page["properties"]["Type"]["multi_select"]]
                # Chave simplificada (ignora microssegundos/timezone)
                key = f"{normalize_text(title)}__{normalize_datetime(start)}__{normalize_datetime(end)}"
                events[key] = {
                    "page_id": page["id"],
                    "title": title,
                    "start": normalize_datetime(start),
                    "end": normalize_datetime(end),
                    "types": types
                }
            except (KeyError, IndexError, TypeError):
                continue
        has_more = data.get("has_more", False)
        next_cursor = data.get("next_cursor", None)
    return events

def add_event(title, start, end, types):
    url = "https://api.notion.com/v1/pages"
    multi_select_types = [{"name": t} for t in types]
    data = {
        "parent": {"database_id": DATABASE_ID},
        "properties": {
            "Nome": {"title": [{"text": {"content": title}}]},
            "Date": {"date": {"start": start.isoformat(), "end": end.isoformat() if end else None}},
            "Project": {"select": {"name": "Inforestudante"}},
            "Type": {"multi_select": multi_select_types}
        }
    }
    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"ALERTA - Erro ao criar evento: {e}")
        return False

def update_event(page_id, title, old_start, old_end, old_types, new_start, new_end, new_types):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    multi_select_types = [{"name": t} for t in new_types]
    data = {
        "properties": {
            "Date": {"date": {"start": new_start.isoformat(), "end": new_end.isoformat() if new_end else None}},
            "Type": {"multi_select": multi_select_types}
        }
    }
    try:
        response = requests.patch(url, headers=headers, json=data)
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"ALERTA - Erro ao atualizar evento: {e}")
        return False

def delete_event(page_id, title):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    data = {"archived": True}
    try:
        response = requests.patch(url, headers=headers, json=data)
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"ALERTA - Erro ao eliminar '{title}': {e}")
        return False


# --- Sincronização ---

start_time = time.time()
existing_events = get_existing_events()
try:
    ics_response = requests.get(ICAL_URL)
    ics_response.raise_for_status()
    cal = Calendar.from_ical(ics_response.text)
except requests.RequestException as e:
    logger.error(f"ALERTA - Erro ao recuperar ICS: {e}")
    exit(1)
except Exception as e:
    logger.error(f"ALERTA - Erro ao processar ICS: {e}")
    exit(1)

ics_events = {}

for component in cal.walk():
    if component.name != "VEVENT":
        continue
    title = str(component.get("summary"))
    start = normalize_datetime(component.get("dtstart").dt)
    
    # fallback se dtend não existir
    dtend = component.get("dtend")
    if dtend is None:
        end = start  # assume que termina no mesmo instante
    else:
        end = normalize_datetime(dtend.dt)
    
    types = determine_types(title)
    key = f"{normalize_text(title)}__{start}__{end}"
    ics_events[key] = {"title": title, "start": start, "end": end, "types": types}


# --- Atualizações e Adições ---

added_events = []
updated_events = []
deleted_events = []

for key, ics_event in ics_events.items():
    if key in existing_events:
        event = existing_events[key]
        if dates_changed(event["start"], event["end"], ics_event["start"], ics_event["end"]) or types_changed(event["types"], ics_event["types"]):
            if update_event(event["page_id"], event["title"], event["start"], event["end"], event["types"], ics_event["start"], ics_event["end"], ics_event["types"]):
                updated_events.append(event["title"])
    else:
        if add_event(ics_event["title"], ics_event["start"], ics_event["end"], ics_event["types"]):
            added_events.append(ics_event["title"])


# --- Elimina os que já não estão no ICS ---

for key, event in existing_events.items():
    if key not in ics_events:
        if delete_event(event["page_id"], event["title"]):
            deleted_events.append(event["title"])

end_time = time.time()
execution_time = end_time - start_time

maintained = len(ics_events) - len(updated_events)

logger.info(f"RESUMO - Mantidos: {maintained} | Atualizados: {len(updated_events)} | Adicionados: {len(added_events)} | Eliminados: {len(deleted_events)} | Tempo: {execution_time:.2f}s")

if len(updated_events) > 0 or len(added_events) > 0 or len(deleted_events) > 0:
    response = input("\nDeseja ver os detalhes das alterações? (s/n): ").strip().lower()
    if response == 's':
        if added_events:
            print(f"\nAdicionados ({len(added_events)}):")
            for title in added_events:
                print(f"  - {title}")
        if updated_events:
            print(f"\nAtualizados ({len(updated_events)}):")
            for title in updated_events:
                print(f"  - {title}")
        if deleted_events:
            print(f"\nEliminados ({len(deleted_events)}):")
            for title in deleted_events:
                print(f"  - {title}")
    else:
        print("Programa terminado.")
else:
    logger.info("Nenhuma alteração necessária.")