"""Build the GovKH crawl status workbook from the live crawl state (read-only)."""
import csv
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

CRAWLER = Path(sys.argv[1])
OUT = Path(sys.argv[2])
sys.path.insert(0, str(CRAWLER))
from govkh_crawler.config import load_sources  # noqa: E402

db = sqlite3.connect(f"file:{CRAWLER / 'data/state.sqlite'}?mode=ro", uri=True)
db.row_factory = sqlite3.Row
sources = load_sources(CRAWLER / "config/sources.yaml")
audit = {}
audit_csv = CRAWLER / "data/reports/source_audit.csv"
if audit_csv.exists():
    with audit_csv.open(encoding="utf-8-sig") as f:
        audit = {r["site_id"]: r for r in csv.DictReader(f)}

FONT = "Arial"
F = Font(name=FONT, size=10)
FB = Font(name=FONT, size=10, bold=True)
FH = Font(name=FONT, size=10, bold=True, color="FFFFFF")
FT = Font(name=FONT, size=14, bold=True)
FS = Font(name=FONT, size=10, italic=True, color="595959")
HEAD = PatternFill("solid", fgColor="1F4E78")
TOT = PatternFill("solid", fgColor="D9E1F2")
SEC = PatternFill("solid", fgColor="F2F2F2")
STATUS_FILL = {
    "Crawling": "E2EFDA", "Blocked by site": "F8CBAD", "Site error": "FFE699",
    "Unreachable": "FFE699", "No usable content": "EDEDED", "Excluded by choice": "EDEDED",
}
thin = Side(style="thin", color="BFBFBF")
BOX = Border(left=thin, right=thin, top=thin, bottom=thin)
NUM, PCT, MB = "#,##0;-#,##0;-", "0.0%;-0.0%;-", "#,##0.0;-#,##0.0;-"

snapshot = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")

# ------------------------------------------------------------------ data
DISABLED = {
    "mef": ("Blocked by site", "Answers 403 / bot challenge to any declared crawler, including a headless browser."),
    "nbc": ("Blocked by site", "CloudFront answers 403 'Request blocked' to crawlers and to a headless browser."),
    "moi": ("Blocked by site", "Bot challenge never completes for a declared crawler (robots.txt itself allows search use)."),
    "trc": ("Blocked by site", "Bot challenge (403) for crawlers and for a headless browser."),
    "mfaic": ("Blocked by site", "Web application firewall answers 'Request Rejected' to every request."),
    "nssf": ("No usable content", "Client-rendered site; even fully rendered it shows almost no documents."),
    "mlvt": ("Site error", "Document sections answer HTTP 500."),
    "senate": ("Excluded by choice", "Reachable (~13,400 posts) but activity news posted as photo galleries with almost no text."),
    "nis": ("Unreachable", "Host did not accept connections during the audit."),
    "national_assembly": ("Unreachable", "Host did not accept connections during the audit."),
}
METHOD = {"ckan": "Catalogue API (CKAN)", "wordpress": "WordPress REST API", "sitemap": "XML sitemap",
          "crawl": "Listing-page crawl", "moeys": "Site JSON API"}

state = defaultdict(Counter)
for r in db.execute("SELECT source_id, state, COUNT(*) n FROM urls WHERE kind IN ('page','file') GROUP BY 1,2"):
    state[r["source_id"]][r["state"]] = r["n"]
assets = Counter({r[0]: r[1] for r in db.execute("SELECT source_id, COUNT(*) FROM urls WHERE kind='asset' AND state='done' GROUP BY 1")})
size_mb = {r[0]: (r[1] or 0) / 1e6 for r in db.execute(
    "SELECT source_id, SUM(bytes) FROM urls WHERE kind IN ('page','file','asset') AND state='done' GROUP BY 1")}

recs = [dict(r) for r in db.execute("SELECT * FROM records")]
retained = [r for r in recs if r["eligibility"] == "eligible" and not r["dup_of"]]
by_src = defaultdict(list)
for r in retained:
    by_src[r["source_id"]].append(r)
