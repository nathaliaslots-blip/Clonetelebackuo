import json
import asyncio
import logging
import os
import re
from pathlib import Path

from telethon import TelegramClient, events, functions, helpers
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("media-forwarder")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
STRING_SESSION = os.environ["STRING_SESSION"]
SOURCE_CHAT_ID = int(os.environ["SOURCE_CHAT_ID"])
# Telegram supergroups commonly appear as -100xxxxxxxxxx. Override this
# variable if your account resolves the destination using another form.
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "-1003997519643"))
EXTRA_TOPIC_ID = int(os.getenv("EXTRA_TOPIC_ID", "264"))
GENERAL_TOPIC_ID = 1
LOG_CHAT_ID = int(os.getenv("LOG_CHAT_ID", "0"))
TOPICS_FILE = Path(os.getenv("TOPICS_FILE", "topics.json"))
QUEUE_DELAY_SECONDS = float(os.getenv("QUEUE_DELAY_SECONDS", "2"))
OTHER_GROUP_VALUE = os.getenv("OTHER_GROUP", "").strip()
if OTHER_GROUP_VALUE:
    try:
        OTHER_GROUP = int(OTHER_GROUP_VALUE)
    except ValueError:
        OTHER_GROUP = OTHER_GROUP_VALUE
else:
    OTHER_GROUP = None
SCHEDULED_EDIT_DELAY_SECONDS = 5
SCHEDULED_POLL_SECONDS = 30

# Railway is non-interactive, so the client must use the provided session
# instead of creating a local .session file and asking for a phone number.
client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
topic_cache = {}
media_queue = asyncio.Queue()
queue_worker_task = None
scheduled_editor_task = None
other_group_entity = None
pending_albums = {}
known_duplicates = []


async def send_log(text):
    """Send important operational logs without affecting the worker."""
    if not LOG_CHAT_ID:
        return
    try:
        await client.send_message(LOG_CHAT_ID, text)
    except Exception:
        pass


def queue_log(text):
    asyncio.create_task(send_log(text))


def load_topics():
    global topic_cache
    if TOPICS_FILE.exists():
        try:
            topic_cache = {
                str(k): int(v)
                for k, v in json.loads(TOPICS_FILE.read_text()).items()
            }
        except (OSError, ValueError, TypeError):
            log.exception("Could not read %s; starting with an empty cache", TOPICS_FILE)


def save_topics():
    temporary = TOPICS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(topic_cache, ensure_ascii=False, indent=2))
    temporary.replace(TOPICS_FILE)


FLAG_PATTERN = r"[\U0001F1E6-\U0001F1FF]{2}"

COUNTRY_FLAGS = {
    "venezuela": "🇻🇪",
    "brazil": "🇧🇷",
    "mexico": "🇲🇽",
    "colombia": "🇨🇴",
    "argentina": "🇦🇷",
    "peru": "🇵🇪",
    "chile": "🇨🇱",
    "ecuador": "🇪🇨",
    "dominican republic": "🇩🇴",
    "spain": "🇪🇸",
    "united states": "🇺🇸",
    "honduras": "🇭🇳",
    "guatemala": "🇬🇹",
    "el salvador": "🇸🇻",
    "panama": "🇵🇦",
    "costa rica": "🇨🇷",
    "paraguay": "🇵🇾",
    "bolivia": "🇧🇴",
    "uruguay": "🇺🇾",
    "cuba": "🇨🇺",
    "nicaragua": "🇳🇮",
    "netherlands": "🇳🇱",
}


def country_name_to_flag(country_name):
    normalized = " ".join(str(country_name).strip().casefold().split())
    flag = COUNTRY_FLAGS.get(normalized)
    if flag is None:
        log.warning("Country not mapped to a flag: %s", country_name)
        return "🏳️"
    return flag


