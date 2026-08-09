"""Visible browser automation for lead search.

Opens a real Chrome window (headed) on the user's screen, goes to a search
engine, types the query, presses Enter, then visits each search result page
and extracts contact/business data. Every step is visible so the user can
watch the scraping happen live.

Requires: pip install playwright  (uses the already-installed Chrome via channel="chrome")
"""
import asyncio
import os
import re
import time
import urllib.parse

from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?91[\s.-]?)?(?:\(?0\d{2,4}\)?[\s.-]?)?"
    r"\d(?:[\s.-]?\d){9}(?!\d)"
)
_NOISE_EMAILS = {
    "example.com", "yourdomain.com", "domain.com", "email.com",
    "company.com", "sentry.io", "wixpress.com", "sentry",
}

_CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "Google", "Chrome", "Application", "chrome.exe",
    ),
]


def _chrome_channel():
    for path in _CHROME_CANDIDATES:
        if path and os.path.isfile(path):
            return "chrome"
    return None


def _clean_emails(raw: set) -> list[str]:
    out = []
    for e in raw:
        if e.endswith(".png") or e.endswith(".jpg") or e.endswith(".jpeg"):
            continue
        if e.split("@")[-1].lower() in _NOISE_EMAILS:
            continue
        if e not in out:
            out.append(e)
    return out[:5]


def _clean_phones(raw: set) -> list[str]:
    out = []
    for p in raw:
        digits = re.sub(r"\D", "", p)
        if digits.startswith("91") and len(digits) == 12:
            normalized = "+" + digits
        elif digits.startswith("0") and len(digits) == 11:
            normalized = "+91" + digits[1:]
        elif len(digits) == 10:
            normalized = "+91" + digits
        else:
            continue
        if normalized in out:
            continue
        out.append(normalized)
    return out[:5]


_CARD_RE = re.compile(
    r"(?ms)^(?P<name>[^\n]{3,80})\n"
    r"(?P<rating>\d(?:\.\d)?)\n"
    r"(?P<reviews>\(\d[\d,]*\.?\d*[Kk]?\))\n"
    r"(?P<category>[^\n]{2,40})\n"
    r"(?P<open>Open[^\n]*)\n"
    r"(?P<body>.*?)(?=\n[^\n]{3,80}\n\d(?:\.\d)?\n\(\d[\d,]*|\Z)"
)

_PROSE_RE = re.compile(
    r"(?m)^(?P<name>[^\n:]{3,80}): Rated (?P<rating>\d(?:\.\d)?)/5 "
    r"stars \((?P<reviews>[\d,.]+[Kk]?[^)]*)\)\. Located at "
    r"(?P<address>.*?)(?:\.\s+(?=[A-Z][a-z])|\.\s*$|$)"
    r"(?P<body>.*)$"
)

# Label-prefixed detail lines Google AI Mode uses inside "Top-rated ..."
# business list sections, e.g. "Location: ..." / "Specialty: ...".
_BUSINESS_LABEL_RE = re.compile(
    r"^(?:location|address|specialty|specialities|specialization|"
    r"specializations|services|services provided|service|rating|reviews?|"
    r"phone|phone number|"
    r"contact|contact (?:&|and) details|contact\s*(?:/|&|and)\s*\w+|"
    r"contact details|details|website|site|"
    r"timing|timings|hours|price|price range|highlights|category|cuisine|"
    r"email|founded|known for|best for|must try|signature|open|closes)\s*:",
    re.I,
)

_CONTINUATION_CUT_RE = re.compile(
    r"(?:\s+\|\s+"
    r"|\b(?:justdial|sulekha|indiamart|zaubacorp|linkedin|facebook|instagram|"
    r"youtube|wikipedia|yellow pages?|practo|hotfrog|yelp|asklaila|show all)\b"
    r"|\bai mode response is ready\b"
    r"|^[+]\d[\d\s().-]*)",
    re.I,
)

# Lines that are headings / table rows / prose, never business names.
_NAME_SKIP_RE = re.compile(
    r"^(?:top[-\s]?rated|top|if you|are you|you can|map data|terms|"
    r"location\s*/\s*zone|property (?:in|rates)|for sale|for rent|"
    r"average|dominant|the luxury|periphery|central|southern|high rental|"
    r"in 20\d\d|price|price per|₹|overview|metrics|verified|featured|"
    r"recommended|our picks|best\s|to help narrow|what is your|"
    r"could you tell|which of the|how much|what budget|tell me|"
    r"justdial\b|linkedin\b|facebook\b|instagram\b|twitter\b|youtube\b|"
    r"show all|magicpin\b|sulekha\b|indiamart\b|more to explore|"
    r"top bpo|top software|top it|popular bpo|"
    r"ai can make|double[- ]?check|if you tell me|let me know|"
    r"are you searching for these|"
    r"\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{2,4})",
    re.I,
)