n_rec = Counter(r["source_id"] for r in recs)
n_rej = Counter(r["source_id"] for r in recs if r["eligibility"] != "eligible")
n_dup = Counter(r["source_id"] for r in recs if r["eligibility"] == "eligible" and r["dup_of"])

FT_COLS = [("PDF", {"pdf"}), ("Web page", {"html"}), ("Word", {"doc", "docx"}), ("Excel", {"xls", "xlsx"}),
           ("PowerPoint", {"ppt", "pptx"})]


def ftype_bucket(ft):
    for name, exts in FT_COLS:
        if ft in exts:
            return name
    return "Other"


FAMILIES = [("legal_instrument", "Legal instruments", 4200), ("announcement", "Announcements", 3000),
            ("procedure", "Public service procedures", 1800), ("report", "Reports and statistics", 1800),
            ("form_fee", "Forms and fee schedules", 1200)]

wb = Workbook()


def setup(ws, widths):
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.sheet_view.showGridLines = False


def title(ws, text, sub):
    ws["A1"], ws["A1"].font = text, FT
    ws["A2"], ws["A2"].font = sub, FS


def header(ws, row, labels, col=1):
    for i, lab in enumerate(labels):
        c = ws.cell(row=row, column=col + i, value=lab)
        c.font, c.fill, c.border = FH, HEAD, BOX
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[row].height = 32


def put(ws, row, col, value, fmt=None, font=F, fill=None, wrap=False):
    c = ws.cell(row=row, column=col, value=value)
    c.font, c.border = font, BOX
    if fmt:
        c.number_format = fmt
    if fill:
        c.fill = fill
    if wrap:
        c.alignment = Alignment(wrap_text=True, vertical="top")
    return c


# ================================================================== By Source
ws = wb.active
ws.title = "By Source"
cols = ["Source ID", "Institution", "Website", "Access method", "Status",
        "URLs found", "Downloaded", "Still pending", "Gone (404)", "Skipped", "Failed", "Blocked / off-list",
        "Download success", "Records built", "Rejected", "Duplicates", "Retained", "Retained excl. press",
        "PDF", "Web page", "Word", "Excel", "PowerPoint", "Other",
        "Scanned PDFs", "Needs OCR", "Khmer", "English", "Mixed", "Images kept for OCR", "Data size (MB)", "Notes"]
setup(ws, [13, 34, 28, 20, 17] + [10] * 7 + [10] + [10] * 5 + [9] * 6 + [9] * 5 + [10, 11, 60])
title(ws, "Collection by source", f"Snapshot {snapshot}. The crawl is still running; counts grow until it finishes.")
header(ws, 4, cols)
ws.freeze_panes = "C5"
row = 5
order = [s for s in sources.values() if s.enabled] + [s for s in sources.values() if not s.enabled]
for s in order:
    st = state[s.id]
    mine = by_src[s.id]
    status, reason = ("Crawling", "") if s.enabled else DISABLED.get(s.id, ("Excluded by choice", ""))
    if s.id == "moeys":
        reason = ("Site firewall began answering file downloads with 'Request Rejected' after the first few files; "
                  "those files are counted as Skipped.")
    notes = reason or " ".join((s.notes or "").split())
    ft = Counter(ftype_bucket(r["file_type"]) for r in mine)
    vals = [s.id, s.institution, ", ".join(s.domains), ", ".join(METHOD.get(a.get("type"), a.get("type")) for a in s.adapters) or "–",
            status, sum(st.values()), st["done"], st["pending"], st["gone"], st["skipped"], st["failed"],
            st["robots_blocked"] + st["offlist"], None, n_rec[s.id], n_rej[s.id], n_dup[s.id], None,
            sum(1 for r in mine if not r["is_press"]),
            *[ft[name] for name, _ in FT_COLS], ft["Other"],
            sum(1 for r in mine if r["file_type"] == "pdf" and r["is_scanned"]),
            sum(1 for r in mine if r["needs_ocr"]),
            sum(1 for r in mine if r["language"] == "km"), sum(1 for r in mine if r["language"] == "en"),
            sum(1 for r in mine if r["language"] == "mixed"), assets[s.id], size_mb.get(s.id, 0), notes]
    for ci, v in enumerate(vals, 1):
        fmt = MB if ci == 31 else NUM if 6 <= ci <= 30 else None
        c = put(ws, row, ci, v, fmt, wrap=(ci == 32))
    put(ws, row, 13, f"=IFERROR(G{row}/(G{row}+I{row}+J{row}+K{row}+L{row}),0)", PCT)
    put(ws, row, 17, f"=N{row}-O{row}-P{row}", NUM, FB)
    ws.cell(row=row, column=5).fill = PatternFill("solid", fgColor=STATUS_FILL[status])
    row += 1
