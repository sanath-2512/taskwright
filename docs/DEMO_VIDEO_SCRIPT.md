# Demo video plan (about 5 minutes)

## Setup before recording

```bash
python -m taskwright reset --memory      # clean company data and no lessons yet
python -m taskwright ui                  # http://127.0.0.1:8800
```

- Arrange the screen with **mission control on the left** and the **terminal on the right**. Tick
  "show browser window" if you want the real Chromium window too.
- Do one silent rehearsal run of task 1. Then run `reset --memory` again so the recording starts clean.
- Waiting on the model is boring. Speed those parts up 2 to 4 times in editing, and say so on screen.

## Script

**0:00 Problem (20 s).** "A one-line request like *enter Acme's latest invoice* hides a lot of
unstated work: which invoice, which procedure, which system, what checks. Taskwright turns the
request into verified, completed work in a real browser."

**0:20 The company (30 s).** Open Mail from the header. Point at the four Acme emails: the
original, the **revised copy**, the **look-alike domain `acme-industria1`**, and last month's.
Open the Handbook and show AP-2 (sender verification), AP-3 (approval at INR 50,000) and AP-4
(revisions supersede originals). "None of this is in the request. The worker has to find it."

**0:50 Task 1 (about 2 min).** Click demo 1, then Run.
- *Understand*: read the goal and interpretation aloud. Point out that "latest" was resolved to the
  revised invoice under AP-4, and that the success criteria include "the superseded original is NOT
  recorded".
- *Execute*: show it searching the mailbox, opening the revised email, downloading the PDF and
  recording facts with their sources. It signs in using a secret placeholder (the model never sees the
  password).
- Show the **automatic retry on 503**.
- **Approval modal**: "the runtime, not the model, decided this needs approval, and it shows exactly
  what will be submitted." Approve it.
- **Session expired mid-submit**: it notices, signs in again and refills the form. The approval is not
  asked twice because the payload is identical. If it hits the date-format error, show it reading the
  error and fixing it.
- *Verify*: "a separate verifier with read-only access and a fresh browser checks the live system
  against each criterion." Show the per-criterion verdict.
- *Learn*: show the lesson saved. Click **Open evidence report** and scroll through the verification
  table, the approvals, the evidence screenshots and the timeline.

**2:50 Task 3, judgement plus memory (about 1 min, sped up).** Run demo 3.
- Point at "lessons from memory" at the start.
- It records Globex, recognises the Acme revision as already done, refuses the phishing email under
  AP-2, and **asks you** about Initech, which is not onboarded. Answer: "Skip it for now; I'll ask
  Procurement to onboard Initech first."
- Show the follow-ups in the outcome (flag to security).

**3:50 Generalization (30 s).** Run task 2 (vendor reply, with an external-email approval) or task 4
(CSV). "Different workflow, zero task-specific code."

**4:20 How it works (40 s).** Show the README diagram. Name the design decisions:
- DOM-grounded actions, so the runtime can inspect payloads,
- policy enforced in code,
- an independent read-only verifier,
- idempotency-aware recovery,
- secrets by reference,
- working memory plus lessons.

Mention `python -m taskwright eval` (ground-truth scoring) and `pytest`.

**5:00 Limits and next steps (15 s).** Browser-only today. Next would be durable task queues,
Slack approvals, compiling successful runs into replayable procedures, and a pixel computer-use
backend.

## If something goes wrong on camera

That is fine, and it can even help, as long as the worker recovers or the verifier catches it. If a
run genuinely fails, keep the report, because "the verifier caught it" is a good story. Then rerun
after `reset`.
