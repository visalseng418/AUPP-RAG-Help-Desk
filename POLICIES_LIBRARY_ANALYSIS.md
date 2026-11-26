# docs/academic-policies/ and docs/library/ — Data Quality Analysis

Analysis only — no files modified, no pipeline changes made. Every file in
both folders was read in full (or, for the two multi-hundred-KB course
files, sampled/grepped structurally) and checked for the two known failure
patterns from `programs/`, `scholarships/`, and `tuition/`:

1. **Flattened tables** — an HTML table whose rows got scraped as
   sequential lines with no `Header: value` labels attached (values
   floating free, disconnected from their column headers).
2. **Roster/list fragmentation risk** — a file listing many discrete
   named items (people, resources, FAQ entries) in one file, long enough
   to be silently split mid-list by the default ~1200-char prose chunker
   (the `board-of-trustees.txt` bug).

Character counts below are the file's body length, i.e. everything after
the `Source:` line (same definition `ingest.py` uses for `MIN_BODY_CHARS`).

## Summary table

### docs/academic-policies/

| file | chars | has_table | table_status | has_roster | roster_chars | at_risk | notes |
|---|---:|---|---|---|---|---|---|
| academic-advising.txt | 632 | no | n/a | no | - | no | clean prose |
| academic-and-student-policies-and-regulations.txt | 55,532 | yes | **flattened** | no | - | n/a | mega-merge of ~35 other policy pages; embeds the *same* broken suspension/dismissal and grading tables described below, duplicated inline |
| academic-calendar.txt | 26,005 | yes | clean | no | - | no | already `Semester: ... \| Week: ... \| Event: ... \| Date: ...` format |
| academic-honors.txt | 822 | no | n/a | no | - | no | clean prose |
| academic-information.txt | 120 | no | n/a | no | - | no | link-hub, correctly excluded by MIN_BODY_CHARS (120 < 150) |
| academic-integrity.txt | 1,444 | no | n/a | no | - | no | clean prose |
| academic-records-student-privacy.txt | 717 | no | n/a | no | - | no | clean prose |
| academic-standing.txt | 2,623 | yes | **flattened** | no | - | n/a | 4-row suspension/dismissal GPA table |
| academic-support-services-learning-center.txt | 552 | no | n/a | no | - | no | clean prose |
| academic-units-overload-course-numbering-system.txt | 2,325 | no | n/a | no | - | no | clean prose |
| class-attendance.txt | 1,170 | no | n/a | no | - | no | clean prose |
| class-levels.txt | 771 | no | n/a | no | - | no | term/definition list, not a broken table (4 items, small) |
| communications-and-e-mail-policy.txt | 2,735 | no | n/a | no | - | no | clean prose (bulleted policy list) |
| course-add-drop.txt | 218 | no | n/a | no | - | no | clean prose |
| course-descriptions-archive.txt | 95,107 | yes | **flattened** | no | - | n/a | ~300+ course records, same shape `course-descriptions.txt` had before its fix — this file was apparently missed |
| course-descriptions.txt | 161,905 | yes | clean | no | - | no | already fixed, has dedicated `course_records` chunker |
| curriculum-changes.txt | 331 | no | n/a | no | - | no | clean prose |
| declaring-and-changing-a-major-and-a-minor.txt | 773 | no | n/a | no | - | no | clean prose, **but see anomaly below** (wrong H1) |
| degree-requirements.txt | 381 | no | n/a | no | - | no | clean prose |
| disciplinary.txt | 2,566 | no | n/a | no | - | no | clean prose |
| discrimination-harassment-and-violent-and-threatening-behaviour.txt | 858 | no | n/a | no | - | no | clean prose |
| elective-courses.txt | 2,974 | yes | **flattened** | no | - | n/a | ~40-row course code/name/credit/prerequisite table |
| exclusion-from-a-course.txt | 717 | no | n/a | no | - | no | clean prose |
| final-examinations.txt | 760 | no | n/a | no | - | no | clean prose |
| general-education.txt | 1,308 | yes | **flattened** | no | - | n/a | 13-row course code/name/credit table |
| general-information.txt | 382 | no | n/a | no | - | no | link-hub page, **see anomaly below** (not excluded, has no real content) |
| grading-grade-point-average-grade-change.txt | 7,781 | yes | **flattened** | no | - | n/a | two flattened tables: 14-row grade scale + 14-row grade-notation table |
| graduation-application-and-requirements.txt | 2,045 | no | n/a | no | - | no | clean prose |
| independent-study.txt | 1,432 | no | n/a | no | - | no | clean prose |
| methods-for-assessing-student-learning.txt | 255 | no | n/a | no | - | no | clean prose |
| methods-of-instruction.txt | 509 | no | n/a | no | - | no | clean prose |
| repeating-courses.txt | 272 | no | n/a | no | - | no | clean prose |
| student-grievance.txt | 1,422 | no | n/a | no | - | no | clean prose |
| student-leave-of-absence.txt | 685 | no | n/a | no | - | no | clean prose |
| student-registration.txt | 346 | no | n/a | no | - | no | clean prose |
| submitting-similar-work-for-multiple-courses.txt | 289 | no | n/a | no | - | no | clean prose |
| teach-out.txt | 1,168 | no | n/a | no | - | no | clean prose |
| unscheduled-class-meetings.txt | 817 | no | n/a | no | - | no | clean prose |
| withdrawals.txt | 1,791 | no | n/a | no | - | no | clean prose |

