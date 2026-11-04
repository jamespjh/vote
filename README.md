# vote

A small live audience poll, in the style of Mentimeter or Slido, written from scratch.

- **<https://research.jh.arc.ucl.ac.uk/vote>**: phones answer Yes or No to "I liked James's Talk".
- **<https://research.jh.arc.ucl.ac.uk/vote/results>**: the projector view: big live bars, a timeline of the running totals, and a QR code for the voting page. It can be shown in an iframe.

## How it works

`app/vote.py` is a WSGI app using only the Python standard library, run by Apache's `mod_wsgi` on the `web` VM. Votes are kept in SQLite. The pages poll the JSON API once a second.

| Route | What |
| --- | --- |
| `GET /vote` | voting page |
| `GET /vote/results` | results page |
| `GET /vote/api/me` | this phone's current answer |
| `POST /vote/api/vote` | `{"choice": "yes"}` or `{"choice": "no"}` |
| `GET /vote/api/results` | totals and timeline |

Run it locally with:

```bash
cd app && python3 -m wsgiref.simple_server 2>/dev/null || python3 -c "from wsgiref.simple_server import make_server; import vote; make_server('', 8000, vote.application).serve_forever()"
```

and test it with `pytest tests`.

## Deploying

Like the other sites: pushing to `main` runs `Test`; when that passes on a push, `trigger.yml` uses the `INFRA` secret to dispatch `deploy_vote.yml` in [jh-infrastructure](https://github.com/jamespjh/jh-infrastructure), which runs `ansible/web.yml` here on the self-hosted runner. The role copies `app/` to `/var/www/builds/vote/app`, puts the database in `/var/www/vote-data` (SELinux type `httpd_sys_rw_content_t`, so Apache can write to it, and outside the build folder so deploys keep the votes) and installs `/etc/httpd/conf.d/vote.conf`.

To clear the votes, delete `/var/www/vote-data/vote.sqlite*` on the VM and restart httpd.

## Choices made

These were made without asking, because the poll was built while James was on stage:

- **One vote per phone** means one per browser: a random ID in an `HttpOnly` cookie, kept for a year. Voting again replaces that phone's vote, so people can change their mind. Someone determined could vote again from a private window; for a friendly room that is fine, and anything stronger (logins) would put people off voting.
- **Polling, not WebSockets or server-sent events.** A request a second from one projector and a few dozen phones is nothing for Apache, and it works through the Cloudflare tunnel with no extra configuration.
- **Standard library only**, so the VM needs nothing beyond `python3` and `mod_wsgi`, which the infrastructure repo already installs.
- **One mod_wsgi daemon process with many threads**, so a single lock keeps SQLite writes simple and safe.
- **The timeline** shows the running Yes and No totals after every vote (or change of mind), thinned to 120 points.
- **The QR code** is drawn in the browser by [qrcode-generator](https://github.com/kazuhikoarase/qrcode-generator) 1.4.4 (MIT), kept in `app/qrcode.js` so the projector doesn't depend on a CDN. It encodes the address the results page was loaded from, so it also works on the `condenser` address.
- **Iframes**: no `X-Frame-Options` or `frame-ancestors` header is sent, so the results page can be embedded in the slides from any origin.
- **Brand**: colours are from the UCL palette used on [www.ucl.ac.uk/isd](https://www.ucl.ac.uk/isd): dark purple `#361A54` and `#2C0442` backgrounds, bright purple `#9A3BFF` for No, cyan `#34C6C6` for Yes, mid purple `#BA82FF` for accents. The type is UCL Sans if it's installed on the projector machine, then Helvetica Neue and Arial. UCL Sans isn't bundled, because the font files are UCL's, not ours to republish.
- **Sizing**: the results page sizes everything from the smaller of the width and height, so it fills a 16:9 iframe of any size without scrolling.
