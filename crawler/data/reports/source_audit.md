# GovKH Search: source feasibility audit
Generated 2026-11-14T09:49:47Z. Estimates are upper bounds before eligibility checks and deduplication.
Estimated documents across sources with a `go` verdict: **34,026**.

| Source | Institution | Access | Homepage | robots.txt | Content signals | Est. documents | Reuse | Verdict |
|---|---|---|---|---|---|---|---|---|
| odc_laws | Royal Government of Cambodia (issuing body not recorded by the archive) | ckan | 200 | ok: no matching group (all allowed) |  | 3657 | unclear | go: run the pilot |
| gdt | General Department of Taxation | sitemap | 200 | ok: all allowed |  | 622 | unclear | disabled after the pilot: see notes |
| gdce | General Department of Customs and Excise | crawl | 200 | missing |  |  | unclear | disabled after the pilot: see notes |
| ccc | Constitutional Council of Cambodia | crawl | 200 | missing |  |  | unclear | go: run the pilot |
| mptc | Ministry of Post and Telecommunications | wordpress | 200 | missing |  | 4449 | unclear | go: run the pilot |
| mrd | Ministry of Rural Development | wordpress | 200 | ok: 1 disallow rule(s) |  | 4074 | unclear | go: run the pilot |
| moe | Ministry of Environment | wordpress | 200 | ok: all allowed |  | 2099 | unclear | go: run the pilot |
| ocm_press | Office of the Council of Ministers (Press and Quick Reaction Unit) | wordpress | 200 | ok: 1 disallow rule(s) |  | 18549 | unclear | go: run the pilot |
| senate | Senate of the Kingdom of Cambodia | wordpress | 200 | ok: 1 disallow rule(s) |  | 13446 | unclear | disabled after the pilot: see notes |
| mlmupc | Ministry of Land Management, Urban Planning and Construction | sitemap | 200 | missing |  | 1198 | unclear | go: run the pilot |
| mpwt | Ministry of Public Works and Transport | crawl | 200 | missing |  |  | unclear | go: run the pilot |
| moh | Ministry of Health | crawl | 200 | ok: all allowed |  |  | unclear | go: run the pilot |
| mlvt | Ministry of Labour and Vocational Training | crawl | 200 | ok: 14 disallow rule(s) |  |  | unclear | disabled after the pilot: see notes |
| moc | Ministry of Commerce | crawl | 200 | ok: 8 disallow rule(s) |  |  | unclear | disabled after the pilot: see notes |
| maff | Ministry of Agriculture, Forestry and Fisheries | crawl | 200 | ok: all allowed |  |  | unclear | go: run the pilot |
| mme | Ministry of Mines and Energy | crawl | 200 | ok: 3 disallow rule(s) |  |  | unclear | go: run the pilot |
| phnompenh | Phnom Penh Capital Administration | crawl | 200 | missing |  |  | unclear | go: run the pilot |
| mef | Ministry of Economy and Finance | none configured | 403 | missing |  |  | unclear | no-go: site not reachable by a polite crawler (403) |
| nbc | National Bank of Cambodia | none configured | 403 | missing |  |  | unclear | no-go: site not reachable by a polite crawler (403) |
| moi | Ministry of Interior | none configured | 403 | ok: all allowed; content-signal search=yes,ai-train=no,use=reference | search=yes,ai-train=no,use=reference |  | unclear | no-go: site not reachable by a polite crawler (403) |
| trc | Telecommunication Regulator of Cambodia | none configured | 403 | ok: all allowed |  |  | unclear | no-go: site not reachable by a polite crawler (403) |
| moeys | Ministry of Education, Youth and Sport | none configured | 200 | missing |  |  | unclear | needs an adapter |
| nssf | National Social Security Fund | none configured | 200 | missing |  |  | unclear | needs an adapter |
| mfaic | Ministry of Foreign Affairs and International Cooperation | none configured | 200 | missing |  |  | unclear | needs an adapter |
| nis | National Institute of Statistics | none configured | robots_unreachable | unreachable |  |  | unclear | no-go: host unreachable (robots.txt could not be read) |
| national_assembly | National Assembly of Cambodia | none configured | robots_unreachable | unreachable |  |  | unclear | no-go: host unreachable (robots.txt could not be read) |

