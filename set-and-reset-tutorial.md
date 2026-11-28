# /set and /reset — every live setting

This is for an admin who has never changed a bot setting before. You do not need to edit code or restart servers. You type a command in Telegram, the bot asks you to confirm, and every worker copies the new value from the database within 30 seconds.

Only people whose numeric Telegram ID is in the admin list can use `/set` and `/reset`. Everyone else gets "Access Denied."

## How a change actually happens

1. You send `/set SEARCH_TIMEOUT 90` (or any other name and value below).
2. The bot replies with the current value and the new value, and asks you to reply `yes` or `no`.
3. If you do not answer within 10 minutes, the request dies and nothing changes. If you type another command that starts with `/`, that command runs instead.
4. `yes` or `y` writes the value into the shared database table `bot_settings`, under the key `set:NAME`.
5. The server you talked to applies it immediately. Every other server re-reads the database every 30 seconds and copies the same value into its memory.
6. The value survives a restart and a brand-new server, because the database is the source of truth. The number written in the Python file is only the built-in default, used when no override exists.

`/reset NAME` deletes that one database row. The next refresh (immediately on the polling server, within 30 seconds on the others) puts the built-in default back.

`/reset all` deletes every `/set` override at once. It does not wipe bans, the source list, search history, or the `/testsearches` switch. Those live in different rows.

`/set` with nothing after it prints every name, the value in effect right now, and the built-in default. A setting you have changed is marked `(changed)`.

`/set NAME` with no value just tells you the current value and the default. It does not change anything.

You can reply `no` or `n` to cancel. If the new value is the same as the current one, the bot does not even ask.

## Names you can type

Some names have a shorter or dotted alias. They store the same setting:

| You type | Stored as |
|---|---|
| `ADMINS` | `ADMIN_USER_ID` |
| `apihelper.CONNECT_TIMEOUT` | `CONNECT_TIMEOUT` |
| `apihelper.READ_TIMEOUT` | `READ_TIMEOUT` |

Example: `/set ADMINS 111,222,333` and `/set ADMIN_USER_ID 111,222,333` do the same thing.

## What does not lag

Workers do not query the database on every chapter or every search tick for these values. They refresh the whole list once every 30 seconds. A download that is already running keeps going. The new number is used the next time that piece of code looks at the variable (the next progress edit, the next search, the next claim of a stuck task, and so on).

Two exceptions worth knowing:

- `SEARCH_SLOTS` replaces the in-memory "how many searches at once" counter. A search that already started finishes on the old counter, so for a short moment you might have one extra search. New searches obey the new cap.
- `SPLIT_PDF` is also written to the older `split_size_pdf` row, which the converter reads when it builds a file. That one is visible to every worker on the next conversion, without waiting for the 30 second loop. `/reset SPLIT_PDF` and `/reset all` delete that row too.

## The settings

Ranges below are what the bot will accept. Anything outside the range is rejected before the yes/no question.

### SEARCH_TIMEOUT

- Built-in default: **90** seconds. Allowed: 15 to 600.
- This is how long a normal search runs before the user gets the final list.
- Partial lists still appear at the times in `SEARCH_PARTIAL_TIMES` (default 50s and 70s), and the final list arrives when this timer ends.
- Raising it makes results more complete and makes each search occupy a slot longer. Lowering it makes searches faster and more likely to miss slow websites.
- The queue wait for a search that had to wait for a free server is this number plus 240 seconds. That extra 240 is not its own `/set` key.
- `/testsearches` can run the background crawl longer than this. Users still stop receiving updates at `SEARCH_TIMEOUT`.

When to change it: users say good novels are missing, and `/testsearches report` shows useful sites answering just after 90 seconds. Bump it to just past that slowest useful answer, for example 120. If searches feel slow and the late sites are junk, leave it at 90 or lower it.

### SEARCH_PARTIAL_TIMES

