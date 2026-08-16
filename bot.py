import os
import re
import io
import json
import html as html_lib
import asyncio
import subprocess
from dataclasses import dataclass
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, urljoin, parse_qs
import sys

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# OCR local pour lire le nom du personnage sur le screenshot DKPARSE.
# Dépendances :
#   pip install rapidocr_onnxruntime pillow numpy
try:
    from rapidocr_onnxruntime import RapidOCR
except Exception:
    RapidOCR = None

try:
    from PIL import Image
except Exception:
    Image = None

try:
    import numpy as np
except Exception:
    np = None


# =============================================================================
# Apogee Discord Bot - Raid-Helper + DKPARSE + ApogeeBot
# =============================================================================
# DKPARSE V2 (SCREEN-ONLY):
# - Le texte du post DKPARSE est TOUJOURS ignoré dès qu'un screenshot est présent.
# - Le screenshot est la source de vérité : personnage, boss, Points, date, spec si visible.
# - Chaque ligne boss est évaluée avec sa propre date Best Log lue sur le screen.
# - Seules les lignes dans la fenêtre DKPARSE et dont la date existe dans #logs-raid sont retenues.
# - #logs-raid sert uniquement à confirmer qu'au moins un lien de raid existe à la même date.
# - DKPARSE ne charge AUCUNE page UwU et ne vérifie plus la présence du joueur dans le raid.
# - Le délai est calculé entre la date Best Log du screen et la date du post Discord.
# - Les bonus sont cumulatifs.
#
# /dkparse-cloture :
# - relit tous les screenshots du salon DKPARSE ;
# - déduplique personnage + boss + date ;
# - produit DKPARSE|Nom:Total|... ;
# - tente de copier ce texte dans le presse-papier de la machine qui exécute le bot ;
# - propose ensuite une confirmation avant de vider entièrement le salon DKPARSE.
#
# APOGEEBOT :
# - surveille les nouveaux liens UwU dans #logs-raid ;
# - analyse les pages de combat du rapport ;
# - reproduit les catégories Top 0.2 / 2 / 5 / 10 / 15 / 20 / 25 / 33 % ;
# - garde en mémoire les URLs déjà traitées pour éviter les doublons après redémarrage.
# =============================================================================