def parse_caption(caption):
    """Extract each field independently from the first matching candidate."""
    if not caption:
        return None
    caption = str(caption)
    name = user_id = country = None
    name_pattern_used = id_pattern_used = country_pattern_used = None

    name_candidates = [
        (
            "👤+id combinado",
            r"👤\s*(.+?)\s*\((\d+)\)",
            True,
        ),
        ("👤 Name:", r"(?m)^\s*👤\s*Name\s*:\s*(.+)", False),
        ("🙍‍♀️", r"(?m)^\s*🙍‍♀️\s*:?\s*(.+)", False),
        ("👩🏻", r"(?m)^\s*👩🏻\s*:?\s*(.+)", False),
        ("👤", r"(?m)^\s*👤\s*(.+)", False),
        ("👩", r"(?m)^\s*👩\s*(.+)", False),
    ]
    for label, pattern, includes_id in name_candidates:
        match = re.search(pattern, caption)
        if not match:
            continue
        name = match.group(1).splitlines()[0].strip()
        name_pattern_used = label
        if includes_id:
            user_id = match.group(2)
            id_pattern_used = "👤+id combinado"
        break

    if user_id is None:
        id_candidates = [
            ("🔍", r"(?m)^\s*🔍\s*:?\s*([0-9]+)"),
            ("🆔", r"(?m)^\s*🆔\s*(?:User\s*ID\s*:?\s*)?([0-9]+)"),
        ]
        for label, pattern in id_candidates:
            match = re.search(pattern, caption)
            if not match:
                continue
            line_start = caption.rfind("\n", 0, match.start()) + 1
            line_end = caption.find("\n", match.start())
            line = caption[line_start:] if line_end < 0 else caption[line_start:line_end]
            if "stream" in line.casefold():
                continue
            user_id = match.group(1)
            id_pattern_used = label
            break

    country_candidates = [
        (
            "🗺️+bandeira",
            rf"(?mi)^\s*🗺️?.*?({FLAG_PATTERN})\s*$",
            False,
        ),
        (
            "Country+conversão",
            r"(?mi)^[•-]?\s*Country\s*:\s*([A-Za-zÀ-ÿ ]+)",
            True,
        ),
    ]
    for label, pattern, is_country_name in country_candidates:
        match = re.search(pattern, caption)
        if not match:
            continue
        country = (
            country_name_to_flag(match.group(1))
            if is_country_name
            else match.group(1)
        )
        country_pattern_used = label
        break

    if name and user_id and country:
        log.info(
            "Caption parseada: nome via %s, ID via %s, país via %s",
            name_pattern_used,
            id_pattern_used,
            country_pattern_used,
        )
        return name, user_id, country

    log.warning("Caption did not match the expected format: %r", caption[:500])
    return None


def make_caption(name, user_id, country):
    return (
        f"👤 Name: {name}\n"
        f"🆔 User ID: {user_id}\n"
        f"🌍 Country: {country}\n"
        "━━━━━━━━━━━━━━\n"
        "@BuzzHubLatam"
    )


async def find_topic(user_id):
    cached = topic_cache.get(str(user_id))
    if cached:
        return cached

    # The local JSON is fast; this API fallback also recovers after cache loss.
    response = await client(
        functions.messages.GetForumTopicsRequest(
            peer=TARGET_CHAT_ID,
            q="",
            offset_date=0,
            offset_id=0,
            offset_topic=0,
            limit=100,
        )
    )
    for topic in getattr(response, "topics", []):
        title = getattr(topic, "title", "") or ""
        if re.search(rf"(?:^|-\s*){re.escape(str(user_id))}\s*$", title):
            topic_cache[str(user_id)] = int(topic.id)
            save_topics()
            return int(topic.id)
    return None


def topic_offsets(topic):
    topic_date = getattr(topic, "date", 0) or 0
    if hasattr(topic_date, "timestamp"):
        topic_date = int(topic_date.timestamp())
    return (
        int(topic_date),
        int(getattr(topic, "top_message", 0) or 0),
        int(getattr(topic, "id", 0) or 0),
    )


def duplicate_lines(duplicates):
    return [
        (
            f'- user_id {user_id}: topic {first_id} ("{first_title}") e '
            f'topic {second_id} ("{second_title}")'
        )
        for user_id, first_id, first_title, second_id, second_title in duplicates
    ]


async def report_duplicates(duplicates):
    if not duplicates:
        return
    header = "⚠️ Tópicos duplicados encontrados (revisar manualmente):"
    chunks = []
    current = header
    for line in duplicate_lines(duplicates):
        if len(current) + len(line) + 1 > 3900:
            chunks.append(current)
            current = line
        else:
            current += f"\n{line}"
    chunks.append(current)
    for chunk in chunks:
        await send_log(chunk)


