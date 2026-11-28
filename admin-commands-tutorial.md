# Every command, explained for a new admin

You talk to the bot in Telegram. A command starts with `/`. The bot that users message is the polling server, called s4. The other servers only download and search. They do not read commands. If a command "does nothing", you are either not talking to the bot, or you are not an admin and the command is admin-only.

Anyone can use the user commands. Admin commands answer "Access Denied" unless your numeric Telegram user ID is on the admin list (`/set ADMINS`).

## The 10 minute rule

When the bot asks you to type something next (a URL, a chapter range, a website, "yes" or "no"), you have 10 minutes. That wait is the setting `REPLY_PROMPT_TIMEOUT_SECONDS` (default 600). If you wait longer, the bot says the request has ended and you send the command again.

If you are in the middle of that wait and you send a new command (`/download`, `/search`, `/cancel`, anything that starts with `/`), the bot does **not** search for the words of that command. It runs the command.

You can also skip the question by putting the answer on the same line. Both of these work:

- `/download` then, when asked, paste the URL
- `/download https://example.com/novel/some-story`

Same idea for `/search`, `/addsite`, `/bansite`, `/banuser`, and the other admin commands that ask a question.

## What a normal download feels like

1. The bot scans the novel page and tells you the title and how many chapters exist. Large sites can take a while. There is a button **Skip scan — enter range now** if you already know the range. The scan keeps going in the background so the title can still fill in.
2. You pick **All chapters** or **Custom range**. A custom range looks like `40-60` or `40 60`. You have 10 minutes.
3. You pick a file format. Right now the buttons are **EPUB** and **Text**. If the buttons cannot be shown, the bot queues an EPUB.
4. The task waits in a queue, then a worker downloads it and sends the file. Cover and synopsis are sent with the file. A copy is also forwarded to the backup groups.

There is a daily limit only if an admin set one with `/userlimit`. Banned users are told they cannot create tasks.

---

## User commands

### /start

Says hello and lists the everyday commands. Nothing is downloaded.

### /search

Find a novel by title across the websites the crawler knows.

Ways to use it:

- `/search` then type the name when asked
- `/search mad god` searches immediately

Rules:

- The name must be at least 2 characters.
- A search runs up to `SEARCH_TIMEOUT` seconds (default 90). You get an early list around 50 seconds and an update around 70 seconds, then the final list at 90. Those two early times are `SEARCH_PARTIAL_TIMES`.
- While it runs you see how many sources have answered, for example `searched 40/133 sources`, and a **Cancel** button. Cancel stops that search. You do not have to wait for the minute to end.
- Results are paged, `RESULTS_PER_PAGE` titles per page (default 10). Open a title, pick the website, confirm, and that URL goes into the same scan as `/download`.
- If the same search was already done in the last `SEARCH_CACHE_SECONDS` (default 24 hours), you get the saved list immediately. The note says the results may be outdated. A **Refresh search results** button sits above Cancel and runs a real search. That button is only on cached results.
- If every search slot on this server is busy, the search is queued and another server can run it. You still get progress and Cancel.

### /download

Start a download, or a search if you typed a title instead of a URL.

Ways to use it:

- `/download` then send a URL or a title when asked
- `/download https://site.com/novel/name` goes straight to the scan and the chapter-range buttons
- `/download some novel title` runs a search (same screens as `/search`). Picking a source then starts the scan.

The thing you send must be either a real `http` or `https` URL, or a title of at least 2 characters.

After the range, you pick EPUB or Text, and the task is queued. The reply tells you the format and, if you have a daily cap, how many tasks you have left in 24 hours.

A link on a banned website is refused with the reason from `/bansite`.

### /cancel

Stop your downloads.

- `/cancel` shows the tasks you can stop (queued or running) and asks you to pick. The question expires after 10 minutes.
- You can also cancel from that list by the number it shows.

Cancelling a running task stops the worker. Chapters already stored for a v4 novel may still sit in cache until the normal cleanup, but the task itself is cancelled and will not be sent.

### /progress

A snapshot of **your** running downloads: title, percent, chapter, range, elapsed time, rough time left, speed. It does not keep editing itself. Send it again for a new snapshot.

If you have more than one running download, live editing is turned off on purpose (see `MAX_LIVE_PROGRESS_PER_USER`) so Telegram does not rate-limit the bot. `/progress` is how you look at those.

### /queue

Shows pending and running downloads for the whole bot, not only yours: task id, user, title or URL, range, status, which server has it. Use this when someone asks "is it stuck or is it waiting?"

