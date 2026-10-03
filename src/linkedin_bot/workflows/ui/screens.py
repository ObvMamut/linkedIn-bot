"""Every screen the bot shows, as a pure function from data to text and buttons."""

import hashlib
from collections.abc import Sequence
from typing import Any

from linkedin_bot.adapters.linkedin.models import Conversation
from linkedin_bot.domain.models import DEFAULT_PRIORITIES, SearchPreferences
from linkedin_bot.domain.workspace import DigestSettings, Pin, SavedSearch
from linkedin_bot.workflows.ui.keyboard import Button, Keyboard, Screen

HOME = Button("🏠 Menu", "m:main")
CANCEL = Button("✖ Cancel", "x:cancel")
LOADING = "⏳ Loading…"
LANGUAGES = {
    "en": "English",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "it": "Italian",
    "pl": "Polish",
}
AGE_PRESETS = ("7", "14", "30", "60", "any")
LIST_FIELDS = {
    "locations": "location",
    "roles": "role",
    "must_keywords": "must-have word",
    "nice_keywords": "nice-to-have word",
    "exclusions": "excluded word",
    "blocked_firms": "blocked firm",
    "preferred_industries": "preferred industry",
}
PRIORITY_LABELS = {
    "role": "Role fit",
    "location": "Location",
    "firm": "Pinned / preferred firms",
    "recency": "Freshness",
    "language": "Language",
}


def pin_key(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:10]


def clip(text: str, size: int) -> str:
    return text if len(text) <= size else text[: size - 1] + "…"


def main_menu() -> Screen:
    return Screen(
        "What would you like to do?",
        [
            [Button("📬 Messages", "m:msgs"), Button("🔎 Search", "m:search")],
            [Button("⭐ Pinned", "m:pins"), Button("💼 Jobs", "m:jobs")],
            [Button("💾 Saved", "m:saved"), Button("⚙️ Settings", "m:set")],
            [Button("📊 Status", "m:status")],
        ],
    )


def error_screen(message: str, back: str = "m:main") -> Screen:
    return Screen(message, [[Button("⬅ Back", back), HOME]])


def messages_screen(items: Sequence[Conversation], skipped: int) -> Screen:
    if not items:
        text = "No conversations found."
    else:
        lines = [
            f"• {c.title}{' [unread]' if c.unread else ''}"
            + (f" · {c.when}" if c.when else "")
            + f"\n  {clip(c.snippet, 140)}"
            for c in items
        ]
        text = "📬 Latest conversations\n\n" + "\n".join(lines)
        if skipped:
            text += f"\n({skipped} unrecognised row{'s' if skipped != 1 else ''} skipped)"
    return Screen(text, [[Button("🔄 Refresh", "m:msgs:r"), HOME]])


def search_menu() -> Screen:
    return Screen(
        "What do you want to search for?",
        [
            [Button("👤 People", "s:people"), Button("🏢 Firms", "s:firms")],
            [Button("💼 Jobs", "s:jobs"), HOME],
        ],
    )


def search_prompt(kind: str) -> Screen:
    if kind == "jobs":
        text = "Send the job search words, optionally followed by “in <place>” (for example: security intern in Berlin)."
    else:
        text = f"Send the search words for {kind} (for example: security engineer Berlin)."
    return Screen(text, [[CANCEL]])


def card(
    header: str,
    items: list[dict[str, Any]],
    index: int,
    token: str,
    *,
    pinnable: bool,
    saved_id: str | None = None,
    can_save: bool = True,
) -> Screen:
    """One result per page with Prev/Next, so a long list never floods the chat."""
    item = items[index]
    lines = [header, "", item["title"]]
    for extra in item.get("lines", []):
        if extra:
            lines.append(extra)
    lines.append(f"\n{index + 1}/{len(items)}")
    nav: list[Button] = []
    if index > 0:
        nav.append(Button("◀ Prev", f"r:{token}:{index - 1}"))
    if index < len(items) - 1:
        nav.append(Button("Next ▶", f"r:{token}:{index + 1}"))
    actions: list[Button] = []
    if pinnable:
        actions.append(Button("⭐ Pin", f"pin:{token}:{index}"))
    if item.get("url"):
        actions.append(Button("🔗 Open", url=item["url"]))
    keyboard: Keyboard = [row for row in (nav, actions) if row]
    extra_row: list[Button] = []
    if can_save and saved_id is None:
        extra_row.append(Button("💾 Save search", f"sv:{token}"))
    if saved_id is not None:
        extra_row.append(Button("🗑 Delete saved", f"sd:{saved_id}"))
    extra_row.append(HOME)
    keyboard.append(extra_row)
    return Screen("\n".join(lines), keyboard)