first, last = 5, row - 1
put(ws, row, 1, "TOTAL", font=FB, fill=TOT)
for ci in range(2, 6):
    put(ws, row, ci, None, fill=TOT)
for ci in list(range(6, 13)) + list(range(14, 32)):
    L = get_column_letter(ci)
    put(ws, row, ci, f"=SUM({L}{first}:{L}{last})", MB if ci == 31 else NUM, FB, TOT)
put(ws, row, 13, f"=IFERROR(G{row}/(G{row}+I{row}+J{row}+K{row}+L{row}),0)", PCT, FB, TOT)
put(ws, row, 32, None, fill=TOT)
TOTAL_ROW = row
ws.auto_filter.ref = f"A4:{get_column_letter(len(cols))}{last}"
r = row + 2
for line in [
    "Columns F to L count document URLs (web pages and files) in the crawl frontier by state. Images kept for OCR are counted separately.",
    "Retained = records built − rejected (eligibility checks) − duplicates. File type, scan, OCR and language columns count retained documents.",
    "Download success = downloaded ÷ (downloaded + gone + skipped + failed + blocked). Pending URLs are not counted yet.",
]:
    ws.cell(row=r, column=1, value=line).font = FS
    r += 1

BS = "'By Source'"

# ================================================================== Summary
sm = wb.create_sheet("Summary", 0)
setup(sm, [44, 16, 4, 30, 14, 14, 4, 22, 12])
title(sm, "GovKH Search: crawl status report", f"Snapshot {snapshot} from the live crawl database. The crawl is still running.")
header(sm, 4, ["Key figure", "Value"])
kpis = [
    ("Document URLs found", f"={BS}!F{TOTAL_ROW}", NUM),
    ("Downloaded", f"={BS}!G{TOTAL_ROW}", NUM),
    ("Still pending", f"={BS}!H{TOTAL_ROW}", NUM),
    ("Could not fetch (gone, skipped, failed, blocked)", f"={BS}!I{TOTAL_ROW}+{BS}!J{TOTAL_ROW}+{BS}!K{TOTAL_ROW}+{BS}!L{TOTAL_ROW}", NUM),
    ("Download success rate", f"={BS}!M{TOTAL_ROW}", PCT),
    ("Records built", f"={BS}!N{TOTAL_ROW}", NUM),
    ("Rejected by eligibility checks", f"={BS}!O{TOTAL_ROW}", NUM),
    ("Removed as duplicates", f"={BS}!P{TOTAL_ROW}", NUM),
    ("Retained documents", f"={BS}!Q{TOTAL_ROW}", NUM),
    ("Retained excluding press releases", f"={BS}!R{TOTAL_ROW}", NUM),
    ("Corpus target (proposal 6.5)", 10001, NUM),
    ("Share of target reached", "=IFERROR(B13/B15,0)", PCT),
    ("Documents still needed", "=MAX(0,B15-B13)", NUM),
    ("Sources being crawled", f'=COUNTIF({BS}!E5:E{last},"Crawling")', NUM),
    ("Sources blocked by the site", f'=COUNTIF({BS}!E5:E{last},"Blocked by site")', NUM),
    ("Sources not crawled for other reasons", f'=COUNTA({BS}!A5:A{last})-B18-B19', NUM),
    ("Original data downloaded (MB)", f"={BS}!AE{TOTAL_ROW}", MB),
]
for i, (lab, val, fmt) in enumerate(kpis, 5):
    put(sm, i, 1, lab, font=FB if lab.startswith("Retained documents") else F)
    c = put(sm, i, 2, val, fmt, FB if lab.startswith("Retained documents") else F)
    if lab.startswith("Corpus target"):
        c.font = Font(name=FONT, size=10, color="0000FF")
        sm.cell(row=i, column=1).value = "Corpus target (proposal 6.5, input)"
