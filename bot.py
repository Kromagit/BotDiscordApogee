
import os
import re
import io
import json
import asyncio
from dataclasses import dataclass
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from pathlib import Path
import sys

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

RAID_HELPER_API = "https://raid-helper.dev/api/v4/events/{event_id}"
WOW_NAME_RE = re.compile(r"^[A-Za-z]{2,12}$")

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


def normalize_status(raw: Any) -> str:
    s = str(raw or "").strip().lower()

    aliases = {
        "signed": "signed",
        "signup": "signed",
        "accepted": "signed",
        "confirmed": "signed",
        "yes": "signed",
        "1": "signed",

        "bench": "bench",
        "benched": "bench",
        "reserve": "bench",
        "backup": "bench",

        "late": "late",
        "lateness": "late",

        "tentative": "tentative",
        "maybe": "tentative",
        "tent": "tentative",

        "absence": "absence",
        "absent": "absence",
        "declined": "absence",
        "no": "absence",
    }
    if s in aliases:
        return aliases[s]

    # Raid-Helper/custom-template tolerant matching.
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


def get_first(d: Dict[str, Any], keys: Iterable[str], default=None):
    for key in keys:
        if key in d and d[key] not in (None, ""):
            return d[key]
    return default


def find_signup_lists(obj: Any) -> List[List[Dict[str, Any]]]:
    """Find likely signup arrays without depending on one Raid-Helper JSON shape."""
    found = []

    def walk(x: Any, parent_key: str = ""):
        if isinstance(x, dict):
            for k, v in x.items():
                kl = str(k).lower()
                if isinstance(v, list) and any(t in kl for t in ("signup", "sign_up", "attendee", "participant", "member")):
                    dict_items = [i for i in v if isinstance(i, dict)]
                    if dict_items:
                        found.append(dict_items)
                walk(v, kl)
        elif isinstance(x, list):
            # A raw list whose entries look like Discord signups.
            dict_items = [i for i in x if isinstance(i, dict)]
            if dict_items:
                score = 0
                for i in dict_items[:10]:
                    keys = {str(k).lower() for k in i.keys()}
                    if keys & {"userid", "user_id", "discordid", "discord_id", "id"}:
                        score += 1
                    if keys & {"status", "type", "signup", "signup_type", "signupstatus"}:
                        score += 1
                if score >= 2:
                    found.append(dict_items)
            for v in x:
                walk(v, parent_key)

    walk(obj)

    # Deduplicate list identities/content.
    unique = []
    seen = set()
    for lst in found:
        marker = json.dumps(lst, sort_keys=True, ensure_ascii=False, default=str)
        if marker not in seen:
            seen.add(marker)
            unique.append(lst)
    return unique


def extract_signup(entry: Dict[str, Any]) -> Optional[Signup]:
    user_obj = entry.get("user") if isinstance(entry.get("user"), dict) else {}
    member_obj = entry.get("member") if isinstance(entry.get("member"), dict) else {}

    uid = get_first(entry, [
        "userId", "user_id", "discordId", "discord_id", "memberId", "member_id"
    ])
    if uid is None:
        uid = get_first(user_obj, ["id", "userId", "user_id"])
    if uid is None:
        uid = get_first(member_obj, ["id", "userId", "user_id"])

    # Only accept bare "id" as a fallback if it looks like a Discord snowflake.
    if uid is None:
        candidate = entry.get("id")
        if candidate and str(candidate).isdigit() and len(str(candidate)) >= 15:
            uid = candidate

    if uid is None or not str(uid).isdigit():
        return None

    name = get_first(entry, [
        "displayName", "display_name", "username", "userName", "name", "memberName"
    ])
    if not name:
        name = get_first(user_obj, ["global_name", "display_name", "username", "name"])
    if not name:
        name = get_first(member_obj, ["display_name", "username", "name"])
    if not name:
        name = f"Discord {uid}"

    raw_status = get_first(entry, [
        "status", "signupStatus", "signup_status", "signupType", "signup_type",
        "type", "state", "button", "buttonName", "button_name", "category"
    ], "signed")

    # Some Raid-Helper payloads expose a nested signup object.
    nested = entry.get("signup")
    if isinstance(nested, dict):
        raw_status = get_first(nested, [
            "status", "type", "name", "button", "buttonName", "category"
        ], raw_status)

    return Signup(int(uid), str(name), normalize_status(raw_status))


