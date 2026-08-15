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
from urllib.parse import urlsplit, urlunsplit
import sys

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


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
            await interaction.followup.send(
                chunk,
                view=ExportView(export_text) if index == len(chunks) - 1 else None,
            )
    except Exception as exc:
        if RH_DEBUG:
            print(repr(exc))
        await interaction.followup.send(f"❌ RH List : {exc}")


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


async def fetch_uwu_report(report: UwuReport) -> Tuple[str, str]:
    timeout = aiohttp.ClientTimeout(total=25)
    headers = {
        "User-Agent": "ApogeeDKParseBot/1.0 (+Discord guild tooling)",
        "Accept": "text/html,application/xhtml+xml",
    }
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(report.url, allow_redirects=True) as resp:
            body = await resp.text(errors="replace")
            if resp.status != 200:
                raise RuntimeError(f"UwU HTTP {resp.status}")
            return str(resp.url), body


async def scan_whitelisted_reports(
    guild: discord.Guild, reference_time: datetime
) -> List[UwuReport]:
    channel = await get_text_channel(
        guild, LOGS_RAID_CHANNEL_ID, "LOGS_RAID_CHANNEL_ID"
    )

    # Small safety margin: user said 7 or 8 days is not important. We use the
    # configured 8-day rule plus one day when scanning Discord, then reject a
    # report whose actual date is outside the allowed window.
    after = reference_time - timedelta(days=DKPARSE_MAX_DAYS + 1)
    before = reference_time + timedelta(days=1)

    reports: List[UwuReport] = []
    seen: set[str] = set()

    async for msg in channel.history(
        limit=None, after=after, before=before, oldest_first=True
    ):
        combined = msg.content or ""
        for embed in msg.embeds:
            if embed.url:
                combined += "\n" + embed.url
            if embed.description:
                combined += "\n" + embed.description

        for url in extract_uwu_urls(combined):
            if url in seen:
                continue
            seen.add(url)
            reports.append(
                UwuReport(
                    url=url,
                    message_id=msg.id,
                    posted_at=msg.created_at,
                    report_date=report_date_from_url(url),
                    label=(msg.content or "")[:120],
                )
            )

    dkp_debug("Rapports whitelistés", [r.url for r in reports])
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
    total = sum(dkp_for_parse(h.parse) for h in hits)
    lines = [
        "KROMADDON_DKPARSE_V1",
        f"PLAYER={character}",
        f"POST_DATE={post_time.date().isoformat()}",
    ]
    for h in hits:
        lines.append(
            "BOSS="
            + h.boss
            + f";PARSE={h.parse:.2f};DKP={dkp_for_parse(h.parse)}"
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

    main_map, _ = await build_main_map(guild)
    character = explicit_character_from_post(message.content) or main_map.get(message.author.id)
    if not character:
        return (
            "⚠️ **DKPARSE À VÉRIFIER**\n"
            "Impossible de déterminer le personnage. Le joueur n'a pas de main "
            "reconnu dans #Main et le message ne contient pas `perso: Nom`.",
            None,
        )

    requested_bosses = requested_bosses_from_post(message.content)
    requested_spec = requested_spec_from_post(message.content)

    reports = await scan_whitelisted_reports(guild, message.created_at)
    reports = [r for r in reports if report_within_delay(r, message.created_at)]

    if not reports:
        return (
            f"❌ **DKPARSE REFUSÉE — {character}**\n"
            f"Aucun rapport UwU whitelisté dans #logs-raid sur les "
            f"{DKPARSE_MAX_DAYS} jours précédant ce post.",
            None,
        )

    all_hits: List[ParseHit] = []
    errors: List[str] = []

    # Avoid hammering UwU. A normal 8-day Apogee window is small.
    semaphore = asyncio.Semaphore(3)

    async def inspect_one(report: UwuReport):
        async with semaphore:
            try:
                final_url, raw_html = await fetch_uwu_report(report)
                report2 = UwuReport(
                    url=canonical_uwu_url(final_url),
                    message_id=report.message_id,
                    posted_at=report.posted_at,
                    report_date=report.report_date or report_date_from_url(final_url),
                    label=report.label,
                )
                if character.lower() not in raw_html.lower():
                    return

                structured = parse_hits_from_json(
                    json_candidates_from_html(raw_html),
                    character,
                    report2.url,
                    report2.report_date,
                )
                if structured:
                    all_hits.extend(structured)
                else:
                    all_hits.extend(
                        parse_hits_from_text(
                            raw_html, character, report2.url, report2.report_date
                        )
                    )
            except Exception as exc:
                errors.append(f"{report.url}: {exc}")

    await asyncio.gather(*(inspect_one(r) for r in reports))
    hits = dedupe_hits(all_hits)

    if requested_bosses:
        hits = [h for h in hits if h.boss in requested_bosses]

    # Only parses that actually earn DKP are relevant.
    hits = [h for h in hits if dkp_for_parse(h.parse) > 0]

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
        f"Post : {message.created_at.strftime('%d/%m/%Y')} — "
        f"fenêtre : {DKPARSE_MAX_DAYS} jours",
        f"Logs guilde examinés : {len(reports)}",
        "",
    ]

    if valid_hits:
        total = 0
        for hit in valid_hits:
            amount = dkp_for_parse(hit.parse)
            total += amount
            date_txt = (
                hit.report_date.strftime("%d/%m/%Y")
                if hit.report_date else "date UwU inconnue"
            )
            lines.append(
                f"✅ **{hit.boss}** — {hit.parse:.2f}% — "
                f"{hit.spec} — {date_txt} → **+{amount} DKP**"
            )
        lines += ["", f"**TOTAL VALIDÉ : +{total} DKP**"]

    if uncertain_hits:
        lines += ["", "**⚠️ À VÉRIFIER (spec non prouvée par UwU)**"]
        for hit in uncertain_hits:
            lines.append(f"• {hit.boss} — {hit.parse:.2f}%")

    if not valid_hits:
        lines += [
            "",
            "⚠️ **Aucun bonus auto-validé.**",
            "Le bot n'a pas trouvé dans les rapports whitelistés une preuve "
            "suffisamment complète `personnage + boss + parse + spec`.",
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
        await interaction.followup.send(
            report,
            view=DKParseExportView(export) if export else None,
        )
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
        reports = await scan_whitelisted_reports(interaction.guild, now)
        reports = [r for r in reports if report_within_delay(r, now)]
        if not reports:
            await interaction.followup.send(
                "Aucun rapport UwU récent trouvé dans #logs-raid.", ephemeral=True
            )
            return

        lines = [f"**{len(reports)} rapport(s) UwU whitelisté(s)**", ""]
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