assert sm["A13"].value == "Retained documents" and sm["A15"].value.startswith("Corpus target")

# Retained by file type
header(sm, 4, ["Retained by file type", "Documents", "Share"], col=4)
for i, (name, colL) in enumerate([("PDF", "S"), ("Web page", "T"), ("Word", "U"), ("Excel", "V"), ("PowerPoint", "W"), ("Other", "X")], 5):
    put(sm, i, 4, name)
    put(sm, i, 5, f"={BS}!{colL}{TOTAL_ROW}", NUM)
    put(sm, i, 6, f"=IFERROR(E{i}/$B$13,0)", PCT)
put(sm, 11, 4, "Scanned PDFs (need OCR)")
put(sm, 11, 5, f"={BS}!Y{TOTAL_ROW}", NUM)
put(sm, 11, 6, f"=IFERROR(E11/$B$13,0)", PCT)
put(sm, 12, 4, "All documents needing OCR")
put(sm, 12, 5, f"={BS}!Z{TOTAL_ROW}", NUM)
put(sm, 12, 6, f"=IFERROR(E12/$B$13,0)", PCT)

header(sm, 14, ["Retained by language", "Documents", "Share"], col=4)
for i, (name, colL) in enumerate([("Khmer", "AA"), ("English", "AB"), ("Mixed Khmer/English", "AC")], 15):
    put(sm, i, 4, name)
    put(sm, i, 5, f"={BS}!{colL}{TOTAL_ROW}", NUM)
    put(sm, i, 6, f"=IFERROR(E{i}/$B$13,0)", PCT)

# Retained by source (sorted) for the chart
header(sm, 4, ["Retained by source", "Documents"], col=8)
for i in range(first, last + 1):
    put(sm, i, 8, f"={BS}!A{i}")
    put(sm, i, 9, f"={BS}!Q{i}", NUM)
chart = BarChart()
chart.type = "bar"
chart.style = 10
chart.title = "Retained documents by source"
chart.y_axis.title = "Documents"
chart.add_data(Reference(sm, min_col=9, min_row=4, max_row=last), titles_from_data=True)
chart.set_categories(Reference(sm, min_col=8, min_row=5, max_row=last))
chart.legend = None
chart.height, chart.width = 11, 16
chart.roundedCorners = False
chart.title.overlay = False
chart.y_axis.title.overlay = False
chart.x_axis.delete = False
chart.y_axis.delete = False
sm.add_chart(chart, "D19")
r = last + 3
for line in [
    "All figures link to the 'By Source' sheet. The corpus target (blue) is the only typed-in input.",
    "Retained = eligible, unique documents issued by a government institution (proposal counting rule, section 6).",
]:
    sm.cell(row=r, column=1, value=line).font = FS
    r += 1

# ================================================================== Document Types
dt = wb.create_sheet("Document Types")
setup(dt, [13, 13, 13, 13, 13, 13, 11, 4, 22, 11])
title(dt, "Retained documents by document type", "Type is taken from the source catalogue where it has one, otherwise from rules on the title.")
header(dt, 4, ["Source ID"] + [lab for _, lab, _ in FAMILIES] + ["Total"])
crawled = [s for s in sources.values() if s.enabled]
for i, s in enumerate(crawled, 5):
    fam = Counter(r["doc_type"] for r in by_src[s.id])
    put(dt, i, 1, s.id)
    for j, (key, _, _) in enumerate(FAMILIES, 2):
        put(dt, i, j, fam[key], NUM)
    put(dt, i, 7, f"=SUM(B{i}:F{i})", NUM, FB)