def app_dir() -> Path:
    """Directory containing the EXE when frozen, otherwise this source file."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = app_dir()
load_dotenv(APP_DIR / ".env")

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0") or 0)
MAIN_CHANNEL_ID = int(os.getenv("MAIN_CHANNEL_ID", "0") or 0)
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0") or 0)

LOGS_RAID_CHANNEL_ID = int(os.getenv("LOGS_RAID_CHANNEL_ID", "0") or 0)
DKPARSE_CHANNEL_ID = int(os.getenv("DKPARSE_CHANNEL_ID", "0") or 0)
DKPARSE_MAX_DAYS = int(os.getenv("DKPARSE_MAX_DAYS", "8") or 8)
UWU_SERVER = os.getenv("UWU_SERVER", "Icecrown").strip() or "Icecrown"
DKPARSE_DEBUG = os.getenv("DKPARSE_DEBUG", "false").strip().lower() in {
    "1", "true", "yes", "on"
}
UWU_PEWPEW_ENABLED = os.getenv("UWU_PEWPEW_ENABLED", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
UWU_PEWPEW_MAX_FIGHTS_PER_BOSS = int(os.getenv("UWU_PEWPEW_MAX_FIGHTS_PER_BOSS", "2") or 2)

RAID_HELPER_API = "https://raid-helper.dev/api/v4/events/{event_id}"
WOW_NAME_RE = re.compile(r"^[A-Za-z]{2,12}$")
UWU_URL_RE = re.compile(
    r"https?://(?:www\.)?uwu-logs\.xyz/reports/[^\s<>()\]\[\"']+",
    re.IGNORECASE,
)
REPORT_DATE_RE = re.compile(r"/reports/(\d{2})-(\d{2})-(\d{2})--")

RH_DEBUG = False

STATUS_ORDER = ["signed", "bench", "late", "tentative", "absence", "unknown"]
STATUS_LABELS = {
    "signed": "✅ INSCRITS",
    "bench": "🪑 BENCH",
    "late": "⏰ RETARD",
    "tentative": "❓ TENTATIVE",
    "absence": "❌ ABSENT",
    "unknown": "❔ STATUT INCONNU",
}
STATUS_CODES = {
    "signed": "S",
    "bench": "B",
    "late": "L",
    "tentative": "T",
    "absence": "A",
    "unknown": "U",
}

DM_UNRECOGNIZED = (
    "Message automatique Apogee :\n"
    "Tu es inscrit pour un évent Apogee mais tu n'as pas ou mal saisi "
    "le nom de ton main en guilde dans le #Main."
)

DM_BAD_MAIN = (
    "Message automatique Apogee :\n"
    "Ton message dans #Main n'est pas valide. "
    "Écris uniquement le nom exact de ton main en guilde, sans texte autour.\n"
    "Exemple : Kromatisme"
)

# Specs DKPARSE retenues.
# Les alias servent uniquement à reconnaître ce qu'UwU renvoie.
DKPARSE_SPECS = {
    "fury": "Fwar",
    "fury warrior": "Fwar",
    "warrior fury": "Fwar",
    "combat": "Combat",
    "combat rogue": "Combat",
    "rogue combat": "Combat",
    "retribution": "Ret",
    "retribution paladin": "Ret",
    "ret": "Ret",
    "unholy": "UH",
    "unholy death knight": "UH",
    "uh": "UH",
    "feral": "FeralDPS",
    "feral combat": "FeralDPS",
    "feral dps": "FeralDPS",
    "fire": "MageFeu",
    "fire mage": "MageFeu",
    "mage fire": "MageFeu",
    "balance": "Boomie",
    "balance druid": "Boomie",
    "boomkin": "Boomie",
    "boomie": "Boomie",
    "shadow": "SP",
    "shadow priest": "SP",
    "sp": "SP",
    "demonology": "Démono",
    "demonology warlock": "Démono",
    "demo": "Démono",
    "marksmanship": "MM",
    "marksmanship hunter": "MM",
    "mm": "MM",
}

# Barème défini pour les DKPARSE.
# Le premier seuil atteint en partant du haut gagne la valeur correspondante.
DKPARSE_BRACKETS = (
    (99.0, 1000),
    (95.0, 700),
    (90.0, 500),
    (85.0, 300),
    (80.0, 200),
    (70.0, 70),
)

BOSS_ALIASES = {
    "lord marrowgar": "Lord Marrowgar",
    "marrowgar": "Lord Marrowgar",
    "marrow": "Lord Marrowgar",
    "lady deathwhisper": "Lady Deathwhisper",
    "deathwhisper": "Lady Deathwhisper",
    "gunship battle": "Gunship Battle",
    "gunship": "Gunship Battle",
    "deathbringer saurfang": "Deathbringer Saurfang",
    "saurfang": "Deathbringer Saurfang",
    "festergut": "Festergut",
    "rotface": "Rotface",
    "professor putricide": "Professor Putricide",
    "putricide": "Professor Putricide",
    "blood prince council": "Blood Prince Council",
    "blood princes": "Blood Prince Council",
    "bpc": "Blood Prince Council",
    "blood-queen lana'thel": "Blood-Queen Lana'thel",
    "blood queen lana'thel": "Blood-Queen Lana'thel",
    "lana'thel": "Blood-Queen Lana'thel",
    "lanathel": "Blood-Queen Lana'thel",
    "valithria dreamwalker": "Valithria Dreamwalker",
    "valithria": "Valithria Dreamwalker",
    "sindragosa": "Sindragosa",
    "sindra": "Sindragosa",
    "the lich king": "The Lich King",
    "lich king": "The Lich King",
    "lk": "The Lich King",
}

# Valithria n'est pas utilisée pour le meilleur parse dans l'environnement
# Kromaddon actuel. Elle est laissée hors DKPARSE par défaut.
DKPARSE_EXCLUDED_BOSSES = {"Gunship Battle", "Valithria Dreamwalker"}

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


@dataclass
class Signup:
    user_id: int
    display_name: str
    status: str
    raw_status: Any = None


@dataclass(frozen=True)
class UwuReport:
    url: str
    message_id: int
    posted_at: datetime
    report_date: Optional[datetime]
    label: str = ""


@dataclass(frozen=True)
class ParseHit:
    character: str
    boss: str
    parse: float
    spec: str
    report_url: str
    report_date: Optional[datetime]
    evidence: str = ""


@dataclass(frozen=True)
class ScreenParseRow:
    boss: str
    parse: float
    spec: str
    best_date: Optional[datetime]
    date_raw: str = ""
    attachment: str = ""


@dataclass
class DKParseScreenResult:
    message_id: int
    character: Optional[str]
    spec: str
    raid_date: Optional[datetime]
    rows: List[ScreenParseRow]
    valid_hits: List[ParseHit]
    issues: List[str]
    total: int
    ocr_detail: str = ""


@dataclass(frozen=True)
class PewPewHit:
    player: str
    boss: str
    top_percent: float
    points: float
    server_best: bool = False


def rh_debug(title: str, value: Any = None) -> None:
    if not RH_DEBUG:
        return
    print("\n" + "=" * 78)
    print(f"[RH DEBUG] {title}")
    if value is not None:
        try:
            if isinstance(value, (dict, list)):
                print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
            else:
                print(value)
        except Exception:
            print(repr(value))
    print("=" * 78)


def dkp_debug(title: str, value: Any = None) -> None:
    if not DKPARSE_DEBUG:
        return
    print("\n" + "-" * 78)
    print(f"[DKPARSE DEBUG] {title}")
    if value is not None:
        if isinstance(value, (dict, list)):
            print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
        else:
            print(value)
    print("-" * 78)


def get_first(d: Dict[str, Any], keys: Iterable[str], default=None):
    for key in keys:
        if key in d and d[key] not in (None, ""):
            return d[key]
    return default


# =============================================================================
# Raid-Helper (existing V3 behavior)
# =============================================================================

def normalize_status(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    aliases = {
        "primary": "signed", "signed": "signed", "signup": "signed",
        "accepted": "signed", "confirmed": "signed", "yes": "signed", "1": "signed",
        "bench": "bench", "benched": "bench", "reserve": "bench", "backup": "bench",
        "late": "late", "lateness": "late",
        "tentative": "tentative", "maybe": "tentative", "tent": "tentative",
        "absence": "absence", "absent": "absence", "declined": "absence", "no": "absence",
    }
    if s in aliases:
        return aliases[s]
    if "bench" in s or "reserve" in s:
        return "bench"
    if "late" in s or "retard" in s:
        return "late"
    if "tent" in s or "maybe" in s:
        return "tentative"
    if "absen" in s or "declin" in s:
        return "absence"
    if "sign" in s or "accept" in s or "confirm" in s:
        return "signed"
    return "unknown"


def extract_raw_status(entry: Dict[str, Any]) -> Any:
    raw_status = get_first(
        entry,
        [
            "status", "signupStatus", "signup_status", "signupType", "signup_type",
            "type", "state", "button", "buttonName", "button_name", "category",
        ],
        None,
    )
    nested = entry.get("signup")
    if isinstance(nested, dict):
        raw_status = get_first(
            nested, ["status", "type", "name", "button", "buttonName", "category"],
            raw_status,
        )
    return raw_status


def signup_category(entry: Dict[str, Any]) -> str:
    class_name = str(get_first(entry, ["className", "cClassName"], "")).strip().lower()
    if class_name == "late":
        return "late"
    if class_name == "bench":
        return "bench"
    if class_name == "tentative":
        return "tentative"
    if class_name == "absence":
        return "absence"
    if class_name:
        return "signed"
    return normalize_status(extract_raw_status(entry))


def find_signup_lists(obj: Any) -> List[List[Dict[str, Any]]]:
    found = []

    def walk(x: Any):
        if isinstance(x, dict):
            for k, v in x.items():
                kl = str(k).lower()
                if isinstance(v, list) and any(
                    t in kl for t in ("signup", "sign_up", "attendee", "participant", "member")
                ):
                    items = [i for i in v if isinstance(i, dict)]
                    if items:
                        found.append(items)
                walk(v)
        elif isinstance(x, list):
            items = [i for i in x if isinstance(i, dict)]
            if items:
                score = 0
                for i in items[:10]:
                    keys = {str(k).lower() for k in i.keys()}
                    if keys & {"userid", "user_id", "discordid", "discord_id", "id"}:
                        score += 1
                    if keys & {"status", "type", "signup", "signup_type", "signupstatus"}:
                        score += 1
                if score >= 2:
                    found.append(items)
            for v in x:
                walk(v)

    walk(obj)
    unique, seen = [], set()
    for lst in found:
        marker = json.dumps(lst, sort_keys=True, ensure_ascii=False, default=str)
        if marker not in seen:
            seen.add(marker)
            unique.append(lst)
    return unique


def compact_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    keys = [
        "id", "userId", "user_id", "discordId", "discord_id", "memberId", "member_id",
        "displayName", "display_name", "username", "userName", "name", "memberName",
        "status", "signupStatus", "signup_status", "signupType", "signup_type",
        "type", "state", "button", "buttonName", "button_name", "category",
        "className", "specName", "role", "position", "emoji", "emote",
    ]
    out = {k: entry[k] for k in keys if k in entry}
    for key in ("user", "member", "signup", "class", "spec"):
        if key in entry:
            out[key] = entry[key]
    return out or entry


def extract_signup(entry: Dict[str, Any]) -> Optional[Signup]:
    user_obj = entry.get("user") if isinstance(entry.get("user"), dict) else {}
    member_obj = entry.get("member") if isinstance(entry.get("member"), dict) else {}

    uid = get_first(
        entry, ["userId", "user_id", "discordId", "discord_id", "memberId", "member_id"]
    )
    if uid is None:
        uid = get_first(user_obj, ["id", "userId", "user_id"])
    if uid is None:
        uid = get_first(member_obj, ["id", "userId", "user_id"])
    if uid is None:
        candidate = entry.get("id")
        if candidate and str(candidate).isdigit() and len(str(candidate)) >= 15:
            uid = candidate
    if uid is None or not str(uid).isdigit():
        return None

    name = get_first(
        entry, ["displayName", "display_name", "username", "userName", "name", "memberName"]
    )
    if not name:
        name = get_first(user_obj, ["global_name", "display_name", "username", "name"])
    if not name:
        name = get_first(member_obj, ["display_name", "username", "name"])
    if not name:
        name = f"Discord {uid}"

    return Signup(
        user_id=int(uid),
        display_name=str(name),
        status=signup_category(entry),
        raw_status=extract_raw_status(entry),
    )


def debug_event_structure(payload: Any, event_id: int) -> None:
    if not RH_DEBUG:
        return
    rh_debug("EVENT ID", event_id)
    rh_debug("JSON RAID-HELPER BRUT", payload)
    for list_index, signup_list in enumerate(find_signup_lists(payload), start=1):
        print(f"[RH DEBUG] Liste #{list_index}: {len(signup_list)}")
        for entry in signup_list:
            print(json.dumps(compact_entry(entry), ensure_ascii=False, indent=2, default=str))


def parse_event_payload(payload: Any) -> Tuple[List[Signup], str]:
    event_title = "Raid-Helper Event"
    if isinstance(payload, dict):
        event_title = str(
            get_first(payload, ["title", "name", "eventTitle", "event_title"], event_title)
        )
        if isinstance(payload.get("event"), dict):
            event_title = str(get_first(payload["event"], ["title", "name"], event_title))

    signups: Dict[int, Signup] = {}
    for lst in find_signup_lists(payload):
        for entry in lst:
            s = extract_signup(entry)
            if s:
                signups[s.user_id] = s

    if not signups and isinstance(payload, dict):
        for key in ("signUps", "signups", "signup", "attendees", "participants"):
            lst = payload.get(key)
            if isinstance(lst, list):
                for entry in lst:
                    if isinstance(entry, dict):
                        s = extract_signup(entry)
                        if s:
                            signups[s.user_id] = s

    return list(signups.values()), event_title


async def fetch_raid_helper_event(event_id: int) -> Any:
    url = RAID_HELPER_API.format(event_id=event_id)
    timeout = aiohttp.ClientTimeout(total=15)
    headers = {"User-Agent": "ApogeeRaidHelperBot/4.0"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"Raid-Helper API HTTP {resp.status}: {body[:500]}")
            try:
                return json.loads(body)
            except json.JSONDecodeError as exc:
                raise RuntimeError("Raid-Helper n'a pas renvoyé un JSON valide.") from exc


async def get_text_channel(guild: discord.Guild, channel_id: int, env_name: str) -> discord.TextChannel:
    if not channel_id:
        raise RuntimeError(f"{env_name} manquant dans .env")
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        try:
            channel = await guild.fetch_channel(channel_id)
        except discord.HTTPException:
            channel = None
    if not isinstance(channel, discord.TextChannel):
        raise RuntimeError(f"{env_name} ne correspond pas à un salon texte accessible.")
    return channel


async def get_main_channel(guild: discord.Guild) -> discord.TextChannel:
    return await get_text_channel(guild, MAIN_CHANNEL_ID, "MAIN_CHANNEL_ID")


async def build_main_map(guild: discord.Guild) -> Tuple[Dict[int, str], List[str]]:
    channel = await get_main_channel(guild)
    by_user: Dict[int, Tuple[str, int]] = {}
    name_owner: Dict[str, int] = {}
    problems: List[str] = []

    async for msg in channel.history(limit=None, oldest_first=True):
        if msg.author.bot:
            continue
        name = msg.content.strip()
        if not WOW_NAME_RE.fullmatch(name):
            problems.append(f"<@{msg.author.id}> : message invalide (`{name[:40]}`)")
            continue
        folded = name.lower()
        if folded in name_owner and name_owner[folded] != msg.author.id:
            problems.append(
                f"<@{msg.author.id}> : `{name}` déjà déclaré par <@{name_owner[folded]}>"
            )
            continue
        if msg.author.id in by_user:
            old_name, _ = by_user[msg.author.id]
            name_owner.pop(old_name.lower(), None)
            problems.append(f"<@{msg.author.id}> : plusieurs messages valides dans #Main")
        by_user[msg.author.id] = (name, msg.id)
        name_owner[folded] = msg.author.id

    return {uid: value[0] for uid, value in by_user.items()}, problems


def can_use_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.guild_permissions.manage_guild:
        return True
    return bool(
        ADMIN_ROLE_ID
        and any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles)
    )


async def dm_unrecognized(guild: discord.Guild, signup: Signup) -> str:
    member = guild.get_member(signup.user_id)
    if member is None:
        try:
            member = await guild.fetch_member(signup.user_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return "membre introuvable"
    try:
        await member.send(DM_UNRECOGNIZED)
        return "DM envoyé"
    except discord.Forbidden:
        return "DM impossible"
    except discord.HTTPException:
        return "erreur DM"


def split_discord_text(text: str, max_len: int = 1900) -> List[str]:
    if len(text) <= max_len:
        return [text]
    chunks, current = [], ""
    for line in text.splitlines():
        candidate = line if not current else current + "\n" + line
        if len(candidate) > max_len:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


class ExportView(discord.ui.View):
    def __init__(self, export_text: str, filename: str = "Kromaddon_RaidHelper.txt"):
        super().__init__(timeout=600)
        self.export_text = export_text
        self.filename = filename

    @discord.ui.button(label="Export Kromaddon", style=discord.ButtonStyle.primary)
    async def export_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        file = discord.File(
            io.BytesIO(self.export_text.encode("utf-8")),
            filename=self.filename,
        )
        await interaction.response.send_message(
            "Export prêt à importer/copier dans Kromaddon :",
            file=file,
            ephemeral=True,
        )


async def run_rh_list(interaction: discord.Interaction, message: discord.Message):
    if not can_use_admin(interaction):
        await interaction.response.send_message(
            "Tu n'as pas la permission d'utiliser RH List.", ephemeral=True
        )
        return
    if not interaction.guild:
        await interaction.response.send_message("Serveur requis.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False, thinking=True)
    try:
        payload = await fetch_raid_helper_event(message.id)
        debug_event_structure(payload, message.id)
        signups, event_title = parse_event_payload(payload)
        if not signups:
            raise RuntimeError(
                "Aucune inscription Raid-Helper détectée dans la réponse API."
            )

        main_map, main_problems = await build_main_map(interaction.guild)
        grouped: Dict[str, List[str]] = defaultdict(list)
        export_items: List[str] = []
        unrecognized: List[Tuple[Signup, str]] = []

        for s in signups:
            main = main_map.get(s.user_id)
            if main:
                grouped[s.status].append(main)
                export_items.append(f"{main}:{STATUS_CODES.get(s.status, 'U')}")
            else:
                unrecognized.append((s, await dm_unrecognized(interaction.guild, s)))

        lines = [f"**{event_title}**", ""]
        for status in STATUS_ORDER:
            names = sorted(grouped.get(status, []), key=str.lower)
            if names:
                lines.append(f"**{STATUS_LABELS[status]}**")
                lines.extend(names)
                lines.append("")

        if unrecognized:
            lines.append("**⚠️ NON RECONNUS**")
            for s, dm_result in unrecognized:
                lines.append(f"<@{s.user_id}> — {s.display_name} — {dm_result}")
            lines.append("")

        if main_problems:
            lines.append("**⚠️ ANOMALIES #Main**")
            lines.extend(main_problems[:20])
            if len(main_problems) > 20:
                lines.append(f"... et {len(main_problems) - 20} autre(s).")
            lines.append("")

        lines.append(
            f"**Total :** {len(signups)} inscription(s) — "
            f"{len(signups)-len(unrecognized)} reconnue(s) — "
            f"{len(unrecognized)} non reconnue(s)"
        )

        export_text = "RH|" + "|".join(sorted(export_items, key=str.lower))
        chunks = split_discord_text("\n".join(lines))
        for index, chunk in enumerate(chunks):
            if index == len(chunks) - 1:
                await interaction.followup.send(
                    chunk,
                    view=ExportView(export_text),
                )
            else:
                await interaction.followup.send(chunk)
    except Exception as exc:
        if RH_DEBUG:
            print(repr(exc))
        await interaction.followup.send(f"❌ RH List : {exc}")


# =============================================================================
# OCR screenshot DKPARSE
# =============================================================================

OCR_ENGINE = None

OCR_IGNORE_WORDS = {
    "icecrown", "boss", "rank", "points", "point", "best", "dps", "dur",
    "kills", "kill", "date", "show", "other", "bosses", "hide", "snapshot",
    "snapshots", "found", "for", "this", "character", "server", "damage",
    "warrior", "rogue", "paladin", "deathknight", "hunter", "mage", "priest",
    "druid", "warlock", "shaman", "frost", "fire", "shadow", "balance",
    "feral", "combat", "fury", "unholy", "demonology", "marksmanship",
}


def get_ocr_engine():
    global OCR_ENGINE
    if OCR_ENGINE is None and RapidOCR is not None:
        OCR_ENGINE = RapidOCR()
    return OCR_ENGINE


def is_image_attachment(att: discord.Attachment) -> bool:
    ctype = (att.content_type or "").lower()
    name = (att.filename or "").lower()
    return (
        ctype.startswith("image/")
        or name.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))
    )


async def download_attachment_bytes(att: discord.Attachment) -> bytes:
    try:
        return await att.read(use_cached=True)
    except TypeError:
        return await att.read()


def _clean_ocr_token(raw: str) -> str:
    return re.sub(r"[^A-Za-z]", "", raw or "")


def _box_geometry(box: Any) -> Tuple[float, float, float, float]:
    try:
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        return min(xs), min(ys), max(xs), max(ys)
    except Exception:
        return 0.0, 0.0, 0.0, 0.0


def choose_character_from_ocr(
    ocr_result: Any,
    image_width: int,
    image_height: int,
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """
    Choose the character name from the UwU character table header.

    Browser screenshots may contain tab/address-bar text above the actual page.
    The old heuristic could therefore select something such as UwULogsAprX.
    We now anchor the name to the visible UwU table: same horizontal band as
    the `Icecrown` server label and above the Boss/Rank/Points header.
    """
    items = _ocr_result_items(ocr_result)
    candidates: List[Dict[str, Any]] = []
    if not items:
        return None, candidates

    def valid_name_item(it: Dict[str, Any]) -> bool:
        token = _clean_ocr_token(it["raw"])
        if not WOW_NAME_RE.fullmatch(token):
            return False
        low = token.lower()
        if low in OCR_IGNORE_WORDS:
            return False
        if "uwulog" in low or low.startswith("apogeebot"):
            return False
        if normalize_boss(token):
            return False
        return True

    raw_candidates = [it for it in items if valid_name_item(it)]

    # Locate table header row and the Icecrown server label.
    table_headers = [
        it for it in items
        if it["lower"].strip() in {"boss", "rank", "points", "date"}
        or "point" in it["lower"]
    ]
    table_y = None
    if table_headers:
        ys = sorted(it["cy"] for it in table_headers)
        table_y = ys[len(ys)//2]

    ice_cells = [
        it for it in items
        if "icecrown" in re.sub(r"[^a-z]", "", it["lower"])
        and (table_y is None or it["cy"] < table_y)
    ]
    ice = None
    if ice_cells:
        if table_y is not None:
            ice = min(ice_cells, key=lambda it: abs(it["cy"] - table_y))
        else:
            ice = max(ice_cells, key=lambda it: it["confidence"])

    def add_candidate(it: Dict[str, Any], anchor_score: float) -> None:
        token = _clean_ocr_token(it["raw"])
        score = it["confidence"] * 4.0 + anchor_score
        candidates.append({
            "name": token,
            "raw": it["raw"],
            "confidence": it["confidence"],
            "x": round(it["cx"], 1),
            "y": round(it["cy"], 1),
            "score": round(score, 4),
        })

    # Strong path: character is left of Icecrown and on almost the same line.
    if ice is not None:
        y_tol = max(16.0, image_height * 0.045)
        anchored = [
            it for it in raw_candidates
            if it["cx"] < ice["cx"]
            and abs(it["cy"] - ice["cy"]) <= y_tol
            and (table_y is None or it["cy"] < table_y)
        ]
        for it in anchored:
            y_prox = max(0.0, 2.5 - abs(it["cy"] - ice["cy"]) / max(y_tol, 1.0) * 2.5)
            x_prox = max(0.0, 1.5 - abs(it["cx"] - image_width * 0.28) / max(image_width, 1) * 2.0)
            add_candidate(it, 5.0 + y_prox + x_prox)

    # Second path: immediately above the Boss/Rank/Points table header.
    if not candidates and table_y is not None:
        header_band = [
            it for it in raw_candidates
            if image_height * 0.03 <= it["cy"] < table_y
            and (table_y - it["cy"]) <= max(90.0, image_height * 0.18)
            and it["cx"] < image_width * 0.62
        ]
        for it in header_band:
            v = max(0.0, 3.0 - (table_y - it["cy"]) / max(image_height, 1) * 8.0)
            x = max(0.0, 1.5 - abs(it["cx"] - image_width * 0.28) / max(image_width, 1) * 2.0)
            add_candidate(it, 3.0 + v + x)

    # Conservative fallback. Penalize browser chrome at the very top.
    if not candidates:
        for it in raw_candidates:
            if image_height > 0 and it["cy"] > image_height * 0.42:
                continue
            y_score = 1.0 - min(max(it["cy"] / max(image_height, 1), 0.0), 1.0)
            x_target = image_width * 0.25
            x_distance = abs(it["cx"] - x_target) / max(image_width, 1)
            x_score = max(0.0, 1.0 - x_distance)
            chrome_penalty = 2.5 if image_height > 0 and it["cy"] < image_height * 0.035 else 0.0
            score = it["confidence"] * 3.0 + y_score * 2.2 + x_score * 1.2 - chrome_penalty
            token = _clean_ocr_token(it["raw"])
            candidates.append({
                "name": token,
                "raw": it["raw"],
                "confidence": it["confidence"],
                "x": round(it["cx"], 1),
                "y": round(it["cy"], 1),
                "score": round(score, 4),
            })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    if not candidates or candidates[0]["confidence"] < 0.35:
        return None, candidates
    return candidates[0]["name"], candidates


async def extract_character_from_screenshot(
    message: discord.Message,
) -> Tuple[Optional[str], str]:
    image_attachments = [a for a in message.attachments if is_image_attachment(a)]

    if not image_attachments:
        return None, "Aucun screenshot image joint au message."

    if RapidOCR is None or Image is None or np is None:
        missing = []
        if RapidOCR is None:
            missing.append("rapidocr_onnxruntime")
        if Image is None:
            missing.append("Pillow")
        if np is None:
            missing.append("numpy")
        return (
            None,
            "OCR local indisponible (" + ", ".join(missing) + "). "
            "Installer : pip install rapidocr_onnxruntime pillow numpy",
        )

    engine = get_ocr_engine()
    if engine is None:
        return None, "Impossible d'initialiser RapidOCR."

    all_candidates: List[Dict[str, Any]] = []

    for att in image_attachments[:4]:
        try:
            raw = await download_attachment_bytes(att)
            image = Image.open(io.BytesIO(raw)).convert("RGB")
            width, height = image.size
            arr = np.array(image)

            result, _elapsed = engine(arr)
            name, candidates = choose_character_from_ocr(result, width, height)

            for c in candidates[:10]:
                c["attachment"] = att.filename
            all_candidates.extend(candidates[:10])

            dkp_debug(
                "OCR SCREENSHOT",
                {
                    "attachment": att.filename,
                    "size": [width, height],
                    "selected": name,
                    "candidates": candidates[:10],
                },
            )

            if name:
                return name, f"Nom lu sur le screen : {name}"

        except Exception as exc:
            dkp_debug(
                "OCR SCREENSHOT ECHEC",
                {"attachment": att.filename, "error": repr(exc)},
            )

    dkp_debug("OCR CANDIDATS GLOBAUX", all_candidates[:20])
    return None, "Le nom du personnage n'a pas pu être lu de façon fiable sur le screenshot."



def _ocr_result_items(ocr_result: Any) -> List[Dict[str, Any]]:
    """Normalize RapidOCR output into positioned text items."""
    if isinstance(ocr_result, tuple) and ocr_result:
        ocr_result = ocr_result[0]
    if not isinstance(ocr_result, list):
        return []

    out: List[Dict[str, Any]] = []
    for item in ocr_result:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        box = item[0]
        raw = str(item[1] or "").strip()
        try:
            confidence = float(item[2]) if len(item) >= 3 else 0.5
        except Exception:
            confidence = 0.5
        x1, y1, x2, y2 = _box_geometry(box)
        out.append({
            "raw": raw,
            "lower": raw.lower(),
            "confidence": confidence,
            "x1": x1,
            "y1": y1,
            "x2": x2,
            "y2": y2,
            "cx": (x1 + x2) / 2.0,
            "cy": (y1 + y2) / 2.0,
            "h": max(1.0, y2 - y1),
        })
    return out


def _ocr_percent_value(raw: str) -> Optional[float]:
    """Read one OCR cell as a 0..100 decimal percentage-like value."""
    text = (raw or "").strip().replace("%", "").replace(",", ".")
    # Points in the UwU table are decimals such as 90.98 / 94.50 / 81.15.
    m = re.fullmatch(r"\s*(\d{1,3}(?:\.\d{1,3})?)\s*", text)
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    return value if 0.0 <= value <= 100.0 else None


def parse_requested_points_from_ocr(
    ocr_result: Any,
    requested_bosses: List[str],
    image_width: int,
    image_height: int,
) -> Dict[str, float]:
    """
    Read the orange/red `Points` column from the UwU character screenshot.

    This is the DKPARSE value the user sees on the screenshot. Fight pages are
    used only to prove participation/date/spec; they must not invent/replace
    this value (the old parser could incorrectly pick unrelated 100 values).
    """
    if not requested_bosses:
        return {}

    items = _ocr_result_items(ocr_result)
    if not items:
        return {}

    # Locate the table header. RapidOCR usually returns `Points` as one cell.
    point_headers = [
        it for it in items
        if "point" in it["lower"]
        and (image_height <= 0 or it["cy"] <= image_height * 0.45)
    ]
    points_x: Optional[float] = None
    if point_headers:
        # Prefer the highest-confidence header near the middle of the table.
        header = max(point_headers, key=lambda it: (it["confidence"], -abs(it["cx"] - image_width * 0.5)))
        points_x = header["cx"]

    # Fallback: infer Points between Rank and Best Dps headers.
    if points_x is None:
        rank_headers = [it for it in items if it["lower"].strip() == "rank"]
        best_headers = [it for it in items if "best" in it["lower"]]
        if rank_headers and best_headers:
            rx = max(rank_headers, key=lambda it: it["confidence"])["cx"]
            bx = max(best_headers, key=lambda it: it["confidence"])["cx"]
            points_x = (rx + bx) / 2.0

    if points_x is None:
        dkp_debug("OCR POINTS HEADER INTROUVABLE", {"requested_bosses": requested_bosses})
        return {}

    found: Dict[str, float] = {}
    for boss in requested_bosses:
        boss_cells = [
            it for it in items
            if normalize_boss(it["raw"]) == boss
        ]
        if not boss_cells:
            continue
        boss_cell = max(boss_cells, key=lambda it: it["confidence"])
        row_tol = max(10.0, boss_cell["h"] * 0.9, image_height * 0.012)

        candidates: List[Tuple[float, float, Dict[str, Any]]] = []
        for it in items:
            if abs(it["cy"] - boss_cell["cy"]) > row_tol:
                continue
            value = _ocr_percent_value(it["raw"])
            if value is None:
                continue
            # Distance to the Points-column header is the primary criterion.
            xdist = abs(it["cx"] - points_x)
            candidates.append((xdist, -it["confidence"], it))

        if not candidates:
            continue
        candidates.sort(key=lambda t: (t[0], t[1]))
        best_item = candidates[0][2]
        value = _ocr_percent_value(best_item["raw"])
        if value is not None:
            found[boss] = value

    dkp_debug(
        "OCR POINTS DKPARSE",
        {
            "requested_bosses": requested_bosses,
            "points_x": round(points_x, 1),
            "values": found,
        },
    )
    return found


async def extract_dkparse_screenshot_data(
    message: discord.Message,
    requested_bosses: List[str],
) -> Tuple[Optional[str], str, Dict[str, float]]:
    """Read character name + requested boss Points values in a single OCR pass."""
    image_attachments = [a for a in message.attachments if is_image_attachment(a)]
    if not image_attachments:
        return None, "Aucun screenshot image joint au message.", {}

    if RapidOCR is None or Image is None or np is None:
        missing = []
        if RapidOCR is None:
            missing.append("rapidocr_onnxruntime")
        if Image is None:
            missing.append("Pillow")
        if np is None:
            missing.append("numpy")
        return (
            None,
            "OCR local indisponible (" + ", ".join(missing) + "). "
            "Installer : pip install rapidocr_onnxruntime pillow numpy",
            {},
        )

    engine = get_ocr_engine()
    if engine is None:
        return None, "Impossible d'initialiser RapidOCR.", {}

    all_candidates: List[Dict[str, Any]] = []
    for att in image_attachments[:4]:
        try:
            raw = await download_attachment_bytes(att)
            image = Image.open(io.BytesIO(raw)).convert("RGB")
            width, height = image.size
            result, _elapsed = engine(np.array(image))
            name, candidates = choose_character_from_ocr(result, width, height)
            points = parse_requested_points_from_ocr(result, requested_bosses, width, height)
            for c in candidates[:10]:
                c["attachment"] = att.filename
            all_candidates.extend(candidates[:10])
            dkp_debug(
                "OCR DKPARSE COMPLET",
                {
                    "attachment": att.filename,
                    "size": [width, height],
                    "selected": name,
                    "points": points,
                    "candidates": candidates[:10],
                },
            )
            if name:
                return name, f"Nom lu sur le screen : {name}", points
        except Exception as exc:
            dkp_debug("OCR DKPARSE ECHEC", {"attachment": att.filename, "error": repr(exc)})

    dkp_debug("OCR CANDIDATS GLOBAUX", all_candidates[:20])
    return None, "Le nom du personnage n'a pas pu être lu de façon fiable sur le screenshot.", {}



def _ocr_date_value(raw: str) -> Optional[datetime]:
    """Parse a Best Log date cell such as 09-08-26 / 12/08/2026."""
    text = (raw or "").strip()
    text = text.replace(".", "-").replace("/", "-")
    m = re.search(r"(?<!\d)(\d{1,2})-(\d{1,2})-(\d{2,4})(?!\d)", text)
    if not m:
        return None
    day, month, year = map(int, m.groups())
    if year < 100:
        year += 2000
    try:
        return datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None


def _points_column_x(items: List[Dict[str, Any]], image_width: int, image_height: int) -> Optional[float]:
    headers = [
        it for it in items
        if "point" in it["lower"]
        and (image_height <= 0 or it["cy"] <= image_height * 0.50)
    ]
    if headers:
        return max(
            headers,
            key=lambda it: (it["confidence"], -abs(it["cx"] - image_width * 0.55)),
        )["cx"]

    rank_headers = [it for it in items if it["lower"].strip() == "rank"]
    best_headers = [it for it in items if "best" in it["lower"]]
    if rank_headers and best_headers:
        rx = max(rank_headers, key=lambda it: it["confidence"])["cx"]
        bx = max(best_headers, key=lambda it: it["confidence"])["cx"]
        return (rx + bx) / 2.0
    return None


def _infer_wow_class_from_ocr_items(items: List[Dict[str, Any]]) -> str:
    """Read the WoW class from the whole UwU character screenshot."""
    joined = " ".join(it["raw"] for it in items)
    low = joined.lower()
    padded = f" {low} "

    class_hints = [
        ("deathknight", ("death knight", "deathknight", "scourgelord")),
        ("warlock", (" warlock ", "dark coven")),
        ("warrior", (" warrior ", "ymirjar lord")),
        ("paladin", (" paladin ", "lightsworn")),
        ("hunter", (" hunter ", "ahn'kahar", "ahn kahar")),
        ("priest", (" priest ", "crimson acolyte")),
        ("druid", (" druid ", "lasherweave")),
        ("rogue", (" rogue ", "shadowblade")),
        ("mage", (" mage ", "bloodmage")),
        ("shaman", (" shaman ", "frost witch")),
    ]
    for cls, hints in class_hints:
        if any(h in padded for h in hints):
            return cls
    return ""


# UwU uses the normal talent-tree order for ?spec=1/2/3.
# Only specs tracked by the guild are mapped to Kromaddon labels.
TRACKED_SPEC_BY_CLASS_INDEX = {
    ("warrior", 2): "Fwar",
    ("rogue", 2): "Combat",
    ("paladin", 3): "Ret",
    ("deathknight", 3): "UH",
    ("druid", 1): "Boomie",
    ("druid", 2): "FeralDPS",
    ("mage", 2): "MageFeu",
    ("priest", 3): "SP",
    ("warlock", 2): "Démono",
    ("hunter", 2): "MM",
}


def _spec_index_from_ocr_url(items: List[Dict[str, Any]]) -> Optional[int]:
    """Read UwU's ?spec=N parameter from the browser address bar."""
    if not items:
        return None

    ordered = sorted(items, key=lambda x: (x["cy"], x["cx"]))
    joined = " ".join(it["raw"] for it in ordered)
    compact = re.sub(r"\s+", "", joined.lower())

    for text in (compact, joined.lower()):
        for pattern in (
            r"(?:[?&]|^)spec[=:]?([123])(?:[&#/]|$)",
            r"\bspec\s*[=:]?\s*([123])\b",
        ):
            m = re.search(pattern, text)
            if m:
                return int(m.group(1))

    spec_cells = [it for it in items if "spec" in it["lower"]]
    for spec_cell in spec_cells:
        nearby = [
            it for it in items
            if it["cx"] >= spec_cell["cx"]
            and it["cx"] - spec_cell["cx"] <= 180
            and abs(it["cy"] - spec_cell["cy"]) <= 18
        ]
        nearby.sort(key=lambda it: it["cx"])
        text = " ".join(it["raw"] for it in nearby)
        m = re.search(r"spec\D*([123])", text, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None


def _selected_spec_index_from_image(
    image_array: Any,
    items: List[Dict[str, Any]],
    image_width: int,
    image_height: int,
) -> Optional[int]:
    """
    Detect the selected UwU spec icon in the top-right.
    The three icons are 1/2/3 from left to right and the active one has a
    bright purple border.
    """
    if image_array is None or np is None or image_width <= 0 or image_height <= 0:
        return None

    try:
        arr = image_array
        if getattr(arr, "ndim", 0) != 3 or arr.shape[2] < 3:
            return None

        ice_cells = [
            it for it in items
            if "icecrown" in re.sub(r"[^a-z]", "", it["lower"])
        ]
        if not ice_cells:
            return None

        table_headers = [
            it for it in items
            if it["lower"].strip() in {"boss", "rank", "points", "date"}
            or "point" in it["lower"]
        ]
        table_y = None
        if table_headers:
            ys = sorted(it["cy"] for it in table_headers)
            table_y = ys[len(ys) // 2]

        usable_ice = [
            it for it in ice_cells
            if table_y is None or it["cy"] < table_y
        ] or ice_cells

        ice = (
            min(usable_ice, key=lambda it: abs(it["cy"] - table_y))
            if table_y is not None
            else max(usable_ice, key=lambda it: it["confidence"])
        )

        y0 = max(0, int(ice["cy"]) - max(28, int(image_height * 0.030)))
        y1 = min(image_height, int(ice["cy"]) + max(38, int(image_height * 0.045)))
        x0 = max(0, int(image_width * 0.875))
        x1 = min(image_width, int(image_width * 0.997))
        if x1 - x0 < 60 or y1 - y0 < 20:
            return None

        crop = arr[y0:y1, x0:x1, :3]
        slot_w = crop.shape[1] / 3.0
        scores: List[int] = []

        for idx in range(3):
            a = int(round(idx * slot_w))
            b = int(round((idx + 1) * slot_w))
            slot = crop[:, a:b, :]
            r = slot[:, :, 0].astype(np.int16)
            g = slot[:, :, 1].astype(np.int16)
            blue = slot[:, :, 2].astype(np.int16)

            purple = (
                (r >= 70)
                & (blue >= 95)
                & (g <= 105)
                & (r >= g + 18)
                & (blue >= g + 28)
            )
            scores.append(int(purple.sum()))

        best_i = max(range(3), key=lambda i: scores[i])
        best = scores[best_i]
        second = sorted(scores, reverse=True)[1]

        selected = best_i + 1 if best >= 18 and best >= second * 1.35 else None
        dkp_debug(
            "OCR SPEC ICON",
            {"scores": scores, "selected_index": selected, "crop": [x0, y0, x1, y1]},
        )
        return selected
    except Exception as exc:
        dkp_debug("OCR SPEC ICON ECHEC", {"error": repr(exc)})
        return None


def _infer_dkparse_spec_from_ocr_items(
    items: List[Dict[str, Any]],
    image_array: Any = None,
    image_width: int = 0,
    image_height: int = 0,
) -> str:
    """
    Active spec priority:
      1) class + UwU URL ?spec=N
      2) class + selected top-right spec icon
      3) class + talent distribution fallback

    We deliberately do NOT scan the entire page for any spec word first,
    because UwU can display both dual-spec builds on the left.
    """
    if not items:
        return ""

    wow_class = _infer_wow_class_from_ocr_items(items)

    url_spec_index = _spec_index_from_ocr_url(items)
    if wow_class and url_spec_index:
        label = TRACKED_SPEC_BY_CLASS_INDEX.get((wow_class, url_spec_index), "")
        dkp_debug(
            "OCR SPEC URL",
            {"class": wow_class, "spec_index": url_spec_index, "label": label},
        )
        if label:
            return label

    icon_spec_index = _selected_spec_index_from_image(
        image_array, items, image_width, image_height
    )
    if wow_class and icon_spec_index:
        label = TRACKED_SPEC_BY_CLASS_INDEX.get((wow_class, icon_spec_index), "")
        dkp_debug(
            "OCR SPEC ICONE",
            {"class": wow_class, "spec_index": icon_spec_index, "label": label},
        )
        if label:
            return label

    joined = " ".join(it["raw"] for it in items)
    builds: List[Tuple[int, int, int]] = []
    for m in re.finditer(
        r"(?<!\d)(\d{1,2})\s*[/|]\s*(\d{1,2})\s*[/|]\s*(\d{1,2})(?!\d)",
        joined,
    ):
        vals = tuple(int(x) for x in m.groups())
        if sum(vals) <= 80 and max(vals) >= 30:
            builds.append(vals)

    if wow_class and builds:
        candidates: List[str] = []
        for vals in builds:
            tree = max(range(3), key=lambda i: vals[i]) + 1
            label = TRACKED_SPEC_BY_CLASS_INDEX.get((wow_class, tree), "")
            if label:
                candidates.append(label)
        unique = list(dict.fromkeys(candidates))
        if len(unique) == 1:
            return unique[0]

    return ""


def parse_all_dkparse_rows_from_ocr(
    ocr_result: Any,
    image_width: int,
    image_height: int,
    attachment: str = "",
    image_array: Any = None,
) -> Tuple[str, List[ScreenParseRow]]:
    """
    Parse ALL ICC DKPARSE rows visible in the screenshot.

    The Discord message text is deliberately not consulted here.
    """
    items = _ocr_result_items(ocr_result)
    if not items:
        return "", []

    spec = _infer_dkparse_spec_from_ocr_items(
        items,
        image_array=image_array,
        image_width=image_width,
        image_height=image_height,
    )
    points_x = _points_column_x(items, image_width, image_height)
    if points_x is None:
        dkp_debug("OCR SCREEN-ONLY POINTS HEADER INTROUVABLE")
        return spec, []

    # One best OCR cell per canonical boss.
    boss_cells: Dict[str, Dict[str, Any]] = {}
    for it in items:
        boss = normalize_boss(it["raw"])
        if not boss or boss in DKPARSE_EXCLUDED_BOSSES:
            continue
        old = boss_cells.get(boss)
        if old is None or it["confidence"] > old["confidence"]:
            boss_cells[boss] = it

    rows: List[ScreenParseRow] = []
    for boss, boss_cell in boss_cells.items():
        row_tol = max(9.0, boss_cell["h"] * 0.95, image_height * 0.013)
        row_items = [it for it in items if abs(it["cy"] - boss_cell["cy"]) <= row_tol]
        row_items.sort(key=lambda it: it["cx"])

        # Points: closest numeric cell to the Points header X coordinate.
        point_candidates: List[Tuple[float, float, Dict[str, Any]]] = []
        for it in row_items:
            value = _ocr_percent_value(it["raw"])
            if value is None:
                continue
            point_candidates.append((abs(it["cx"] - points_x), -it["confidence"], it))
        if not point_candidates:
            continue
        point_candidates.sort(key=lambda t: (t[0], t[1]))
        point_item = point_candidates[0][2]
        parse_value = _ocr_percent_value(point_item["raw"])
        if parse_value is None:
            continue

        # Date: preferably a single OCR cell, otherwise the whole reconstructed row.
        best_date: Optional[datetime] = None
        date_raw = ""
        for it in reversed(row_items):
            dt = _ocr_date_value(it["raw"])
            if dt:
                best_date = dt
                date_raw = it["raw"]
                break
        if best_date is None:
            row_text = " ".join(it["raw"] for it in row_items)
            best_date = _ocr_date_value(row_text)
            if best_date:
                date_raw = row_text

        rows.append(
            ScreenParseRow(
                boss=boss,
                parse=parse_value,
                spec=spec,
                best_date=best_date,
                date_raw=date_raw,
                attachment=attachment,
            )
        )

    rows.sort(key=lambda r: (r.best_date or datetime(1970, 1, 1, tzinfo=timezone.utc), r.boss), reverse=True)
    dkp_debug(
        "OCR SCREEN-ONLY ROWS",
        {
            "spec": spec,
            "rows": [
                {
                    "boss": r.boss,
                    "parse": r.parse,
                    "date": r.best_date.date().isoformat() if r.best_date else None,
                    "attachment": r.attachment,
                }
                for r in rows
            ],
        },
    )
    return spec, rows


async def extract_dkparse_screen_only(
    message: discord.Message,
) -> Tuple[Optional[str], str, str, List[ScreenParseRow]]:
    """Read character + all DKPARSE rows from screenshots; ignore message text entirely."""
    image_attachments = [a for a in message.attachments if is_image_attachment(a)]
    if not image_attachments:
        return None, "Aucun screenshot image joint au message.", "", []

    if RapidOCR is None or Image is None or np is None:
        missing = []
        if RapidOCR is None:
            missing.append("rapidocr_onnxruntime")
        if Image is None:
            missing.append("Pillow")
        if np is None:
            missing.append("numpy")
        return (
            None,
            "OCR local indisponible (" + ", ".join(missing) + "). "
            "Installer : pip install rapidocr_onnxruntime pillow numpy",
            "",
            [],
        )

    engine = get_ocr_engine()
    if engine is None:
        return None, "Impossible d'initialiser RapidOCR.", "", []

    character: Optional[str] = None
    spec = ""
    rows: List[ScreenParseRow] = []
    all_candidates: List[Dict[str, Any]] = []

    for att in image_attachments[:4]:
        try:
            raw = await download_attachment_bytes(att)
            image = Image.open(io.BytesIO(raw)).convert("RGB")
            width, height = image.size
            image_array = np.array(image)
            result, _elapsed = engine(image_array)

            name, candidates = choose_character_from_ocr(result, width, height)
            if name and not character:
                character = name
            for c in candidates[:10]:
                c["attachment"] = att.filename
            all_candidates.extend(candidates[:10])

            img_spec, img_rows = parse_all_dkparse_rows_from_ocr(
                result,
                width,
                height,
                att.filename,
                image_array=image_array,
            )
            if img_spec and not spec:
                spec = img_spec
            rows.extend(img_rows)

            dkp_debug(
                "OCR SCREEN-ONLY COMPLET",
                {
                    "attachment": att.filename,
                    "selected": name,
                    "spec": img_spec,
                    "row_count": len(img_rows),
                },
            )
        except Exception as exc:
            dkp_debug("OCR SCREEN-ONLY ECHEC", {"attachment": att.filename, "error": repr(exc)})

    # Dedupe exact OCR duplicates across multiple attachments.
    unique: Dict[Tuple[str, Optional[datetime], int], ScreenParseRow] = {}
    for row in rows:
        key = (row.boss, row.best_date, int(round(row.parse * 100)))
        unique[key] = row
    rows = list(unique.values())

    if not character:
        dkp_debug("OCR CANDIDATS GLOBAUX", all_candidates[:20])
        return None, "Le nom du personnage n'a pas pu être lu de façon fiable sur le screenshot.", spec, rows

    return character, f"Nom lu sur le screen : {character}", spec, rows


def _screen_date_within_post_window(best_date: datetime, post_time: datetime) -> bool:
    delta = (post_time.astimezone(timezone.utc).date() - best_date.date()).days
    return -1 <= delta <= DKPARSE_MAX_DAYS


def _lograid_date_set(reports: List[UwuReport]) -> set:
    return {r.report_date.date() for r in reports if r.report_date is not None}

# =============================================================================
# DKPARSE
# =============================================================================

def canonical_uwu_url(url: str) -> str:
    """Remove query/fragment and normalize a report URL for duplicate handling."""
    url = html_lib.unescape(url).rstrip(".,;!?)>'\"")
    parts = urlsplit(url)
    path = parts.path
    if not path.endswith("/"):
        path += "/"
    return urlunsplit(("https", "uwu-logs.xyz", path, "", ""))


def report_date_from_url(url: str) -> Optional[datetime]:
    m = REPORT_DATE_RE.search(url)
    if not m:
        return None
    yy, mm, dd = map(int, m.groups())
    try:
        return datetime(2000 + yy, mm, dd, tzinfo=timezone.utc)
    except ValueError:
        return None


def extract_uwu_urls(text: str) -> List[str]:
    out, seen = [], set()
    for raw in UWU_URL_RE.findall(text or ""):
        url = canonical_uwu_url(raw)
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def normalize_spec(raw: str) -> str:
    """Recognize a tracked spec without substring false positives (ex: SP in spell)."""
    s = re.sub(r"\s+", " ", (raw or "").strip().lower())
    for alias, label in sorted(DKPARSE_SPECS.items(), key=lambda kv: -len(kv[0])):
        pattern = r"(?<![a-z0-9])" + re.escape(alias.lower()) + r"(?![a-z0-9])"
        if re.search(pattern, s):
            return label
    return ""


def normalize_boss(raw: str) -> str:
    s = re.sub(r"[-_]+", " ", (raw or "").strip().lower())
    s = re.sub(r"\s+", " ", s)
    if s in BOSS_ALIASES:
        return BOSS_ALIASES[s]
    for alias, boss in sorted(BOSS_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if alias in s:
            return boss
    return ""


def dkp_for_parse(parse_value: float) -> int:
    for threshold, amount in DKPARSE_BRACKETS:
        if parse_value >= threshold:
            return amount
    return 0


def html_to_text(raw_html: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", raw_html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def json_candidates_from_html(raw_html: str) -> List[Any]:
    """Parse JSON-ish script blocks when UwU embeds raid data client-side."""
    found: List[Any] = []
    scripts = re.findall(r"(?is)<script[^>]*>(.*?)</script>", raw_html)
    for script in scripts:
        script = html_lib.unescape(script.strip())
        candidates = []
        if script.startswith("{") or script.startswith("["):
            candidates.append(script)
        for m in re.finditer(r"(?s)(?:=|:)\s*(\{.*?\}|\[.*?\])\s*;", script):
            candidates.append(m.group(1))
        for candidate in candidates[:30]:
            try:
                found.append(json.loads(candidate))
            except Exception:
                pass
    return found


def walk_dicts(obj: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from walk_dicts(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from walk_dicts(value)


def _numeric(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = re.search(r"(?<!\d)(\d{1,3}(?:[.,]\d+)?)\s*%?", value)
        if m:
            try:
                return float(m.group(1).replace(",", "."))
            except ValueError:
                return None
    return None


def parse_hits_from_json(
    objects: List[Any], character: str, report_url: str, report_date: Optional[datetime]
) -> List[ParseHit]:
    hits: List[ParseHit] = []
    char_l = character.lower()

    name_keys = ("name", "player", "playerName", "character", "characterName")
    boss_keys = ("boss", "bossName", "fight", "fightName", "encounter", "encounterName")
    spec_keys = ("spec", "specName", "specialization", "specialisation")
    parse_keys = (
        "parse", "percent", "percentage", "performance", "performancePercent",
        "rankPercent", "dpsPercent", "points",
    )

    for obj in objects:
        for d in walk_dicts(obj):
            values_lower = " ".join(str(v).lower() for v in d.values() if isinstance(v, str))
            name = str(get_first(d, name_keys, ""))
            if char_l not in name.lower() and char_l not in values_lower:
                continue

            boss_raw = str(get_first(d, boss_keys, ""))
            boss = normalize_boss(boss_raw or values_lower)
            if not boss or boss in DKPARSE_EXCLUDED_BOSSES:
                continue

            spec_raw = str(get_first(d, spec_keys, ""))
            spec = normalize_spec(spec_raw or values_lower)

            parse_value = None
            for key in parse_keys:
                if key in d:
                    n = _numeric(d[key])
                    if n is not None and 0 <= n <= 100:
                        parse_value = n
                        break
            if parse_value is None:
                continue

            hits.append(
                ParseHit(
                    character=character,
                    boss=boss,
                    parse=parse_value,
                    spec=spec,
                    report_url=report_url,
                    report_date=report_date,
                    evidence="JSON UwU",
                )
            )
    return hits


def parse_hits_from_text(
    raw_html: str, character: str, report_url: str, report_date: Optional[datetime]
) -> List[ParseHit]:
    """
    Fallback for UwU HTML.

    We only accept a value if character + boss + percentage/rank-like value are
    physically close in the returned page. This intentionally prefers false
    negatives over paying a wrong DKPARSE.
    """
    text = html_to_text(raw_html)
    lower = text.lower()
    char_l = character.lower()
    hits: List[ParseHit] = []

    positions = [m.start() for m in re.finditer(re.escape(char_l), lower)]
    for pos in positions:
        window = text[max(0, pos - 700): min(len(text), pos + 1200)]
        window_l = window.lower()

        boss = ""
        boss_pos = 10**9
        for alias, canonical in BOSS_ALIASES.items():
            p = window_l.find(alias)
            if p >= 0 and p < boss_pos:
                boss, boss_pos = canonical, p
        if not boss or boss in DKPARSE_EXCLUDED_BOSSES:
            continue

        spec = normalize_spec(window)

        # Strong labels first.
        patterns = [
            r"(?:parse|performance|points?|rank)\s*[:=]?\s*(\d{2,3}(?:[.,]\d+)?)\s*%?",
            r"(\d{2,3}(?:[.,]\d+)?)\s*%\s*(?:parse|performance|points?|rank)?",
        ]
        values: List[float] = []
        for pattern in patterns:
            for m in re.finditer(pattern, window, re.IGNORECASE):
                try:
                    value = float(m.group(1).replace(",", "."))
                except ValueError:
                    continue
                if 0 <= value <= 100:
                    values.append(value)

        # DKPARSE only starts at 70, which also removes many unrelated values.
        values = [v for v in values if v >= 70]
        if not values:
            continue

        # Use the largest candidate only inside this tightly bounded evidence
        # window. If UwU exposes structured JSON, that path is preferred.
        value = max(values)
        hits.append(
            ParseHit(
                character=character,
                boss=boss,
                parse=value,
                spec=spec,
                report_url=report_url,
                report_date=report_date,
                evidence="HTML UwU",
            )
        )
    return hits


def boss_from_uwu_url(url: str) -> str:
    """Return the canonical boss name from an UwU ?boss=... query."""
    try:
        query = parse_qs(urlsplit(html_lib.unescape(url)).query)
        raw = (query.get("boss") or [""])[0]
    except Exception:
        raw = ""
    return normalize_boss(raw)


def extract_uwu_fight_urls(raw_html: str, report_url: str) -> List[str]:
    """
    UwU's report landing page does not necessarily contain the raid roster.
    Individual fight pages are linked through ?boss=... URLs.

    Collect same-report fight URLs from href/data attributes and JS strings.
    """
    base = canonical_uwu_url(report_url)
    base_parts = urlsplit(base)
    candidates: List[str] = []

    # HTML attributes.
    attr_pattern = r"""(?is)(?:href|data-url|data-href|value)\s*=\s*["']([^"']+)["']"""
    for m in re.finditer(attr_pattern, raw_html):
        candidates.append(html_lib.unescape(m.group(1).strip()))

    # JS/JSON strings containing report queries.
    js_pattern = r"""(?is)["']([^"']*(?:\?|&amp;|&)boss=[^"']+)["']"""
    for m in re.finditer(js_pattern, raw_html):
        candidates.append(html_lib.unescape(m.group(1).strip()))

    out: List[str] = []
    seen = set()

    for raw in candidates:
        if not raw or "boss=" not in raw.lower():
            continue

        absolute = urljoin(base, raw)
        parts = urlsplit(absolute)

        if parts.netloc.lower() not in {"uwu-logs.xyz", "www.uwu-logs.xyz"}:
            continue

        if parts.path.rstrip("/") != base_parts.path.rstrip("/"):
            continue

        url = urlunsplit(("https", "uwu-logs.xyz", parts.path, parts.query, ""))
        if url not in seen:
            seen.add(url)
            out.append(url)

    return out


def _candidate_parse_values(text: str) -> List[float]:
    """
    Extract plausible performance/parse values.

    Strong labelled values are preferred. As a fallback for an UwU player row,
    decimal/percent values in the DKPARSE range are accepted.
    """
    strong: List[float] = []
    fallback: List[float] = []

    labelled_patterns = [
        r"(?:parse|performance|points?|rank)\s*[:=]?\s*(\d{2,3}(?:[.,]\d+)?)\s*%?",
        r"(\d{2,3}(?:[.,]\d+)?)\s*%\s*(?:parse|performance|points?|rank)?",
    ]

    for pattern in labelled_patterns:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            try:
                value = float(m.group(1).replace(",", "."))
            except ValueError:
                continue
            if 70 <= value <= 100:
                strong.append(value)

    for m in re.finditer(r"(?<!\d)(\d{2,3}(?:[.,]\d{1,3})?)(?!\d)", text):
        raw = m.group(1)
        try:
            value = float(raw.replace(",", "."))
        except ValueError:
            continue
        if not 70 <= value <= 100:
            continue

        around = text[max(0, m.start() - 2): m.end() + 2]
        if "." in raw or "," in raw or "%" in around:
            fallback.append(value)

    return strong or fallback



def _edit_distance_max1(a: str, b: str) -> int:
    """Distance <=1, with adjacent transposition counted as one edit."""
    a = (a or "").lower()
    b = (b or "").lower()
    if a == b:
        return 0
    if abs(len(a) - len(b)) > 1:
        return 2
    if len(a) == len(b):
        diffs = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        if len(diffs) == 1:
            return 1
        if (len(diffs) == 2 and diffs[1] == diffs[0] + 1
                and a[diffs[0]] == b[diffs[1]]
                and a[diffs[1]] == b[diffs[0]]):
            return 1
        return 2
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    i = j = edits = 0
    while i < len(short) and j < len(long_):
        if short[i] == long_[j]:
            i += 1
            j += 1
        else:
            edits += 1
            j += 1
            if edits > 1:
                return 2
    return 1


def _row_matches_character(row_html: str, character: str) -> bool:
    """Exact name first; distance-1 matching only as fallback."""
    if re.search(rf"(?i)(?<![A-Za-z]){re.escape(character)}(?![A-Za-z])", row_html):
        return True
    visible = html_to_text(row_html)
    for token in re.findall(r"\b[A-Za-z]{2,12}\b", visible):
        if token.lower() in OCR_IGNORE_WORDS:
            continue
        if _edit_distance_max1(character, token) <= 1:
            return True
    return False


def parse_hits_from_fight_page(
    raw_html: str,
    character: str,
    fight_url: str,
    report_date: Optional[datetime],
) -> List[ParseHit]:
    """
    Parse a selected UwU fight page.

    Boss is read directly from ?boss=... in the fight URL, so the parser no
    longer requires boss + character + parse to appear together on the landing
    page.
    """
    char_l = character.lower()

    boss = boss_from_uwu_url(fight_url)
    if not boss or boss in DKPARSE_EXCLUDED_BOSSES:
        return []

    hits: List[ParseHit] = []

    rows = re.findall(r"(?is)<tr\b[^>]*>.*?</tr>", raw_html)
    relevant_rows = [
        row for row in rows
        if re.search(rf"(?i)(?<![A-Za-z]){re.escape(character)}(?![A-Za-z])", row)
    ]
    if not relevant_rows:
        relevant_rows = [row for row in rows if _row_matches_character(row, character)]

    fragments: List[str] = relevant_rows[:10]
    if not fragments:
        for m in re.finditer(re.escape(character), raw_html, re.IGNORECASE):
            fragments.append(
                raw_html[max(0, m.start() - 1800): min(len(raw_html), m.end() + 2600)]
            )
            if len(fragments) >= 10:
                break

    for fragment in fragments:
        visible = html_to_text(fragment)
        spec = normalize_spec(visible + " " + fragment)
        values = _candidate_parse_values(visible + " " + fragment)
        if not values:
            continue

        value = max(values)
        hits.append(
            ParseHit(
                character=character,
                boss=boss,
                parse=value,
                spec=spec,
                report_url=fight_url,
                report_date=report_date,
                evidence="UwU fight page",
            )
        )

    return hits



def parse_presence_from_fight_page(
    raw_html: str,
    character: str,
    fight_url: str,
    report_date: Optional[datetime],
    screenshot_parse: float,
) -> Optional[ParseHit]:
    """Prove character participation/spec on one requested boss; parse comes from screenshot."""
    boss = boss_from_uwu_url(fight_url)
    if not boss or boss in DKPARSE_EXCLUDED_BOSSES:
        return None

    rows = re.findall(r"(?is)<tr\b[^>]*>.*?</tr>", raw_html)
    exact_rows = [
        row for row in rows
        if re.search(rf"(?i)(?<![A-Za-z]){re.escape(character)}(?![A-Za-z])", row)
    ]
    relevant_rows = exact_rows or [row for row in rows if _row_matches_character(row, character)]

    fragments: List[str] = relevant_rows[:10]
    if not fragments:
        # Conservative fallback around an exact textual occurrence only.
        for m in re.finditer(re.escape(character), raw_html, re.IGNORECASE):
            fragments.append(raw_html[max(0, m.start()-1400):min(len(raw_html), m.end()+2200)])
            if len(fragments) >= 5:
                break

    if not fragments:
        return None

    # Prefer a recognized DKPARSE spec if one appears in the player's row/fragment.
    spec = ""
    for fragment in fragments:
        spec = normalize_spec(html_to_text(fragment) + " " + fragment)
        if spec:
            break

    return ParseHit(
        character=character,
        boss=boss,
        parse=screenshot_parse,
        spec=spec,
        report_url=fight_url,
        report_date=report_date,
        evidence="Screenshot Points + présence UwU",
    )


def _fight_url_priority(url: str) -> Tuple[int, int]:
    """Prefer a concrete attempt URL, then mode URL, then generic boss URL."""
    q = parse_qs(urlsplit(html_lib.unescape(url)).query)
    has_attempt = 0 if "attempt" in q else 1
    has_mode = 0 if "mode" in q else 1
    return has_attempt, has_mode

async def _fetch_uwu_with_retry(url: str, user_agent: str) -> Tuple[str, str]:
    """
    Lecture UwU avec retry sur erreurs temporaires.

    429 = rate limit.
    500/502/503/504 = erreurs serveur/proxy temporaires.
    """
    timeout = aiohttp.ClientTimeout(total=30)
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml",
    }
    transient_statuses = {429, 500, 502, 503, 504}
    last_status = None
    last_error = None

    for attempt in range(5):
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(url, allow_redirects=True) as resp:
                    body = await resp.text(errors="replace")
                    last_status = resp.status

                    if resp.status == 200:
                        return str(resp.url), body

                    if resp.status in transient_statuses:
                        retry_raw = resp.headers.get("Retry-After", "")
                        try:
                            delay = float(retry_raw)
                        except (TypeError, ValueError):
                            delay = 1.0 * (2 ** attempt)

                        delay = max(0.75, min(delay, 10.0))
                        dkp_debug(
                            "UWU HTTP RETRY",
                            {
                                "url": url,
                                "status": resp.status,
                                "attempt": attempt + 1,
                                "max_attempts": 5,
                                "delay": delay,
                            },
                        )

                        if attempt < 4:
                            await asyncio.sleep(delay)
                            continue
                        break

                    raise RuntimeError(f"UwU HTTP {resp.status}")

        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_error = exc
            delay = max(0.75, min(1.0 * (2 ** attempt), 10.0))
            dkp_debug(
                "UWU NETWORK RETRY",
                {
                    "url": url,
                    "error": f"{type(exc).__name__}: {exc}",
                    "attempt": attempt + 1,
                    "max_attempts": 5,
                    "delay": delay,
                },
            )
            if attempt < 4:
                await asyncio.sleep(delay)
                continue
            break

    if last_status is not None:
        raise RuntimeError(f"UwU HTTP {last_status} après retries")
    if last_error is not None:
        raise RuntimeError(
            f"UwU réseau après retries: {type(last_error).__name__}: {last_error}"
        )
    raise RuntimeError("UwU lecture impossible après retries")


async def fetch_uwu_url(url: str) -> Tuple[str, str]:
    return await _fetch_uwu_with_retry(url, "ApogeeDKParseBot/1.3 (+Discord guild tooling)")


async def fetch_uwu_report(report: UwuReport) -> Tuple[str, str]:
    dkp_debug("UWU LECTURE DEBUT", report.url)
    try:
        final_url, body = await _fetch_uwu_with_retry(
            report.url, "ApogeeDKParseBot/1.3 (+Discord guild tooling)"
        )
        dkp_debug(
            "UWU LECTURE REPONSE",
            {
                "requested_url": report.url,
                "final_url": final_url,
                "status": 200,
                "body_chars": len(body),
            },
        )
        return final_url, body
    except Exception:
        raise


async def scan_whitelisted_reports(
    guild: discord.Guild, reference_time: datetime
) -> List[UwuReport]:
    channel = await get_text_channel(
        guild, LOGS_RAID_CHANNEL_ID, "LOGS_RAID_CHANNEL_ID"
    )

    HISTORY_LIMIT = 300
    reports: List[UwuReport] = []
    seen: set[str] = set()
    scanned_messages = 0
    messages_with_urls = 0

    dkp_debug(
        "SCAN #logs-raid",
        {
            "channel_id": channel.id,
            "channel_name": channel.name,
            "reference_time": reference_time.isoformat(),
            "history_limit": HISTORY_LIMIT,
            "mode": "latest messages, no Discord after/before filter",
        },
    )

    try:
        async for msg in channel.history(limit=HISTORY_LIMIT, oldest_first=False):
            scanned_messages += 1

            combined_parts = [msg.content or ""]

            for embed in msg.embeds:
                if embed.url:
                    combined_parts.append(embed.url)
                if embed.title:
                    combined_parts.append(embed.title)
                if embed.description:
                    combined_parts.append(embed.description)
                for field in embed.fields:
                    if field.name:
                        combined_parts.append(field.name)
                    if field.value:
                        combined_parts.append(field.value)

            for attachment in msg.attachments:
                combined_parts.append(attachment.url or "")
                combined_parts.append(attachment.filename or "")

            combined = "\n".join(x for x in combined_parts if x)
            urls = extract_uwu_urls(combined)

            if DKPARSE_DEBUG:
                dkp_debug(
                    f"MSG {msg.id}",
                    {
                        "created_at": msg.created_at.isoformat(),
                        "author": f"{msg.author} ({msg.author.id})",
                        "content": (msg.content or "")[:500],
                        "urls_found": urls,
                    },
                )

            if urls:
                messages_with_urls += 1

            for url in urls:
                if url in seen:
                    continue

                seen.add(url)
                rdate = report_date_from_url(url)
                report = UwuReport(
                    url=url,
                    message_id=msg.id,
                    posted_at=msg.created_at,
                    report_date=rdate,
                    label=(msg.content or "")[:120],
                )
                reports.append(report)

                dkp_debug(
                    "RAPPORT DÉTECTÉ",
                    {
                        "url": url,
                        "report_date": rdate.isoformat() if rdate else None,
                        "discord_posted_at": msg.created_at.isoformat(),
                        "eligible_for_request": report_within_delay(report, reference_time),
                    },
                )

    except discord.Forbidden as exc:
        raise RuntimeError(
            "Le bot n'a pas la permission de lire l'historique de #logs-raid."
        ) from exc
    except discord.HTTPException as exc:
        raise RuntimeError(
            f"Discord n'a pas pu lire l'historique de #logs-raid : {exc}"
        ) from exc

    dkp_debug(
        "RÉSUMÉ SCAN #logs-raid",
        {
            "messages_scannés": scanned_messages,
            "messages_avec_url": messages_with_urls,
            "rapports_uniques": len(reports),
            "rapports_éligibles": sum(
                1 for r in reports if report_within_delay(r, reference_time)
            ),
            "urls": [r.url for r in reports],
        },
    )

    return reports

def report_within_delay(report: UwuReport, post_time: datetime) -> bool:
    # Best source is date encoded in UwU report URL.
    if report.report_date:
        post_day = post_time.astimezone(timezone.utc).date()
        report_day = report.report_date.date()
        delta = (post_day - report_day).days
        return -1 <= delta <= DKPARSE_MAX_DAYS

    # If URL format changes, the Discord #logs-raid timestamp is acceptable as
    # fallback, but the result will remain conservative elsewhere.
    delta = post_time - report.posted_at
    return timedelta(days=-1) <= delta <= timedelta(days=DKPARSE_MAX_DAYS + 1)


def explicit_character_from_post(content: str) -> Optional[str]:
    """
    Optional override:
      perso: Kroob
      player=Kroob
      character Kroob
    Otherwise the Discord author's #Main is used.
    """
    m = re.search(
        r"(?i)\b(?:perso|player|character|char|personnage)\s*[:= -]\s*([A-Za-z]{2,12})\b",
        content or "",
    )
    return m.group(1) if m else None


def requested_bosses_from_post(content: str) -> List[str]:
    """Return only bosses explicitly named in the post, in textual order."""
    text = (content or "").lower()
    first_pos: Dict[str, int] = {}
    for alias, boss in BOSS_ALIASES.items():
        if boss in DKPARSE_EXCLUDED_BOSSES:
            continue
        pos = text.find(alias)
        if pos < 0:
            continue
        if boss not in first_pos or pos < first_pos[boss]:
            first_pos[boss] = pos
    return [boss for boss, _pos in sorted(first_pos.items(), key=lambda kv: kv[1])]


def requested_raid_date_from_post(
    content: str, post_time: datetime
) -> Optional[datetime]:
    """
    Lit une date de raid écrite dans le post, ex. LOD 12/08 ou LOD 09/08/2026.

    Elle sert uniquement à prioriser les rapports UwU à examiner.
    La fenêtre DKPARSE reste calculée depuis la date du post Discord.
    """
    text = content or ""
    m = re.search(
        r"(?i)\\b(?:lod\\s*)?(\\d{1,2})[/-](\\d{1,2})(?:[/-](\\d{2,4}))?\\b",
        text,
    )
    if not m:
        return None

    day = int(m.group(1))
    month = int(m.group(2))
    raw_year = m.group(3)

    if raw_year:
        year = int(raw_year)
        if year < 100:
            year += 2000
    else:
        year = post_time.astimezone(timezone.utc).year

    try:
        return datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None


def requested_spec_from_post(content: str) -> str:
    return normalize_spec(content or "")


def dedupe_hits(hits: List[ParseHit]) -> List[ParseHit]:
    """
    Multiple people may upload the same raid.
    Keep one reward candidate per boss: the highest proven parse, while
    preferring a hit with a recognized spec.
    """
    best: Dict[str, ParseHit] = {}
    for hit in hits:
        old = best.get(hit.boss)
        if old is None:
            best[hit.boss] = hit
            continue
        if hit.parse > old.parse:
            best[hit.boss] = hit
        elif hit.parse == old.parse and hit.spec and not old.spec:
            best[hit.boss] = hit
    return sorted(best.values(), key=lambda h: h.boss.lower())


def build_kromaddon_dkparse_export(
    character: str, post_time: datetime, hits: List[ParseHit]
) -> str:
    """
    Export DKPARSE cumulatif.
    Chaque boss explicitement demandé dans le post et validé reçoit son bonus.
    """
    lines = [
        "KROMADDON_DKPARSE_V1",
        f"PLAYER={character}",
        f"POST_DATE={post_time.date().isoformat()}",
    ]
    total = 0
    for h in hits:
        amount = dkp_for_parse(h.parse)
        total += amount
        lines.append(
            "BOSS=" + h.boss
            + f";PARSE={h.parse:.2f};DKP={amount}"
            + (f";SPEC={h.spec}" if h.spec else "")
        )
    lines.append(f"TOTAL={total}")
    lines.append("END")
    return "\n".join(lines)


async def evaluate_dkparse_screen_message(
    guild: discord.Guild,
    message: discord.Message,
    log_dates: Optional[set] = None,
) -> DKParseScreenResult:
    """
    DKPARSE SCREEN-ONLY.

    The message text is ignored. Every visible boss row is evaluated from its
    own Points + Best Log date. #logs-raid is only used as a date whitelist.
    """
    issues: List[str] = []

    if DKPARSE_CHANNEL_ID and message.channel.id != DKPARSE_CHANNEL_ID:
        issues.append("Message hors du salon DKPARSE configuré.")

    character, ocr_detail, spec, rows = await extract_dkparse_screen_only(message)
    if not character:
        issues.append(ocr_detail)
        return DKParseScreenResult(
            message_id=message.id,
            character=None,
            spec=spec,
            raid_date=None,
            rows=rows,
            valid_hits=[],
            issues=issues,
            total=0,
            ocr_detail=ocr_detail,
        )

    dated_rows = [r for r in rows if r.best_date is not None]
    if not dated_rows:
        issues.append("Aucune date Best Log lisible sur le screenshot.")
        return DKParseScreenResult(
            message_id=message.id,
            character=character,
            spec=spec,
            raid_date=None,
            rows=rows,
            valid_hits=[],
            issues=issues,
            total=0,
            ocr_detail=ocr_detail,
        )

    in_window_rows = [
        r for r in dated_rows
        if r.best_date is not None
        and _screen_date_within_post_window(r.best_date, message.created_at)
    ]

    if not in_window_rows:
        newest = max(r.best_date for r in dated_rows if r.best_date is not None)
        delta = (
            message.created_at.astimezone(timezone.utc).date() - newest.date()
        ).days
        issues.append(
            "Aucune date Best Log du screen n'est dans la fenêtre DKPARSE "
            f"(max {DKPARSE_MAX_DAYS} jours). Date la plus récente : "
            f"{newest.strftime('%d/%m/%Y')} ({delta} jour(s))."
        )
        # Important: no raid is retained and #logs-raid is not scanned.
        return DKParseScreenResult(
            message_id=message.id,
            character=character,
            spec=spec,
            raid_date=None,
            rows=rows,
            valid_hits=[],
            issues=issues,
            total=0,
            ocr_detail=ocr_detail,
        )

    if log_dates is None:
        reports = await scan_whitelisted_reports(guild, message.created_at)
        log_dates = _lograid_date_set(reports)

    dates_needed = sorted({r.best_date.date() for r in in_window_rows if r.best_date})
    missing_dates = [d for d in dates_needed if d not in log_dates]
    for d in missing_dates:
        issues.append(
            f"Aucun raid n'est enregistré dans #logs-raid le {d.strftime('%d/%m/%Y')}."
        )

    valid_hits: List[ParseHit] = []
    for row in in_window_rows:
        if row.best_date is None or row.best_date.date() not in log_dates:
            continue
        amount = dkp_for_parse(row.parse)
        if amount <= 0:
            continue
        valid_hits.append(
            ParseHit(
                character=character,
                boss=row.boss,
                parse=row.parse,
                spec=row.spec or spec,
                report_url="",
                report_date=row.best_date,
                evidence="Screenshot OCR + date présente dans #logs-raid",
            )
        )

    # Deduplicate same boss/date inside one post; highest parse wins.
    best_by_key: Dict[Tuple[str, datetime], ParseHit] = {}
    for hit in valid_hits:
        if hit.report_date is None:
            continue
        key = (hit.boss, hit.report_date)
        old = best_by_key.get(key)
        if old is None or hit.parse > old.parse:
            best_by_key[key] = hit
    valid_hits = sorted(
        best_by_key.values(),
        key=lambda h: (h.report_date or datetime(1970,1,1,tzinfo=timezone.utc), h.boss),
    )
    total = sum(dkp_for_parse(h.parse) for h in valid_hits)

    if not valid_hits:
        qualifying = [r for r in in_window_rows if dkp_for_parse(r.parse) > 0]
        if not qualifying:
            issues.append("Aucun boss du screen n'atteint le seuil DKPARSE minimum (70%).")

    retained_dates = sorted({h.report_date for h in valid_hits if h.report_date})
    raid_date = retained_dates[-1] if retained_dates else None

    dkp_debug(
        "DKPARSE SCREEN-ONLY RESULTAT",
        {
            "message_id": message.id,
            "message_text_ignored": bool(message.content),
            "character": character,
            "spec": spec,
            "retained_dates": [d.date().isoformat() for d in retained_dates],
            "valid_hits": [
                {"boss": h.boss, "parse": h.parse, "dkp": dkp_for_parse(h.parse),
                 "date": h.report_date.date().isoformat() if h.report_date else None}
                for h in valid_hits
            ],
            "issues": issues,
        },
    )

    return DKParseScreenResult(
        message_id=message.id,
        character=character,
        spec=spec,
        raid_date=raid_date,
        rows=rows,
        valid_hits=valid_hits,
        issues=issues,
        total=total,
        ocr_detail=ocr_detail,
    )


async def analyze_dkparse_message(
    guild: discord.Guild, message: discord.Message
) -> Tuple[str, Optional[str]]:
    result = await evaluate_dkparse_screen_message(guild, message)

    if not result.character:
        return (
            "⚠️ **DKPARSE À VÉRIFIER**\n"
            + "\n".join(result.issues or ["Aucun personnage lisible sur le screenshot."]),
            None,
        )

    lines = [
        f"**DKPARSE — {result.character}**",
        f"Personnage lu sur le screen : **{result.character}**",
    ]

    retained_dates = sorted({
        h.report_date.date() for h in result.valid_hits if h.report_date is not None
    })
    if retained_dates:
        label = "Raid retenu d'après le screen" if len(retained_dates) == 1 else "Raids retenus d'après le screen"
        lines.append(
            f"{label} : **" + ", ".join(d.strftime('%d/%m/%Y') for d in retained_dates) + "**"
        )

    if result.spec:
        lines.append(f"Spec lue sur le screen : **{result.spec}**")
    else:
        lines.append("Spec : non lisible sur ce screen (non bloquant)")

    if result.valid_hits:
        lines += [""]
        for hit in result.valid_hits:
            amount = dkp_for_parse(hit.parse)
            spec_txt = f" — {hit.spec}" if hit.spec else ""
            lines.append(
                f"✅ **{hit.boss}** — {hit.parse:.2f}%{spec_txt} — "
                f"{hit.report_date.strftime('%d/%m/%Y') if hit.report_date else '?'} "
                f"→ **+{amount} DKP VALIDÉ**"
            )
        lines += ["", f"**BONUS DKPARSE TOTAL : +{result.total} DKP**"]

    # If character + dated rows were read, a zero bonus is a deterministic
    # rejection under the screen-only rules, not an uncertainty.
    deterministic_reject = (
        not result.valid_hits
        and bool(result.rows)
        and any(r.best_date is not None for r in result.rows)
    )

    if result.issues:
        icon = "❌" if deterministic_reject else "⚠️"
        lines += ["", f"**{icon} Vérifications / remarques :**"]
        lines.extend(f"• {issue}" for issue in result.issues)

    if not result.valid_hits:
        if deterministic_reject:
            lines += [
                "",
                "❌ **DKPARSE REFUSÉ**",
                "Aucun bonus DKPARSE n'est valide d'après le screenshot et les dates de #logs-raid.",
            ]
        else:
            lines += [
                "",
                "⚠️ **DKPARSE À VÉRIFIER**",
                "Le screenshot n'a pas pu être lu avec assez de certitude pour décider.",
            ]
        return "\n".join(lines), None

    export = build_kromaddon_dkparse_export(
        result.character,
        message.created_at,
        result.valid_hits,
    )
    return "\n".join(lines), export


class DKParseExportView(discord.ui.View):
    def __init__(self, export_text: str):
        super().__init__(timeout=900)
        self.export_text = export_text

    @discord.ui.button(label="Export DKPARSE Kromaddon", style=discord.ButtonStyle.success)
    async def export_dkparse(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        file = discord.File(
            io.BytesIO(self.export_text.encode("utf-8")),
            filename="Kromaddon_DKPARSE.txt",
        )
        await interaction.response.send_message(
            "Export DKPARSE prêt pour Kromaddon :",
            file=file,
            ephemeral=True,
        )


async def run_dkparse_check(
    interaction: discord.Interaction, message: discord.Message
):
    if not can_use_admin(interaction):
        await interaction.response.send_message(
            "Tu n'as pas la permission de valider les DKPARSE.", ephemeral=True
        )
        return
    if not interaction.guild:
        await interaction.response.send_message("Serveur requis.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False, thinking=True)
    try:
        report, export = await analyze_dkparse_message(interaction.guild, message)
        if export:
            await interaction.followup.send(
                report,
                view=DKParseExportView(export),
            )
        else:
            await interaction.followup.send(report)
    except Exception as exc:
        if DKPARSE_DEBUG:
            import traceback
            traceback.print_exc()
        await interaction.followup.send(f"❌ DKPARSE : {exc}")




# =============================================================================
# DKPARSE weekly closure
# =============================================================================

def build_kromaddon_dkparse_batch(totals: Dict[str, int]) -> str:
    """Compact clipboard format, analogous to RH|Name:Code used by Kromaddon."""
    parts = ["DKPARSE"]
    for name in sorted(totals, key=str.lower):
        parts.append(f"{name}:{int(totals[name])}")
    return "|".join(parts)


def try_copy_host_clipboard(text: str) -> Tuple[bool, str]:
    """
    Best effort: copies to the clipboard of the MACHINE RUNNING THE BOT.
    This cannot directly control the Discord client's clipboard on another PC.
    """
    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Set-Clipboard"],
                input=text,
                text=True,
                capture_output=True,
                timeout=8,
            )
            if proc.returncode == 0:
                return True, "copié dans le presse-papier Windows de la machine du bot"
        except Exception as exc:
            dkp_debug("PRESSE-PAPIER WINDOWS ECHEC", repr(exc))

    try:
        import tkinter  # stdlib, may fail on a headless host
        root = tkinter.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        root.destroy()
        return True, "copié dans le presse-papier de la machine du bot"
    except Exception as exc:
        dkp_debug("PRESSE-PAPIER FALLBACK ECHEC", repr(exc))
        return False, "presse-papier hôte indisponible ; utilise le bloc texte/fichier Discord"


async def collect_dkparse_batch(
    guild: discord.Guild,
    channel: discord.TextChannel,
) -> Tuple[Dict[str, int], List[str], int, int]:
    """Analyze every screenshot post in DKPARSE and aggregate deduplicated bonuses."""
    # One Discord-only scan of #logs-raid. No UwU HTTP request is made here.
    reports = await scan_whitelisted_reports(guild, datetime.now(timezone.utc))
    log_dates = _lograid_date_set(reports)

    screen_messages = 0
    total_messages = 0
    warnings: List[str] = []
    dedup: Dict[Tuple[str, str, datetime], ParseHit] = {}

    async for msg in channel.history(limit=None, oldest_first=True):
        total_messages += 1
        if msg.author.bot:
            continue
        if not any(is_image_attachment(a) for a in msg.attachments):
            continue

        screen_messages += 1
        result = await evaluate_dkparse_screen_message(guild, msg, log_dates=log_dates)
        if not result.character:
            warnings.append(f"Message {msg.id}: personnage illisible")
            continue

        if result.issues and not result.valid_hits:
            warnings.append(
                f"{result.character} / message {msg.id}: " + "; ".join(result.issues[:2])
            )

        for hit in result.valid_hits:
            if hit.report_date is None:
                continue
            key = (hit.character.lower(), hit.boss, hit.report_date)
            old = dedup.get(key)
            if old is None or hit.parse > old.parse:
                dedup[key] = hit

    totals: Dict[str, int] = defaultdict(int)
    display_names: Dict[str, str] = {}
    for (char_l, _boss, _date), hit in dedup.items():
        display_names.setdefault(char_l, hit.character)
        totals[display_names[char_l]] += dkp_for_parse(hit.parse)

    return dict(totals), warnings, screen_messages, total_messages


class DKParsePurgeConfirmView(discord.ui.View):
    def __init__(self, owner_id: int, channel_id: int):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.channel_id = channel_id

    def _allowed(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id or can_use_admin(interaction)

    @discord.ui.button(
        label="CONFIRMER ET VIDER LE SALON DKPARSE",
        style=discord.ButtonStyle.danger,
    )
    async def confirm_purge(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not self._allowed(interaction):
            await interaction.response.send_message("Permission refusée.", ephemeral=True)
            return
        if not interaction.guild:
            await interaction.response.send_message("Serveur requis.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            channel = await get_text_channel(
                interaction.guild, self.channel_id, "DKPARSE_CHANNEL_ID"
            )
            deleted = await channel.purge(
                limit=None,
                reason=f"Clôture DKPARSE confirmée par {interaction.user}",
                bulk=True,
            )
            try:
                await interaction.edit_original_response(view=None)
            except Exception:
                pass
            self.stop()
            await interaction.followup.send(
                f"✅ Salon DKPARSE vidé : {len(deleted)} message(s) supprimé(s).",
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(
                f"❌ Suppression DKPARSE : {exc}", ephemeral=True
            )

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    async def cancel_purge(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not self._allowed(interaction):
            await interaction.response.send_message("Permission refusée.", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(view=None)


@bot.tree.command(
    name="dkparse-cloture",
    description="Exporte les DKPARSE de la semaine puis propose de vider le salon.",
)
async def dkparse_cloture(interaction: discord.Interaction):
    if not can_use_admin(interaction):
        await interaction.response.send_message("Permission refusée.", ephemeral=True)
        return
    if not interaction.guild:
        await interaction.response.send_message("Serveur requis.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        channel = await get_text_channel(
            interaction.guild, DKPARSE_CHANNEL_ID, "DKPARSE_CHANNEL_ID"
        )
        totals, warnings, screen_messages, total_messages = await collect_dkparse_batch(
            interaction.guild, channel
        )

        export_text = build_kromaddon_dkparse_batch(totals)
        copied, clipboard_detail = try_copy_host_clipboard(export_text)

        lines = [
            "**Clôture DKPARSE — aperçu AVANT suppression**",
            f"Screens analysés : **{screen_messages}**",
            f"Messages actuellement dans le salon : **{total_messages}**",
            f"Joueurs avec bonus : **{len(totals)}**",
            "",
        ]
        if totals:
            for name in sorted(totals, key=str.lower):
                lines.append(f"• **{name}** : +{totals[name]} DKP")
        else:
            lines.append("Aucun bonus DKPARSE validé.")

        if warnings:
            lines += ["", f"⚠️ {len(warnings)} avertissement(s) avant suppression :"]
            lines.extend(f"• {w}" for w in warnings[:12])
            if len(warnings) > 12:
                lines.append(f"• ... et {len(warnings) - 12} autre(s)")

        lines += [
            "",
            f"Presse-papier : **{'OK' if copied else 'NON'}** — {clipboard_detail}",
        ]

        # Keep the confirmation/export message safely below Discord's 2000-char limit.
        summary_text = "\n".join(lines)
        for chunk in split_discord_text(summary_text):
            await interaction.followup.send(chunk, ephemeral=True)

        file = discord.File(
            io.BytesIO(export_text.encode("utf-8")),
            filename="Kromaddon_DKPARSE_BATCH.txt",
        )
        view = DKParsePurgeConfirmView(interaction.user.id, channel.id)
        export_message = (
            "**Texte Kromaddon :**\n"
            f"```text\n{export_text}\n```\n"
            "Clique sur le bouton rouge uniquement après avoir récupéré/importé ce texte."
        )
        await interaction.followup.send(
            export_message,
            file=file,
            view=view,
            ephemeral=True,
        )
    except Exception as exc:
        if DKPARSE_DEBUG:
            import traceback
            traceback.print_exc()
        await interaction.followup.send(f"❌ Clôture DKPARSE : {exc}", ephemeral=True)



# =============================================================================
# ApogeeBot equivalent
# =============================================================================

PEWPEW_TIERS: Tuple[Tuple[float, str], ...] = (
    (0.2, "🔴 Top 0.2% 🔴"),
    (2.0, "🟠 Top 2% 🟠"),
    (5.0, "🟡 Top 5% 🟡"),
    (10.0, "🟣 Top 10% 🟣"),
    (15.0, "🔵 Top 15% 🔵"),
    (20.0, "🟢 Top 20% 🟢"),
    (25.0, "⚪ Top 25% ⚪"),
    (33.0, "⚫ Top 33% ⚫"),
)
PEWPEW_STATE_FILE = APP_DIR / "apogeebot_seen_v3.json"
PEWPEW_LOCK = asyncio.Lock()


def _load_pewpew_seen() -> set:
    try:
        data = json.loads(PEWPEW_STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {canonical_uwu_url(str(x)) for x in data if x}
    except Exception:
        pass
    return set()


PEWPEW_SEEN_REPORTS = _load_pewpew_seen()


def _save_pewpew_seen() -> None:
    try:
        PEWPEW_STATE_FILE.write_text(
            json.dumps(sorted(PEWPEW_SEEN_REPORTS), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        dkp_debug("APOGEEBOT STATE SAVE ECHEC", repr(exc))


def _generic_boss_from_uwu_url(url: str) -> str:
    try:
        q = parse_qs(urlsplit(html_lib.unescape(url)).query)
        raw = (q.get("boss") or [""])[0]
    except Exception:
        raw = ""
    if not raw:
        return ""
    known = normalize_boss(raw)
    if known:
        return known
    words = raw.replace("_", "-").split("-")
    small = {"of", "the", "and"}
    pretty = []
    for i, word in enumerate(words):
        lw = word.lower()
        pretty.append(lw if i > 0 and lw in small else lw.capitalize())
    return " ".join(pretty)


def _html_cells(row_html: str) -> List[str]:
    cells = re.findall(r"(?is)<t[dh]\b[^>]*>(.*?)</t[dh]>", row_html)
    return [html_to_text(c) for c in cells]


def _extract_player_name_from_html_row(row_html: str, cells: List[str]) -> str:
    # Strongest source: character/player link query.
    patterns = [
        r"(?i)[?&](?:name|player)=([A-Za-z]{2,12})(?:[&\"'])",
        r"(?i)data-(?:player|name)=[\"']([A-Za-z]{2,12})[\"']",
    ]
    for pattern in patterns:
        m = re.search(pattern, row_html)
        if m and WOW_NAME_RE.fullmatch(m.group(1)):
            return m.group(1)

    # Then anchor text.
    for raw in re.findall(r"(?is)<a\b[^>]*>(.*?)</a>", row_html):
        text = html_to_text(raw)
        for token in re.findall(r"\b[A-Za-z]{2,12}\b", text):
            if WOW_NAME_RE.fullmatch(token) and token.lower() not in OCR_IGNORE_WORDS:
                return token

    # Finally inspect likely first cells, avoiding headers/class words.
    for cell in cells[:3]:
        for token in re.findall(r"\b[A-Za-z]{2,12}\b", cell):
            if WOW_NAME_RE.fullmatch(token) and token.lower() not in OCR_IGNORE_WORDS:
                if normalize_boss(token):
                    continue
                return token
    return ""


def parse_pewpew_hits_from_fight_page(
    raw_html: str,
    fight_url: str,
) -> List[PewPewHit]:
    """Extract player Points from an UwU fight table and convert to old PewPew top %."""
    boss = _generic_boss_from_uwu_url(fight_url)
    if not boss:
        return []

    rows_html = re.findall(r"(?is)<tr\b[^>]*>.*?</tr>", raw_html)
    if not rows_html:
        return []

    header_cells: List[str] = []
    points_idx: Optional[int] = None
    name_idx: Optional[int] = None

    for row in rows_html:
        cells = _html_cells(row)
        lowers = [c.strip().lower() for c in cells]
        if any("point" in c for c in lowers):
            header_cells = cells
            for i, c in enumerate(lowers):
                if "point" in c:
                    points_idx = i
                    break
            for i, c in enumerate(lowers):
                if c in {"player", "name", "character"} or "player" in c:
                    name_idx = i
                    break
            break

    hits: List[PewPewHit] = []
    for row in rows_html:
        cells = _html_cells(row)
        if not cells:
            continue
        if header_cells and cells == header_cells:
            continue

        player = _extract_player_name_from_html_row(row, cells)
        if not player:
            continue

        points: Optional[float] = None
        if points_idx is not None and points_idx < len(cells):
            points = _numeric(cells[points_idx])

        # Fallback: look for a labelled Points value inside the row only.
        if points is None:
            m = re.search(
                r"(?i)(?:points?|performance)\s*[:=]?\s*(\d{1,3}(?:[.,]\d+)?)",
                html_to_text(row),
            )
            if m:
                try:
                    points = float(m.group(1).replace(",", "."))
                except ValueError:
                    points = None

        if points is None or not (0.0 <= points <= 100.0):
            continue

        top_percent = max(0.0, min(100.0, 100.0 - points))
        if top_percent > 33.0001:
            continue

        hits.append(
            PewPewHit(
                player=player,
                boss=boss,
                top_percent=top_percent,
                points=points,
                server_best=("server best" in html_to_text(row).lower() or points >= 99.995),
            )
        )

    # Fallback: UwU sometimes embeds player performance data in JSON/JS instead
    # of rendering a normal HTML table. Only explicit Points-like fields are
    # accepted to avoid inventing a percentile from unrelated numbers.
    seen = {(h.player.lower(), h.boss, round(h.points, 4)) for h in hits}
    name_keys = ("name", "player", "playerName", "character", "characterName")
    points_keys = (
        "points", "performancePoints", "performance_points",
        "playerPoints", "player_points",
    )
    for obj in json_candidates_from_html(raw_html):
        for d in walk_dicts(obj):
            raw_name = str(get_first(d, name_keys, "")).strip()
            if not WOW_NAME_RE.fullmatch(raw_name):
                continue
            points = None
            for key in points_keys:
                if key in d:
                    points = _numeric(d[key])
                    if points is not None:
                        break
            if points is None or not (0.0 <= points <= 100.0):
                continue
            top_percent = max(0.0, min(100.0, 100.0 - points))
            if top_percent > 33.0001:
                continue
            key = (raw_name.lower(), boss, round(points, 4))
            if key in seen:
                continue
            seen.add(key)
            blob = " ".join(str(v) for v in d.values() if isinstance(v, str)).lower()
            hits.append(
                PewPewHit(
                    player=raw_name,
                    boss=boss,
                    top_percent=top_percent,
                    points=points,
                    server_best=("server best" in blob or points >= 99.995),
                )
            )

    return hits


def _pewpew_tier(value: float) -> Optional[float]:
    for threshold, _label in PEWPEW_TIERS:
        if value <= threshold + 1e-9:
            return threshold
    return None


def _fmt_top_percent(value: float) -> str:
    if abs(value - round(value)) < 0.05:
        return f"{int(round(value))}%"
    return f"{value:.1f}%"


def format_pewpew_report(hits: List[PewPewHit]) -> str:
    # Best result per player + boss across attempts.
    best: Dict[Tuple[str, str], PewPewHit] = {}
    display_name: Dict[str, str] = {}
    for hit in hits:
        pl = hit.player.lower()
        display_name.setdefault(pl, hit.player)
        key = (pl, hit.boss)
        old = best.get(key)
        if old is None or hit.top_percent < old.top_percent:
            best[key] = hit

    by_player: Dict[str, List[PewPewHit]] = defaultdict(list)
    for (pl, _boss), hit in best.items():
        by_player[pl].append(hit)

    tier_players: Dict[float, List[Tuple[str, List[PewPewHit]]]] = defaultdict(list)
    for pl, phits in by_player.items():
        phits.sort(key=lambda h: (h.top_percent, h.boss))
        tier = _pewpew_tier(phits[0].top_percent)
        if tier is not None:
            tier_players[tier].append((display_name[pl], phits))

    if not tier_players:
        return ""

    lines = ["Congratulations to the following players for achieving high damage on bosses!"]
    for threshold, label in PEWPEW_TIERS:
        players = tier_players.get(threshold, [])
        if not players:
            continue
        players.sort(key=lambda item: (item[1][0].top_percent, item[0].lower()))
        lines.append(label)

        for name, phits in players:
            lead = [h for h in phits if h.top_percent <= threshold + 1e-9]
            lead_parts = []
            for h in lead:
                value = "SERVER BEST!" if h.server_best else _fmt_top_percent(h.top_percent)
                lead_parts.append(f"**{value}** on {h.boss}")

            extras = []
            thresholds = [t for t, _ in PEWPEW_TIERS if t > threshold]
            for t in thresholds:
                count = sum(1 for h in phits if h.top_percent <= t + 1e-9)
                if count:
                    label_num = "0.2" if t == 0.2 else str(int(t))
                    extras.append(f"**{count}** top {label_num}%")

            suffix = (", also " + ", ".join(extras)) if extras else ""
            lines.append(f"  🔸  *{name}*: " + ", ".join(lead_parts) + suffix)

    return "\n".join(lines)


def _pewpew_attempt_number(url: str) -> int:
    try:
        q = parse_qs(urlsplit(html_lib.unescape(url)).query)
        raw = (q.get("attempt") or ["-1"])[0]
        return int(raw)
    except Exception:
        return -1


def _choose_pewpew_fight_url(urls: List[str]) -> Optional[str]:
    """
    Pick one concrete fight per boss.

    UwU usually exposes:
      - one or more concrete ?attempt=...&s=...&f=... links
      - a ?boss=...&mode=... link
      - a generic ?boss=... link

    The highest concrete attempt is normally the final/kill attempt and avoids
    hammering UwU with several requests per boss.
    """
    unique = list(dict.fromkeys(urls))
    if not unique:
        return None

    concrete = []
    for url in unique:
        q = parse_qs(urlsplit(html_lib.unescape(url)).query)
        if "attempt" in q and ("s" in q or "f" in q):
            concrete.append(url)

    if concrete:
        concrete.sort(key=lambda u: (_pewpew_attempt_number(u), u), reverse=True)
        return concrete[0]

    with_attempt = [u for u in unique if "attempt" in parse_qs(urlsplit(html_lib.unescape(u)).query)]
    if with_attempt:
        with_attempt.sort(key=lambda u: (_pewpew_attempt_number(u), u), reverse=True)
        return with_attempt[0]

    return sorted(unique, key=_fight_url_priority)[0]


def _apogee_report_id(url: str) -> str:
    try:
        path = urlsplit(canonical_uwu_url(url)).path.rstrip("/")
        return path.rsplit("/", 1)[-1]
    except Exception:
        return ""


def _extract_report_id_from_text(text: str) -> str:
    m = re.search(
        r"(\\d{2}-\\d{2}-\\d{2}--\\d{2}-\\d{2}--[^/\\s\\\"'<>]+--[^/\\s\\\"'<>]+)",
        str(text or ""),
        re.IGNORECASE,
    )
    return m.group(1) if m else ""


APOGEE_SPEC_TREE_HINTS = {
    "blood": 1, "frost": 2, "unholy": 3,
    "balance": 1, "feral": 2, "restoration": 3,
    "beast mastery": 1, "marksmanship": 2, "survival": 3,
    "arcane": 1, "fire": 2,
    "holy": 1, "protection": 2, "retribution": 3,
    "discipline": 1, "shadow": 3,
    "assassination": 1, "combat": 2, "subtlety": 3,
    "elemental": 1, "enhancement": 2,
    "affliction": 1, "demonology": 2, "destruction": 3,
    "arms": 1, "fury": 2,
}


def _infer_tree_index_from_row(row_html: str) -> int:
    text = (html_to_text(row_html) + " " + row_html).lower()
    # Longest/specific hints first to avoid generic collisions.
    for hint in sorted(APOGEE_SPEC_TREE_HINTS, key=len, reverse=True):
        if re.search(r"(?<![a-z])" + re.escape(hint) + r"(?![a-z])", text):
            return APOGEE_SPEC_TREE_HINTS[hint]
    # Query-style hints occasionally appear in player/spec links.
    for pattern in (r"[?&]spec=(\\d)", r"data-spec=[\\\"']?(\\d)"):
        m = re.search(pattern, row_html, re.IGNORECASE)
        if m and m.group(1) in {"1", "2", "3"}:
            return int(m.group(1))
    return 0


def extract_apogeebot_participants(raw_html: str) -> Dict[str, int]:
    """Extract player names and, when visible, their talent-tree index."""
    out: Dict[str, int] = {}
    for row in re.findall(r"(?is)<tr\\b[^>]*>.*?</tr>", raw_html):
        cells = _html_cells(row)
        if not cells:
            continue
        name = _extract_player_name_from_html_row(row, cells)
        if not name:
            continue
        low = name.lower()
        tree = _infer_tree_index_from_row(row)
        if low not in out or (out[low] == 0 and tree):
            out[low] = tree

    # Fallback to embedded JSON/JS participant objects.
    for obj in json_candidates_from_html(raw_html):
        for d in walk_dicts(obj):
            name = str(get_first(d, ("playerName", "characterName", "player", "character", "name"), "")).strip()
            if not WOW_NAME_RE.fullmatch(name):
                continue
            if normalize_boss(name) or name.lower() in OCR_IGNORE_WORDS:
                continue
            blob = " ".join(str(v) for v in d.values() if isinstance(v, str))
            tree = _infer_tree_index_from_row(blob)
            low = name.lower()
            if low not in out or (out[low] == 0 and tree):
                out[low] = tree
    return out


async def _post_uwu_character_json(player: str, spec_idx: int) -> Any:
    """POST /character with transient-error retry."""
    url = "https://uwu-logs.xyz/character"
    timeout = aiohttp.ClientTimeout(total=30)
    headers = {
        "User-Agent": "ApogeeBot/3.0 (+Discord guild tooling)",
        "Accept": "application/json,text/plain,*/*",
    }
    data = {"name": player, "server": UWU_SERVER, "spec": str(spec_idx)}
    transient = {429, 500, 502, 503, 504}
    last_status = None
    last_error = None

    for attempt in range(5):
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.post(url, data=data, allow_redirects=True) as resp:
                    text = await resp.text(errors="replace")
                    last_status = resp.status
                    if resp.status == 200:
                        try:
                            return json.loads(text)
                        except Exception:
                            # Some deployments may wrap JSON in whitespace/text.
                            return json.loads(text.strip())
                    if resp.status in transient:
                        retry_raw = resp.headers.get("Retry-After", "")
                        try:
                            delay = float(retry_raw)
                        except Exception:
                            delay = 0.8 * (2 ** attempt)
                        if attempt < 4:
                            await asyncio.sleep(max(0.6, min(delay, 8.0)))
                            continue
                        break
                    raise RuntimeError(f"UwU HTTP {resp.status}")
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < 4:
                await asyncio.sleep(min(0.8 * (2 ** attempt), 8.0))
                continue
            break

    if last_status is not None:
        raise RuntimeError(f"UwU /character HTTP {last_status} après retries")
    if last_error is not None:
        raise RuntimeError(f"UwU /character: {type(last_error).__name__}: {last_error}")
    raise RuntimeError("UwU /character impossible")


def _apogeebot_hits_from_character_payload(
    payload: Any,
    player: str,
    current_report_id: str,
) -> List[PewPewHit]:
    """
    Read character Points and keep only personal-best rows whose report ID is
    the report currently posted in #logs-raid. Old Apogee/UwU messages used
    100 - Points as the displayed Top X% value.
    """
    found: Dict[Tuple[str, str], PewPewHit] = {}
    current_low = current_report_id.lower()

    def add_candidate(boss: str, points: Optional[float], report_id: str) -> None:
        if not boss or points is None or not (0.0 <= points <= 100.0):
            return
        if not report_id or report_id.lower() != current_low:
            return
        top = max(0.0, min(100.0, 100.0 - points))
        if top > 33.0001:
            return
        key = (player.lower(), boss)
        hit = PewPewHit(
            player=player,
            boss=boss,
            top_percent=top,
            points=points,
            server_best=(points >= 99.995),
        )
        old = found.get(key)
        if old is None or hit.top_percent < old.top_percent:
            found[key] = hit

    # Dict-shaped API rows.
    for d in walk_dicts(payload):
        values = list(d.values())
        blob = " ".join(str(v) for v in values if isinstance(v, (str, int, float)))
        boss = ""
        for key in ("boss", "bossName", "fight", "fightName", "encounter", "encounterName", "name"):
            if key in d:
                boss = normalize_boss(str(d[key]))
                if boss:
                    break
        if not boss:
            boss = normalize_boss(blob)

        report_id = ""
        for key in ("reportId", "report_id", "report", "reportName", "report_name", "log", "logId", "url"):
            if key in d:
                report_id = _extract_report_id_from_text(str(d[key])) or str(d[key]).strip().strip("/").split("/")[-1]
                if report_id:
                    break
        if not report_id:
            report_id = _extract_report_id_from_text(blob)

        points = None
        for key in ("points", "performancePoints", "performance_points", "playerPoints", "player_points", "score", "performance"):
            if key in d:
                n = _numeric(d[key])
                if n is not None and 0 <= n <= 100:
                    points = n
                    break
        add_candidate(boss, points, report_id)

    # List-shaped rows: [Boss, Points, ..., ReportId, ...].
    def walk_lists(obj: Any) -> Iterable[List[Any]]:
        if isinstance(obj, list):
            yield obj
            for v in obj:
                yield from walk_lists(v)
        elif isinstance(obj, dict):
            for v in obj.values():
                yield from walk_lists(v)

    for arr in walk_lists(payload):
        if not (3 <= len(arr) <= 20):
            continue
        boss = ""
        report_id = ""
        numeric: List[float] = []
        for v in arr:
            if isinstance(v, str):
                if not boss:
                    boss = normalize_boss(v)
                if not report_id:
                    report_id = _extract_report_id_from_text(v)
            elif isinstance(v, (int, float)):
                n = float(v)
                if 0 <= n <= 100:
                    numeric.append(n)
        # Points is normally the high 0..100 value; ranks >100 are excluded.
        points = max(numeric) if numeric else None
        add_candidate(boss, points, report_id)

    return list(found.values())


async def _apogeebot_character_hits(
    player: str,
    preferred_tree: int,
    current_report_id: str,
) -> Tuple[List[PewPewHit], int]:
    order = []
    if preferred_tree in (1, 2, 3):
        order.append(preferred_tree)
    order.extend(i for i in (1, 2, 3) if i not in order)

    queries = 0
    all_hits: List[PewPewHit] = []
    for spec_idx in order:
        payload = await _post_uwu_character_json(player, spec_idx)
        queries += 1
        hits = _apogeebot_hits_from_character_payload(payload, player, current_report_id)
        if hits:
            all_hits.extend(hits)
            # A report's personal-best rows belong to the active spec; no need
            # to hammer the two remaining specs once current-report hits exist.
            break
        await asyncio.sleep(0.08)
    return all_hits, queries


async def build_pewpew_report(
    report_url: str,
) -> Tuple[str, Dict[str, int]]:
    """Build ApogeeBot rankings for a newly posted UwU report."""
    report_url = canonical_uwu_url(report_url)
    current_report_id = _apogee_report_id(report_url)
    final_url, raw_html = await _fetch_uwu_with_retry(
        report_url,
        "ApogeeBot/3.0 (+Discord guild tooling)",
    )
    report_url = canonical_uwu_url(final_url)
    current_report_id = _apogee_report_id(report_url) or current_report_id
    fight_urls = extract_uwu_fight_urls(raw_html, report_url)

    by_boss: Dict[str, List[str]] = defaultdict(list)
    for url in fight_urls:
        boss = _generic_boss_from_uwu_url(url)
        if boss:
            by_boss[boss].append(url)

    stats: Dict[str, int] = {
        "fight_links": len(fight_urls),
        "bosses": len(by_boss),
        "pages_ok": 0,
        "pages_failed": 0,
        "participants": 0,
        "character_queries": 0,
        "hits": 0,
    }
    if not by_boss:
        return "", stats

    participants: Dict[str, Tuple[str, int]] = {}
    fight_fallback_hits: List[PewPewHit] = []

    for boss, urls in by_boss.items():
        fight_url = _choose_pewpew_fight_url(urls)
        if not fight_url:
            continue
        try:
            page_url, fight_html = await _fetch_uwu_with_retry(
                fight_url,
                "ApogeeBot/3.0 (+Discord guild tooling)",
            )
            stats["pages_ok"] += 1

            for low, tree in extract_apogeebot_participants(fight_html).items():
                # Recover display casing from a row when possible.
                display = low
                for row in re.findall(r"(?is)<tr\\b[^>]*>.*?</tr>", fight_html):
                    cells = _html_cells(row)
                    name = _extract_player_name_from_html_row(row, cells) if cells else ""
                    if name and name.lower() == low:
                        display = name
                        break
                old = participants.get(low)
                if old is None or (old[1] == 0 and tree):
                    participants[low] = (display, tree)

            # Keep legacy fight parser only as a fallback if /character is
            # unavailable. It is no longer the primary ranking source.
            fight_fallback_hits.extend(parse_pewpew_hits_from_fight_page(fight_html, page_url))
        except Exception as exc:
            stats["pages_failed"] += 1
            print(f"[APOGEEBOT] ECHEC {boss}: {type(exc).__name__}: {exc}")
            dkp_debug(
                "APOGEEBOT FIGHT ECHEC",
                {"boss": boss, "url": fight_url, "error": f"{type(exc).__name__}: {exc}"},
            )
        await asyncio.sleep(0.15)

    stats["participants"] = len(participants)

    hits: List[PewPewHit] = []
    for _low, (player, tree) in list(participants.items())[:40]:
        try:
            phits, qcount = await _apogeebot_character_hits(player, tree, current_report_id)
            stats["character_queries"] += qcount
            hits.extend(phits)
            if phits:
                print(f"[APOGEEBOT] {player}: {len(phits)} résultat(s) Top 33 du rapport")
        except Exception as exc:
            print(f"[APOGEEBOT] /character {player}: {type(exc).__name__}: {exc}")
        await asyncio.sleep(0.12)

    if not hits and fight_fallback_hits:
        hits = fight_fallback_hits

    stats["hits"] = len(hits)
    return format_pewpew_report(hits), stats


def _message_uwu_urls(message: discord.Message) -> List[str]:
    parts = [message.content or ""]
    for embed in message.embeds:
        parts.extend([embed.url or "", embed.title or "", embed.description or ""])
        for field in embed.fields:
            parts.extend([field.name or "", field.value or ""])
    return extract_uwu_urls("\n".join(x for x in parts if x))


async def _pewpew_set_reaction(
    message: discord.Message,
    emoji: str,
) -> None:
    try:
        await message.add_reaction(emoji)
    except discord.HTTPException:
        pass


async def _pewpew_remove_own_reaction(
    message: discord.Message,
    emoji: str,
) -> None:
    try:
        if bot.user:
            await message.remove_reaction(emoji, bot.user)
    except discord.HTTPException:
        pass


async def handle_uwu_pewpew_message(
    message: discord.Message,
    *,
    force: bool = False,
) -> None:
    if not UWU_PEWPEW_ENABLED:
        return
    if message.author.bot or not message.guild:
        return
    if message.channel.id != LOGS_RAID_CHANNEL_ID:
        return

    urls = _message_uwu_urls(message)
    if not urls:
        print(
            f"[APOGEEBOT] Message {message.id} reçu dans #logs-raid "
            "mais aucun lien UwU détecté."
        )
        return

    print(f"[APOGEEBOT] Message {message.id}: {len(urls)} rapport(s) UwU détecté(s)")
    await _pewpew_set_reaction(message, "🔎")

    final_status = "✅"

    async with PEWPEW_LOCK:
        for url in urls:
            canonical = canonical_uwu_url(url)

            if canonical in PEWPEW_SEEN_REPORTS and not force:
                print(f"[APOGEEBOT] Déjà traité: {canonical}")
                continue

            print(f"[APOGEEBOT] Analyse: {canonical}")

            try:
                report, stats = await build_pewpew_report(canonical)
                print(f"[APOGEEBOT] Résultat {canonical}: {stats}")

                if stats["fight_links"] <= 0 or stats["bosses"] <= 0:
                    final_status = "⚠️"
                    await message.reply(
                        "⚠️ **ApogeeBot : rapport détecté, mais aucun combat "
                        "n'a pu être extrait de la page UwU.**\n"
                        "Le rapport n'est pas marqué comme traité : tu peux le "
                        "reposter après correction du bot/site.",
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    continue

                if stats["pages_ok"] <= 0:
                    final_status = "⚠️"
                    await message.reply(
                        "⚠️ **ApogeeBot : rapport détecté, mais aucune page de "
                        "combat UwU n'a pu être lue.**\n"
                        f"Pages en échec : {stats['pages_failed']}. "
                        "Le rapport n'est pas marqué comme traité.",
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    continue

                if report:
                    first = True
                    for chunk in split_discord_text(report):
                        if first:
                            await message.reply(
                                chunk,
                                mention_author=False,
                                allowed_mentions=discord.AllowedMentions.none(),
                            )
                            first = False
                        else:
                            await message.channel.send(
                                chunk,
                                allowed_mentions=discord.AllowedMentions.none(),
                            )
                else:
                    # Important diagnostic: this is a successful read with no
                    # qualifying result, not a silent parser/network failure.
                    await message.reply(
                        "ℹ️ **ApogeeBot : rapport analysé.** "
                        "Aucun nouveau meilleur résultat Top 33% détecté pour ce rapport.",
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )

                PEWPEW_SEEN_REPORTS.add(canonical)
                _save_pewpew_seen()

            except Exception as exc:
                final_status = "⚠️"
                print(
                    f"[APOGEEBOT] ERREUR RAPPORT {canonical}: "
                    f"{type(exc).__name__}: {exc}"
                )
                dkp_debug(
                    "APOGEEBOT REPORT ECHEC",
                    {
                        "url": canonical,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                try:
                    await message.reply(
                        "⚠️ **ApogeeBot : erreur pendant l'analyse du rapport.**\n"
                        f"`{type(exc).__name__}: {str(exc)[:500]}`\n"
                        "Le rapport n'est pas marqué comme traité.",
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass

    await _pewpew_remove_own_reaction(message, "🔎")
    await _pewpew_set_reaction(message, final_status)

# =============================================================================
# Discord commands / events
# =============================================================================

@app_commands.context_menu(name="RH List")
async def rh_list_context(
    interaction: discord.Interaction, message: discord.Message
):
    await run_rh_list(interaction, message)


@app_commands.context_menu(name="DKPARSE")
async def dkparse_context(
    interaction: discord.Interaction, message: discord.Message
):
    await run_dkparse_check(interaction, message)


@bot.tree.command(name="main-audit", description="Vérifie le salon #Main.")
async def main_audit(interaction: discord.Interaction):
    if not can_use_admin(interaction):
        await interaction.response.send_message("Permission refusée.", ephemeral=True)
        return
    if not interaction.guild:
        await interaction.response.send_message("Serveur requis.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        mapping, problems = await build_main_map(interaction.guild)
        lines = [f"✅ {len(mapping)} main(s) valide(s)."]
        if problems:
            lines += ["", "⚠️ Problèmes :"] + problems[:50]
        else:
            lines += ["", "Aucune anomalie détectée."]
        await interaction.followup.send("\n".join(lines), ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"❌ Audit : {exc}", ephemeral=True)


@bot.tree.command(
    name="apogee-log-test",
    description="Force l'analyse ApogeeBot d'un message #logs-raid.",
)
@app_commands.describe(message_id="ID du message Discord contenant le lien UwU")
async def pewpew_test(interaction: discord.Interaction, message_id: str):
    if not can_use_admin(interaction):
        await interaction.response.send_message("Permission refusée.", ephemeral=True)
        return
    if not interaction.guild:
        await interaction.response.send_message("Serveur requis.", ephemeral=True)
        return
    if not message_id.isdigit():
        await interaction.response.send_message("ID de message invalide.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        channel = await get_text_channel(
            interaction.guild,
            LOGS_RAID_CHANNEL_ID,
            "LOGS_RAID_CHANNEL_ID",
        )
        message = await channel.fetch_message(int(message_id))

        urls = _message_uwu_urls(message)
        if not urls:
            await interaction.followup.send(
                "Aucun lien UwU détecté dans ce message.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Analyse forcée lancée pour {len(urls)} rapport(s). "
            "Le résultat sera posté en réponse au message dans #logs-raid.",
            ephemeral=True,
        )
        await handle_uwu_pewpew_message(message, force=True)

    except Exception as exc:
        await interaction.followup.send(
            f"❌ ApogeeBot test : {type(exc).__name__}: {exc}",
            ephemeral=True,
        )


@bot.tree.command(
    name="dkparse-check",
    description="Valide un message DKPARSE par son ID Discord.",
)
@app_commands.describe(message_id="ID du message posté dans le salon DKPARSE")
async def dkparse_check(interaction: discord.Interaction, message_id: str):
    if not can_use_admin(interaction):
        await interaction.response.send_message("Permission refusée.", ephemeral=True)
        return
    if not interaction.guild:
        await interaction.response.send_message("Serveur requis.", ephemeral=True)
        return
    if not message_id.isdigit():
        await interaction.response.send_message("ID de message invalide.", ephemeral=True)
        return

    try:
        channel = await get_text_channel(
            interaction.guild, DKPARSE_CHANNEL_ID, "DKPARSE_CHANNEL_ID"
        )
        message = await channel.fetch_message(int(message_id))
    except Exception as exc:
        await interaction.response.send_message(
            f"Impossible de récupérer ce message : {exc}", ephemeral=True
        )
        return

    await run_dkparse_check(interaction, message)


@bot.tree.command(
    name="dkparse-logs",
    description="Affiche les dates de logs guilde trouvées dans #logs-raid.",
)
async def dkparse_logs(interaction: discord.Interaction):
    if not can_use_admin(interaction):
        await interaction.response.send_message("Permission refusée.", ephemeral=True)
        return
    if not interaction.guild:
        await interaction.response.send_message("Serveur requis.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        now = datetime.now(timezone.utc)
        all_reports = await scan_whitelisted_reports(interaction.guild, now)
        reports = [r for r in all_reports if report_within_delay(r, now)]
        if not reports:
            await interaction.followup.send(
                f"Aucun rapport UwU éligible trouvé dans #logs-raid. "
                f"({len(all_reports)} rapport(s) détecté(s) dans le scan.)",
                ephemeral=True,
            )
            return

        lines = [
            f"**{len(reports)} rapport(s) UwU éligible(s)** "
            f"({len(all_reports)} détecté(s) au total)",
            "",
        ]
        for r in reports[:40]:
            date_txt = (
                r.report_date.strftime("%d/%m/%Y")
                if r.report_date else r.posted_at.strftime("%d/%m/%Y")
            )
            lines.append(f"• {date_txt} — {r.url}")
        if len(reports) > 40:
            lines.append(f"... et {len(reports)-40} autre(s).")

        for chunk in split_discord_text("\n".join(lines)):
            await interaction.followup.send(chunk, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"❌ DKPARSE logs : {exc}", ephemeral=True)


async def enforce_main_message(message: discord.Message):
    if (
        message.author.bot
        or not message.guild
        or message.channel.id != MAIN_CHANNEL_ID
    ):
        return

    content = message.content.strip()
    if not WOW_NAME_RE.fullmatch(content):
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        try:
            await message.author.send(DM_BAD_MAIN)
        except discord.HTTPException:
            pass
        return

    owner_of_name: Optional[int] = None
    previous_messages: List[discord.Message] = []

    async for other in message.channel.history(limit=None):
        if other.id == message.id or other.author.bot:
            continue
        other_name = other.content.strip()
        if WOW_NAME_RE.fullmatch(other_name):
            if other_name.lower() == content.lower():
                owner_of_name = other.author.id
            if other.author.id == message.author.id:
                previous_messages.append(other)

    if owner_of_name is not None and owner_of_name != message.author.id:
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        try:
            await message.author.send(
                "Message automatique Apogee :\n"
                f"`{content}` est déjà déclaré comme main par un autre membre. "
                "Ton message dans #Main a été supprimé."
            )
        except discord.HTTPException:
            pass
        return

    for old in previous_messages:
        try:
            await old.delete()
        except discord.HTTPException:
            pass


@bot.event
async def on_message(message: discord.Message):
    await enforce_main_message(message)
    if (
        UWU_PEWPEW_ENABLED
        and not message.author.bot
        and message.guild
        and message.channel.id == LOGS_RAID_CHANNEL_ID
    ):
        print(
            f"[APOGEEBOT] Nouveau message #logs-raid: "
            f"id={message.id} auteur={message.author}"
        )
        asyncio.create_task(handle_uwu_pewpew_message(message))
    await bot.process_commands(message)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if after.channel.id == MAIN_CHANNEL_ID:
        await enforce_main_message(after)
    if (
        UWU_PEWPEW_ENABLED
        and not after.author.bot
        and after.guild
        and after.channel.id == LOGS_RAID_CHANNEL_ID
        and _message_uwu_urls(after)
    ):
        asyncio.create_task(handle_uwu_pewpew_message(after))


@bot.event
async def on_ready():
    print("Apogee Raid-Helper Bot V5 + DKPARSE SCREEN-ONLY + ApogeeBot")
    print(f"Connecté en tant que {bot.user} ({bot.user.id})")
    print(f"#Main channel ID: {MAIN_CHANNEL_ID}")
    print(f"#logs-raid channel ID: {LOGS_RAID_CHANNEL_ID or 'NON CONFIGURE'}")
    print(f"#dkparse channel ID: {DKPARSE_CHANNEL_ID or 'NON CONFIGURE'}")
    print(f"DKPARSE window: {DKPARSE_MAX_DAYS} jours")
    print("DKPARSE OCR: " + ("RapidOCR OK" if (RapidOCR is not None and Image is not None and np is not None) else "INDISPONIBLE"))
    print(f"ApogeeBot: {'ACTIVE' if UWU_PEWPEW_ENABLED else 'DESACTIVE'}")


@bot.event
async def setup_hook():
    for command in (rh_list_context, dkparse_context):
        try:
            bot.tree.add_command(command)
        except app_commands.CommandAlreadyRegistered:
            pass

    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f"{len(synced)} commande(s) synchronisée(s) sur le serveur de test.")
    else:
        synced = await bot.tree.sync()
        print(f"{len(synced)} commande(s) globale(s) synchronisée(s).")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN manquant dans .env")
    if not MAIN_CHANNEL_ID:
        raise SystemExit("MAIN_CHANNEL_ID manquant dans .env")

    bot.run(TOKEN)