def pins_screen(pins: Sequence[Pin]) -> Screen:
    if not pins:
        return Screen(
            "⭐ Nothing pinned yet. Search for firms or people and tap Pin on a result.",
            [[Button("🔎 Search", "m:search"), HOME]],
        )
    rows = [
        [
            Button(
                f"{'🏢' if pin.kind == 'firm' else '👤'} {clip(pin.name, 40)}",
                f"pd:{pin_key(pin.url)}",
            )
        ]
        for pin in pins
    ]
    rows.append([HOME])
    firms = sum(1 for p in pins if p.kind == "firm")
    return Screen(f"⭐ Pinned: {firms} firm(s), {len(pins) - firms} person(s)", rows)


def pin_detail(pin: Pin) -> Screen:
    key = pin_key(pin.url)
    lines = [f"{'🏢' if pin.kind == 'firm' else '👤'} {pin.name}", pin.url]
    lines.append(f"Note: {pin.note}" if pin.note else "Note: none")
    lines.append(f"Tags: {', '.join(pin.tags)}" if pin.tags else "Tags: none")
    if pin.kind == "firm":
        lines.append(
            f"Job board: {pin.board_provider}/{pin.board_slug}"
            if pin.board_slug
            else "Job board: not linked"
        )
    rows = [[Button("✏️ Edit note", f"nt:{key}"), Button("🏷 Tags", f"tg:{key}")]]
    if pin.kind == "firm":
        rows.append([Button("🔎 Find job board", f"lb:{key}")])
    rows.append([Button("🗑 Unpin", f"up:{key}"), Button("🔗 Open", url=pin.url)])
    rows.append([Button("⬅ Pinned", "m:pins"), HOME])
    return Screen("\n".join(lines), rows)


def saved_screen(saved: Sequence[SavedSearch]) -> Screen:
    if not saved:
        return Screen(
            "💾 No saved searches yet. Run a search and tap Save search.",
            [[Button("🔎 Search", "m:search"), HOME]],
        )
    icons = {"people": "👤", "firms": "🏢", "jobs": "💼"}
    rows = [[Button(f"{icons[s.kind]} {clip(s.name, 40)}", f"sr:{s.id}")] for s in saved]
    rows.append([HOME])
    return Screen("💾 Saved searches (tap one to see what is new)", rows)


def no_new_screen(name: str, saved_id: str) -> Screen:
    return Screen(
        f"💾 {name}\n\nNo new results since the last run.",
        [
            [Button("🔄 Run again", f"sr:{saved_id}"), Button("🗑 Delete saved", f"sd:{saved_id}")],
            [HOME],
        ],
    )


def prompt(text: str, back: str = "m:main") -> Screen:
    return Screen(text, [[CANCEL]])


def listing(values: Sequence[str]) -> str:
    return ", ".join(values) if values else "none"