def parse_event_payload(payload: Any) -> Tuple[List[Signup], str]:
    event_title = "Raid-Helper Event"
    if isinstance(payload, dict):
        event_title = str(get_first(payload, ["title", "name", "eventTitle", "event_title"], event_title))
        if isinstance(payload.get("event"), dict):
            event_title = str(get_first(payload["event"], ["title", "name"], event_title))

    lists = find_signup_lists(payload)
    signups: Dict[int, Signup] = {}

    for lst in lists:
        for entry in lst:
            s = extract_signup(entry)
            if s:
                # Last representation wins if API duplicates a user in nested views.
                signups[s.user_id] = s

    # Direct common keys fallback.
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
    headers = {"User-Agent": "ApogeeRaidHelperBot/1.0"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"Raid-Helper API HTTP {resp.status}: {body[:300]}")
            try:
                return json.loads(body)
            except json.JSONDecodeError as exc:
                raise RuntimeError("Raid-Helper n'a pas renvoyé un JSON valide.") from exc


async def get_main_channel(guild: discord.Guild) -> discord.TextChannel:
    channel = guild.get_channel(MAIN_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        try:
            channel = await guild.fetch_channel(MAIN_CHANNEL_ID)
        except discord.HTTPException:
            channel = None
    if not isinstance(channel, discord.TextChannel):
        raise RuntimeError("MAIN_CHANNEL_ID ne correspond pas à un salon texte accessible.")
    return channel


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

        # If an author has several historical messages, newest valid one wins.
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
    if ADMIN_ROLE_ID and any(r.id == ADMIN_ROLE_ID for r in interaction.user.roles):
        return True
    return False


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
    def __init__(self, export_text: str):
        super().__init__(timeout=600)
        self.export_text = export_text

    @discord.ui.button(label="Export Kromaddon", style=discord.ButtonStyle.primary)
    async def export_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = self.export_text.encode("utf-8")
        file = discord.File(io.BytesIO(data), filename="Kromaddon_RaidHelper.txt")
        await interaction.response.send_message(
            "Export prêt à copier dans Kromaddon :",
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
        await interaction.response.send_message("Commande utilisable uniquement sur le serveur.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False, thinking=True)

    try:
        payload = await fetch_raid_helper_event(message.id)
        signups, event_title = parse_event_payload(payload)
        if not signups:
            raise RuntimeError(
                "Aucune inscription Raid-Helper détectée dans la réponse API. "
                "Le format de l'API a peut-être changé."
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
                dm_result = await dm_unrecognized(interaction.guild, s)
                unrecognized.append((s, dm_result))

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

        report = "\n".join(lines)
        export_text = "RH|" + "|".join(sorted(export_items, key=str.lower))

        chunks = split_discord_text(report)
        for i, chunk in enumerate(chunks):
            if i == len(chunks) - 1:
                await interaction.followup.send(chunk, view=ExportView(export_text))
            else:
                await interaction.followup.send(chunk)

    except Exception as exc:
        await interaction.followup.send(f"❌ RH List : {exc}")


@app_commands.context_menu(name="RH List")
async def rh_list_context(interaction: discord.Interaction, message: discord.Message):
    await run_rh_list(interaction, message)


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


async def enforce_main_message(message: discord.Message):
    if message.author.bot or not message.guild or message.channel.id != MAIN_CHANNEL_ID:
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

    # Scan existing valid messages.
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

    # Same main already belongs to someone else -> reject the new message.
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

    # New valid declaration replaces any older declaration from same Discord ID.
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
    print(f"Connecté en tant que {bot.user} ({bot.user.id})")
    print(f"#Main channel ID: {MAIN_CHANNEL_ID}")


class ApogeeBot(commands.Bot):
    pass


async def setup_commands():
    pass


@bot.event
async def setup_hook():
    try:
        bot.tree.add_command(rh_list_context)
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
