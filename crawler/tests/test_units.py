from govkh_crawler import classify as C
from govkh_crawler.dedup import minhash, similarity, text_hash
from govkh_crawler.discovery.sitemap import parse_sitemap
from govkh_crawler.robots import Robots
from govkh_crawler.textextract import parse_html, sniff
from govkh_crawler.urlnorm import canonicalize, drive_download, extension, is_social

UA = "GovKHSearchBot/0.1 (test)"


def test_canonicalize():
    assert canonicalize("HTTPS://WWW.Example.gov.kh:443/a//b/../c.pdf?utm_source=x&id=2#top") == "https://www.example.gov.kh/a/c.pdf?id=2"
    assert canonicalize("docs/ឯកសារ.pdf", "https://m.gov.kh/kh/") == canonicalize("https://m.gov.kh/kh/docs/%E1%9E%AF%E1%9E%80%E1%9E%9F%E1%9E%B6%E1%9E%9A.pdf")
    assert canonicalize("mailto:a@b.c") is None and canonicalize("javascript:void(0)") is None
    assert canonicalize("https://x.gov.kh/a/") == "https://x.gov.kh/a/"
    assert extension("https://x.gov.kh/a/File.PDF?dl=1") == "pdf"
    assert is_social("https://www.facebook.com/moc") and is_social("https://t.me/x") and not is_social("https://moc.gov.kh/")


def test_robots_wildcards_and_groups():
    rb = Robots.parse(
        "User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /*?id=*\nDisallow: /*.php$\nCrawl-delay: 2\n"
        "Content-Signal: search=yes,ai-train=no\nSitemap: https://x/sitemap.xml\n\nUser-agent: GPTBot\nDisallow: /\n")
    assert rb.allowed("https://x/kh/news", UA)
    assert not rb.allowed("https://x/api/v1/docs", UA)
    assert not rb.allowed("https://x/page?id=5", UA)
    assert not rb.allowed("https://x/a/index.php", UA) and rb.allowed("https://x/a/index.php?x=1", UA)
    assert rb.crawl_delay(UA) == 2 and rb.sitemaps == ["https://x/sitemap.xml"]
    assert rb.signals(UA) == {"search": "yes", "ai-train": "no"}
    assert not rb.allowed("https://x/kh/news", "GPTBot/1.0")


def test_robots_specific_group_and_status():
    rb = Robots.parse("User-agent: *\nDisallow: /\n\nUser-agent: govkhsearchbot\nDisallow: /tmp/\n")
    assert rb.allowed("https://x/docs", UA) and not rb.allowed("https://x/tmp/a", UA)
    assert Robots(status="missing").allowed("https://x/a", UA)
    assert not Robots(status="unreachable").allowed("https://x/a", UA)
    assert not Robots.parse("User-agent: *\nContent-Signal: search=no\nAllow: /\n").allowed("https://x/a", UA)
    # Longest match wins; on a tie, allow wins.
    rb = Robots.parse("User-agent: *\nDisallow: /wp-admin/\nAllow: /wp-admin/admin-ajax.php\n")
    assert rb.allowed("https://x/wp-admin/admin-ajax.php", UA) and not rb.allowed("https://x/wp-admin/x", UA)


def test_language():
    assert C.detect_language("ច្បាប់ស្តីពីអត្តសញ្ញាណប័ណ្ណសញ្ជាតិខ្មែរ មាត្រា ១ ដល់ មាត្រា ២០") == "km"
    assert C.detect_language("Law on Khmer identity cards, articles 1 to 20") == "en"
    assert C.detect_language("ប្រកាសលេខ ១២៣ ស្តីពីពន្ធ Prakas No. 123 on the tax on salary of employees") == "mixed"
    assert C.detect_language("กฎหมายว่าด้วยบัตรประจำตัวประชาชน พ.ศ. ๒๕๒๖") == "other"
    assert C.detect_language("123") == ""


def test_doc_type_rules():
    assert C.classify_doc_type("សេចក្តីប្រកាសព័ត៌មាន ស្តីពីលទ្ធផលកិច្ចប្រជុំ") == ("announcement", "press_release")
    assert C.classify_doc_type("សេចក្ដីជូនដំណឹង ស្ដីពីថ្ងៃឈប់សម្រាក") == ("announcement", "notification")  # ្ដ spelling
    assert C.classify_doc_type("ប្រកាសលេខ ០៩៨ សហវ.ប្រក") == ("legal_instrument", "prakas")
    assert C.classify_doc_type("អនុក្រឹត្យលេខ ១២៣ អនក្រ.បក") == ("legal_instrument", "sub_decree")
    assert C.classify_doc_type("Sub-Decree No. 60 on Khmer ID cards") == ("legal_instrument", "sub_decree")
    assert C.classify_doc_type("ព្រះរាជក្រឹត្យ ស្តីពីការតែងតាំង") == ("legal_instrument", "royal_decree")
    assert C.classify_doc_type("ពាក្យស្នើសុំចុះបញ្ជីពាណិជ្ជកម្ម") == ("form_fee", "form")
    assert C.classify_doc_type("Annual Report 2025") == ("report", "report")
    assert C.classify_doc_type("", "nothing useful") is None