async def sync_all_topics():
    """Load every forum topic and keep the oldest topic per user ID."""
    global known_duplicates
    topic_cache.clear()
    topic_metadata = {}
    known_duplicates = []
    total_topics = 0
    offset_date = offset_id = offset_topic = 0
    seen_offsets = set()

    while True:
        response = await client(
            functions.messages.GetForumTopicsRequest(
                peer=TARGET_CHAT_ID,
                q="",
                offset_date=offset_date,
                offset_id=offset_id,
                offset_topic=offset_topic,
                limit=100,
            )
        )
        topics = list(getattr(response, "topics", []))
        if not topics:
            break
        total_topics += len(topics)

        for topic in topics:
            title = getattr(topic, "title", "") or ""
            match = re.search(r"(?:^|-\s*)(\d+)\s*$", title)
            if not match:
                continue
            user_id = match.group(1)
            topic_id = int(topic.id)
            previous = topic_metadata.get(user_id)
            if previous is not None:
                known_duplicates.append(
                    (user_id, previous[0], previous[1], topic_id, title)
                )
                if topic_id < previous[0]:
                    topic_cache[user_id] = topic_id
                    topic_metadata[user_id] = (topic_id, title)
            else:
                topic_cache[user_id] = topic_id
                topic_metadata[user_id] = (topic_id, title)

        next_offsets = topic_offsets(topics[-1])
        if next_offsets in seen_offsets:
            log.warning("Forum topic pagination repeated an offset; stopping safely")
            break
        seen_offsets.add(next_offsets)
        offset_date, offset_id, offset_topic = next_offsets

    save_topics()
    log.info(
        "Synchronized %d topics; %d duplicate user IDs found",
        total_topics,
        len(known_duplicates),
    )
    await send_log(
        f"🔄 Sincronização concluída: {total_topics} tópicos; "
        f"{len(known_duplicates)} duplicata(s) encontrada(s)."
    )
    await report_duplicates(known_duplicates)
    return total_topics, list(known_duplicates)


async def get_or_create_topic(name, user_id, country):
    topic_id = topic_cache.get(str(user_id))
    if topic_id:
        return topic_id
    topic_id = await find_topic(user_id)
    if topic_id:
        return topic_id

    result = await client(
        functions.messages.CreateForumTopicRequest(
            peer=TARGET_CHAT_ID,
            title=f"{country} {name} - {user_id}",
        )
    )
    topic_id = None
    # The created topic's root service message is the reply target for the
    # forum topic. This shape is returned by current Telethon versions.
    for update in getattr(result, "updates", []):
        message = getattr(update, "message", None)
        action = getattr(message, "action", None)
        if message and action and action.__class__.__name__ == "MessageActionTopicCreate":
            topic_id = int(message.id)
            break
    if topic_id is None:
        raise RuntimeError("Telegram did not return the new forum topic id")

    topic_cache[str(user_id)] = topic_id
    save_topics()
    return topic_id


async def forward_to_topic(messages, topic_id, caption, label):
    # The high-level Telethon helper does not expose forum-topic routing in
    # the version running on Railway. Use the raw Telegram request instead.
    try:
        source_peer = await client.get_input_entity(SOURCE_CHAT_ID)
        target_peer = await client.get_input_entity(TARGET_CHAT_ID)
        source_ids = [int(message.id) for message in messages]
        result = await client(
            functions.messages.ForwardMessagesRequest(
                from_peer=source_peer,
                id=source_ids,
                random_id=[helpers.generate_random_long() for _ in source_ids],
                to_peer=target_peer,
                drop_author=True,
                drop_media_captions=True,
                top_msg_id=topic_id,
            )
        )
        forwarded_ids = [
            update.message.id
            for update in getattr(result, "updates", [])
            if getattr(update, "message", None) is not None
        ]
        if not forwarded_ids:
            raise RuntimeError("Telegram did not return forwarded message ids")
        # Albums have one caption, so edit the first forwarded item only.
        await client.edit_message(
            entity=TARGET_CHAT_ID,
            message=forwarded_ids[0],
            text=caption,
        )
        log.info("Forwarded to %s topic %s", label, topic_id)
        return True
    except FloodWaitError as error:
        log.warning("FloodWait while forwarding to %s topic %s: %s", label, topic_id, error)
        queue_log(f"⚠️ FloodWait ao encaminhar para {label} topic {topic_id}: {error}")
    except Exception as error:
        log.exception("Failed to forward to %s topic %s", label, topic_id)
        queue_log(f"❌ Erro ao encaminhar para {label} topic {topic_id}: {error}")
    return False