tl = 4 + len(crawled)
t = tl + 1
put(dt, t, 1, "TOTAL", font=FB, fill=TOT)
for j in range(2, 8):
    L = get_column_letter(j)
    put(dt, t, j, f"=SUM({L}5:{L}{tl})", NUM, FB, TOT)
put(dt, t + 1, 1, "Planning target", font=FB)
for j, (_, _, tgt) in enumerate(FAMILIES, 2):
    c = put(dt, t + 1, j, tgt, NUM)
    c.font = Font(name=FONT, size=10, color="0000FF")
put(dt, t + 1, 7, f"=SUM(B{t + 1}:F{t + 1})", NUM, FB)
put(dt, t + 2, 1, "Share of target", font=FB)
for j in range(2, 8):
    L = get_column_letter(j)
    put(dt, t + 2, j, f"=IFERROR({L}{t}/{L}{t + 1},0)", PCT)
dt.cell(row=t + 3, column=1, value="Planning targets (blue) are from proposal section 6.1.").font = FS

header(dt, 4, ["Subtype", "Retained"], col=9)
subs = Counter(r["doc_subtype"] or "other" for r in retained).most_common()
for i, (k, v) in enumerate(subs, 5):
    put(dt, i, 9, k.replace("_", " "))
    put(dt, i, 10, v, NUM)

# ================================================================== Cannot Fetch
cf = wb.create_sheet("Cannot Fetch")
setup(cf, [16, 40, 26, 20, 18, 22, 70])
title(cf, "What we cannot fetch, and why", "Part 1: whole sites not crawled. Part 2: individual URLs that failed on sites we do crawl.")
header(cf, 4, ["Source ID", "Institution", "Website", "Category", "Homepage answer", "robots.txt", "Reason"])
i = 5
for s in sources.values():
    if s.enabled:
        continue
    cat, reason = DISABLED.get(s.id, ("Excluded by choice", " ".join((s.notes or "").split())))
    a = audit.get(s.id, {})
    for ci, v in enumerate([s.id, s.institution, ", ".join(s.domains), cat, a.get("homepage", ""), a.get("robots", ""), reason], 1):
        c = put(cf, i, ci, v, wrap=(ci == 7))
    cf.cell(row=i, column=4).fill = PatternFill("solid", fgColor=STATUS_FILL[cat])
    i += 1
i += 1
put(cf, i, 1, "Also partly blocked", font=FB, fill=SEC)
for ci in range(2, 8):
    put(cf, i, ci, None, fill=SEC)
i += 1
for sid, cat, reason in [
    ("moeys", "Blocked by site", "Crawled through the site's own API, but its firewall began rejecting file downloads after the first few files ('Request Rejected'). Those files are in Part 2 as skipped; not bypassed."),
    ("mpwt", "Handled", "Many documents are Google Drive embeds; these are now downloaded from Drive (Drive's robots.txt disallows its download endpoint for crawlers; setting fetch_embedded_files turns this off)."),
]:
    s = sources[sid]
    for ci, v in enumerate([sid, s.institution, ", ".join(s.domains), cat, "", "", reason], 1):
        put(cf, i, ci, v, wrap=(ci == 7))
    i += 1