## Notes per source

- **odc_laws** (https://data.opendevelopmentcambodia.net): CKAN package_search count (one record may hold a Khmer and an English file). Trusted archive hosting original files of Cambodian legal instruments, with document number, type, promulgation date, status and issuing agency per record. Drafts are rejected.
- **gdt** (https://www.tax.gov.kh): URLs matching filters in the first 12 sitemap file(s). robots.txt allows all agents and the sitemap lists about 630 article URLs, but the pilot shows each article page is an empty shell filled in by JavaScript (the dedup stage collapsed them as near-identical). Needs a site-specific adapter for the content API; ODC holds the tax instruments meanwhile.
- **gdce** (https://www.customs.gov.kh): not estimable without crawling. Listing pages are filled in by JavaScript, so a plain crawl finds no document links. Needs a site-specific adapter.
- **ccc** (https://www.ccc.gov.kh): not estimable without crawling.
- **mptc** (https://mptc.gov.kh): WP REST X-WP-Total: posts=4066, media (pdf)=383.
- **mrd** (https://mrd.gov.kh): WP REST X-WP-Total: posts=3997, media (pdf)=77.
- **moe** (https://moe.gov.kh): WP REST X-WP-Total: posts=1891, media (pdf)=208.
- **ocm_press** (https://pressocm.gov.kh): WP REST X-WP-Total: posts=17696, media (pdf)=853, posts=http_404, media (pdf)=invalid_json.
- **senate** (https://senate.gov.kh): WP REST X-WP-Total: posts=13438, media (pdf)=8. Reachable (about 13,400 posts), but the pilot shows activity news posted as photo galleries with almost no text. Outside the document types of the proposal; enable only if the team decides otherwise.
- **mlmupc** (https://mlmupc.gov.kh): URLs matching filters in the first 12 sitemap file(s).
- **mpwt** (https://www.mpwt.gov.kh): not estimable without crawling. Many official documents are embedded Google Drive previews, which are off the whitelist; the service and press pages are plain HTML.
- **moh** (https://moh.gov.kh): not estimable without crawling.
- **mlvt** (https://www.mlvt.gov.kh): not estimable without crawling. The document sections (ឯកសារផ្លូវការ) answered HTTP 500 during the audit. Retry later; ODC holds the labour instruments meanwhile.
- **moc** (https://moc.gov.kh): not estimable without crawling. Document lists are loaded from /api/, which robots.txt disallows; the server-rendered pages carry no document links.
- **maff** (https://www.maff.gov.kh): not estimable without crawling.
- **mme** (https://mme.gov.kh): not estimable without crawling.
- **phnompenh** (https://phnompenh.gov.kh): not estimable without crawling.
- **mef** (https://mef.gov.kh): no estimate. Answers 403 to every request from a declared crawler, robots.txt included. Not bypassed; ask the ministry or rely on ODC and GDT.
- **nbc** (https://www.nbc.gov.kh): no estimate. Answers 403 to a declared crawler. Not bypassed.
- **moi** (https://www.interior.gov.kh): no estimate. robots.txt allows search use (Content-Signal search=yes, ai-train=no) but the pages sit behind a 403 bot challenge.
- **trc** (https://trc.gov.kh): no estimate. robots.txt allows all, but pages and the sitemap answer 403 to a declared crawler.
- **moeys** (https://moeys.gov.kh): no estimate. Client-rendered application with no crawlable HTML or sitemap; needs a site-specific API adapter.
- **nssf** (https://www.nssf.gov.kh): no estimate. Client-rendered application with no crawlable links; needs a site-specific API adapter.
- **mfaic** (https://www.mfaic.gov.kh): no estimate. Every path returns a JavaScript challenge page.
- **nis** (https://www.nis.gov.kh): no estimate. Host did not accept connections during the audit; retry later.
- **national_assembly** (https://www.national-assembly.org.kh): no estimate. Host did not accept connections during the audit; retry later.