- Built-in default: **50,70**. Two whole numbers, first smaller than the second. Each must be from 1 to 600.
- At the first number of seconds the user gets an early list. At the second number that list is refreshed. The final list is still at `SEARCH_TIMEOUT`.
- Example: `/set SEARCH_PARTIAL_TIMES 40,80`
- If you set the second number higher than `SEARCH_TIMEOUT`, that second update never shows, because the search already ended. Keep both below the timeout.

When to change it: 50 seconds feels too long before anyone sees anything. Try `30,60`. Do not put them a few seconds apart or the user gets two near-identical edits and you waste Telegram rate limit.

### SEARCH_CACHE_SECONDS

- Built-in default: **86400** (24 hours). Allowed: 0 to 604800 (7 days).
- If someone searches the same words again inside this window, the bot reuses the saved results instead of hitting every website again. The list says the results may be outdated and shows **Refresh search results**. Refresh ignores the cache once.
- `0` turns the cache off. Every search hits the websites.

When to change it: a site's catalog changes all day and users complain the list is stale. Drop it to `3600` (1 hour) or `10800` (3 hours). If websites are flaky and you want fewer live searches, raise it toward a few days.

### SEARCH_SLOTS

- Built-in default: **2**, unless the server was started with the environment variable `SEARCH_SLOTS`. Allowed: 1 to 8.
- How many searches one server will run at the same time. A third search is written to the database and picked up by any server that has a free slot, including this one when a slot frees.
- Higher means faster service when many people search together, and more load (about `SEARCH_CONCURRENCY` website checks per running search).

When to change it: searches sit on "queued" while the server looks idle. Try 3. If the server runs out of memory or websites start timing out in bulk, put it back to 2 or 1.

### SEARCH_CONCURRENCY

- Built-in default: **10**. Allowed: 1 to 40.
- Inside one search, how many websites are contacted at the same time.
- Higher finishes the source list faster but is easier for hosts to rate-limit, and it can leave many sources "never started" if the timeout arrives first. `/testsearches report` says so when that happens.

When to change it: the test report says a lot of sources never started. Try 15 or 20, and keep an eye on errors. If you see bursts of DNS or blocking errors, go back down.

### RESULTS_PER_PAGE

- Built-in default: **10**. Allowed: 3 to 30.
- How many novel titles are on one page of search results. The buttons stay the same: next, previous, refresh (only for cached results), cancel.

When to change it: titles are long and 10 buttons feel cramped. Try 5. If users page a lot, try 15.

### SOURCE_DROP_AFTER

- Built-in default: **5**. Allowed: 1 to 30.
- A website that fails DNS this many searches in a row is dropped from future searches. A failure only counts when other sites answered, so a dead network on your side does not ban the whole internet.
- Dropped sites are listed by `/deadsources`.

When to change it: one flaky site gets dropped too easily. Raise it to 8. A dead domain is wasting the first seconds of every search. Lower it to 3.

### SOURCE_RETRY_DAYS

- Built-in default: **3**. Allowed: 1 to 30.
- After a site is dropped, it is tried again once this many days have passed. `/deadsources reset` tries them again immediately.

When to change it: you know a host will be down for a week. Set 7 so you do not keep probing it.

### HARD_TASK_LIMIT_HOURS

- Built-in default: **10** hours. Allowed: 1 to 48.
- One download attempt is killed after this much wall-clock time. For the v4 crawler, chapters already saved stay on that worker and the user can `/retry` within the retry window. It does not delete the novel cache.
- This is a safety cap against a crawl that will never finish, not a target. A healthy novel should finish long before this.

When to change it: a huge novel on a slow site is dying at 10 hours with most chapters already stored, and retries are annoying. Try 16. Do not set 48 unless you accept a worker being busy that long.

### STUCK_PROGRESS_MINUTES

- Built-in default: **20**. Allowed: 2 to 180.
- If a running task sends no progress heartbeat for this long, this worker cancels its local attempt and puts the task back in the queue for a different worker. The worker also starts a fresh task loop so one hung download cannot block it forever.
- Too low: a slow but alive site (one chapter every few minutes, plus image downloads) gets killed and restarted in a loop. Too high: a truly dead crawl occupies the worker.

