import os
import re
import io
import json
import html as html_lib
import asyncio
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
# APogee Discord Bot - Raid-Helper + DKPARSE
# =============================================================================
# DKPARSE V1:
# - #logs-raid is the whitelist: only UwU reports posted there are eligible.
# - A DKPARSE post may be validated against reports posted around the preceding
#   8 calendar days (configurable).
# - Multiple UwU uploads of the same raid are harmless: results are deduplicated
#   by character + boss + report date + parse value.
# - The Discord post timestamp is the reference date, NOT the day the bot runs.
# - No RL / 33% guild computation: presence in #logs-raid is the guild-raid proof.
# - The UwU parser is deliberately defensive. If UwU changes its HTML/JS format
#   or a parse cannot be proven, the bot returns "A VERIFIER" instead of paying.
#
# Required .env additions for DKPARSE:
#   LOGS_RAID_CHANNEL_ID=123...
#   DKPARSE_CHANNEL_ID=123...
#
# Optional:
#   DKPARSE_MAX_DAYS=8
#   UWU_SERVER=Icecrown
#   DKPARSE_DEBUG=false
#
# Context menus:
#   RH List      -> existing Raid-Helper feature
#   DKPARSE      -> right click a player's DKPARSE message
#
# Slash commands:
#   /main-audit
#   /dkparse-check message_id:<id>
#   /dkparse-logs
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
    candidates: List[Dict[str, Any]] = []

    if isinstance(ocr_result, tuple) and ocr_result:
        ocr_result = ocr_result[0]

    if not isinstance(ocr_result, list):
        return None, candidates

    for item in ocr_result:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue

        box = item[0]
        raw_text = str(item[1] or "").strip()
        try:
            confidence = float(item[2]) if len(item) >= 3 else 0.5
        except Exception:
            confidence = 0.5

        token = _clean_ocr_token(raw_text)
        if not WOW_NAME_RE.fullmatch(token):
            continue
        if token.lower() in OCR_IGNORE_WORDS:
            continue

        x1, y1, x2, y2 = _box_geometry(box)
        cx = (x1 + x2) / 2.0
        cy = (y1 + y2) / 2.0

        # Le nom UwU est normalement dans la partie haute du screenshot.
        if image_height > 0 and cy > image_height * 0.42:
            continue

        y_score = 1.0 - min(max(cy / max(image_height, 1), 0.0), 1.0)
        x_target = image_width * 0.25
        x_distance = abs(cx - x_target) / max(image_width, 1)
        x_score = max(0.0, 1.0 - x_distance)
        score = confidence * 3.0 + y_score * 2.2 + x_score * 1.2

        candidates.append({
            "name": token,
            "raw": raw_text,
            "confidence": confidence,
            "x": round(cx, 1),
            "y": round(cy, 1),
            "score": round(score, 4),
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)

    if not candidates:
        return None, candidates

    best = candidates[0]
    if best["confidence"] < 0.35:
        return None, candidates

    return best["name"], candidates


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
    s = re.sub(r"\s+", " ", (raw or "").strip().lower())
    for alias, label in sorted(DKPARSE_SPECS.items(), key=lambda kv: -len(kv[0])):
        if alias in s:
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

    return _dedupe_fight_urls_per_boss_mode(out)


def _dedupe_fight_urls_per_boss_mode(urls: List[str]) -> List[str]:
    """
    UwU exposes the same boss kill under many URLs: a bare ``?boss=X``, a
    ``?boss=X&mode=Y`` summary, and one ``?boss=X&mode=Y&attempt=N&s=..&f=..``
    per individual pull. All of them show the same roster/parse table for
    that fight, so fetching every variant multiplies requests to
    uwu-logs.xyz for nothing (up to ~40-45 pages per report). This is what
    was triggering the 429/502 rate limiting seen in DKPARSE_DEBUG.

    Keep a single, best-quality URL per (boss, mode) pair: prefer the
    mode-qualified summary (no attempt/s/f), then a bare boss URL, and only
    fall back to one attempt-level URL if nothing else was linked for that
    fight. ``?boss=all`` is dropped entirely (large, redundant aggregate
    page not needed once we fetch per-boss pages).
    """
    best: Dict[Tuple[str, str], Tuple[int, str]] = {}
    for url in urls:
        query = parse_qs(urlsplit(url).query)
        boss = (query.get("boss") or [""])[0]
        if not boss or boss == "all":
            continue
        mode = (query.get("mode") or [""])[0]
        has_attempt = "attempt" in query
        if mode and not has_attempt:
            rank = 2
        elif not mode and not has_attempt:
            rank = 1
        else:
            rank = 0
        key = (boss, mode)
        current = best.get(key)
        if current is None or rank > current[0]:
            best[key] = (rank, url)
    return [url for _, url in best.values()]


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
    if char_l not in raw_html.lower():
        return []

    boss = boss_from_uwu_url(fight_url)
    if not boss or boss in DKPARSE_EXCLUDED_BOSSES:
        return []

    hits: List[ParseHit] = []

    rows = re.findall(r"(?is)<tr\b[^>]*>.*?</tr>", raw_html)
    relevant_rows = [row for row in rows if char_l in row.lower()]

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


# -----------------------------------------------------------------------
# Rate-limited / cached / retrying fetcher for uwu-logs.xyz
# -----------------------------------------------------------------------
# The old code opened a brand new aiohttp session per request with no shared
# pacing or cache, so a single DKPARSE check could fire 150-200+ concurrent
# requests at uwu-logs.xyz (one per fight-page variant, per report). That
# reliably triggered HTTP 429 and cascading 502s. Everything now goes
# through _fetch_uwu(): a small global concurrency cap, a minimum delay
# between requests, a short-lived cache (repeated /dkparse-check calls in
# the same minute reuse the same pages instead of refetching), and a
# retry-with-backoff on 429/502/503.
_UWU_CACHE: Dict[str, Tuple[float, str, str]] = {}  # url -> (fetched_at, final_url, body)
_UWU_CACHE_TTL = 600.0  # seconds
_UWU_GLOBAL_SEMAPHORE = asyncio.Semaphore(2)
_UWU_MIN_DELAY = 0.4  # seconds between consecutive requests, across all tasks
_uwu_last_request_at = 0.0
_uwu_pacing_lock = asyncio.Lock()


async def _uwu_pace() -> None:
    global _uwu_last_request_at
    async with _uwu_pacing_lock:
        now = asyncio.get_event_loop().time()
        wait = _uwu_last_request_at + _UWU_MIN_DELAY - now
        if wait > 0:
            await asyncio.sleep(wait)
        _uwu_last_request_at = asyncio.get_event_loop().time()


async def _fetch_uwu_raw(
    url: str,
    *,
    method: str = "GET",
    json_body: Optional[Dict[str, Any]] = None,
    retries: int = 3,
) -> Tuple[str, str]:
    cache_key = url if json_body is None else f"{url}::{json.dumps(json_body, sort_keys=True)}"
    cached = _UWU_CACHE.get(cache_key)
    if cached and (asyncio.get_event_loop().time() - cached[0]) < _UWU_CACHE_TTL:
        dkp_debug("UWU CACHE HIT", {"url": url, "body": json_body})
        return cached[1], cached[2]

    timeout = aiohttp.ClientTimeout(total=25)
    headers = {
        "User-Agent": "ApogeeDKParseBot/1.3 (+Discord guild tooling)",
        "Accept": "application/json, text/html;q=0.8",
    }

    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        async with _UWU_GLOBAL_SEMAPHORE:
            await _uwu_pace()
            try:
                async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                    request_cm = (
                        session.post(url, json=json_body)
                        if method == "POST"
                        else session.get(url, allow_redirects=True)
                    )
                    async with request_cm as resp:
                        body = await resp.text(errors="replace")

                        if resp.status in (429, 502, 503) and attempt < retries - 1:
                            wait = 1.5 * (attempt + 1)
                            dkp_debug(
                                "UWU RETRY",
                                {
                                    "url": url,
                                    "method": method,
                                    "status": resp.status,
                                    "attempt": attempt + 1,
                                    "wait_s": wait,
                                },
                            )
                            last_exc = RuntimeError(f"UwU HTTP {resp.status}")
                            await asyncio.sleep(wait)
                            continue

                        dkp_debug(
                            "UWU LECTURE REPONSE",
                            {
                                "requested_url": url,
                                "method": method,
                                "body_sent": json_body,
                                "final_url": str(resp.url),
                                "status": resp.status,
                                "content_type": resp.headers.get("Content-Type"),
                                "body_chars": len(body),
                            },
                        )

                        if resp.status != 200:
                            raise RuntimeError(f"UwU HTTP {resp.status}")

                        final_url = str(resp.url)
                        _UWU_CACHE[cache_key] = (asyncio.get_event_loop().time(), final_url, body)
                        return final_url, body
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt < retries - 1:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise

    raise last_exc or RuntimeError("UwU: échec après plusieurs tentatives")


async def _fetch_uwu(url: str, retries: int = 3) -> Tuple[str, str]:
    return await _fetch_uwu_raw(url, method="GET", retries=retries)


async def _fetch_uwu_json(url: str, payload: Dict[str, Any], retries: int = 3) -> Any:
    _, body = await _fetch_uwu_raw(url, method="POST", json_body=payload, retries=retries)
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("UwU: réponse JSON invalide") from exc


async def fetch_uwu_url(url: str) -> Tuple[str, str]:
    return await _fetch_uwu(url)


async def fetch_uwu_report(report: UwuReport) -> Tuple[str, str]:
    dkp_debug("UWU LECTURE DEBUT", report.url)
    return await _fetch_uwu(report.url)


# -----------------------------------------------------------------------
# Character API (uwu-logs' own "Characters" page)
# -----------------------------------------------------------------------
# uwu-logs.xyz already tracks, per character and per boss, the all-time
# BEST parse and the exact report it came from (visible on the site's
# character page, in the "Date" column). Instead of crawling every raid
# report + every fight-page variant to find a character's parses, we just
# ask uwu-logs directly: POST /character {name, server, spec}. This is the
# same request the site's own frontend makes (character.js), confirmed by
# inspecting it in the browser network tab. It returns, per boss, a
# "points" field (the displayed % * 100) and a "report_id" that is exactly
# the report URL slug — so we can cross-check it against the reports
# whitelisted in #logs-raid without ever fetching a report body.
UWU_CHARACTER_API = "https://uwu-logs.xyz/character"
UWU_PROFILE_SPECS = (1, 2, 3)  # the 3 talent-tree slots shown on the site

# class_i -> class name, exactly matching the insertion order of the
# CLASSES dict in uwu-logs' own c_player_classes.py (confirmed by reading
# that file's source: Death Knight, Druid, Hunter, Mage, Paladin, Priest,
# Rogue, Shaman, Warlock, Warrior, 0-indexed in that order).
UWU_CLASS_NAMES = {
    0: "Death Knight",
    1: "Druid",
    2: "Hunter",
    3: "Mage",
    4: "Paladin",
    5: "Priest",
    6: "Rogue",
    7: "Shaman",
    8: "Warlock",
    9: "Warrior",
}

# (class_i, spec index 1-3) -> DKPARSE canonical spec label (DKPARSE_SPECS
# values). Spec order per class also comes straight from c_player_classes
# (CLASSES dict order), and was independently confirmed against the
# guild's own class/spec table. Only the DPS specs DKPARSE actually pays
# for are listed; tank/healer specs intentionally have no entry, so a hit
# on those stays unproven (spec="") exactly like before.
UWU_CLASS_SPEC_TO_DKPARSE_SPEC = {
    (0, 3): "UH",        # Death Knight - Unholy
    (1, 1): "Boomie",    # Druid - Balance
    (1, 2): "FeralDPS",  # Druid - Feral Combat
    (2, 2): "MM",        # Hunter - Marksmanship
    (3, 2): "MageFeu",   # Mage - Fire
    (4, 3): "Ret",       # Paladin - Retribution
    (5, 3): "SP",        # Priest - Shadow
    (6, 2): "Combat",    # Rogue - Combat
    (7, 2): "",          # Shaman - Enhancement (not a recognized DKPARSE spec)
    (8, 2): "Démono",    # Warlock - Demonology
    (9, 2): "Fwar",      # Warrior - Fury
}


def uwu_report_id_to_url(report_id: str) -> str:
    return canonical_uwu_url(f"https://uwu-logs.xyz/reports/{report_id}/")


async def fetch_uwu_character_profile(character: str, spec: int) -> Dict[str, Any]:
    payload = {"name": character, "server": UWU_SERVER, "spec": str(spec)}
    data = await _fetch_uwu_json(UWU_CHARACTER_API, payload)
    return data if isinstance(data, dict) else {}


async def fetch_uwu_character_best_parses(
    character: str,
) -> List[Tuple[str, float, str, str]]:
    """
    Query all 3 spec slots and return (boss, percent, report_id, spec) for
    every boss where uwu-logs has a recorded best parse for this character.
    class_i (also returned by the API) lets us resolve the exact spec name
    for that query, so DKP can be auto-validated instead of always landing
    in "à vérifier". Only 3 lightweight requests total, cached/rate-limited
    like everything else hitting uwu-logs.
    """
    results: List[Tuple[str, float, str, str]] = []

    async def one_spec(spec: int):
        try:
            profile = await fetch_uwu_character_profile(character, spec)
        except Exception as exc:
            dkp_debug(
                "UWU CHARACTER API ECHEC",
                {"character": character, "spec": spec, "error": f"{type(exc).__name__}: {exc}"},
            )
            return

        class_i = profile.get("class_i")
        spec_label = ""
        if isinstance(class_i, int):
            spec_label = UWU_CLASS_SPEC_TO_DKPARSE_SPEC.get((class_i, spec), "")

        bosses = profile.get("bosses")
        if not isinstance(bosses, dict):
            return

        for boss_raw, entry in bosses.items():
            if not isinstance(entry, dict):
                continue
            points = entry.get("points")
            report_id = entry.get("report_id")
            if not isinstance(points, (int, float)) or points <= 0 or not report_id:
                continue
            boss = normalize_boss(boss_raw)
            if not boss or boss in DKPARSE_EXCLUDED_BOSSES:
                continue
            results.append((boss, round(float(points) / 100.0, 2), str(report_id), spec_label))

    await asyncio.gather(*(one_spec(spec) for spec in UWU_PROFILE_SPECS))


    dkp_debug(
        "UWU CHARACTER API RESULTAT",
        {"character": character, "count": len(results), "results": results},
    )
    return results


# -----------------------------------------------------------------------
# Historique des meilleurs parses (pour détecter les améliorations)
# -----------------------------------------------------------------------
DKPARSE_HISTORY_FILE = APP_DIR / "dkparse_history.json"


def load_dkparse_history() -> Dict[str, Dict[str, float]]:
    try:
        with open(DKPARSE_HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    history: Dict[str, Dict[str, float]] = {}
    for char, bosses in data.items():
        if not isinstance(bosses, dict):
            continue
        clean: Dict[str, float] = {}
        for boss, value in bosses.items():
            try:
                clean[str(boss)] = float(value)
            except (TypeError, ValueError):
                continue
        history[str(char).lower()] = clean
    return history


def save_dkparse_history(history: Dict[str, Dict[str, float]]) -> None:
    try:
        tmp_path = DKPARSE_HISTORY_FILE.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        tmp_path.replace(DKPARSE_HISTORY_FILE)
    except OSError as exc:
        dkp_debug("HISTORY SAVE ECHEC", {"error": repr(exc)})


DKPARSE_HISTORY: Dict[str, Dict[str, float]] = load_dkparse_history()


def record_parse_improvements(
    character: str, hits: List[ParseHit]
) -> List[Tuple[ParseHit, Optional[float]]]:
    """
    Compare each proven hit against the character's best known parse for
    that boss (persisted in dkparse_history.json, survives bot restarts)
    and update the record when it's beaten.

    Returns (hit, previous_best) pairs for every new personal best.
    previous_best is None the first time a boss is recorded for that
    character (shown as "first recorded parse" rather than a delta).
    """
    key = character.lower()
    per_boss = DKPARSE_HISTORY.setdefault(key, {})
    improvements: List[Tuple[ParseHit, Optional[float]]] = []

    for hit in hits:
        previous = per_boss.get(hit.boss)
        if previous is None or hit.parse > previous:
            improvements.append((hit, previous))
            per_boss[hit.boss] = hit.parse

    if improvements:
        save_dkparse_history(DKPARSE_HISTORY)

    return improvements


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
    text = (content or "").lower()
    found = []
    for alias, boss in sorted(BOSS_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if alias in text and boss not in found and boss not in DKPARSE_EXCLUDED_BOSSES:
            found.append(boss)
    return found


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
    Export DKPARSE non cumulatif.

    Un joueur ne touche qu'un seul bonus DKPARSE pour son post : le meilleur
    palier atteint parmi tous les boss validés. Les autres boss restent dans
    l'export pour conserver le détail de la vérification, mais leur DKP vaut 0
    afin d'éviter tout double crédit côté Kromaddon si celui-ci additionne les
    lignes BOSS.
    """
    best_hit = max(hits, key=lambda h: dkp_for_parse(h.parse)) if hits else None
    total = dkp_for_parse(best_hit.parse) if best_hit else 0

    lines = [
        "KROMADDON_DKPARSE_V1",
        f"PLAYER={character}",
        f"POST_DATE={post_time.date().isoformat()}",
    ]

    reward_assigned = False
    for h in hits:
        amount = 0
        if (
            best_hit is not None
            and not reward_assigned
            and h is best_hit
        ):
            amount = total
            reward_assigned = True

        lines.append(
            "BOSS="
            + h.boss
            + f";PARSE={h.parse:.2f};DKP={amount}"
            + (f";SPEC={h.spec}" if h.spec else "")
        )

    lines.append(f"TOTAL={total}")
    lines.append("END")
    return "\n".join(lines)


async def analyze_dkparse_message(
    guild: discord.Guild, message: discord.Message
) -> Tuple[str, Optional[str]]:
    if DKPARSE_CHANNEL_ID and message.channel.id != DKPARSE_CHANNEL_ID:
        return (
            "⚠️ Ce message n'est pas dans le salon DKPARSE configuré "
            "(`DKPARSE_CHANNEL_ID`).",
            None,
        )

    # Source du personnage DKPARSE = le nom visible sur le screenshot.
    # Aucun fallback sur le nom Discord ni sur #Main.
    character, ocr_detail = await extract_character_from_screenshot(message)

    if not character:
        return (
            "⚠️ **DKPARSE À VÉRIFIER**\n"
            f"{ocr_detail}\n"
            "Aucun personnage n'est supposé à partir du nom Discord.",
            None,
        )

    dkp_debug(
        "PERSONNAGE DKPARSE",
        {
            "discord_author": f"{message.author} ({message.author.id})",
            "character_from_screen": character,
            "detail": ocr_detail,
        },
    )

    requested_bosses = requested_bosses_from_post(message.content)
    requested_spec = requested_spec_from_post(message.content)

    reports = await scan_whitelisted_reports(guild, message.created_at)
    reports = [r for r in reports if report_within_delay(r, message.created_at)]

    if not reports:
        return (
            f"⚠️ **DKPARSE À VÉRIFIER — {character}**\n"
            f"Personnage lu sur le screen : **{character}**\n"
            f"Aucun rapport UwU exploitable trouvé dans #logs-raid pour cette demande.\n"
            f"Fenêtre DKPARSE : {DKPARSE_MAX_DAYS} jours avant le post du joueur.",
            None,
        )

    # #logs-raid reste la whitelist : seule une date de rapport qui y a été
    # postée (et dans la fenêtre DKPARSE) peut faire gagner du DKP.
    eligible_dates = {
        r.report_date.date() for r in reports if r.report_date
    }

    errors: List[str] = []
    all_hits: List[ParseHit] = []

    try:
        # uwu-logs conserve déjà, par personnage et par boss, le meilleur
        # parse jamais réalisé + le rapport exact d'où il vient (le même
        # calcul que la page "Characters" du site). 3 requêtes légères
        # (une par spec) remplacent tout le crawl de rapports/pages de
        # combat qui saturait uwu-logs.xyz.
        best_parses = await fetch_uwu_character_best_parses(character)
    except Exception as exc:
        error_text = f"UwU character API: {type(exc).__name__}: {exc}"
        errors.append(error_text)
        dkp_debug("UWU CHARACTER API ECHEC GLOBAL", {"error": error_text})
        best_parses = []

    for boss, percent, report_id, spec_label in best_parses:
        report_url = uwu_report_id_to_url(report_id)
        report_date = report_date_from_url(report_url)
        eligible = bool(report_date) and report_date.date() in eligible_dates

        dkp_debug(
            "UWU CHARACTER BOSS",
            {
                "boss": boss,
                "percent": percent,
                "report_id": report_id,
                "report_date": report_date.isoformat() if report_date else None,
                "eligible": eligible,
            },
        )

        if not eligible:
            # Meilleur parse réel, mais réalisé en dehors des logs guilde
            # whitelistés dans #logs-raid (ou hors fenêtre DKPARSE) : ne
            # compte pas comme preuve pour ce post.
            continue

        all_hits.append(
            ParseHit(
                character=character,
                boss=boss,
                parse=percent,
                spec=spec_label,
                report_url=report_url,
                report_date=report_date,
                evidence="uwu-logs character API",
            )
        )

    hits = dedupe_hits(all_hits)

    if requested_bosses:
        hits = [h for h in hits if h.boss in requested_bosses]

    # Only parses that actually earn DKP are relevant.
    hits = [h for h in hits if dkp_for_parse(h.parse) > 0]

    # Comparaison au meilleur parse connu par boss, tous specs/validations
    # confondus (l'amélioration en elle-même n'est pas soumise à la même
    # exigence de preuve de spec que le paiement du DKP).
    improvements = record_parse_improvements(character, hits)

    # Spec validation:
    # - explicit spec in post -> every proven different spec is rejected
    # - no explicit spec -> a hit with unknown spec is not auto-paid
    valid_hits: List[ParseHit] = []
    uncertain_hits: List[ParseHit] = []
    for hit in hits:
        if requested_spec:
            if hit.spec and hit.spec != requested_spec:
                continue
            if not hit.spec:
                uncertain_hits.append(hit)
                continue
            valid_hits.append(hit)
        else:
            if hit.spec:
                valid_hits.append(hit)
            else:
                uncertain_hits.append(hit)

    lines = [
        f"**DKPARSE — {character}**",
        f"Personnage lu sur le screen : **{character}**",
        f"Post : {message.created_at.strftime('%d/%m/%Y')} — "
        f"fenêtre : {DKPARSE_MAX_DAYS} jours",
        f"Logs guilde examinés : {len(reports)}",
        "",
    ]

    if valid_hits:
        # DKPARSE = bonus unique par post/joueur, NON cumulatif entre les boss.
        # On garde toutes les améliorations dans le rapport, puis on paie
        # uniquement le meilleur palier atteint.
        best_hit = max(valid_hits, key=lambda h: dkp_for_parse(h.parse))
        total = dkp_for_parse(best_hit.parse)

        for hit in valid_hits:
            amount = dkp_for_parse(hit.parse)
            date_txt = (
                hit.report_date.strftime("%d/%m/%Y")
                if hit.report_date else "date UwU inconnue"
            )
            marker = "🏆" if hit is best_hit else "✅"
            suffix = (
                f"→ **+{amount} DKP VALIDÉ**"
                if hit is best_hit
                else f"→ palier **+{amount} DKP** (non cumulé)"
            )
            lines.append(
                f"{marker} **{hit.boss}** — {hit.parse:.2f}% — "
                f"{hit.spec} — {date_txt} {suffix}"
            )

        lines += [
            "",
            f"**BONUS DKPARSE VALIDÉ : +{total} DKP**",
            "_Un seul bonus est attribué : le meilleur palier du post._",
        ]

    if uncertain_hits:
        lines += ["", "**⚠️ À VÉRIFIER (spec non prouvée par UwU)**"]
        for hit in uncertain_hits:
            lines.append(f"• {hit.boss} — {hit.parse:.2f}%")

    if improvements:
        lines += ["", "**📈 AMÉLIORATION DE PARSE**"]
        for hit, previous in sorted(improvements, key=lambda pair: pair[0].boss.lower()):
            if previous is None:
                lines.append(f"• {hit.boss} — {hit.parse:.2f}% (premier parse enregistré)")
            else:
                lines.append(
                    f"• {hit.boss} — {previous:.2f}% → {hit.parse:.2f}% "
                    f"(+{hit.parse - previous:.2f} pts)"
                )

    if not valid_hits:
        lines += [
            "",
            "⚠️ **DKPARSE À VÉRIFIER**",
            f"Personnage lu sur le screen : **{character}**",
            "Aucune parse exploitable correspondant à ce personnage n'a été "
            "trouvée dans les pages de combat des rapports guilde éligibles.",
        ]
        if errors:
            lines.append(
                f"{len(errors)} rapport(s) UwU n'ont pas pu être lus. "
                "Active `DKPARSE_DEBUG=true` pour le détail."
            )
        return "\n".join(lines), None

    export = build_kromaddon_dkparse_export(character, message.created_at, valid_hits)
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
    description="Affiche les logs guilde UwU trouvés sur les 8 derniers jours.",
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
    await bot.process_commands(message)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if after.channel.id == MAIN_CHANNEL_ID:
        await enforce_main_message(after)


@bot.event
async def on_ready():
    print("Apogee Raid-Helper Bot V4 + DKPARSE")
    print(f"Connecté en tant que {bot.user} ({bot.user.id})")
    print(f"#Main channel ID: {MAIN_CHANNEL_ID}")
    print(f"#logs-raid channel ID: {LOGS_RAID_CHANNEL_ID or 'NON CONFIGURE'}")
    print(f"#dkparse channel ID: {DKPARSE_CHANNEL_ID or 'NON CONFIGURE'}")
    print(f"DKPARSE window: {DKPARSE_MAX_DAYS} jours")
    print("DKPARSE OCR: " + ("RapidOCR OK" if (RapidOCR is not None and Image is not None and np is not None) else "INDISPONIBLE"))


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