Pending means no worker has taken it yet. Running means a server is on it.

### /tasks

Your own tasks from the last day: what you queued, what finished, what failed. Use it to find a task id for `/retry`.

### /retry

Try again only the chapters that did not download, using the cache already on that worker. This is for v4 tasks. It is not a brand-new download.

It is offered when:

- the file was sent but some chapters were missing, or
- the download hit the hard time limit (`HARD_TASK_LIMIT_HOURS`, default 10 hours) and saved chapters were kept

The offer lasts `USER_RETRY_WINDOW_HOURS` (default 20). Anyone may trigger it, not only the person who queued the task. One user can only have `USER_RETRY_SLOT_LIMIT` retries in the window (default 3).

Ways to use it:

- `/retry` then follow the prompt
- `/retry 1234` with the task id
- `/retry https://site.com/novel/name` for the latest matching task

The retry is pinned to the server that already has the chapter cache. Another server cannot see those files.

### /sources

Lists the websites the admin catalog says are supported, plus any note (stable, unstable, paid chapters, and so on). This is the list `/addsource` edits. It is not a live probe of the internet. A site can be listed and still fail, or work even if a note says unstable.

### /webtoepub

Sends the tutorial links for the browser extension WebToEpub. Use this when the bot does not support the site, or the site needs a login or a paid chapter unlock. The usual advice for wtr-lab and similar sites is: download with WebToEpub yourself, then the bot can be given the result with `/webtoepub` as the guide, not as an uploader.

### /servers

Not admin-only in the handler. Shows each worker's id, online/offline, and last heartbeat (clock shifted +3 hours in the text). Use it when downloads sit in `pending` and you suspect a worker is dead.

---

## Admin commands — users

### /banuser

Stops that person from creating download tasks. Searches are a separate path; the ban is checked when a task is inserted.

Ways:

- `/banuser 123456789`
- `/banuser @theirname`
- `/banuser` then reply with the id or `@name`

`@name` only works if that person has already used the bot, because the id is looked up from an earlier task. If they have never used it, use the numeric id. You can get ids from `/logs` or `/tasks`.

### /unbanuser

Removes the ban. Same three ways as `/banuser`: id, `@name`, or the command alone and then a reply.

### /userlimit

Caps how many download tasks that person may create in 24 hours. Counted statuses are pending, running, and done. Failed tasks do not add to the cap. Cancelled tasks are not in that count.

Ways:

- `/userlimit 123456789 5` means 5 tasks per 24 hours
- `/userlimit @name off` removes the cap
- `/userlimit` then reply `123456789 5` or `@name off`

The number must be 1 or more. `off` means "no personal limit".

---

## Admin commands — websites

Domains are stored without `www`. You can paste a full URL. The bot keeps the host. `site.com` also matches `www.site.com` and `m.site.com`.

If you send the command with no extra text, the bot asks and waits 10 minutes. If your reply starts with `/`, it is treated as a new command.

### /addsource and /addsite

`/addsite` is only another name for `/addsource`.

Adds a website to the `/sources` list. It does **not** teach the crawler a new site. The crawler only downloads sites its library already knows. This list is the public catalog users read, including your notes.

Ways:

- `/addsite site.com`
- `/addsite site.com stable, prefer this one`
- `/addsource` then reply `site.com` or `site.com your note`

Notes are optional. Adding a domain that is already there updates the note.

### /removesource and its aliases

All of these are the same command:

- `/removesource`
- `/deletesource`
- `/delsource`
- `/removesite`
- `/deletesite`
- `/delsite`

They remove a domain from the `/sources` list. They do not ban it. A user who already has the URL can still `/download` it. To block downloads, use `/bansite`.

Ways:

- `/delsource site.com`
- `/removesite` then reply `site.com`

### /bansite

Blocks downloads from that domain. The user sees "This source is not supported" plus your reason. The reason is required.

Ways:

- `/bansite site.com copyright complaint`
- `/bansite` then reply `site.com copyright complaint`

Banning does not remove the domain from `/sources`. Do both if you want it gone from the catalog and refused as a URL.

### /unbansite

Clears the ban reason so downloads are allowed again. The image rule, if you set one, is left alone.

Ways:

- `/unbansite site.com`
- `/unbansite` then reply `site.com`

### /ignoreimages

For that domain, chapter pictures are not downloaded. The cover is still handled earlier. Use this when a site's images hang the download or blow up the EPUB.

Ways:

- `/ignoreimages site.com`
- `/ignoreimages` then reply `site.com`

### /unignoreimages

Turns chapter images back on for that domain.