async def resolve_other_group():
    """Resolve OTHER_GROUP and load dialogs when only a numeric ID is available."""
    global other_group_entity
    if other_group_entity is not None:
        return other_group_entity

    try:
        other_group_entity = await client.get_input_entity(OTHER_GROUP)
    except ValueError:
        log.info("Loading dialogs to resolve OTHER_GROUP %s", OTHER_GROUP)
        await client.get_dialogs()
        other_group_entity = await client.get_input_entity(OTHER_GROUP)
    return other_group_entity


async def edit_scheduled_messages():
    """Normalize captions of up to 100 scheduled messages in OTHER_GROUP."""
    if not OTHER_GROUP:
        return 0

    other_group_entity = await resolve_other_group()
    scheduled_response = await client(
        functions.messages.GetScheduledHistoryRequest(
            peer=other_group_entity,
            hash=0,
        )
    )
    scheduled_messages = list(getattr(scheduled_response, "messages", []))
    if not scheduled_messages:
        return 0

    candidates = []
    for message in scheduled_messages:
        source_caption = getattr(message, "message", None) or getattr(message, "text", None)
        parsed = parse_caption(source_caption)
        if parsed:
            candidates.append((message, parsed))

    edited = 0
    for index, (message, parsed) in enumerate(candidates):
        name, user_id, country = parsed
        new_caption = make_caption(name, user_id, country)
        current_caption = getattr(message, "message", None) or getattr(message, "text", None)
        if current_caption == new_caption:
            log.debug("Scheduled message %s already has the standard caption", message.id)
            continue
        try:
            await client.edit_message(
                entity=other_group_entity,
                message=message.id,
                text=new_caption,
                schedule=message.date,
            )
            edited += 1
            log.info("Edited scheduled message %s in %s", message.id, OTHER_GROUP)
            queue_log(
                f"✏️ Mensagem agendada {message.id} editada em {OTHER_GROUP}: "
                f"{name} ({user_id})"
            )
        except FloodWaitError as error:
            log.warning(
                "FloodWait while editing scheduled message %s: %s",
                message.id,
                error,
            )
            queue_log(
                f"⚠️ FloodWait ao editar mensagem agendada {message.id}: {error}"
            )
        except Exception as error:
            log.exception("Failed to edit scheduled message %s", message.id)
            queue_log(
                f"❌ Erro ao editar mensagem agendada {message.id}: {error}"
            )

        if index < len(candidates) - 1:
            await asyncio.sleep(SCHEDULED_EDIT_DELAY_SECONDS)

    return edited


async def scheduled_editor_worker():
    while True:
        try:
            edited = await edit_scheduled_messages()
            if edited:
                log.info("Edited %d scheduled message(s) in %s", edited, OTHER_GROUP)
        except FloodWaitError as error:
            log.warning("FloodWait while reading scheduled messages: %s", error)
            queue_log(f"⚠️ FloodWait ao consultar mensagens agendadas: {error}")
            await asyncio.sleep(error.seconds)
        except Exception as error:
            log.exception("Unexpected scheduled editor error")
            queue_log(f"❌ Erro inesperado no editor de agendadas: {error}")
        await asyncio.sleep(SCHEDULED_POLL_SECONDS)


async def forward_media(item):
    messages = item["messages"]
    extra_topic_id = item["extra_topic_id"]
    if not messages:
        return
    log.info(
        "Received %d media message(s): ids=%s grouped_id=%s",
        len(messages),
        [m.id for m in messages],
        getattr(messages[0], "grouped_id", None),
    )
    source_caption = next(
        (
            getattr(m, "message", None) or getattr(m, "text", None)
            for m in messages
            if getattr(m, "message", None) or getattr(m, "text", None)
        ),
        None,
    )
    parsed = parse_caption(source_caption)
    if not parsed:
        log.warning("Media ignored because no supported caption was found")
        queue_log("⚠️ Mídia descartada: legenda não reconhecida")
        return
    name, user_id, country = parsed
    new_caption = make_caption(name, user_id, country)

    try:
        person_topic_id = await get_or_create_topic(name, user_id, country)
    except FloodWaitError as error:
        log.warning("FloodWait while resolving topic for user %s: %s", user_id, error)
        queue_log(f"⚠️ FloodWait ao criar/consultar tópico de {user_id}: {error}")
        person_topic_id = None
    except Exception as error:
        log.exception("Failed to resolve person topic for user %s", user_id)
        queue_log(f"❌ Erro ao criar/consultar tópico de {user_id}: {error}")
        person_topic_id = None

    if person_topic_id:
        person_ok = await forward_to_topic(messages, person_topic_id, new_caption, "person")
    else:
        person_ok = False
    fixed_ok = await forward_to_topic(messages, extra_topic_id, new_caption, "fixed")
    if person_ok or fixed_ok:
        successful_topics = []
        if person_ok:
            successful_topics.append(str(person_topic_id))
        if fixed_ok:
            successful_topics.append(str(extra_topic_id))
        queue_log(
            f"✅ Encaminhado: {name} ({user_id}) -> topic(s) {', '.join(successful_topics)}"
        )