def test_doc_number_and_dates():
    assert C.extract_doc_number("ប្រកាសលេខ ០៩៨ សហវ.ប្រក ស្តីពីពន្ធ") == "098 សហវ.ប្រក"
    assert C.extract_doc_number("អនុក្រឹត្យលេខ ១២៣ អនក្រ.បក ចុះថ្ងៃទី") == "123 អនក្រ.បក"
    assert C.extract_doc_number("Sub-Decree No. 60 ANK.BK on ID cards") == "60 ANK.BK"
    assert C.extract_doc_number("Notification no. 4001 on fuel prices") == "4001"
    assert C.extract_doc_number("no number here") == ""
    assert C.extract_doc_number("1 នស/រកម/១២២៣/០២២ ៧ ធ្នូ ២០២៣ ច្បាប់ស្តីពីហិរញ្ញវត្ថុ") == "នស/រកម/1223/022"
    assert C.extract_doc_number("Law NS/RKM/1094/006 on civil servants") == "NS/RKM/1094/006"
    assert C.extract_doc_number("on 12/05/2024") == ""
    assert C.extract_date("ចុះថ្ងៃទី១១ ខែវិច្ឆិកា ឆ្នាំ២០២៦") == "2026-11-11"
    assert C.extract_date("ចេញផ្សាយ ២៨ កក្កដា ២០២៦ របាយការណ៍") == "2026-07-28"
    assert C.extract_date("dated 5 March 2024") == "2024-03-05" and C.extract_date("January 26, 1996") == "1996-01-26"
    assert C.extract_date("on 31/12/2025") == "2025-12-31" and C.extract_date("no date, ref 99/99/2020") == ""
    assert C.iso_date("2026-11-11T07:14:17.44") == "2026-11-11" and C.iso_date("") == ""


def test_personal_and_titles():
    assert C.looks_personal("បញ្ជីឈ្មោះបេក្ខជនជាប់") and C.looks_personal("List of successful candidates 2026")
    assert not C.looks_personal("ប្រកាសស្តីពីការប្រឡង")
    assert C.good_title("ទាញយក") == "" and C.good_title("Download") == "" and C.good_title("Prakas on tax") == "Prakas on tax"


def test_near_duplicate_signatures():
    base = "".join(f"មាត្រា {i} ច្បាប់នេះមានគោលបំណងកំណត់អំពីការគ្រប់គ្រងពន្ធដារប្រភេទទី {i * 7}។ Article {i} defines tax class {i * 13}. " for i in range(120))
    assert text_hash(base) == text_hash(base.replace(" ", "\n  ").replace("១", "1"))  # layout and digit form ignored
    a, b = minhash(base), minhash(base + " បន្ថែម​បន្តិច។")
    c = minhash("របាយការណ៍ប្រចាំឆ្នាំស្តីពីវិស័យអប់រំ យុវជន និងកីឡា។ " * 60)
    assert similarity(a, b) > 0.9 and similarity(a, c) < 0.3
    assert minhash("short") is None


def test_sniff_and_html():
    assert sniff(b"%PDF-1.7 ...", "https://x/a") == "pdf"
    assert sniff(b"<!DOCTYPE html><html>", "https://x/a.pdf", "application/pdf") == "html"
    assert sniff(b"\xff\xd8\xff\xe0", "https://x/a") == "jpg"
    assert sniff(b"PK\x03\x04....", "https://x/a.docx") == "docx"
    html = ('<html lang="km"><body><nav><a href="/menu">Menu</a></nav><main><h1>ប្រកាស</h1><p>អត្ថបទ</p>'
            '<a href="/f/a.pdf">ទាញយក</a><img src="/img/notice-scan.jpg"><img src="/img/logo.png"></main></body></html>').encode()
    doc = parse_html(html, "https://m.gov.kh/kh/post/1")
    assert doc.title == "ប្រកាស" and doc.lang == "km" and "Menu" not in doc.text
    assert ("https://m.gov.kh/f/a.pdf", "ទាញយក", True) in doc.links and ("https://m.gov.kh/menu", "Menu", False) in doc.links
    assert doc.images == ["https://m.gov.kh/img/notice-scan.jpg"]


def test_parse_sitemap():
    idx = b"<sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'><sitemap><loc>https://x/s1.xml</loc></sitemap></sitemapindex>"
    assert parse_sitemap(idx) == (["https://x/s1.xml"], [])
    us = b"<urlset><url><loc> https://x/a </loc><lastmod>2026-01-02</lastmod></url><url><loc>https://x/b</loc></url></urlset>"
    assert parse_sitemap(us) == ([], [("https://x/a", "2026-01-02"), ("https://x/b", "")])
    assert parse_sitemap(b"<html>not xml") == ([], [])


def test_drive_links():
    fid = "1eh_FoQIJ0_ZQJvrZUA5YoMCgx71CC_QB"
    want = f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t"
    assert drive_download(f"https://drive.google.com/file/d/{fid}/preview") == want
    assert drive_download(f"https://drive.google.com/open?id={fid}") == want
    assert drive_download(want) == want
    assert drive_download(f"https://docs.google.com/document/d/{fid}/edit") == f"https://docs.google.com/document/d/{fid}/export?format=pdf"
    assert drive_download(f"https://drive.google.com/drive/folders/{fid}") is None
    assert drive_download("https://moc.gov.kh/a.pdf") is None
    html = (f'<main><h1>Notice</h1><p>{"text " * 60}</p><iframe src="https://drive.google.com/file/d/{fid}/preview"></iframe>'
            '<table><tr><td>Prakas No. 12 on fees</td><td><a href="/f/1">KH</a></td></tr></table></main>').encode()
    doc = parse_html(html, "https://x.gov.kh/p")
    assert (want, "", True) in doc.links
    assert ("https://x.gov.kh/f/1", "Prakas No. 12 on fees KH", True) in doc.links