def settings_main(preferences: SearchPreferences) -> Screen:
    remote = {None: "any", True: "preferred", False: "on-site only"}[preferences.remote]
    lines = [
        "⚙️ Settings (plain rules, no AI)",
        "",
        f"📍 Locations: {listing(preferences.locations)}  · remote: {remote}",
        f"🔑 Roles: {listing(preferences.roles)}",
        f"🏢 Blocked firms: {listing(preferences.blocked_firms)}",
        f"🗣 Languages: {listing(preferences.languages) if preferences.languages else 'any'}"
        f"  · max age: {preferences.max_age_days or 'any'} days",
        "↕ Priorities: " + " > ".join(PRIORITY_LABELS[p] for p in preferences.priorities),
    ]
    return Screen(
        "\n".join(lines),
        [
            [Button("📍 Locations", "set:loc"), Button("🔑 Role keywords", "set:roles")],
            [Button("🏢 Firms", "set:firm"), Button("🗣 Languages & age", "set:lang")],
            [Button("↕ Priorities", "set:prio"), Button("🕘 Digest & alerts", "set:digest")],
            [Button("📝 Interests", "set:interests"), HOME],
        ],
    )


def delete_buttons(field: str, values: Sequence[str]) -> Keyboard:
    return [
        [Button(f"❌ {clip(value, 40)}", f"set:del:{field}:{i}")] for i, value in enumerate(values)
    ]


def locations_screen(preferences: SearchPreferences) -> Screen:
    remote = {None: "any", True: "preferred", False: "on-site only"}[preferences.remote]
    internship = "yes" if preferences.internship else "no preference"
    rows = delete_buttons("locations", preferences.locations)
    rows.append([Button("➕ Add location", "set:add:locations")])
    rows.append(
        [
            Button(f"🏠 Remote: {remote}", "set:remote"),
            Button(f"🎓 Internship: {internship}", "set:intern"),
        ]
    )
    rows.append([Button("⬅ Settings", "m:set"), HOME])
    return Screen(
        "📍 Locations (tap one to remove it)\nThe first match wins; a location you do not list counts as a gap.",
        rows,
    )


def roles_screen(preferences: SearchPreferences) -> Screen:
    sections = (
        ("roles", "Roles", preferences.roles),
        ("must_keywords", "Must have", preferences.must_keywords),
        ("nice_keywords", "Nice to have", preferences.nice_keywords),
        ("exclusions", "Exclude", preferences.exclusions),
    )
    lines = [
        "🔑 Role keywords",
        "Must have and Exclude are hard filters; the others only rank.",
        "",
    ]
    rows: Keyboard = []
    for field, label, values in sections:
        lines.append(f"{label}: {listing(values)}")
        rows.append([Button(f"➕ {label}", f"set:add:{field}")])
        rows.extend(delete_buttons(field, values))
    rows.append([Button("⬅ Settings", "m:set"), HOME])
    return Screen("\n".join(lines), rows)


def firms_screen(preferences: SearchPreferences, pinned: int) -> Screen:
    rows = [[Button("➕ Block a firm", "set:add:blocked_firms")]]
    rows.extend(delete_buttons("blocked_firms", preferences.blocked_firms))
    rows.append([Button("➕ Preferred industry", "set:add:preferred_industries")])
    rows.extend(delete_buttons("preferred_industries", preferences.preferred_industries))
    rows.append([Button("⬅ Settings", "m:set"), HOME])
    text = (
        "🏢 Firm rules\n"
        f"Blocked firms are never shown: {listing(preferences.blocked_firms)}\n"
        f"Preferred industries get a boost: {listing(preferences.preferred_industries)}\n"
        f"Pinned firms ({pinned}) always get the biggest boost."
    )
    return Screen(text, rows)


def language_screen(preferences: SearchPreferences) -> Screen:
    rows: Keyboard = []
    chosen = set(preferences.languages)
    pairs = list(LANGUAGES.items())
    for start in range(0, len(pairs), 3):
        rows.append(
            [
                Button(f"{'✅' if code in chosen else '⬜'} {name}", f"set:lang:{code}")
                for code, name in pairs[start : start + 3]
            ]
        )
    age = str(preferences.max_age_days) if preferences.max_age_days else "any"
    rows.append(
        [
            Button(f"{'🔘' if preset == age else '⚪'} {preset}", f"set:age:{preset}")
            for preset in AGE_PRESETS
        ]
    )
    rows.append([Button("⬅ Settings", "m:set"), HOME])
    text = (
        "🗣 Languages & age\n"
        "Postings in a language you did not tick are hidden when the language is clear. "
        "None ticked means any.\n"
        f"Maximum age in days: {age} (older postings are hidden)."
    )
    return Screen(text, rows)