i += 2
cf.cell(row=i, column=1, value="Part 2: URLs that could not be fetched on crawled sites").font = Font(name=FONT, size=12, bold=True)
i += 1
header(cf, i, ["Source ID", "Problem", "State", "URLs", "Share of source", "Example URL", "What it means"])
hdr2 = i
i += 1
MEANING = {
    "http_404": "The site links to a page or file that no longer exists.",
    "expected a file, got HTML": "A download link returned a web page instead (error page, login wall or firewall rejection).",
    "too_large": "Larger than the 100 MB limit (settings.max_bytes).",
    "robots_disallowed": "robots.txt forbids it, so it was not requested.",
}
probs = db.execute(
    "SELECT source_id, state, COALESCE(last_error, 'retrying') err, COUNT(*) n, MIN(url) ex FROM urls"
    " WHERE kind IN ('page','file') AND (state IN ('gone','skipped','failed','robots_blocked','offlist') OR (state='pending' AND attempts>0))"
    " GROUP BY 1,2,3 ORDER BY n DESC").fetchall()
for p in probs:
    err = p["err"]
    key = "too_large" if err.startswith("larger than") else err
    meaning = MEANING.get(key) or ("Temporary error; will be retried automatically." if p["state"] == "pending" else "")
    total = sum(state[p["source_id"]].values()) or 1
    for ci, v in enumerate([p["source_id"], err, p["state"], p["n"], p["n"] / total, p["ex"], meaning], 1):
        put(cf, i, ci, v, PCT if ci == 5 else NUM if ci == 4 else None, wrap=(ci in (6, 7)))
    i += 1
if not probs:
    put(cf, i, 1, "No failed URLs so far.")
    i += 1
put(cf, i, 1, "TOTAL", font=FB, fill=TOT)
put(cf, i, 4, f"=SUM(D{hdr2 + 1}:D{i - 1})", NUM, FB, TOT)
for ci in (2, 3, 5, 6, 7):
    put(cf, i, ci, None, fill=TOT)

# ================================================================== Rejected & Duplicates
rj = wb.create_sheet("Rejected & Duplicates")
REASONS = {
    "thin_content": "Page has almost no text and no attachment",
    "landing_page": "Page only introduces an attachment (the attachment is kept)",
    "draft_not_issued": "Draft that was never issued",
    "personal_data": "List of named individuals (exam results, candidates)",
    "language_out_of_scope": "Not Khmer or English",
    "unreadable_file": "Corrupt or encrypted file",
    "missing_institution": "No issuing institution",
    "press_excluded": "Press release (excluded by setting)",
}
reasons = sorted({r["reject_reason"] for r in recs if r["reject_reason"]}, key=lambda k: -sum(1 for r in recs if r["reject_reason"] == k))
dupr = ["exact_checksum", "normalized_text", "near_duplicate"]
labels = [REASONS.get(k, k) for k in reasons] + ["Duplicate: identical file", "Duplicate: identical text", "Duplicate: near-identical text"]
setup(rj, [13] + [16] * len(labels) + [10])
title(rj, "Documents removed from the corpus, by reason", "Rejected records fail an eligibility rule; duplicates are copies of a document kept elsewhere (listed as its alternate URL).")
header(rj, 4, ["Source ID"] + labels + ["Total"])
srcs_with = sorted({r["source_id"] for r in recs})
for i, sid in enumerate(srcs_with, 5):
    put(rj, i, 1, sid)
    for j, k in enumerate(reasons, 2):
        put(rj, i, j, sum(1 for r in recs if r["source_id"] == sid and r["reject_reason"] == k), NUM)
    for j, k in enumerate(dupr, 2 + len(reasons)):
        put(rj, i, j, sum(1 for r in recs if r["source_id"] == sid and r["eligibility"] == "eligible" and r["dup_of"] and r["dup_reason"] == k), NUM)
    lc = get_column_letter(1 + len(labels))
    put(rj, i, 2 + len(labels), f"=SUM(B{i}:{lc}{i})", NUM, FB)
e = 4 + len(srcs_with)
put(rj, e + 1, 1, "TOTAL", font=FB, fill=TOT)
for j in range(2, 3 + len(labels)):
    L = get_column_letter(j)
    put(rj, e + 1, j, f"=SUM({L}5:{L}{e})", NUM, FB, TOT)