async def queue_worker():
    while True:
        item = await media_queue.get()
        try:
            await forward_media(item)
        except Exception as error:
            log.exception("Unexpected queue worker error")
            queue_log(f"❌ Erro inesperado no worker da fila: {error}")
        finally:
            media_queue.task_done()
            await asyncio.sleep(QUEUE_DELAY_SECONDS)


async def debounce_album(grouped_id):
    try:
        await asyncio.sleep(2)
        entry = pending_albums.get(grouped_id)
        if entry is None:
            return
        item = list(entry["messages"])
        pending_albums.pop(grouped_id, None)
        log.info(
            "Álbum grouped_id=%s completo com %d mensagens, enviado à fila",
            grouped_id,
            len(item),
        )
        media_queue.put_nowait(
            {"messages": item, "extra_topic_id": EXTRA_TOPIC_ID}
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        pending_albums.pop(grouped_id, None)
        log.exception("Error while debouncing album %s", grouped_id)
        queue_log(f"❌ Erro ao agrupar álbum {grouped_id}: {error}")


@client.on(events.NewMessage(chats=SOURCE_CHAT_ID))
async def message_handler(event):
    if not event.message.media:
        return
    grouped_id = event.message.grouped_id
    log.info(
        "EVENTO disparado: mídia %s grouped_id=%s",
        event.message.id,
        grouped_id,
    )
    if grouped_id is None:
        media_queue.put_nowait(
            {"messages": [event.message], "extra_topic_id": EXTRA_TOPIC_ID}
        )
        return

    entry = pending_albums.setdefault(
        grouped_id,
        {"messages": [], "task": None},
    )
    entry["messages"].append(event.message)
    if entry["task"] is not None:
        entry["task"].cancel()
    entry["task"] = asyncio.create_task(debounce_album(grouped_id))


async def count_topics():
    total = 0
    offset_date = 0
    offset_id = 0
    offset_topic = 0
    seen_offsets = set()

    while True:
        response = await client(
            functions.messages.GetForumTopicsRequest(
                peer=TARGET_CHAT_ID,
                q="",
                offset_date=offset_date,
                offset_id=offset_id,
                offset_topic=offset_topic,
                limit=100,
            )
        )
        topics = list(getattr(response, "topics", []))
        total += len(topics)
        if len(topics) < 100:
            return total

        last = topics[-1]
        last_date = getattr(last, "date", 0) or 0
        if hasattr(last_date, "timestamp"):
            last_date = int(last_date.timestamp())
        next_offsets = (
            int(last_date),
            int(getattr(last, "top_message", 0) or 0),
            int(getattr(last, "id", 0) or 0),
        )
        if next_offsets in seen_offsets:
            return total
        seen_offsets.add(next_offsets)
        offset_date, offset_id, offset_topic = next_offsets


async def clone_command_handler(event):
    text = event.raw_text or ""
    stripped = text.strip()
    if stripped == "/topico":
        try:
            total = await count_topics()
            await event.reply(f"📊 Total de tópicos: {total}")
        except Exception as error:
            log.exception("Failed to count topics")
            queue_log(f"❌ Erro ao contar tópicos: {error}")
            await event.reply("❌ Não foi possível consultar os tópicos.")
        return

    if stripped == "/sync":
        try:
            total, duplicates = await sync_all_topics()
            await event.reply(
                f"🔄 Sincronização concluída: {total} tópicos, "
                f"{len(duplicates)} duplicata(s)."
            )
        except Exception as error:
            log.exception("Failed to synchronize topics")
            queue_log(f"❌ Erro ao sincronizar tópicos: {error}")
            await event.reply("❌ Não foi possível sincronizar os tópicos.")
        return

    if stripped == "/duplicados":
        try:
            total, duplicates = await sync_all_topics()
            if not duplicates:
                await event.reply("✅ Nenhum tópico duplicado encontrado.")
                return
            lines = duplicate_lines(duplicates)
            chunks = []
            current = "⚠️ Tópicos duplicados encontrados (revisar manualmente):"
            for line in lines:
                if len(current) + len(line) + 1 > 3900:
                    chunks.append(current)
                    current = line
                else:
                    current += f"\n{line}"
            chunks.append(current)
            for index, chunk in enumerate(chunks):
                if index == 0:
                    await event.reply(chunk)
                else:
                    await event.respond(chunk)
        except Exception as error:
            log.exception("Failed to list duplicate topics")
            queue_log(f"❌ Erro ao listar tópicos duplicados: {error}")
            await event.reply("❌ Não foi possível consultar duplicatas.")
        return

    if not re.match(r"^/clone(?:\s|$)", stripped):
        return

    requested_ids = [
        int(value) for value in re.findall(r"\d+", stripped[len("/clone") :])
    ]
    if not requested_ids:
        await event.reply("🕓 0 mídia(s) enfileirada(s) para clonagem.")
        return

    try:
        fetched = await client.get_messages(
            SOURCE_CHAT_ID,
            ids=requested_ids,
        )
    except Exception as error:
        log.exception("Failed to fetch clone messages")
        queue_log(f"❌ Erro ao buscar mensagens para clonagem: {error}")
        await event.reply("❌ Não foi possível buscar as mensagens solicitadas.")
        return

    fetched = fetched if isinstance(fetched, list) else [fetched]
    by_id = {}
    grouped_known_ids = {}
    for requested_id, message in zip(requested_ids, fetched):
        if message is None or not message.media:
            log.warning("Clone ID %s not found or has no media", requested_id)
            queue_log(f"⚠️ Clone ignorado: mensagem {requested_id} inexistente ou sem mídia")
            continue
        by_id[message.id] = message
        if message.grouped_id is not None:
            grouped_known_ids.setdefault(message.grouped_id, []).append(message.id)

    # An album caption may be attached to a sibling message rather than the
    # requested message. Query a window around every known album message,
    # instead of querying only the explicitly supplied IDs.
    for grouped_id, known_ids in grouped_known_ids.items():
        for known_id in known_ids:
            try:
                neighbors = await client.get_messages(
                    SOURCE_CHAT_ID,
                    min_id=max(1, known_id - 10),
                    max_id=known_id + 11,
                    limit=20,
                )
                neighbors = neighbors if isinstance(neighbors, list) else [neighbors]
                for message in neighbors:
                    if (
                        message is not None
                        and message.media
                        and message.grouped_id == grouped_id
                        and message.id not in by_id
                    ):
                        by_id[message.id] = message
            except Exception as error:
                log.warning(
                    "Could not expand clone album %s around message %s: %s",
                    grouped_id,
                    known_id,
                    error,
                )
                queue_log(
                    f"⚠️ Não foi possível expandir o álbum {grouped_id}: {error}"
                )

    grouped = {}
    for message in by_id.values():
        key = (
            ("album", message.grouped_id)
            if message.grouped_id is not None
            else ("message", message.id)
        )
        grouped.setdefault(key, []).append(message)

    for messages in grouped.values():
        media_queue.put_nowait(
            {"messages": messages, "extra_topic_id": GENERAL_TOPIC_ID}
        )

    await event.reply(
        f"🕓 {len(grouped)} mídia(s) enfileirada(s) para clonagem."
    )


@client.on(events.NewMessage(chats=LOG_CHAT_ID, outgoing=True))
async def logs_command_handler(event):
    await clone_command_handler(event)


async def main():
    global queue_worker_task, scheduled_editor_task
    load_topics()
    await client.start()
    await sync_all_topics()
    if not LOG_CHAT_ID:
        log.warning("LOG_CHAT_ID is not configured; Telegram command/log chat is disabled")
    if queue_worker_task is None or queue_worker_task.done():
        queue_worker_task = asyncio.create_task(queue_worker())
    if OTHER_GROUP:
        if scheduled_editor_task is None or scheduled_editor_task.done():
            scheduled_editor_task = asyncio.create_task(scheduled_editor_worker())
        log.info("Scheduled-message editor enabled for %s", OTHER_GROUP)
    else:
        log.warning("OTHER_GROUP is not configured; scheduled-message editor is disabled")
    log.info("Monitoring %s and forwarding to %s", SOURCE_CHAT_ID, TARGET_CHAT_ID)
    await client.run_until_disconnected()


if __name__ == "__main__":
    with client:
        client.loop.run_until_complete(main())