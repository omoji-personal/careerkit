# What has gone wrong

This is the honest list. If you are going to point this thing at your own job
search, you should be able to see what it has got wrong before you trust it.

Every bug below is fixed and has a test holding it fixed. They are here because
a tool that quietly hands you a wrong answer is worse than one that crashes,
and every single one of these handed back a wrong answer without any error.

You do not need to read the code to follow this. If you only read one line:
**none of these bugs crashed. They all looked like normal, confident output.**

## The ones that cost you jobs

**A blank line in your exclusions list hid every job.**
If your list of things you did not want had an empty entry in it, that empty
entry matched everything. The result was a report with nothing in it, which
looks exactly like a quiet week on the job boards. You would have had no way to
tell the difference. Now an unusable exclusion stops the run and says so.

**Two different jobs at one company became one job.**
Two openings with the same title at the same employer collapsed into a single
row. Marking one as applied hid the other from every future report. You would
never have known the second one existed.

**Roles you qualified for were quietly downgraded.**
Some job boards tell the tool directly that a role is remote. That fact was
used when the job was first found, but never saved. So the next time the tool
re-checked its own stored jobs, it had forgotten, and moved genuinely remote
roles into the "needs a manual check" pile. Running the command the
instructions told you to run made your results worse.

## The ones that told you the wrong number

**A $500 stipend was read as a $500,000 salary.**
A "$500 home office stipend" sitting next to a real salary range was read as
half a million dollars and became the top of the reported range.

**Salaries vanished from the spreadsheet export.**
The tool read a salary out of a job posting, used it to decide whether the job
passed your filter, and then never wrote it down. The report said "Comp not
stated" on one line and quoted the actual range on the line directly below it.
About a third of the salary data was missing from the spreadsheet export, and
nothing anywhere said so.

**Hourly contract rates were treated as salaries.**
A contract paying $144 an hour could be presented as if it were a $299,000
salary.

## The ones where a check was not really checking

**A safety check that could never fail.**
One of the consistency checks was looking for a pattern that could not match
anything, so it reported no problems, forever. A check that cannot fail is
worse than no check, because it tells you everything is fine.

**Employers that did not exist.**
One job board answers "no problem" for absolutely any company name you ask it
about, including nonsense. So the tool "found" and registered 27 companies that
do not exist, then politely checked them for jobs forever. Two-letter company
shortcuts also collided: looking for one large law firm found an unrelated
company in Poland, and its jobs would have flowed into the report as if they
were the law firm's.

**A brand new install claimed it had backed up your database.**
On the very first run, before you had any data at all, it announced it was
backing up your database. There was nothing to back up.

## Found on 2026-08-06, by using it for a real job search all day

A day of running this against a live search found more than the previous weeks
of building it did. Every one of these was silent.

**It recommended a job that had already said no.**
A role was presented as the best find of the day. The application had gone in a
week earlier and the rejection had arrived the day before. Checking further, the
tool knew about six applications out of roughly twenty in six weeks, and five had
left no trace anywhere at all. It now reads an evidence file of what you have
applied to, and warns when a live posting sits at an employer that already
declined you.

**Application history had two writers and no safe repair path.**
SQLite suppressed jobs that `tracker.md` did not link, while tracker-only
applications remained `new` and resurfaced. The warning described useful
free-form narratives as missing merely because they lacked the canonical job
URL, then told the operator to run individual writes. `tracker-sync` now prints
the exact bidirectional changes without writing by default. With explicit
`--apply`, it appends canonical-link lines without rewriting tracker prose and
changes only unambiguous database rows without replacing notes. Board-level
links that match several openings are refused.

**A prepared draft was treated as bad application evidence.**
`applications.jsonl` describes work before and after submission, but `prepared`
was reported as an unknown state. Worse, valid later-stage states such as
`interviewing` were written directly into a database whose query layer did not
understand them, and generated evidence text replaced the user's notes. Prepared
records are now an explicit no-write preflight state; submitted pipeline states
remain suppressed while their richer state is retained in the event history,
and existing notes survive.