# ================================================================== document lists
def doc_sheet(name, rows, extra_cols):
    d = wb.create_sheet(name)
    cols_ = ["Record ID", "Source ID", "Institution", "Title", "Doc type", "Subtype", "Language", "File type",
             "Size (KB)", "Pages", "Scanned", "Needs OCR", "Issue date", "Document number", "Press", "URL"] + [c for c, _ in extra_cols]
    setup(d, [24, 11, 28, 60, 16, 14, 9, 8, 10, 7, 8, 9, 11, 18, 7, 60] + [w for _, w in extra_cols])
    header(d, 1, cols_)
    d.freeze_panes = "E2"
    for i, r in enumerate(rows, 2):
        vals = [r["record_id"], r["source_id"], r["institution"], r["title"], r["doc_type"], r["doc_subtype"], r["language"],
                r["file_type"], round((r["bytes"] or 0) / 1024, 1), r["page_count"],
                None if r["is_scanned"] is None else ("yes" if r["is_scanned"] else "no"),
                "yes" if r["needs_ocr"] else "no", r["issue_date"] or None, r["doc_number"] or None,
                "yes" if r["is_press"] else "no", r["url"]]
        if extra_cols:
            vals += [r["reject_reason"] or f"duplicate ({r['dup_reason']})", r["dup_of"] or None]
        for ci, v in enumerate(vals, 1):
            c = d.cell(row=i, column=ci, value=v)
            c.font = F
            if ci == 9:
                c.number_format = "#,##0.0"
    d.auto_filter.ref = f"A1:{get_column_letter(len(cols_))}{max(2, len(rows) + 1)}"


doc_sheet("Retained Documents", sorted(retained, key=lambda r: (r["source_id"], r["title"] or "")), [])
removed = [r for r in recs if r["eligibility"] != "eligible" or r["dup_of"]]
doc_sheet("Removed Documents", sorted(removed, key=lambda r: (r["source_id"], r["reject_reason"] or "")), [("Reason", 26), ("Duplicate of", 24)])

# ================================================================== Notes
nt = wb.create_sheet("Notes")
setup(nt, [26, 100])
title(nt, "How to read this workbook", f"Snapshot {snapshot}")
notes = [
    ("Source", "Live crawl database Project/crawler/data/state.sqlite, read-only, while the full crawl was still running."),
    ("Refresh", "Run `python -m govkh_crawler process` in Project/crawler, then run `../.venv/bin/python report_xlsx.py . ../Docs/GovKH_Crawl_Status_Report.xlsx` to rebuild this file."),
    ("Counting rule", "One original document issued by an institution is one record. A Khmer original and its English version are two records. A web page that only introduces an attachment is not counted; the attachment is."),
    ("Status: Crawling", "The site is in the whitelist and is being collected."),
    ("Status: Blocked by site", "The site refuses declared crawlers (403, bot challenge or firewall) even in a headless browser. Not bypassed."),
    ("Status: Site error / Unreachable", "The site answered with server errors or did not accept connections during the audit. Worth retrying later."),
    ("Status: No usable content / Excluded", "Reachable, but there is nothing in scope to collect (see the reason)."),
    ("Still pending", "Found but not yet downloaded. The crawl sends about one request per second per site, so large sources (ODC, MRD, OCM press) take hours."),
    ("Needs OCR", "Scanned PDFs, and web pages whose only content is an image of the notice. Their text is empty until the OCR stage."),
    ("Data size", "Bytes of original files and pages stored under data/raw (identical files stored once)."),
]
header(nt, 4, ["Term", "Meaning"])
for i, (k, v) in enumerate(notes, 5):
    put(nt, i, 1, k, font=FB)
    put(nt, i, 2, v, wrap=True)

for w in wb.worksheets:
    for row_ in w.iter_rows():
        for c in row_:
            if c.value is not None and c.font.name != FONT:
                c.font = Font(name=FONT, size=c.font.size or 10, bold=c.font.bold, italic=c.font.italic, color=c.font.color)
wb.save(OUT)
print("saved", OUT, "retained", len(retained), "removed", len(removed), "total_row", TOTAL_ROW)
