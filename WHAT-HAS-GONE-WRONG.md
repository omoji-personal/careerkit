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

## The uncomfortable pattern

After the first round of fixes, the fixes were attacked directly. **Every
serious problem found in the second round had been introduced by a fix from the
first round.** That is the honest reason this page exists rather than a
reassuring paragraph about quality.

## What is still true today

- **US locations only.** Roles outside the US are screened out unless you
  change that. If you are searching outside the US, that is the first thing to
  fix, and help is welcome.
- **It is new, and one person wrote it.** There are 181 automated tests and
  nearly all of them exist because something on this page broke first. That is
  not the same as being battle-tested.
- **Job boards change without warning.** Four of the seventeen boards are
  checked against saved real responses, so if those change shape a test fails.
  The other thirteen are not, which is the most useful thing anyone could
  contribute.
- **It cannot spot a fake job posting.** A listing that was never a real
  opening still looks like a real opening to it.

If you hit something not on this page, opening an issue genuinely helps.