### docs/library/

| file | chars | has_table | table_status | has_roster | roster_chars | at_risk | notes |
|---|---:|---|---|---|---|---|---|
| library-library-faqs.txt | 4,497 | no | n/a | yes (list) | 4,497 | **yes** | 28 numbered FAQ entries, no `Header:value` table but same fragmentation shape as a roster |
| library-library-mission-and-vision.txt | 1,004 | no | n/a | no | - | no | clean prose (bulleted mission/vision/goals) |
| library-library-staff.txt | ~70 | no | n/a | yes (people) | ~70 | **excluded entirely** | 2 staff members; body is under `MIN_BODY_CHARS` (150) so it's dropped from the index, not fragmented |
| library-online-library-database.txt | 52,883 | no* | n/a | yes (list) | 52,883 | **yes — severe** | A–Z resource directory, ~150+ named database/resource entries; *no flattened table, but structurally identical fragmentation risk to a roster, at 44x the size of board-of-trustees.txt |
| library-rules-and-policies-of-the-library.txt | 5,504 | no | n/a | no | - | no | long prose policy sections, legitimately long — not a roster |
| library.txt | 5,709 | no | n/a | no | - | no | homepage: mixed nav/marketing/event content, messy but not a table or roster |

## Files needing table fixes

Five academic-policies files (plus the mega-merge file that duplicates two
of these tables inline) have genuinely flattened tables: header row's
words listed first, then every row's values crammed in sequence
afterward with no `Header: value` label attached to anything. No library
file has this pattern.

### 1. `academic-standing.txt` — 4 rows

```
Number of Credit hours Completed
Minimum Grade Point Average – Suspension
Minimum Grade Point Average – Dismissal
0-30 credit hours
1.50
N/A
31-60 credit hours
1.70
1.50
61-90 credit hours
1.80
1.70
91 and above
1.90
1.80
```
Should become 4 rows like:
`Credit Hours Completed: 0-30 | Minimum GPA – Suspension: 1.50 | Minimum GPA – Dismissal: N/A`

### 2. `grading-grade-point-average-grade-change.txt` — 14 + 14 rows (two separate tables)

Table A (grade scale):
```
Grade
Percentage Range
GPA Quality Points
Description
A
93-100%
4.00
Outstanding attainment of course goals
A-
90-92.99%
3.67
Superior attainment of course goals
...
```
Table B (non-traditional grade notations, no header row in the source but
still a repeating record structure — AU, CC, CH, CR, EX, FN, I, NC, NR, P,
SP, TR, W, WF):
```
AU
Audit
0.00
A notation of AU is assigned to not regularly enrolled students...
CC
Continuing Course
0.00
A notation that indicates a course which is more than a semester in length is continuing.
...
```

### 3. `general-education.txt` — 13 rows

```
Course Code
Course Name
Credits
COMM 103
Oral Communications
3
ENGL 101
English Composition I
3
...
SMNR 101
SMNR 101
1
```
Note: the last row (`SMNR 101` / `SMNR 101` / `1`) has the same text in
both the code and name slots — worth a human check against the source
page; may be a genuine data gap on AUPP's site rather than a scraping bug.

### 4. `elective-courses.txt` — ~40 rows

```
Course Code
Course Name
Credits
Prerequisites
MGMT450 ****
Leadership
3
All BUS Foundations
ENTR 110*
Introduction to Entrepreneurship
3
None
...
```
Also note a likely typo carried over from the source, not a scraping
artifact: row `IR 361 | Gener and Development` (line 123) — should almost
certainly be "Gender and Development."

### 5. `course-descriptions-archive.txt` — ~300+ rows (largest offender)

Same 6-field-per-record shape (`Course Code / Course Name / Credit /
Description / Grading Mode / Pre-Requisite`) that `course-descriptions.txt`
already had fixed — this archive file appears to have been missed by that
earlier pass:
```
Course Code
Course Name
Credit
Description
Grading Mode
Pre-Requisite
ACCT 201
Financial Accounting
3
Learn about the important mechanisms for communicating the financial health of an organization...
Normal
MATH 177
ACCT 202
Managerial Accounting
3
Explore the alternative methods of preparing essential accounting information...
Normal
ACCT 201
...
```
At 95KB across ~300 records this is the single biggest table-fix job in
either folder — bigger than the fix already applied to
`course-descriptions.txt` (161KB / ~261 records).

### Also present, duplicated: `academic-and-student-policies-and-regulations.txt`

