#!/usr/bin/env python3
"""
Smartlead reply triage.

Pulls every reply from the Smartlead campaigns you name and sorts each
replying lead into one of three buckets:

  POSITIVE      interested, asking questions, wants a call
  NEGATIVE      unsubscribe, not interested, wrong person, do not contact
  AUTO-REPLY    out of office, auto-responders, "no longer with the company"

Positive replies are printed with name, email, company, campaign and the
reply text. The same data is written to a Markdown summary and a CSV.

Only the Python standard library is used, so there is nothing to install.

Usage
-----
    export SMARTLEAD_API_KEY=...        # or the script will prompt for it
    python3 smartlead_replies.py

    # options
    python3 smartlead_replies.py --campaign "Campaign 1 - Sequence A" \
                                 --campaign "Campaign 2 - Sequence B"
    python3 smartlead_replies.py --campaign-id 123456 --campaign-id 123457
    python3 smartlead_replies.py --full-scan       # check every lead's thread
    python3 smartlead_replies.py --out-dir ./reports

Smartlead endpoints used (base https://server.smartlead.ai/api/v1, auth via
the api_key query parameter):

    GET /campaigns                                       list campaigns
    GET /leads/fetch-categories                          lead category names
    GET /campaigns/{id}/leads?offset=&limit=             leads in a campaign
    GET /campaigns/{id}/statistics?email_status=replied  who replied
    GET /campaigns/{id}/leads/{lead_id}/message-history  full email thread
"""

import argparse
import csv
import getpass
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

BASE_URL = "https://server.smartlead.ai/api/v1"
DEFAULT_CAMPAIGNS = ["Campaign 1 - Sequence A", "Campaign 2 - Sequence B"]
PAGE_SIZE = 100

# Smartlead allows 10 requests per 2 seconds; stay comfortably under it.
MIN_INTERVAL = 0.25

POSITIVE, NEGATIVE, AUTO = "positive", "negative", "auto"
BUCKET_TITLES = {
    POSITIVE: "POSITIVE / INTERESTED",
    NEGATIVE: "UNSUBSCRIBE / NOT INTERESTED",
    AUTO: "AUTO-REPLY / OUT OF OFFICE",
}


# --------------------------------------------------------------------------
# API client
# --------------------------------------------------------------------------