# Businesses whose name came from a directory/social page should be dropped.
_LEAD_NAME_JUNK_RE = re.compile(
    r"(?:justdial|sulekha|indiamart|magicpin|linkedin|facebook|instagram|"
    r"twitter|youtube|google maps|show all|bharatbiz|buzzook|urbanpro|"
    r"more to explore)",
    re.I,
)

_NAME_SUFFIX_JUNK_RE = re.compile(
    r"(?:\s*(?:[-–—|:;,./]\s*)?(?:overview|details|profile|info|information|"
    r"reviews?|rating|photos?|timings?|contact|address|location|website|"
    r"more|listings?|official website|specialties|specialities))\s*$",
    re.I,
)


def _clean_business_name(name: str) -> str:
    """Strip Google UI headings / citation fragments glued to a business name
    (e.g. 'Cyberbells ITES Services Overview' -> 'Cyberbells ITES Services')."""
    name = (name or "").strip()
    prev = None
    while prev != name:
        prev = name
        name = _NAME_SUFFIX_JUNK_RE.sub("", name).strip()
    return name


def _parse_label_blocks(text: str) -> list[dict]:
    """Parse 'Name\nLocation: ...\nSpecialty: ...' style business records
    that Google AI Mode lists in its answer (e.g. 'Top-rated ... in ...')."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return []
    n = len(lines)
    blocks: list[dict] = []
    current: dict | None = None
    last_label: str | None = None

    def next_label(i: int) -> int:
        for j in range(i, min(i + 3, n)):
            if _BUSINESS_LABEL_RE.match(lines[j]):
                return j
        return -1

    def name_ok(ln: str) -> bool:
        return (
            3 <= len(ln) <= 80
            and "₹" not in ln
            and "sq. ft" not in ln
            and not _NAME_SKIP_RE.match(ln)
        )

    i = 0
    while i < n:
        ln = lines[i]
        m = _BUSINESS_LABEL_RE.match(ln)
        if m:
            if current is not None:
                key = m.group(0).partition(":")[0].strip().lower()
                value = " ".join(ln[m.end():].strip().split())
                current[key] = value
                last_label = key
            i += 1
            continue

        nxt = next_label(i + 1)
        if current is not None:
            if nxt != -1 and name_ok(ln):
                current = {"business_name": ln}
                blocks.append(current)
                last_label = None
            elif _CONTINUATION_CUT_RE.search(ln):
                last_label = None
            elif last_label and not _NAME_SKIP_RE.match(ln) and len(ln) < 200 \
                    and not ln.endswith("?"):
                prev = current.get(last_label) or ""
                current[last_label] = " ".join((prev + " " + ln).split())
            i += 1
            continue

        if nxt != -1 and name_ok(ln):
            current = {"business_name": ln}
            blocks.append(current)
            last_label = None
        i += 1

    for b in blocks:
        vals = " ".join(str(v) for v in b.values() if isinstance(v, str))
        phones = _clean_phones(set(_PHONE_RE.findall(vals)))
        emails = _clean_emails(set(_EMAIL_RE.findall(vals)))
        if phones and not b.get("phone"):
            b["phone"] = phones[0]
        if emails and not b.get("email"):
            b["email"] = emails[0]
        wm = re.search(r"(?:www\.|https?://)[^\s|]+", vals, re.I)
        if wm and not b.get("website"):
            b["website"] = wm.group(0).strip(".,")

    return blocks


_DASH_LEAD_RE = re.compile(
    r"^(?P<name>[^\n]{3,80}?)\s+[-–—]\s+(?P<body>.+)$",
    re.MULTILINE,
)
_DASH_LOC_PATTERN = re.compile(
    r"(?:located\s+(?:at|on|in)|situated\s+(?:at|on|in)|based\s+(?:out\s+of|in|at)|"
    r"headquartered\s+(?:at|in)|operating\s+(?:in|from|out\s+of)|"
    r"with\s+strong\s+roots\s+in)\s+(.*)",
    re.I,
)


def _extract_dash_address(body: str) -> str:
    """Pull the address out of a dash-list description. Stops at the first
    comma that starts a lowercase clause ('specializing in ...', 'offering
    ...') so trailing description text is not glued into the address."""
    m = _DASH_LOC_PATTERN.search(body or "")
    if not m:
        return ""
    parts = [p.strip() for p in m.group(1).split(",")]
    kept = [parts[0]]
    for p in parts[1:]:
        if p and p[0].isupper():
            kept.append(p)
        else:
            break
    addr = " ".join(x.strip(" .") for x in kept).strip()
    if addr.lower().startswith("the "):
        addr = addr[4:].strip()
    return addr


def _parse_dash_lines(text: str) -> list[dict]:
    """Parse 'Name – description' style lists Google AI Overview emits, e.g.
    'Trantor Software Pvt. Ltd. – Located at Plot No. 52, Industrial Area
    Phase II, specializing in enterprise technology solutions.'"""
    leads: list[dict] = []
    for m in _DASH_LEAD_RE.finditer(text or ""):
        name = " ".join(m.group("name").split()).strip(" .-–—|")
        body = " ".join(m.group("body").split())
        if not name or not body or len(name) < 3:
            continue
        if _NAME_SKIP_RE.match(name) or _LEAD_NAME_JUNK_RE.search(name):
            continue
        address = _extract_dash_address(body)
        phones = _clean_phones(set(_PHONE_RE.findall(body)))
        emails = _clean_emails(set(_EMAIL_RE.findall(body)))
        leads.append({
            "business_name": name,
            "rating": "",
            "reviews": "",
            "category": "",
            "price_range": "",
            "status": "",
            "address": address,
            "timing": "",
            "services": "",
            "highlights": "",
            "website": "",
            "email": emails[0] if emails else "",
            "phone": phones[0] if phones else "",
            "description": body,
        })
    return leads


def _merge_ai_leads(leads: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for lead in leads:
        name = (lead.get("business_name") or "").strip()
        key = re.sub(r"[^a-z0-9]+", "", name.lower())
        if not key:
            continue
        if key not in merged:
            merged[key] = dict(lead)
        else:
            for k, v in lead.items():
                if v and not merged[key].get(k):
                    merged[key][k] = v
    return list(merged.values())


_CARD_BODY_STOP_RE = re.compile(
    r"(?i)^(?:note:|are you looking|are you searching|if you are looking|"
    r"looking for (?:job|office|a|the)|is this answer|justdial|linkedin|"
    r"facebook|instagram|twitter|youtube|show all|ai mode response is ready|"
    r"related searches|more to explore|feedback|report an issue|"
    r"map data|terms|top[- ]?rated|top bpo|other (?:options|companies|"
    r"places|businesses)|also (?:consider|try|check|see)|read more|see more|"
    r"ai can make|double[- ]?check)"
)
_BUTTON_WORDS_RE = re.compile(
    r"(?:call|directions|get\s*directions|view\s*on\s*google\s*maps|"
    r"website|share|save|report|book\s*now|menu|call\s*now|more)",
    re.I,
)


def _cut_card_body(body: str) -> str:
    """A card's body should only hold that business's own detail lines. Google
    AI Mode sometimes dumps the whole rest of the answer (source citations,
    follow-up text, 'Show all') right after the last card - cut all of that.

    Button lines (Call / Directions / Website / Share ...) that sit at the
    start of a card are skipped, not treated as trailing junk.
    """
    out = []
    for ln in (body or "").splitlines():
        s = " ".join(ln.strip().split())
        if not s:
            continue
        if _CARD_BODY_STOP_RE.match(s):
            break
        if _BUTTON_WORDS_RE.sub("", s).strip() == "":
            continue
        out.append(s)
    return "\n".join(out)


def _parse_ai_business_cards(ai_text: str) -> list[dict]:
    """Parse the per-business details Google AI Mode embeds in its answer.

    Handles both the structured card layout (name / rating / reviews /
    category / address / timing / services) and the prose layout
    ("Name: Rated X/5 stars (N reviews). Located at ...").
    """
    leads: list[dict] = []
    text = ai_text or ""
    for m in _CARD_RE.finditer(text):
        name = m.group("name").strip()
        body = _cut_card_body(m.group("body") or "")
        fields = {}
        for key, label in (("address", "Address"), ("address", "Location"),
                           ("timing", "Timing"), ("services", "Services"),
                           ("highlights", "Highlights")):
            fm = re.search(rf"^{label}:\s*(.*)$", body, re.M)
            if fm:
                fields[key] = " ".join(fm.group(1).split())
        if not fields.get("address"):
            am = re.search(
                r"(?:Located\s+(?:at|in|on|nearby(?: on)?)|Situated\s+(?:in|at)|Based\s+in)\s+([^.\n]+)",
                body, re.I)
            if am:
                fields["address"] = " ".join(am.group(1).split()).strip(" .")
                if fields["address"].lower().startswith("the "):
                    fields["address"] = fields["address"][4:].strip()
        category_raw = m.group("category").strip()
        price_range = ""
        pm = re.match(r"^₹[\d.,\s\u2013\u2014-]+", category_raw)
        if pm:
            price_range = pm.group(0).strip(" \u2013\u2014-")
            rest = category_raw[pm.end():].strip(" \u2013\u2014-")
            if rest:
                category_raw = rest
        status_raw = m.group("open").strip()
        status = status_raw
        address = fields.get("address", "")
        sm = re.match(r"^(Open|Closed|Closes)(?![a-z])", status_raw)
        if sm and len(status_raw) > len(sm.group(0)):
            rest = status_raw[sm.end():].strip()
            if rest and not re.match(
                    r"^(?:until|till|at|now|24[ -]?hours?|[·\u2013\u2014\-]|closes?\b)",
                    rest, re.I):
                status = sm.group(1)
                if not address:
                    address = rest
        leads.append({
            "business_name": name,
            "rating": m.group("rating"),
            "reviews": m.group("reviews"),
            "category": category_raw,
            "price_range": price_range,
            "status": status,
            "address": address,
            "timing": fields.get("timing", ""),
            "services": fields.get("services", ""),
            "highlights": fields.get("highlights", ""),
            "website": "",
            "email": "",
            "phone": "",
            "description": " ".join(body.split()),
        })

    for m in _PROSE_RE.finditer(text):
        address = " ".join(m.group("address").split())
        body = m.group("body").strip()
        timing = ""
        tm = re.search(r"Open (.{10,120}?)(?:\.|$)", body, re.I)
        if tm:
            timing = tm.group(1).strip()
        leads.append({
            "business_name": m.group("name").strip(),
            "rating": m.group("rating"),
            "reviews": m.group("reviews"),
            "category": "",
            "status": "",
            "address": address,
            "timing": timing,
            "services": "",
            "highlights": "",
            "website": "",
            "email": "",
            "phone": "",
            "description": " ".join((address + " " + body).split()),
        })

    card_spans = [(m.start(), m.end()) for m in _CARD_RE.finditer(text)]
    masked = ""
    prev = 0
    for start, end in card_spans:
        masked += text[prev:start] + "\n" * (end - start)
        prev = end
    masked += text[prev:]
    leads.extend(_parse_label_blocks(masked))
    leads.extend(_parse_dash_lines(masked))
    merged = _merge_ai_leads(leads)
    detail_fields = (
        "rating", "reviews", "location", "address", "specialty", "services",
        "phone", "email", "category", "timing", "highlights", "description",
    )
    merged = [
        lead for lead in merged
        if any(lead.get(f) for f in detail_fields)
        and not _LEAD_NAME_JUNK_RE.search(lead.get("business_name") or "")
    ]
    for lead in merged:
        lead["business_name"] = _clean_business_name(
            lead.get("business_name") or "")
        if not lead.get("description") and lead.get("specialty"):
            lead["description"] = lead["specialty"]
    return merged


_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROFILE_DIR = os.path.join(_BASE_DIR, ".chrome_profile")
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


async def _dismiss_google_consent(page) -> None:
    try:
        if "consent.google.com" not in page.url:
            return
        for sel in ("button:has-text('Accept all')", "#L2AGLb",
                    "button:has-text('I agree')"):
            loc = page.locator(sel).first
            try:
                if await loc.count():
                    await loc.click(timeout=3000)
                    await asyncio.sleep(1)
                    return
            except Exception:
                pass
    except Exception:
        pass


_AI_ANSWER_JS = """(query) => {
    const t0 = (document.body.innerText || '').replace(/\\u00a0/g, ' ');
    if (t0.length < 300 && /not available for this search/i.test(t0))
        return '__unavailable__';
    let t = t0;
    const hdr = t.indexOf('AI Mode conversation');
    if (hdr >= 0) t = t.slice(hdr);
    const q = query.replace(/[?.!,]/g, '').toLowerCase().trim();
    const lines = t.split('\\n').map(s => s.trim()).filter(s => s);
    const out = [];
    let started = false;
    for (const line of lines) {
        const clean = line.replace(/[?.!,]/g, '').toLowerCase().trim();
        if (!started) {
            if (clean === q) { started = true; }
            continue;
        }
        if (line.toLowerCase().trim() === 'searching') continue;
        let cut = false;
        for (const m of ['Ask a follow-up', 'Suggested follow-up', 'Follow-up suggestions',
                         'Related searches', 'More to explore', 'Feedback', 'Report an issue',
                         'Start a new topic', 'If you are looking', 'Are you looking',
                         'AI can make mistakes', 'Show all', 'AI Mode response is ready',
                         'To help narrow down', 'What is your core objective']) {
            if (line.indexOf(m) === 0) { cut = true; break; }
        }
        if (cut) break;
        out.push(line);
    }
    const junk = /not available for this search|can't generate an ai overview|try again later|error translating content|हिन्दी|हिंदी/i;
    const cleaned = out.filter((ln, i) => !(i < 6 && junk.test(ln)));
    return cleaned.join('\\n');
}"""

_AI_OVERVIEW_JS = """() => {
    const cands = document.querySelectorAll('[data-md="516"]');
    if (cands.length) return cands[0].innerText || '';
    const t = (document.body.innerText || '').replace(/\\u00a0/g, ' ');
    if (/AI Overview is not available/i.test(t)) return '__unavailable__';
    const idx = t.indexOf('AI Overview');
    if (idx >= 0) {
        let slice = t.slice(idx);
        for (const m of ['Related searches', 'People also ask', 'Feedback']) {
            const j = slice.indexOf(m);
            if (j > 0) return slice.slice(0, j);
        }
        return slice;
    }
    return '';
}"""


_AI_TRAILING_CUT_RE = re.compile(
    r"^(?:if you are looking|are you looking|if you meant|ai can make mistakes|"
    r"show all|ai mode response is ready|related searches|more to explore|"
    r"feedback|report an issue|suggested follow-up|follow-up suggestions|"
    r"start a new topic|ask a follow-up|not sure|learn more|to help narrow|"
    r"what is your core|map data|looking for (?:job|office|a|the)|"
    r"is this answer|also (?:consider|try|check|see)|double[- ]?check|"
    r"table_title:|table_content:)",
    re.I,
)

_AI_DATE_LINE_RE = re.compile(
    r"^\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{2,4}\b",
    re.I,
)

_AI_ATTRIBUTION_LINE_RE = re.compile(
    r"^(?:"
    r"[+]\d[\d\s().-]{0,16}"                    # phone-number citation chips
    r"|"
    r"(?:Justdial|Sulekha|IndiaMART|Practo|UrbanClap|Urban Company|"
    r"LinkedIn|Facebook|Instagram|YouTube|Wikipedia|Yellow Pages|"
    r"AskLaila|BharatBizz?|India Business Directory|Hotfrog|Yelp|"
    r"Naukri(?:\.com)?|ZaubaCorp|Indeed|Glassdoor|Shine|Monster|"
    r"GoodFirms?|Clutch)"
    r"|"
    r"[A-Za-z][\w .&'()-]{1,40}?\s+India"       # "LinkedIn India" style chips
    r")$",
    re.I,
)


def _clean_ai_text(text: str) -> str:
    """Remove citation attribution chips (e.g. 'Justdial', '+1', 'LinkedIn
    India') and cut everything from follow-up/disclaimer lines onward, so the
    AI answer shown to the user is just the business content."""
    lines_out = []
    for ln in (text or "").split("\n"):
        s = " ".join(ln.strip().split())
        if not s:
            continue
        if _AI_TRAILING_CUT_RE.match(s):
            break
        if _AI_ATTRIBUTION_LINE_RE.match(s):
            continue
        if _AI_DATE_LINE_RE.match(s):
            continue
        lines_out.append(s)
    return "\n".join(lines_out)


async def _google_ai_overview(page) -> str:
    """Capture the AI Overview shown on the regular Google results page."""
    try:
        text = await page.evaluate(_AI_OVERVIEW_JS)
    except Exception:
        text = ""
    return " ".join((text or "").split())[:4000]


async def _ai_mode_completed(page) -> bool:
    """True when Google has finished streaming the AI Mode answer (it shows a
    'response is ready' chip / follow-up prompt at the end of the answer)."""
    try:
        return bool(await page.evaluate(
            """() => {
                const t = document.body.innerText || '';
                for (const m of ['AI Mode response is ready', 'Ask a follow-up',
                                 'Suggested follow-up', 'Follow-up suggestions',
                                 'Report an issue', 'Start a new topic',
                                 'Related searches', 'More to explore']) {
                    if (t.indexOf(m) >= 0) return true;
                }
                return false;
            }"""
        ))
    except Exception:
        return False


async def _capture_ai_mode_answer(page, query: str) -> str:
    """Poll the AI Mode page until the streamed answer finishes, then return
    the cleaned answer text ('' if the page is not on AI Mode)."""
    text = ""
    last_len = -1
    stable = 0
    polls = 0
    completion_seen = False
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        await asyncio.sleep(4)
        polls += 1
        try:
            await page.mouse.wheel(0, 1500)
        except Exception:
            pass
        try:
            text = await page.evaluate(_AI_ANSWER_JS, query)
        except Exception:
            text = ""
        if text == "__unavailable__":
            stable += 1
        elif len(text) == last_len:
            stable += 1
        else:
            stable = 0
        last_len = len(text)
        ready = await _ai_mode_completed(page)
        if ready:
            if completion_seen:
                break
            completion_seen = True
        elif (text and text != "__unavailable__"
              and len(text) >= 200 and stable >= 4):
            break
        if polls >= 8 and not text and not ready:
            print("[browser] AI Mode did not stream an answer within 32s.")
            break

    if text == "__unavailable__" or not text:
        print("[browser] AI Mode had no answer for this query - trying the "
              "regular AI Overview instead.")
        try:
            await page.goto(
                f"https://www.google.com/search?q={urllib.parse.quote(query)}",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await asyncio.sleep(4)
            text = await page.evaluate(_AI_OVERVIEW_JS)
        except Exception:
            text = ""

    if text == "__unavailable__":
        text = ""

    lines = (text or "").split("\n")
    text = "\n".join(" ".join(l.split()) for l in lines).strip()
    for prefix in ("AI Mode", "AI Overview"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    return _clean_ai_text(text)


async def _google_ai_mode_answer(page, query: str) -> str:
    """Go to Google AI Mode and capture the streamed AI answer.

    AI Mode answers stream progressively, and the per-business sections (e.g.
    'Verified Local ... Agencies') land at the very END of the answer. So we
    keep polling until the response actually finishes (Google shows the
    'response is ready' chip / a follow-up prompt), scrolling the page so the
    late sections load, and we do NOT truncate the captured text early. If
    Google reports no AI answer at all, fall back to the regular AI Overview.
    """
    clicked = False
    try:
        tab = page.locator("a").filter(has_text="AI Mode").first
        if await tab.count():
            try:
                await tab.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            await tab.click(timeout=6000)
            clicked = True
    except Exception:
        clicked = False

    if not clicked:
        try:
            await page.goto(
                f"https://www.google.com/search?q={urllib.parse.quote(query)}&udm=50",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            clicked = True
        except Exception:
            return ""

    text = await _capture_ai_mode_answer(page, query)
    return text[:100000] if clicked and text else ""


_LOOKUP_ADDR_PATTERN = re.compile(
    r"(?:address\s*(?::|is)\s*|located\s+(?:at|on|in)\s*|situated\s+(?:at|on|in)\s*|"
    r"based\s+(?:in|at)\s*)(.+)",
    re.I,
)


def _extract_lookup_address(t: str) -> str:
    """Pull an address out of an AI Mode lookup answer, stopping at the next
    field label / a comma that starts a lowercase clause / a sentence
    boundary (while keeping abbreviations like 'No.' intact)."""
    m = _LOOKUP_ADDR_PATTERN.search(t or "")
    if not m:
        return ""
    tail = re.split(
        r"\s+(?:phone|email|contact|timing|open|closes|website|rating)\b",
        m.group(1), flags=re.I)[0]
    parts = [p.strip() for p in tail.split(",")]
    kept = [parts[0]]
    for p in parts[1:]:
        if p and p[0].isupper():
            kept.append(p)
        else:
            break
    addr = ", ".join(kept).strip(" .")
    for mm in re.finditer(r"\.\s+[A-Z][a-z]", addr):
        prev = re.search(r"([A-Za-z]+)\s*$", addr[:mm.start()])
        if prev and prev.group(1).lower() in (
                "no", "st", "rd", "dr", "ltd", "pvt", "inc", "sr", "jr",
                "vs", "etc", "mr", "mrs", "ms", "e", "g"):
            continue
        addr = addr[:mm.start()]
        break
    addr = re.split(r"\s+\([a-z]", addr)[0]
    addr = re.split(
        r"\s+(?:and\s+neighboring|frequently|often|sometimes|"
        r"usually|typically|per|according\s+to)\b",
        addr, flags=re.I)[0]
    addr = addr.strip(" .")
    if addr.lower().startswith("the "):
        addr = addr[4:].strip()
    return addr


def _extract_lookup_fields(text: str) -> dict:
    """Pull rating / reviews / phone / email / address out of an AI Mode
    answer for a '<business> rating phone email address' style query."""
    t = text or ""
    fields: dict[str, str] = {}
    rm = re.search(
        r"(?:rating\s+(?:of|is)\s+|rated\s+|rating:\s*)(\d(?:\.\d)?)"
        r"\s*(?:out\s+of\s*5|/5|\bstar)", t, re.I)
    if not rm:
        rm = re.search(r"\b(\d(?:\.\d)?)\s*/\s*5\b", t)
    if rm:
        fields["rating"] = rm.group(1)
    vm = re.search(r"(\d[\d,]*\.?\d*[Kk]?)\s*(?:Google\s+)?reviews?\b", t, re.I)
    if vm:
        fields["reviews"] = vm.group(1)
    phones = _clean_phones(set(_PHONE_RE.findall(t)))
    if phones:
        fields["phone"] = phones[0]
    emails = _clean_emails(set(_EMAIL_RE.findall(t)))
    if emails:
        fields["email"] = emails[0]
    addr = _extract_lookup_address(t)
    if addr:
        fields["address"] = addr
    return fields


async def _ai_mode_lookup(page, business: str, loc: str) -> dict:
    """Search '<business> <loc> rating phone email address' in Google AI Mode
    and extract the business details from the answer."""
    query = " ".join(x for x in (business, loc) if x)
    query = f"{query} rating phone email address"
    print(f"[browser]  - AI Mode lookup: {query!r}")
    try:
        await page.goto(
            f"https://www.google.com/search?q={urllib.parse.quote(query)}&udm=50",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        text = await _capture_ai_mode_answer(page, query)
    except Exception as exc:
        print(f"[browser]  - AI Mode lookup error: {type(exc).__name__}: {exc}")
        return {}
    return _extract_lookup_fields(text)


async def _ai_mode_lookup_businesses(
    page, leads: list[dict], query: str, max_results: int,
) -> None:
    """For every business parsed from the AI answer, run one combined AI Mode
    lookup ('<name> <loc> rating phone email address') and merge rating /
    reviews / phone / email / address into the lead."""
    loc = ""
    m = re.match(r"^.*?\s+(?:in|at|near|nearby)\s+(.+)$", query, re.I)
    if m:
        loc = m.group(1).strip()
    for i, lead in enumerate(leads[:max_results], 1):
        name = (lead.get("business_name") or "").strip()
        if not name:
            continue
        print(f"[browser] ({i}/{min(len(leads), max_results)}) AI Mode "
              f"detail lookup for: {name}")
        info = await _ai_mode_lookup(page, name, loc)
        for k in ("rating", "reviews", "phone", "email", "address"):
            v = info.get(k)
            if v:
                lead[k] = v
        await asyncio.sleep(0.5)


async def _google_search(page, query: str, slow_mo: int) -> bool:
    """Type query + Enter on Google. Returns False if Google blocked the request."""
    await page.goto("https://www.google.com", wait_until="domcontentloaded", timeout=30000)
    await _dismiss_google_consent(page)
    await page.goto("https://www.google.com", wait_until="domcontentloaded", timeout=30000)
    box = page.locator("textarea[name=q], input[name=q]").first
    try:
        await box.click(timeout=8000)
        await box.type(query, delay=60)
        await box.press("Enter")
    except Exception:
        print("[browser] Search box not found on homepage - opening the "
              "search URL directly.")
        await page.goto(
            f"https://www.google.com/search?q={urllib.parse.quote(query)}",
            wait_until="domcontentloaded",
            timeout=30000,
        )
    await page.wait_for_load_state("domcontentloaded", timeout=30000)
    await asyncio.sleep(2.5)
    try:
        html = await page.content()
        if "unusual traffic" in html or "sorry/index" in page.url:
            return False
    except Exception:
        pass
    return True


_BUSINESS_WORD_RE = re.compile(
    r"(?:company|companies|agency|agencies|service|services|firm|"
    r"consultancy|consultant|dealer|dealers|store|stores|shop|shops|"
    r"studio|studios|restaurant|restaurants|cafe|cafes|hotel|hotels|"
    r"parlor|parlors|parlour|parlours|clinic|clinics|hospital|hospitals|"
    r"gym|gyms|salon|salons|spa|spas|boutique|boutiques|bakery|bakeries|"
    r"diner|diners|pub|pubs|gymnasium|architects|contractors)\s*$",
    re.I,
)


def _refined_query(query: str) -> str:
    """Nudge Google into listing named businesses instead of market analysis.

    'real estate in chandigarh' -> 'real estate companies in chandigarh'
    'cafe in delhi'             -> 'top 10 cafe in delhi'
    'best bakery near me'       -> 'top 10 best bakery near me'
    """
    q = query.strip().strip("?.! ")
    m = re.match(
        r"^(?P<cat>.+?)\s+(?:in|at|near|nearby)\s+(?P<loc>.+)$", q, re.I)
    if m:
        cat, loc = m.group("cat").strip(), m.group("loc").strip()
        if loc.lower() in ("me", "here", "my location", "my area"):
            return f"top 10 {cat} near me"
        if _BUSINESS_WORD_RE.search(cat):
            return f"top 10 {cat} in {loc}"
        return f"{cat} companies in {loc}"
    return f"top 10 {q}"


async def _google_ai_search(
    query: str, max_results: int, slow_mo: int,
) -> dict:
    """Google AI Mode / AI Overview search with a persistent profile.

    Google blocks throwaway sessions, so we reuse a persistent Chrome profile.
    If Google shows a CAPTCHA / sign-in, the window stays open and waits for the
    user to complete it, then the search runs automatically.
    """
    ai_text = ""
    errors: list[str] = []
    leads: list[dict] = []

    launch_kwargs = {
        "headless": False,
        "slow_mo": slow_mo,
        "viewport": {"width": 1440, "height": 900},
        "locale": "en-US",
        "user_agent": _USER_AGENT,
        "args": ["--disable-blink-features=AutomationControlled"],
        "ignore_default_args": ["--enable-automation"],
    }
    if _chrome_channel():
        launch_kwargs["channel"] = "chrome"

    print(f"[browser] Opening Chrome (persistent profile) for Google AI search: {query!r}")
    print(f"[browser] Profile: {_PROFILE_DIR}")
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(_PROFILE_DIR, **launch_kwargs)
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            blocked = not await _google_search(page, query, slow_mo)
            if blocked:
                print("[browser] Google asked for verification - solve the CAPTCHA / sign in "
                      "in the window now, the search will continue automatically.")
                deadline = time.monotonic() + 300
                while time.monotonic() < deadline:
                    await asyncio.sleep(2)
                    try:
                        if "sorry/index" not in page.url:
                            break
                        if "unusual traffic" not in await page.content():
                            break
                    except Exception:
                        break
                if not await _google_search(page, query, slow_mo):
                    msg = ("Google still blocked the request. Make sure you are signed in "
                           "to Google in the profile window, then run again.")
                    print("[browser] " + msg)
                    return {
                        "query": query, "engine": "google-ai", "total_results": 0,
                        "results": [], "errors": [msg], "ai_overview": "",
                    }

            ai_overview = await _google_ai_overview(page)

            ai_text = await _google_ai_mode_answer(page, query)
            if not ai_text:
                ai_text = ai_overview
            if ai_text:
                ai_text = _clean_ai_text(ai_text)
                print(f"[browser] AI answer scraped ({len(ai_text)} chars)")
            else:
                print("[browser] No answer text visible on the AI mode page.")

            card_leads = _parse_ai_business_cards(ai_text)
            if not card_leads:
                refined = _refined_query(query)
                if refined.lower() != query.lower():
                    print(f"[browser] No business records parsed - retrying "
                          f"with refined query: {refined!r}")
                    try:
                        await _google_search(page, refined, slow_mo)
                    except Exception:
                        pass
                    ai_text2 = await _google_ai_mode_answer(page, refined)
                    if ai_text2:
                        ai_text = ai_text2
                        card_leads = _parse_ai_business_cards(ai_text)
                        if card_leads:
                            print(f"[browser] Refined query yielded "
                                  f"{len(card_leads)} business record(s)")

            try:
                with open(os.path.join(_BASE_DIR, "ai_debug.txt"), "w",
                          encoding="utf-8") as _f:
                    _f.write(ai_text)
                print(f"[debug] Raw AI text saved to ai_debug.txt "
                      f"({len(ai_text)} chars)")
            except Exception as _e:
                print(f"[debug] Could not save ai_debug.txt: {_e}")

            if card_leads:
                seen = {c["business_name"].lower() for c in leads}
                uniq = [c for c in card_leads
                        if c["business_name"].lower() not in seen]
                print(f"[browser] Parsed {len(uniq)} business record(s) "
                      "from the AI answer")
                leads.extend(uniq)
            else:
                print("[browser] No business records parsed from the AI answer.")

            if leads:
                print(f"[browser] Running AI Mode detail lookups for "
                      f"{min(len(leads), max_results)} business(es) to grab "
                      "rating / phone / email / address")
                await _ai_mode_lookup_businesses(page, leads, query,
                                                 max_results)
            else:
                print("[browser] No business records to enrich - "
                      "skipping website visits.")
        finally:
            try:
                await context.close()
            except Exception:
                pass

    print(f"[browser] Done. Scraped the AI mode answer ({len(ai_text)} chars).")
    return {
        "query": query,
        "engine": "google-ai",
        "total_results": len(leads),
        "results": leads,
        "errors": errors,
        "ai_overview": ai_text,
    }


async def search_and_scrape(
    query: str,
    max_results: int = 8,
    slow_mo: int = 250,
) -> dict:
    """Run the visible Google AI mode automation. Returns lead dicts."""
    return await _google_ai_search(query, max_results, slow_mo)