This 55KB mega-file is a straight concatenation of nearly every other
academic-policies page under one URL. It contains its own inline copies of
the academic-standing table (line 48) and both grading tables (line 202
onward), in the same broken/flattened form as the standalone files above.
Fixing the five source files won't fix this one — it needs the same
row-reformatting treatment applied to its own copies of that content, and
its existing `titled_sections` chunker has no table-row awareness at all,
so today these rows aren't just unlabeled, they're also liable to be cut
mid-row wherever the 1200-char prose splitter lands.

## Files at risk of roster fragmentation

None of the academic-policies files have a genuine multi-person roster —
`class-levels.txt` is a 4-item list, well under any risk threshold.

Three library files are at risk, in three different ways:

| file | chars | shape | current failure mode under `ingest.py`'s "library" = Group D rules |
|---|---:|---|---|
| `library-online-library-database.txt` | 52,883 | ~150+ named database/resource entries with descriptions, A–Z organized | No `Header:value` pattern, so 100% of it falls to the plain 1200-char prose splitter — it will be sliced into ~44 arbitrary chunks with zero entry-boundary awareness. A query like "does the library have a Cambodian law database" retrieves whichever arbitrary slice happened to land near that entry; entries near chunk boundaries get split in half. This is the same failure class as board-of-trustees.txt, just ~27x larger. |
| `library-library-faqs.txt` | 4,497 | 28 numbered Q&A entries | Same plain prose splitter, ~4 chunks. Less severe than the directory above (each Q&A is fairly self-contained) but a "how many FAQs are there about X" or a multi-part answer spanning adjacent numbered entries can still land split across chunks. |
| `library-library-staff.txt` | ~70 | 2 named staff members (Chhovy Chhorn – Deputy Librarian, Gay Garcia – Assistant Librarian) | **Not** a fragmentation risk — it's below `MIN_BODY_CHARS` (150) and gets silently **excluded from the index entirely** by the current hygiene filter. This is arguably worse than fragmentation: a "who works at the library" question currently gets *no* retrieved chunk at all, not a partial one. |

The online-database directory is the standout case: it's far too large for
the same "just raise the whole-file cap" fix used for
`board-of-trustees.txt` (`ROSTER_CAP = 2500`) — at 52,883 chars it would
need genuine per-entry-aware chunking (pack whole A–Z entries per chunk,
similar in spirit to `chunk_table_aware`'s row-packing, but keyed on entry
boundaries instead of the `Header: value \| Header: value` regex since
this file has no such labels).

## Other anomalies worth a human look

- **`declaring-and-changing-a-major-and-a-minor.txt`** — its H1 (line 3)
  reads "Degree Requirements," not "Declaring and Changing a Major and a
  Minor." Looks like a scraping/copy-paste mislabel; the body content
  itself is about declaring/changing majors and minors, so this is just
  the heading, not the content, but it means `derive_url_context` and the
  in-body heading now disagree — one says "declaring and changing a major
  and a minor," the embedded H1 says "degree requirements."
- **`general-information.txt`** — a pure link-hub page (body: "Click on
  one of the links below for more detailed information" + 5 links to
  other policy pages) with no answerable content, but at 382 body chars
  it's above `MIN_BODY_CHARS` (150) so it does **not** get excluded like
  `academic-information.txt` (120 chars) does. It'll get embedded as a
  real chunk that answers nothing.
- **`library-library-staff.txt`** — `Gay\tGarcia` (line 6) has a literal
  tab character between first and last name instead of a space —
  consistent with an HTML table cell boundary that got flattened without
  inserting a space, similar in spirit to the rowspan issue already fixed
  elsewhere.
- **`library.txt`** — homepage with dated "Library Events" entries
  (April 2026, August 2025, June 2025, March 2025) mixed in with
  evergreen service descriptions. Not a scraping bug, but worth knowing
  this page's content will go stale (events section) independent of
  chunking strategy.

## Final counts

**docs/academic-policies/ (37 files)**
- Clean, no action needed: 30
- Flattened tables needing fixes: 6 (`academic-standing.txt`,
  `grading-grade-point-average-grade-change.txt`, `general-education.txt`,
  `elective-courses.txt`, `course-descriptions-archive.txt`, and
  `academic-and-student-policies-and-regulations.txt` for its embedded
  duplicate tables)
- Roster fragmentation risk: 0
- Flagged for a human look but no fix required: 2
  (`declaring-and-changing-a-major-and-a-minor.txt` mislabeled H1,
  `general-information.txt` unfiltered hub page)

**docs/library/ (6 files)**
- Clean, no action needed: 3
- Flattened tables needing fixes: 0
- Roster/list fragmentation risk: 2 (`library-online-library-database.txt`,
  `library-library-faqs.txt`)
- Silently excluded from the index entirely (different failure mode,
  same root cause): 1 (`library-library-staff.txt`)

**Totals across both folders: 6 files need table fixes; 2 files need
roster/list-aware chunking; 1 file needs its exclusion threshold or
category-default cap revisited so it isn't dropped outright.**
