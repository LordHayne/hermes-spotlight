# Card format

hermes-spotlight renders parts of an answer as native GTK cards. A card is a
fenced code block with the language `card` and a JSON object inside:

````markdown
Tuesday is the nicest day of the week.

```card
{"type": "weather", "place": "Vienna", "min": 8, "max": 19, "icon": "sun"}
```

Planning a walk? Tuesday is your day.
````

The text around the block is rendered as usual; the block itself becomes the
card. Nothing about this is Hermes-specific: any agent or tool that can write
Markdown can produce cards.

- [How the agent learns the format](#how-the-agent-learns-the-format)
- [Rendering rules](#rendering-rules)
- [Templates: `weather`, `events`, `package`](#templates)
- [Blueprint: `blocks`](#blueprint-blocks)
- [Adding a new card type](#adding-a-new-card-type)

## How the agent learns the format

The spotlight sends `CARD_PROMPT` (in `hermes-spotlight.py`) as part of the
`system_message` of every request. It describes the three templates and the
block blueprint, and tells the model when a card is worth it.

Some models skim past the end of a long system prompt. For questions that
touch a template topic (weather, appointments, packages), the spotlight also
appends a one-line, conditional reminder to the user message
(`CARD_TOPICS` / `with_card_reminder`). The model still decides whether the
question really asks for that data. The `blocks` blueprint gets no reminder:
the agent chooses when to build one.

Set `"cards": false` in the config to send neither.

## Rendering rules

- **Never fatal.** Invalid JSON, an unknown `type`, or a card with nothing
  valid in it is dropped silently. The text answer always stands on its own.
- **Plain text only.** All fields are shown as plain text, except `text`
  blocks, which allow the inline Markdown subset (`**bold**`, `*italic*`,
  `` `code` ``, links).
- **Capped.** Strings and lists are cut to the limits listed below, so a
  runaway answer cannot blow up the window.
- **Links are http(s).** `url` actions and Markdown links must start with
  `http://` or `https://`; anything else is not made clickable.
- **No duplicate lists.** If the text contains a Markdown list (2+ items) and
  at least 80 % of its items appear in a card of the same answer, the list is
  hidden. Copying the answer keeps the list and drops the card JSON.
- **Streaming.** A card block that is still streaming is hidden until it is
  complete, so half-written JSON never shows.
- **Unknown fields** are ignored; every field except `type` is optional.

## Templates

Templates are designed cards for common questions. They look better than
anything composed freely, so prefer them when the data fits.

### `weather`

```json
{
  "type": "weather",
  "place": "Vienna",
  "title": "Tue, Oct 6",
  "icon": "sun",
  "min": 8,
  "max": 19,
  "summary": "clear skies · morning fog",
  "days": [
    {"day": "Tue", "icon": "sun", "min": 8, "max": 19, "note": "best day", "best": true},
    {"day": "Thu", "icon": "storm", "min": 12, "max": 19, "note": "umbrella"}
  ]
}
```

| Field | Type | Notes |
|---|---|---|
| `place`, `title` | string | shown as "place · title" |
| `icon` | string | `sun` `clear` `partly` `cloud` `fog` `showers` `rain` `storm` `snow` `wind`; unknown → 🌡️ |
| `min`, `max` | number | °C, rounded |
| `summary` | string | one line of conditions |
| `days[]` | object | up to 7 tiles: `day`, `icon`, `min`, `max`, `note`, `best` (highlighted) |

### `events`

```json
{
  "type": "events",
  "title": "Today · Mon, Oct 5",
  "events": [
    {"time": "all day", "title": "Mom's birthday"},
    {"time": "14:00", "end": "14:45", "title": "Dentist", "place": "Main Street 4", "next": true}
  ]
}
```

| Field | Type | Notes |
|---|---|---|
| `title` | string | the day |
| `events[]` | object | up to 10: `time` (`"HH:MM"` or `"all day"`), `end`, `title`, `place`, `next` (highlighted) |

The header shows the number of events. Order is kept as sent (all-day first,
then by time).

### `package`

```json
{
  "type": "package",
  "title": "Mechanical keyboard",
  "carrier": "DHL",
  "tracking": "00340434161094042557",
  "stage": "out",
  "status": "Out for delivery",
  "eta": "today 10–14",
  "events": [{"time": "Oct 5 07:12", "text": "Loaded onto the delivery vehicle"}]
}
```

| Field | Type | Notes |
|---|---|---|
| `title`, `carrier`, `tracking` | string | tracking number is selectable |
| `stage` | string | `label` `shipped` `transit` `out` `delivered` fill a 5-step bar; `problem` turns it orange |
| `status` | string | latest status in words |
| `eta` | string | hidden once delivered |
| `events[]` | object | up to 4, newest first: `time`, `text` |

## Blueprint: `blocks`

For everything else the agent composes its own card from building blocks:
comparisons, specs, rankings, checklists, stats. One card per answer, only
when it reads better than prose.

```json
{
  "type": "blocks",
  "icon": "🎮",
  "title": "RTX 4070 vs RX 7800 XT",
  "subtitle": "1440p · average of 12 games",
  "value": "≈ tie",
  "blocks": [
    {"kind": "stats", "items": [{"label": "RTX 4070", "value": "112 fps", "highlight": true}]},
    {"kind": "table", "columns": ["", "RTX 4070", "RX 7800 XT"], "rows": [["VRAM", "12 GB", "16 GB"]]},
    {"kind": "actions", "items": [{"label": "Which one for me?", "ask": "which one fits my setup?"}]}
  ]
}
```

Header fields (all optional): `icon` (an emoji, ≤ 8 chars), `title` (≤ 80),
`subtitle`, `value` (big text on the right, ≤ 24). Up to 10 blocks are
rendered (the prompt asks for 8); blocks with an unknown `kind` are skipped
and don't count.

| `kind` | Fields | Renders as | Limits |
|---|---|---|---|
| `text` | `text` | a paragraph, inline Markdown | 600 chars |
| `stats` | `items[]`: `label`, `value`, `icon`, `note`, `highlight` | tiles side by side | 6 tiles |
| `bars` | `items[]`: `label`, `value` (0–100), `text` | labelled level bars | 10 rows |
| `list` | `items[]`: `lead`, `title`, `sub`, `highlight` (or plain strings) | rows with a lead column (time, rank, number) | 10 rows |
| `kv` | `items[]`: `["key", "value"]` or `{"key", "value"}` | key/value pairs | 14 pairs |
| `table` | `columns[]`, `rows[][]` | a grid; an empty first column header styles the first column as row labels | 6 × 10 |
| `progress` | `steps[]`, `current` (index), `problem` | a step bar with labels | 8 steps |
| `chips` | `items[]` (strings) | tags | 16 |
| `actions` | `items[]`: `label` + `ask` *or* `url` | buttons | 4 |

**Actions.** An `ask` button sends its question to the agent when clicked,
as if the user had typed it (≤ 300 chars). A `url` button opens an
http(s) link in the default browser. Buttons with neither are dropped.

## Adding a new card type

A template is worth it when a topic comes up often and a designed card is
clearly nicer than a `blocks` card. Everything lives in `hermes-spotlight.py`:

1. **Renderer.** Write `_mytype_card(d: dict) -> Gtk.Widget`. Build it from
   the `_lbl(text, css_class, **kw)` helper and the existing classes
   (`card`, `cardtitle`, `cardsub`, `daytile`, `eventrow`, …) so it follows
   every theme. Cap lists and strings.
2. **Register** it in `CARD_RENDERERS`.
3. **Describe** the JSON in `CARD_PROMPT`: when to send it, the fields,
   "real data only". Keep it short — it is sent with every request.
4. **Optional reminder.** Add `("<topic in words>", re.compile(...))` to
   `CARD_TOPICS` if models tend to forget the card for that topic. Keep the
   keywords loose; the reminder is conditional. Avoid words that are also
   names (the agent is called Hermes — so is a parcel carrier).
5. **CSS.** New classes go into `build_css()`, using theme colors (`t[...]`).
6. **Tests.** Add a case to `tests/ui_features.py`: render the card from
   JSON, check the visible text, and check that odd input doesn't crash.
   Run `./verify.sh`.

To preview a card without asking the agent, feed it straight into the
renderer:

```python
widgets = _md_widgets('Text\n\n```card\n{"type": "mytype", ...}\n```', THEMES["tokyo-night"])
```