def priorities_screen(preferences: SearchPreferences) -> Screen:
    rows: Keyboard = []
    order = preferences.priorities
    for index, name in enumerate(order):
        row = [Button(f"{index + 1}. {PRIORITY_LABELS[name]}", "x:noop")]
        if index > 0:
            row.append(Button("⬆", f"set:prio:up:{name}"))
        if index < len(order) - 1:
            row.append(Button("⬇", f"set:prio:dn:{name}"))
        if len(order) > 1:
            row.append(Button("✖", f"set:prio:rm:{name}"))
        rows.append(row)
    missing = [name for name in DEFAULT_PRIORITIES if name not in order]
    for name in missing:
        rows.append([Button(f"➕ {PRIORITY_LABELS[name]}", f"set:prio:add:{name}")])
    rows.append([Button("⬅ Settings", "m:set"), HOME])
    return Screen(
        "↕ Priorities: the higher a criterion, the more it counts in the score. "
        "Criteria you remove are ignored (hard filters still apply).",
        rows,
    )


def digest_screen(settings: DigestSettings) -> Screen:
    def mark(value: bool) -> str:
        return "✅ on" if value else "⬜ off"

    text = (
        "🕘 Digest & alerts (nothing runs unless you switch it on)\n\n"
        f"Daily digest: {mark(settings.enabled)} at {settings.local_time}\n"
        f"Alerts for new openings at pinned firms: {mark(settings.alerts_boards)}\n"
        f"Alerts for unread LinkedIn messages: {mark(settings.alerts_messages)}\n"
        f"Quiet hours: {settings.quiet_start}–{settings.quiet_end}"
    )
    return Screen(
        text,
        [
            [Button(f"Digest: {mark(settings.enabled)}", "dg:toggle")],
            [
                Button("⏪ −30 min", "dg:time:-"),
                Button(settings.local_time, "x:noop"),
                Button("+30 min ⏩", "dg:time:+"),
            ],
            [Button(f"Pinned-firm alerts: {mark(settings.alerts_boards)}", "dg:alerts_boards")],
            [Button(f"Message alerts: {mark(settings.alerts_messages)}", "dg:alerts_messages")],
            [Button("⬅ Settings", "m:set"), HOME],
        ],
    )


def interests_screen(preferences: SearchPreferences, llm_enabled: bool) -> Screen:
    note = (
        "An AI model will use this together with your CV to flag internships at pinned firms."
        if llm_enabled
        else "AI matching is not enabled yet (no local model installed), so this is saved for later "
        "and ranking uses your plain rules only."
    )
    text = f"📝 Interests\n\n{preferences.interests or '(empty)'}\n\n{note}"
    return Screen(
        text, [[Button("✏️ Edit", "set:interests:edit")], [Button("⬅ Settings", "m:set"), HOME]]
    )


def status_screen(
    *,
    mode: str,
    paused: bool,
    linkedin_state: str,
    linkedin_ok: str | None,
    pinned: int,
    saved: int,
    preferences_revision: int,
    digest_on: bool,
    llm_enabled: bool,
) -> Screen:
    lines = [
        "📊 Status",
        f"Mode: {mode} (nothing is ever sent to employers)",
        f"Paused: {'yes' if paused else 'no'}",
        f"LinkedIn session: {linkedin_state}"
        + (f" (last ok {linkedin_ok[:16]})" if linkedin_ok else ""),
        f"Pinned: {pinned}   Saved searches: {saved}",
        f"Settings revision: {preferences_revision}",
        f"Daily digest: {'on' if digest_on else 'off'}",
        f"AI matching: {'enabled' if llm_enabled else 'not enabled'}",
    ]
    return Screen("\n".join(lines), [[Button("🔄 Refresh", "m:status"), HOME]])
