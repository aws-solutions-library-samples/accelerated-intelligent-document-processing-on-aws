# ZAP DAST — Dynamic API Scan

OWASP ZAP baseline/active scan of the deployed UI API (`POST /op/{field}`), seeded from a generated OpenAPI spec of every operation. Rules muted in `scripts/sdlc/zap-rules.conf` are excluded from the alert counts.

## Summary

- **Gate (High alerts):** PASS ✅
- **Alerts:** High=0 Medium=1 Low=0 Info=0
- **Rules exercised:** 118 (116 PASS · 1 WARN · 0 FAIL · 1 IGNORE)
- **URLs scanned:** 112

## Alerts (findings, most severe first)

| Risk | Alert | Instances | Remediation |
|------|-------|----------:|-------------|
| Medium | Cross-Domain Misconfiguration | 5 | Ensure that sensitive data is not available in an unauthenticated manner (using IP address white-listing, for instance). Configure the "Access-Control-Allow-Ori |

## Rules exercised (full outcome list)

Every ZAP rule run against the seeded API, with its outcome. `WARN`/`FAIL` are actionable; `IGNORE` is muted in `scripts/sdlc/zap-rules.conf`; `PASS` means the rule ran and found nothing.

### Non-PASS outcomes

| Outcome | Rule | Plugin ID | Instances |
|---------|------|-----------|----------:|
| IGNORE-NEW | Timestamp Disclosure - Unix | `10096` | 45 |
| WARN-NEW | Cross-Domain Misconfiguration | `10098` | 14 |

<details><summary>All PASS rules (116)</summary>

| Rule | Plugin ID |
|------|-----------|
| Directory Browsing | `0` |
| Vulnerable JS Library (Powered by Retire.js) | `10003` |
| In Page Banner Information Leak | `10009` |
| Cookie No HttpOnly Flag | `10010` |
| Cookie Without Secure Flag | `10011` |
| Re-examine Cache-control Directives | `10015` |
| Cross-Domain JavaScript Source File Inclusion | `10017` |
| Content-Type Header Missing | `10019` |
| Anti-clickjacking Header | `10020` |
| X-Content-Type-Options Header Missing | `10021` |
| Information Disclosure - Debug Error Messages | `10023` |
| Information Disclosure - Sensitive Information in URL | `10024` |
| Information Disclosure - Sensitive Information in HTTP Referrer Header | `10025` |
| HTTP Parameter Override | `10026` |
| Information Disclosure - Suspicious Comments | `10027` |
| Off-site Redirect | `10028` |
| Cookie Poisoning | `10029` |
| User Controllable Charset | `10030` |
| User Controllable HTML Element Attribute (Potential XSS) | `10031` |
| Viewstate | `10032` |
| Directory Browsing | `10033` |
| Heartbleed OpenSSL Vulnerability (Indicative) | `10034` |
| Strict-Transport-Security Header | `10035` |
| HTTP Server Response Header | `10036` |
| Server Leaks Information via "X-Powered-By" HTTP Response Header Field(s) | `10037` |
| Content Security Policy (CSP) Header Not Set | `10038` |
| X-Backend-Server Header Information Leak | `10039` |
| Secure Pages Include Mixed Content | `10040` |
| HTTP to HTTPS Insecure Transition in Form Post | `10041` |
| HTTPS to HTTP Insecure Transition in Form Post | `10042` |
| User Controllable JavaScript Event (XSS) | `10043` |
| Big Redirect Detected (Potential Sensitive Information Leak) | `10044` |
| Source Code Disclosure - /WEB-INF Folder | `10045` |
| HTTPS Content Available via HTTP | `10047` |
| Remote Code Execution - Shell Shock | `10048` |
| Content Cacheability | `10049` |
| Retrieved from Cache | `10050` |
| X-ChromeLogger-Data (XCOLD) Header Information Leak | `10052` |
| Cookie without SameSite Attribute | `10054` |
| CSP | `10055` |
| X-Debug-Token Information Leak | `10056` |
| Username Hash Found | `10057` |
| GET for POST | `10058` |
| X-AspNet-Version Response Header | `10061` |
| PII Disclosure | `10062` |
| Permissions Policy Header Not Set | `10063` |
| Hash Disclosure | `10097` |
| Source Code Disclosure | `10099` |
| User Agent Fuzzer | `10104` |
| Weak Authentication Method | `10105` |
| HTTP Only Site | `10106` |
| Reverse Tabnabbing | `10108` |
| Modern Web Application | `10109` |
| Dangerous JS Functions | `10110` |
| Authentication Request Identified | `10111` |
| Session Management Response Identified | `10112` |
| Verification Request Identified | `10113` |
| Script Served From Malicious Domain (polyfill) | `10115` |
| ZAP is Out of Date | `10116` |
| Absence of Anti-CSRF Tokens | `10202` |
| Private IP Disclosure | `2` |
| Heartbleed OpenSSL Vulnerability | `20015` |
| Source Code Disclosure - CVE-2012-1823 | `20017` |
| Remote Code Execution - CVE-2012-1823 | `20018` |
| External Redirect | `20019` |
| Session ID in URL Rewrite | `3` |
| Buffer Overflow | `30001` |
| Format String Error | `30002` |
| CRLF Injection | `40003` |
| Parameter Tampering | `40008` |
| Server Side Include | `40009` |
| Cross Site Scripting (Reflected) | `40012` |
| Cross Site Scripting (Persistent) | `40014` |
| Cross Site Scripting (Persistent) - Prime | `40016` |
| Cross Site Scripting (Persistent) - Spider | `40017` |
| SQL Injection | `40018` |
| SQL Injection - MySQL (Time Based) | `40019` |
| SQL Injection - Hypersonic SQL (Time Based) | `40020` |
| SQL Injection - Oracle (Time Based) | `40021` |
| SQL Injection - PostgreSQL (Time Based) | `40022` |
| Cross Site Scripting (DOM Based) | `40026` |
| SQL Injection - MsSQL (Time Based) | `40027` |
| ELMAH Information Leak | `40028` |
| Trace.axd Information Leak | `40029` |
| .htaccess Information Leak | `40032` |
| .env Information Leak | `40034` |
| Hidden File Finder | `40035` |
| Spring Actuator Information Leak | `40042` |
| Log4Shell | `40043` |
| Exponential Entity Expansion (Billion Laughs Attack) | `40044` |
| Spring4Shell | `40045` |
| Remote Code Execution (React2Shell) | `40048` |
| Script Active Scan Rules | `50000` |
| Script Passive Scan Rules | `50001` |
| Path Traversal | `6` |
| Remote File Inclusion | `7` |
| Insecure JSF ViewState | `90001` |
| Java Serialization Object | `90002` |
| Sub Resource Integrity Attribute Missing | `90003` |
| Insufficient Site Isolation Against Spectre Vulnerability | `90004` |
| Charset Mismatch | `90011` |
| XSLT Injection | `90017` |
| Server Side Code Injection | `90019` |
| Remote OS Command Injection | `90020` |
| XPath Injection | `90021` |
| Application Error Disclosure | `90022` |
| XML External Entity Attack | `90023` |
| Generic Padding Oracle | `90024` |
| SOAP Action Spoofing | `90026` |
| SOAP XML Injection | `90029` |
| WSDL File Detection | `90030` |
| Loosely Scoped Cookie | `90033` |
| Cloud Metadata Potentially Exposed | `90034` |
| Server Side Template Injection | `90035` |
| Server Side Template Injection (Blind) | `90036` |
| Remote OS Command Injection (Time Based) | `90037` |

</details>