When to change it: logs show "Requeueing stuck task" on novels that were actually moving. Raise it to 40. If hung tasks sit for 20 minutes too often, try 10, and watch for false kills.

### PROGRESS_UPDATE_INTERVAL

- Built-in default: **10** seconds. Allowed: 3 to 120.
- How often the "Downloading… 40%" message is edited, and how often the database copy of that progress is written.
- Telegram will answer 429 (too many requests) if you edit too often, especially when one user has several downloads. The bot already limits live edits to `MAX_LIVE_PROGRESS_PER_USER`.

When to change it: you see `PROGRESS 429` in the logs. Raise it to 20 or 30. Users want smoother percentages and you have few users. Try 5. Do not go to 3 unless you enjoy rate limits.

### MAX_LIVE_PROGRESS_PER_USER

- Built-in default: **1**. Allowed: 1 to 10.
- If a user already has more than this many tasks in `running`, extra tasks do not edit a progress message. They get one notice telling them to use `/progress`.
- The database heartbeat still updates, so `/progress` and the stuck-task reaper keep working.

When to change it: you are the only heavy user and you want two live bars. Set 2. If 429 errors come back, set it back to 1.

### UPLOAD_RETRY_DELAY_MINUTES

- Built-in default: **10**. Allowed: 1 to 180.
- If Telegram refuses the EPUB, the file is kept and the first automatic resend waits this many minutes.

When to change it: refusals are from a short rate limit and 10 minutes is longer than you want the user to wait. Try 3. If the network is down for a while, 10 or 15 is safer.

### UPLOAD_RETRY_SECOND_DELAY_MINUTES

- Built-in default: **20**. Allowed: 1 to 180.
- If the first resend also fails, the second wait is this long. After two failures the row is held until it expires.

When to change it: same idea as the first delay. Give a longer outage more time with 30, or shorten it if Telegram recovers quickly.

### UPLOAD_RETRY_KEEP_HOURS

- Built-in default: **12**. Allowed: 1 to 72.
- How long the pending EPUB is kept on disk for those retries. After that the cleanup marks it expired and deletes the file.

When to change it: files are large and the disk fills. Lower it to 6. Users often come back the next day and the file is already gone. Raise it to 24.

### USER_RETRY_WINDOW_HOURS

- Built-in default: **20**. Allowed: 1 to 72.
- After a download finishes with missing chapters, or hits the hard time limit on v4, anyone can `/retry` it until this many hours have passed. The chapter cache is kept at least this long (cleanup waits a few extra hours on top).

When to change it: users retry the next day and are told the window closed. Try 36. Cache disk is tight. Try 12, and tell users.

### USER_RETRY_SLOT_LIMIT

- Built-in default: **3**. Allowed: 1 to 20.
- How many distinct tasks one user may retry inside the retry window. Stops one person from requeueing the whole history.

When to change it: a trusted user is blocked after three bad novels. Set 6. Someone is abusing retry. Set 1.

### REPLY_PROMPT_TIMEOUT_SECONDS

- Built-in default: **600** (10 minutes). Allowed: 60 to 3600.
- How long the bot waits when it has asked for a reply: the novel URL, a chapter range, an admin argument such as the site to ban, and the yes/no on `/set`.
- When it expires, the bot says the request has ended. The chapter-range screen uses this same number.

When to change it: admins are slow to answer `/set`. Try 1200. You want abandoned prompts to die faster. Try 180. Do not set 60 if you expect people to copy a URL from another app.

### LOCAL_API_FALLBACK_AFTER_MINUTES

- Built-in default: **20** (or the environment variable of the same name). Allowed: 1 to 180.
- The local Bot API is what allows files larger than Telegram's cloud limit of about 50 MB. If that local server stops answering, s4 waits this long, then logs the bot out of the dead local server and switches everyone to the official cloud API.
- The real wait is the smaller of this value and `LOCAL_API_MAX_SILENCE_MINUTES`.

When to change it: the local API blips for a minute and you do not want a cloud switch. Leave 20, or raise it. The local API is often down and users with big books wait too long. Lower it to 5.

