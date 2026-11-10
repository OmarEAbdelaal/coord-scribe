# Phase one status and what is still needed

Phase one goal: Coord as the company meeting secretary: record or join meetings, live Arabic/English captions, minutes with decisions and actions, agenda, reminders. Later phases extend it to questions over project files.

## Status (version 0.3)

| Capability | State | Notes |
| --- | --- | --- |
| Record a meeting on the PC without joining it | Built, needs Windows acceptance run | WASAPI loopback + optional microphone |
| Notice a meeting starting and ask to record | Built | Banner + Windows notification; auto-record optional; stops when the meeting ends |
| Upcoming meetings and reminders | Built for classic Outlook | New Outlook needs a Microsoft Graph adapter |
| Join as a guest so recording continues after you leave | Built, best effort | Visible Edge guest on this PC; host must admit it; English join screens only |
| Live captions, Arabic + English | Built | Rolling one-second updates, final text at each pause |
| Live translation | Built | Local LLM, AECO terminology in the prompt |
| Minutes: summary, decisions, actions, risks, questions | Built | Local LLM, redacted; PDF/Markdown export; Teams/email delivery |
| Action register, overdue view, agenda draft | Built | Agenda is assembled from open items, no model needed |
| Who is speaking | Partial | Voices are separated (S1, S2…) after the meeting and named by hand |
| AI on a company server | Built, needs a live deployment test | See [server.md](server.md) |
| Installer | Built, unsigned | Needs the company code-signing certificate |

Automated tests use simulated audio devices, speech and LLM back ends. Nothing in this table has been exercised against real meetings in CI.

## What is needed to call phase one finished

1. **Windows acceptance run** on a real PC and a real Teams meeting: capture, detection prompt, guest admission, caption latency on the target GPU, minutes quality in Arabic and mixed speech. The checklist is at the end of [desktop.md](desktop.md).
2. **Deploy the AI server** and point two or three pilot PCs at it; measure caption delay and concurrent-meeting capacity.
3. **Speaker names from Teams.** Three routes, in order of effort:
   - *Read the Teams roster and active-speaker indicator from the guest browser* and stamp each caption with the name Teams shows. Works with the current guest; fragile when Teams changes its web interface.
   - *Voice enrolment*: store a voice print per employee (pyannote embeddings) so S1/S2 become names automatically in every meeting, including in-room ones.
   - *Teams bot through Microsoft Graph* (application-hosted media): exact per-participant audio and names, joins without any PC being on. Needs an Azure app registration, tenant admin consent and a hosted Windows service. This is the proper long-term answer and also removes the "PC must stay on" limit of the guest.
4. **Microsoft Graph sign-in** (one Entra app, delegated permissions): calendar for new Outlook, sending minutes as the user by email/Teams chat without webhooks or SMTP passwords, and creating Planner/To Do tasks from actions with real reminders.
5. **Arabic quality pass**: collect 5–10 real recordings, measure errors, extend `glossary.txt` with project and company vocabulary, and test dialect-tuned Whisper models. Egyptian and Gulf dialect accuracy is the main recognition risk.
6. **Company-wide data**: today each user has a private library. A small central service (PostgreSQL + the existing store API) with per-project permissions lets a team share minutes and actions, and is the base for phase two.
7. **Minutes template**: an APG-branded Word/PDF MoM template with attendees, distribution list and numbered items; attendees come from the calendar invitation.
8. **Operations**: code-signed installer, auto-update, start with Windows, central configuration (server address pre-set), retention policy for recordings, and a short recording-consent policy.

## Phase two outline: questions over project files

Index project folders (drawings register, specifications, RFIs, correspondence, minutes) into a local vector + keyword index on the company server; answer with citations to file and page; respect folder permissions per user; expose the same search through the existing MCP server so it works from Claude Desktop as well as from Coord.