**Two different jobs at one company became one row, again.**
Anything in brackets was deleted from a title before comparing, so "Success
Architect (Agentforce)" and "Success Architect (Data Cloud)" were treated as the
same posting. Marking either one applied hid the other. This is the same failure
as the duplicate-title collapse listed above, surviving in a form nobody had
tested. Identity now keeps the words in brackets; grouping still drops them, so
genuine siblings still appear together.

**A job labelled remote that requires five days a week in an office.**
The location field said "Remote" and the description said "Onsite 5x per week
(Outside of Atlanta, GA)". The tool believed the label. A person reading the
description caught it. It now checks the description for a binding attendance
requirement, and says which sentence made it doubt the label.

**Jobs discarded for saying the opposite of what a filter screens for.**
"This is not a quota-carrying role" tripped the filter that removes
quota-carrying roles. "No security clearance is required" tripped the clearance
filter. Both threw away jobs you could have had.

**Postings nobody could read were passing every check.**
A posting with no description scored as a clean match, because every content
check passed against an empty page. That is not the posting being fine, it is the
checks never running, and nothing in the result showed the difference. Four roles
were recommended in one day whose disqualifying requirement sat in a section the
listing never included.

**A requirement the employer called optional was still disqualifying.**
"3+ years with Marketing Cloud preferred, not required" was read as a
requirement.

**A salary of $2,080 to $520,000,000.**
A listing advertised "Pay: $1.00 - $250,000.00 per year". Anything under $1,000
was assumed to be an hourly rate and multiplied by 2,080, both ends of it.

**Half the compensation data was missing from the spreadsheet.**
The tool read a salary out of a listing, used it to decide whether the job
passed, then never saved it. The report said "Comp not stated" on one line and
quoted the range on the line below.

**Re-checking your saved jobs made the results worse.**
Some boards state directly that a role is remote. That fact was used when the job
was found and never stored, so re-checking saved jobs silently downgraded
genuinely remote roles. The instructions tell you to re-check after changing your
criteria.

**A job board belonging to a completely different company.**
Searching for "National Public Radio" found a public affairs agency in Toronto,
because the tool guessed a web address from the name and never asked whose it
was. The same mistake had already found a Polish IT company and a Belgian law
firm. It now asks the board what company it belongs to.

**The tool told websites it was Chrome.**
It sent a browser identity it was not, while this page and the README told you it
made ordinary requests and named which sources it scrapes. It now says what it
is and links here.

**And one caused by the fix for another.**
The check added to catch fake listings flagged 55 of 56 live jobs the first time
it ran, because it treated "we never looked for this" as "we looked and found
nothing". A check that flags everything is a check nobody reads.

## The uncomfortable pattern

After the first round of fixes, the fixes were attacked directly. **Every
serious problem found in the second round had been introduced by a fix from the
first round.** That is the honest reason this page exists rather than a
reassuring paragraph about quality.

It kept happening. The check for fake listings flagged almost everything. The
feature that fetches a fuller job description replaced good listings with 20,000
characters of website navigation. A test written to prove the salary bug was
fixed passed even with the bug put back, because the sample data it used had no
salaries in it. Each was caught by pointing the thing at real data and by
deliberately breaking the code to watch the test fail.

## What is still true today

- **US locations only.** Roles outside the US are screened out unless you
  change that. If you are searching outside the US, that is the first thing to
  fix, and help is welcome.
- **It is new, and one person wrote it.** There are 280+ automated tests and
  nearly all of them exist because something on this page broke first. That is
  not the same as being battle-tested.
- **Job boards change without warning.** All eighteen employer adapters are
  checked against sanitized responses saved from real public boards, so a
  known schema change now fails a test. That catches shape drift; it does not
  make a saved response a live availability check.
- **It cannot spot a fake job posting.** A listing that was never a real
  opening still looks like a real opening to it.

If you hit something not on this page, opening an issue genuinely helps.