class Smartlead:
    def __init__(self, api_key):
        self.api_key = api_key
        self._last = 0.0
        self.calls = 0

    def get(self, path, **params):
        params = {k: v for k, v in params.items() if v is not None}
        params["api_key"] = self.api_key
        url = f"{BASE_URL}{path}?{urllib.parse.urlencode(params)}"

        last_err = "rate limited"
        for attempt in range(6):
            wait = MIN_INTERVAL - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.calls += 1
            try:
                req = urllib.request.Request(url, headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    body = resp.read().decode("utf-8", "replace")
                    return json.loads(body) if body.strip() else None
            except urllib.error.HTTPError as e:
                if e.code == 429 or e.code >= 500:
                    time.sleep(2 * (attempt + 1))
                    continue
                detail = e.read().decode("utf-8", "replace")[:300]
                raise ApiError(f"GET {path} -> HTTP {e.code}: {detail}") from None
            except (urllib.error.URLError, TimeoutError) as e:
                time.sleep(2 * (attempt + 1))
                last_err = e
                continue
        raise ApiError(f"GET {path} failed after retries ({last_err})")


class ApiError(Exception):
    pass


def as_list(payload, *keys):
    """Smartlead wraps lists inconsistently; accept a bare list or {key: [...]}."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in keys + ("data", "results", "items"):
            if isinstance(payload.get(k), list):
                return payload[k]
    return []


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def resolve_campaigns(api, names, ids):
    campaigns = as_list(api.get("/campaigns"), "campaigns")
    by_id = {str(c.get("id")): c for c in campaigns}

    chosen = []
    for cid in ids:
        c = by_id.get(str(cid))
        chosen.append({"id": int(cid), "name": c.get("name") if c else f"Campaign {cid}"})

    for name in names:
        want = name.strip().lower()
        match = [c for c in campaigns if (c.get("name") or "").strip().lower() == want]
        if not match:
            match = [c for c in campaigns if want in (c.get("name") or "").lower()]
        if not match:
            print(f"! No campaign named '{name}'. Campaigns on this account:", file=sys.stderr)
            for c in campaigns:
                print(f"    {c.get('id'):>10}  {c.get('name')}  [{c.get('status')}]", file=sys.stderr)
            sys.exit(1)
        for c in match:
            chosen.append({"id": c["id"], "name": c.get("name")})

    seen, unique = set(), []
    for c in chosen:
        if c["id"] not in seen:
            seen.add(c["id"])
            unique.append(c)
    return unique


def fetch_categories(api):
    try:
        cats = as_list(api.get("/leads/fetch-categories"), "categories")
    except ApiError:
        return {}
    return {c.get("id"): c.get("name") for c in cats if c.get("id") is not None}


def fetch_leads(api, campaign_id):
    leads, offset = [], 0
    while True:
        payload = api.get(f"/campaigns/{campaign_id}/leads", offset=offset, limit=PAGE_SIZE)
        rows = as_list(payload, "leads")
        for row in rows:
            lead = row.get("lead") if isinstance(row.get("lead"), dict) else row
            leads.append({
                "lead_id": lead.get("id") or row.get("lead_id"),
                "first_name": lead.get("first_name") or "",
                "last_name": lead.get("last_name") or "",
                "email": (lead.get("email") or "").strip(),
                "company": lead.get("company_name") or "",
                "status": row.get("status") or "",
                "category_id": row.get("lead_category_id") or lead.get("lead_category_id"),
            })
        if len(rows) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return leads


def fetch_replied_emails(api, campaign_id):
    """Emails of leads that replied, from campaign statistics. None = endpoint unusable."""
    replied, offset = set(), 0
    try:
        while True:
            payload = api.get(f"/campaigns/{campaign_id}/statistics",
                              email_status="replied", offset=offset, limit=PAGE_SIZE)
            if not isinstance(payload, (dict, list)):
                return None
            rows = as_list(payload, "stats")
            for r in rows:
                if r.get("reply_time"):
                    email = (r.get("lead_email") or "").strip().lower()
                    if email:
                        replied.add(email)
            if len(rows) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
    except ApiError as e:
        print(f"  ! statistics endpoint unavailable ({e}); falling back to full scan", file=sys.stderr)
        return None
    return replied


def fetch_replies(api, campaign_id, lead_id):
    payload = api.get(f"/campaigns/{campaign_id}/leads/{lead_id}/message-history")
    messages = as_list(payload, "history", "messages")
    replies = []
    for m in messages:
        kind = str(m.get("type") or m.get("direction") or "").upper()
        if kind not in ("REPLY", "INBOUND", "RECEIVED"):
            continue
        replies.append({
            "time": m.get("time") or m.get("sent_at") or "",
            "subject": m.get("subject") or "",
            "from": m.get("from") or "",
            "text": clean_reply(m.get("email_body") or m.get("body") or ""),
        })
    replies.sort(key=lambda r: r["time"])
    return replies


# --------------------------------------------------------------------------
# Text cleanup
# --------------------------------------------------------------------------

QUOTE_MARKERS = [
    r"^\s*On .{0,200}wrote:\s*$",
    r"^\s*-{2,}\s*Original Message\s*-{2,}",
    r"^\s*_{5,}\s*$",
    r"^\s*From:\s.+",
    r"^\s*Sent from my (iPhone|iPad|Android|mobile)",
    r"^\s*Get Outlook for",
]
QUOTE_RE = re.compile("|".join(QUOTE_MARKERS), re.IGNORECASE | re.MULTILINE)


def clean_reply(body):
    """HTML -> plain text, with the quoted original email cut off."""
    text = re.sub(r"(?is)<(script|style|head).*?</\1>", "", body)
    text = re.sub(r"(?is)<blockquote.*", "", text)            # quoted thread
    text = re.sub(r'(?is)<div[^>]*class="gmail_quote.*', "", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\r", "").replace("\xa0", " ")
    m = QUOTE_RE.search(text)
    if m:
        text = text[:m.start()]
    lines = [ln.rstrip() for ln in text.split("\n") if not ln.lstrip().startswith(">")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

# Smartlead's AI categories (default set plus common custom names).
CATEGORY_BUCKETS = {
    "interested": POSITIVE,
    "meeting request": POSITIVE,
    "meeting booked": POSITIVE,
    "information request": POSITIVE,
    "not interested": NEGATIVE,
    "do not contact": NEGATIVE,
    "unsubscribe": NEGATIVE,
    "unsubscribed": NEGATIVE,
    "wrong person": NEGATIVE,
    "out of office": AUTO,
    "auto reply": AUTO,
    "auto-reply": AUTO,
    "sender originated bounce": AUTO,
}

AUTO_PATTERNS = [
    r"out of (the )?office", r"\booo\b", r"auto(matic)?[- ]?reply", r"autoreply",
    r"auto[- ]?response", r"automatic(ally)? (generated|response)", r"on (annual |paid )?(vacation|holiday|leave|pto)",
    r"(currently|will be) (away|out|traveling|travelling)", r"limited (access to )?(e-?mail|email)",
    r"(i|we) will (be back|return|respond)", r"returning (on|to the office)", r"back in the office",
    r"maternity|paternity|parental leave", r"no longer (with|at|employed)", r"has left the company",
    r"this (mailbox|inbox|email address) is (no longer|not) (monitored|active)",
    r"thank you for (your (e-?mail|message)|contacting).{0,80}(respond|get back|reply).{0,40}(soon|shortly|possible)",
    r"delivery (has )?failed|undeliverable|mail delivery subsystem",
]

NEGATIVE_PATTERNS = [
    r"unsubscribe", r"remove (me|us|my)", r"take (me|us) off", r"opt[- ]?out",
    r"stop (e-?mailing|emailing|contacting|sending|messaging)", r"do ?n[o']t (e-?mail|contact|reach out|send)",
    r"don'?t (e-?mail|contact|reach out|send)", r"not interested", r"no interest", r"no,? thank(s| you)",
    r"not (for sale|selling|looking to sell|a fit|the right (person|fit))", r"(we'?re|we are|i'?m|i am) not (selling|interested)",
    r"never (going to )?sell", r"not now,? not ever", r"please stop", r"wrong (person|contact)", r"spam",
    r"\bpass\b", r"lose my (number|email)",
]

POSITIVE_PATTERNS = [
    r"interested", r"tell me more", r"(more|send( me)?) (info|information|details)", r"let'?s (talk|chat|connect|set|schedule|hop)",
    r"(schedule|book|set up|hop on|jump on) a (call|meeting|time)", r"\bcall me\b", r"give me a call",
    r"(what|which) (times?|day)s? (work|are good)", r"calendly|calendar|availability|available (on|this|next)",
    r"\b(sure|yes|absolutely|open to)\b", r"who (is|are) (the|your) (buyer|client)", r"what('s| is) (it|my business|the business) worth",
    r"valuation|multiple|ebitda|offer", r"how (does|would) (this|it) work", r"\bnda\b",
    r"\d{3}[-.\s)]\d{3}[-.\s]\d{4}",   # they sent a phone number
]


def _match(patterns, text):
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def classify_text(text, subject=""):
    hay = f"{subject}\n{text}"
    if re.match(r"\s*(automatic reply|auto(matic)?[- ]?reply|out of office)", subject or "", re.IGNORECASE):
        return AUTO, "subject"
    if _match(AUTO_PATTERNS, hay):
        return AUTO, "keywords"
    # A "not interested" with a polite yes-word in it is still a no, so check negatives first.
    if _match(NEGATIVE_PATTERNS, text):
        return NEGATIVE, "keywords"
    if _match(POSITIVE_PATTERNS, text):
        return POSITIVE, "keywords"
    return None, ""


def classify_lead(replies, category_name):
    """Return (bucket, reason). Smartlead's own category wins when it maps cleanly."""
    if category_name:
        bucket = CATEGORY_BUCKETS.get(category_name.strip().lower())
        if bucket:
            return bucket, f"Smartlead category: {category_name}"

    # Judge on the newest reply that isn't an auto-responder; a person often
    # answers for real after their OOO fires.
    verdicts = [classify_text(r["text"], r["subject"]) for r in replies]
    human = [(b, why) for b, why in verdicts if b != AUTO]
    if not human:
        return AUTO, "auto-reply text"
    bucket, why = human[-1]
    if bucket:
        return bucket, f"reply {why}"
    # Unclear human reply: surface it with the positives so no live lead is
    # missed. It's marked "review" in the output.
    return POSITIVE, "unclear - review"


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def name_of(r):
    full = f"{r['first_name']} {r['last_name']}".strip()
    if full:
        return full
    m = re.match(r'\s*"?([^"<]+?)"?\s*<', r["replies"][-1]["from"] if r["replies"] else "")
    return m.group(1) if m else "(no name)"


def company_of(r):
    if r["company"]:
        return r["company"]
    domain = r["email"].split("@")[-1] if "@" in r["email"] else ""
    return f"({domain})" if domain else "(unknown)"


def short_time(t):
    try:
        return datetime.fromisoformat(str(t).replace("Z", "+00:00")).strftime("%b %d %Y %H:%M")
    except ValueError:
        return str(t)[:16]


def indent(text, prefix="      "):
    return "\n".join(prefix + ln if ln else prefix.rstrip() for ln in text.split("\n"))


def render_text(results, campaigns):
    buckets = {b: [r for r in results if r["bucket"] == b] for b in (POSITIVE, NEGATIVE, AUTO)}
    rule = "=" * 78
    out = [rule, f"SMARTLEAD REPLY SUMMARY  -  {datetime.now():%b %d %Y %H:%M}", rule]
    out.append("Campaigns: " + "; ".join(c["name"] for c in campaigns))
    out.append(f"Leads that replied: {len(results)}")
    for b in (POSITIVE, NEGATIVE, AUTO):
        out.append(f"  {BUCKET_TITLES[b]:<32} {len(buckets[b]):>5}")
    review = sum(1 for r in buckets[POSITIVE] if r["reason"].startswith("unclear"))
    if review:
        out.append(f"  (of the positives, {review} are unclear and marked REVIEW)")

    out += ["", rule, f"{BUCKET_TITLES[POSITIVE]}  ({len(buckets[POSITIVE])})", rule]
    clear = [r for r in buckets[POSITIVE] if not r["reason"].startswith("unclear")]
    unclear = [r for r in buckets[POSITIVE] if r["reason"].startswith("unclear")]
    for i, r in enumerate(clear + unclear, 1):
        last = r["replies"][-1] if r["replies"] else {"time": "", "text": ""}
        flag = "  [REVIEW]" if r["reason"].startswith("unclear") else ""
        out.append(f"\n{i:>3}. {name_of(r)}{flag}")
        out.append(f"     Email:    {r['email']}")
        out.append(f"     Company:  {company_of(r)}")
        out.append(f"     Campaign: {r['campaign']}")
        out.append(f"     Replied:  {short_time(last['time'])}   ({r['reason']})")
        for rep in r["replies"]:
            if len(r["replies"]) > 1:
                out.append(f"     --- {short_time(rep['time'])}")
            out.append(indent(rep["text"] or "(empty reply)"))

    for b in (NEGATIVE, AUTO):
        out += ["", rule, f"{BUCKET_TITLES[b]}  ({len(buckets[b])})", rule]
        for r in buckets[b]:
            snippet = re.sub(r"\s+", " ", r["replies"][-1]["text"] if r["replies"] else "")[:70]
            out.append(f"  {name_of(r)[:22]:<22}  {r['email'][:34]:<34}  {snippet}")
    return "\n".join(out) + "\n"


def render_markdown(results, campaigns):
    buckets = {b: [r for r in results if r["bucket"] == b] for b in (POSITIVE, NEGATIVE, AUTO)}
    md = [f"# Smartlead reply summary - {datetime.now():%b %d %Y}", ""]
    md.append("**Campaigns:** " + "; ".join(c["name"] for c in campaigns) + "  ")
    md.append(f"**Leads that replied:** {len(results)}")
    md += ["", "| Bucket | Leads |", "|---|---:|"]
    md += [f"| {BUCKET_TITLES[b].title()} | {len(buckets[b])} |" for b in (POSITIVE, NEGATIVE, AUTO)]

    md += ["", f"## Positive / interested ({len(buckets[POSITIVE])})", ""]
    ordered = sorted(buckets[POSITIVE], key=lambda r: r["reason"].startswith("unclear"))
    for r in ordered:
        flag = " - **review**" if r["reason"].startswith("unclear") else ""
        last = r["replies"][-1] if r["replies"] else {"time": ""}
        md.append(f"### {name_of(r)}{flag}")
        md.append(f"- **Email:** {r['email']}")
        md.append(f"- **Company:** {company_of(r)}")
        md.append(f"- **Campaign:** {r['campaign']}")
        md.append(f"- **Replied:** {short_time(last['time'])} ({r['reason']})")
        md.append("")
        for rep in r["replies"]:
            md.append("\n".join("> " + ln for ln in (rep["text"] or "(empty reply)").split("\n")))
            md.append("")

    for b in (NEGATIVE, AUTO):
        md += [f"## {BUCKET_TITLES[b].title()} ({len(buckets[b])})", "",
               "| Name | Email | Company | Campaign | Reply |", "|---|---|---|---|---|"]
        for r in buckets[b]:
            snippet = re.sub(r"\s+", " ", r["replies"][-1]["text"] if r["replies"] else "")[:90]
            snippet = snippet.replace("|", "/")
            md.append(f"| {name_of(r)} | {r['email']} | {company_of(r)} | {r['campaign']} | {snippet} |")
        md.append("")
    return "\n".join(md)


def write_csv(results, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["bucket", "needs_review", "name", "email", "company", "campaign",
                    "smartlead_category", "last_reply_time", "reply_text"])
        for r in results:
            last = r["replies"][-1] if r["replies"] else {"time": ""}
            text = "\n\n---\n\n".join(rep["text"] for rep in r["replies"])
            w.writerow([r["bucket"], "yes" if r["reason"].startswith("unclear") else "",
                        name_of(r), r["email"], company_of(r), r["campaign"],
                        r["category"] or "", last["time"], text])


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Sort Smartlead replies into positive / negative / auto-reply.")
    ap.add_argument("--campaign", action="append", default=[], help="campaign name (repeatable)")
    ap.add_argument("--campaign-id", action="append", default=[], help="campaign id (repeatable)")
    ap.add_argument("--full-scan", action="store_true",
                    help="open every lead's thread instead of only leads Smartlead marks as replied")
    ap.add_argument("--out-dir", default=".", help="where to write the .md and .csv files")
    args = ap.parse_args()

    api_key = os.environ.get("SMARTLEAD_API_KEY") or getpass.getpass("Smartlead API key: ").strip()
    if not api_key:
        sys.exit("No API key given.")
    api = Smartlead(api_key)

    names = args.campaign or ([] if args.campaign_id else DEFAULT_CAMPAIGNS)
    campaigns = resolve_campaigns(api, names, args.campaign_id)
    categories = fetch_categories(api)
    labels = ", ".join("%s (#%s)" % (c["name"], c["id"]) for c in campaigns)
    print(f"Campaigns: {labels}", file=sys.stderr)

    results = []
    for camp in campaigns:
        leads = fetch_leads(api, camp["id"])
        replied = None if args.full_scan else fetch_replied_emails(api, camp["id"])
        if replied is None:
            todo = leads
        else:
            # Also include anything Smartlead has categorised, in case the
            # statistics feed missed a reply.
            todo = [l for l in leads if l["email"].lower() in replied or l["category_id"]]
        print(f"  {camp['name']}: {len(leads)} leads, checking {len(todo)} threads", file=sys.stderr)

        for n, lead in enumerate(todo, 1):
            if n % 25 == 0:
                print(f"    {n}/{len(todo)}", file=sys.stderr)
            if not lead["lead_id"]:
                continue
            try:
                replies = fetch_replies(api, camp["id"], lead["lead_id"])
            except ApiError as e:
                print(f"    ! {lead['email']}: {e}", file=sys.stderr)
                continue
            if not replies:
                continue
            cat = categories.get(lead["category_id"]) if lead["category_id"] else None
            bucket, reason = classify_lead(replies, cat)
            results.append({**lead, "campaign": camp["name"], "category": cat,
                            "replies": replies, "bucket": bucket, "reason": reason})

    results.sort(key=lambda r: r["replies"][-1]["time"] if r["replies"] else "", reverse=True)

    os.makedirs(args.out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    md_path = os.path.join(args.out_dir, f"smartlead-replies-{stamp}.md")
    csv_path = os.path.join(args.out_dir, f"smartlead-replies-{stamp}.csv")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(results, campaigns))
    write_csv(results, csv_path)

    print(render_text(results, campaigns))
    print(f"Saved: {md_path}\n       {csv_path}\n({api.calls} API calls)", file=sys.stderr)


if __name__ == "__main__":
    main()