### LOCAL_API_MAX_SILENCE_MINUTES

- Built-in default: **30** (or the environment variable). Allowed: 1 to 180.
- A second cap on how long workers may keep talking to a dead local server once the cloud API is allowed again. The switch happens at the smaller of the two minute values.

When to change it: you want a hard ceiling even if someone sets the fallback very high. Set 15.

### TELEGRAM_CLOUD_LOCKOUT_MINUTES

- Built-in default: **15**. Allowed: 1 to 120.
- After the bot logs out of the cloud API in order to move back to the local server, it will not use the cloud API again for this many minutes. Telegram needs that gap. If the local server dies during the lockout, downloads that need the cloud API wait.

When to change it: almost never. Telegram's own rule is about 10 minutes; 15 is the safety margin. Setting 1 can make the switch fail and leave the bot logged out of both places.

### CONNECT_TIMEOUT

- Built-in default: **60** seconds. Allowed: 5 to 300.
- Alias: `apihelper.CONNECT_TIMEOUT`.
- How long the Telegram client waits to open a connection to the Bot API (cloud or local). This is not the novel-website timeout.

When to change it: the local API is on a distant machine and connects are slow. Try 90. Connects fail fast on a bad route and you want to notice sooner. Try 20. It does not make novels download faster.

### READ_TIMEOUT

- Built-in default: **600** seconds (10 minutes). Allowed: 30 to 3600.
- Alias: `apihelper.READ_TIMEOUT`.
- How long the Telegram client waits for a response after the connection is open. Uploading a large EPUB needs a long read timeout. If this is shorter than the upload, a successful upload can be reported as a failure and retried, which sends the file twice or queues a pointless retry.

When to change it: big books fail around the 10 minute mark while the local API is still receiving them. Try 1200. Do not drop it below a few minutes if you send large files.

### ADMIN_USER_ID

- Built-in default: **7994985826,8402824940,7378945311**. Allowed: 1 to 30 numeric IDs.
- Alias: `ADMINS`.
- These user IDs may use every admin command, including `/set`. The value is a comma-separated list. Spaces are fine: `/set ADMINS 111, 222, 333`.
- This replaces the whole list. It does not add one person. To add someone, send the old IDs plus the new one.
- The bot will let you remove yourself. After you confirm, you cannot `/set` yourself back unless another admin does it, or someone `/reset ADMIN_USER_ID` while they are still on the old list. The reset only works for someone who is still an admin at the moment they send it. If you lock everyone out, the fix is `/reset` from an ID that is still in the list, or deleting the `set:ADMIN_USER_ID` row in the database by hand.

When to change it: a moderator joins or leaves. Always include at least one ID you can still log into.

### SPLIT_PDF

- Built-in default: **500** chapters per file. Allowed: 0 to 5000. `0` means one file with every chapter.
- Only matters when a conversion format is actually split. EPUB and Text are never split. The PDF size is stored for the converter the same way `/split` stores it.
- `/reset SPLIT_PDF` also removes the `split_size_pdf` row, so `/split`'s saved PDF size goes back to 500 as well.

When to change it: one huge PDF conversion dies or takes hours. Try 200 so each part is smaller and is sent as soon as it is done. 500 is already the default because smaller parts mean more Telegram uploads.

## Examples

Change the search timeout:

```
/set SEARCH_TIMEOUT 120
```

Bot: `Change SEARCH_TIMEOUT from 90 to 120? Reply yes or no.`

You: `yes`

Replace the admins:

```
/set ADMINS 7994985826,8402824940,7378945311
```

That example is the built-in list. Your real command must contain every ID that should keep admin, and only those.

Put one setting back:

```
/reset SEARCH_TIMEOUT
```

Put everything back:

```
/reset all
```

See the board:

```
/set
```

## What /set will not do

It will not change the bot token, the database password, which server is s4, the source catalog, bans, or image rules. Those have their own commands (`/addsource`, `/bansite`, `/banuser`, and the rest). It will not run Python. An unknown name is rejected.