Ways:

- `/unignoreimages site.com`
- `/unignoreimages` then reply `site.com`

### /split

Tells the converter to cut a **converted** format into parts of N chapters. Each part is sent as soon as it is ready. EPUB and Text are built directly and cannot be split. Today the catalog of output buttons is EPUB and Text, so `/split` often has nothing it is allowed to change until a converted format such as PDF is offered again.

Ways:

- `/split` shows the current sizes
- `/split pdf 500` means parts of 500 chapters
- `/split pdf all` or `/split pdf 0` means one file
- `/split pdf default` clears the saved size and returns to the built-in default (500 for PDF)

Allowed sizes are 20 to 5000, or 0 for "do not split". The same PDF number can be changed with `/set SPLIT_PDF`. `/reset SPLIT_PDF` clears it.

---

## Admin commands — logs and health

### /logs

Every download task the database still has, oldest first, with user id, username, name, server, crawler, status, URL, range, created time, completed time. The message is split if it is long, and it is temporary. Use it to find a user id or to see who is filling the queue.

### /searches

Search history.

Ways:

- `/searches` shows the last 50
- `/searches 20` shows the last 20 (a number of 4 digits or fewer is a count, max 200)
- `/searches 123456789` a longer number is treated as a user id
- `/searches @name` filters by username

Each row says whether it ran on the requesting server, waited in the queue, or was served from cache, plus status, source count, result count, a few titles, errors, and duration.

### /testsearches

A measuring mode, not a user feature. Leave it **off** except during a short test.

While it is on, users still get their list on the normal schedule (partial at 50 and 70, final at `SEARCH_TIMEOUT`, default 90). The search itself keeps running up to the test length so late websites can be recorded. Those late hits are written into the cache. The server holds its search slot for the whole test, so a long test blocks real searches. The mode turns itself off after 30 minutes so it cannot be forgotten.

Ways:

- `/testsearches` shows on/off and the latest reports
- `/testsearches on` uses 5 minutes (`SEARCH_TEST_DEFAULT_SECONDS`)
- `/testsearches 2 minutes` or `/testsearches 90s` sets the length. It must be longer than the user timeout and at most 10 minutes
- `/testsearches off` stops it. `stop` and `disable` work too
- `/testsearches report` prints the newest report
- `/testsearches report 4` prints report number 4

The report tells you, for each website that answered after the user already got their final list: the exact second, the site, and the novel titles and URLs it found. It also lists the slowest answers, sites that never started, sites that started and never answered, and errors. Use it to decide whether `SEARCH_TIMEOUT` should grow. If the only late answers are loose, wrong matches, do not raise the timeout.

### /deadsources

Websites the search health tracker has seen fail DNS.

A site is dropped after `SOURCE_DROP_AFTER` failures in a row (default 5), and only when most other sites did answer, so your own network outage does not drop everyone. A dropped site is skipped for `SOURCE_RETRY_DAYS` (default 3), then tried again.

Ways:

- `/deadsources` lists dropped sites and sites that are close to being dropped
- `/deadsources reset` clears every failure count
- `/deadsources reset novel` clears only URLs containing that text

### /set and /reset

Live settings shared by every server. Full detail, defaults, and when to touch each one are in [set-and-reset-tutorial.md](set-and-reset-tutorial.md).

Short version:

- `/set` lists every name, the live value, and the built-in default
- `/set SEARCH_TIMEOUT 120` asks `yes` or `no`, then saves it
- `/set ADMINS 111,222,333` replaces the admin list
- `/reset SEARCH_TIMEOUT` puts that one default back
- `/reset all` puts every `/set` value back

Other servers notice within 30 seconds. You have 10 minutes to answer yes or no.

---

## Admin checklist

A new website should be in the user-facing list:

1. `/addsite site.com your short note`
2. Confirm with `/sources`

A website must not be downloadable:

1. `/bansite site.com the reason users should see`
2. Optionally `/delsource site.com` so `/sources` stops advertising it

A website's images hang downloads:

1. `/ignoreimages site.com`

A user is abusing the queue:

1. `/userlimit 123456789 2` or `/banuser 123456789`
2. Undo with `/userlimit 123456789 off` or `/unbanuser 123456789`

Searches feel incomplete:

1. `/testsearches 3 minutes`
2. Run one `/search` you care about
3. `/testsearches report`
4. `/testsearches off`
5. Only then consider `/set SEARCH_TIMEOUT ...`

You changed a setting and regret it:

1. `/reset NAME` for one, or `/reset all` for every `/set` change
2. Reply `yes`
