# Observe Insurance — Claims Knowledge Base

Each entry is one retrievable chunk. `keywords:` lines widen lexical recall for
the way callers actually phrase things.

Every entry carries its provenance, because in insurance "where did the agent
get that from" is a compliance question rather than a nicety:

* `owner` — the team that is allowed to change this answer.
* `source` — the document it came from. Returned with the answer and written
  to the call record, so a human can check the agent against it afterwards.
* `reviewed` / `review_by` — office hours and addresses go stale, and an agent
  reading a two-year-old address confidently is worse than one that refuses.
  `scripts/check_kb.py` fails once any entry is past its review date.
* `voice_max_sentences` — how long this is allowed to be out loud. One of
  these ran to twenty-three seconds on a live call.

## Office hours
keywords: office hours, open, closed, when can I call, business hours, weekend, holiday, timings
owner: claims-ops
source: claims-handbook-2026-v3 §1.1
reviewed: 2026-09-14
review_by: 2027-03-14
voice_max_sentences: 2
Our claims support team is available Monday through Friday, 8am to 6pm Pacific
Time, and Saturday 9am to 1pm Pacific. We are closed Sundays and on federal
holidays. This automated line can check claim status any time, day or night.

## Mailing address
keywords: mailing address, address, where do I mail, where do I send, where are you located, postal, send documents by mail, physical address, headquarters, po box
owner: claims-ops
source: claims-handbook-2026-v3 §1.4
reviewed: 2026-09-14
review_by: 2027-03-14
voice_max_sentences: 2
You can mail documents to: Observe Insurance Claims Department, P.O. Box 48120,
Sacramento, California 95814. Always write your claim number on the first page
so it routes to the right adjuster. Mailed documents take five to seven
business days to appear on a claim, so the portal is faster if you have it.

## How to start a new claim
keywords: start a new claim, file a claim, open a claim, report an accident, new claim, first notice of loss
owner: claims-ops
source: claims-handbook-2026-v3 §2.1
reviewed: 2026-08-30
review_by: 2027-02-28
voice_max_sentences: 3
You can start a new claim three ways: online at observeinsurance.com/claims,
in the Observe mobile app under "Report a Claim", or by calling our claims line
during office hours. Have your policy number, the date and location of the
incident, a short description of what happened, and any photos ready. You will
receive a claim number by text and email within about fifteen minutes.

## General claims process
keywords: claims process, what happens next, what happens after, after i submit, how long does a claim take, how long does it take, stages, steps, timeline, review process
owner: claims-ops
source: claims-handbook-2026-v3 §2.0
reviewed: 2026-08-30
review_by: 2027-02-28
voice_max_sentences: 3
A claim moves through four stages. First, intake, where we create the claim and
assign an adjuster, usually within one business day. Second, review, where the
adjuster collects documents, photos and any inspection results. Third, a
decision, where the claim is approved in full, approved in part, or denied with
a written explanation. Fourth, payment, which is issued within three to five
business days of approval. A straightforward claim closes in seven to ten
business days; claims needing an inspection or extra documents take longer.

## How to submit documents
keywords: submit documents, upload, send paperwork, how do I send photos, email documents, fax, portal, add a photo, add photos, add more photos, additional photos, add documents, send more photos, upload later, attach a photo
owner: claims-ops
source: claims-handbook-2026-v3 §3.2
reviewed: 2026-09-28
review_by: 2027-03-28
voice_max_sentences: 2
There are four ways to submit documents. The portal at
observeinsurance.com/upload is fastest and posts to your claim within minutes.
You can email them to claims@observeinsurance.com with your claim number in the
subject line, which posts within one business day. You can fax them to
916-555-0188. Or you can mail them to our Sacramento P.O. box. Accepted formats
are PDF, JPG, PNG and HEIC, up to 25 megabytes per file.

## Payment timing
keywords: when will I get paid, how long until I get paid, get my money, my money, payment, payout, direct deposit, check, reimbursement
owner: finance-ops
source: payments-sop-2026-v2 §4
reviewed: 2026-09-02
review_by: 2027-03-02
voice_max_sentences: 2
Approved payments are issued within three to five business days. Direct deposit
arrives in one to two business days after issue; a paper check takes seven to
ten business days by mail. You can see the exact issue date on your claim in
the portal.

## Appealing a denied claim
keywords: appeal, denied, denial, dispute, disagree, decision letter, rejected, overturn, reconsider, second opinion
owner: compliance
source: appeals-policy-2026-v1 §2
reviewed: 2026-07-19
review_by: 2027-01-19
voice_max_sentences: 3
If a claim is denied you have thirty days from the date of the decision letter
to appeal. Submit a written appeal through the portal or by mail, include any
new evidence such as medical records, invoices or an independent estimate, and a
different senior adjuster reviews it within fifteen business days. Filing an
appeal does not affect your premium.

## Deductibles
keywords: deductible, out of pocket, how much do I pay, excess
owner: underwriting
source: policy-wording-2026-v5 §7.3
reviewed: 2026-07-19
review_by: 2027-01-19
voice_max_sentences: 2
Your deductible is the amount you pay before coverage applies, and it is
subtracted from an approved payout rather than billed to you. The exact amount
is on your policy documents, and a licensed representative can confirm it for
your specific policy.

## Adjuster contact
keywords: adjuster, who is handling my claim, speak to my adjuster, inspector, assigned
owner: claims-ops
source: claims-handbook-2026-v3 §5.1
reviewed: 2026-09-14
review_by: 2027-03-14
voice_max_sentences: 2
Every claim has a named adjuster, and this line can tell you who yours is once
your identity is verified. Adjusters return calls within one business day. If
yours has not been in touch for more than two business days, ask for a
representative and we will escalate it.

## Rental car coverage
keywords: rental car, loaner, courtesy car, hire car, while my car is repaired
owner: underwriting
source: policy-wording-2026-v5 §9.2
reviewed: 2026-07-19
review_by: 2027-01-19
voice_max_sentences: 2
If your policy includes rental reimbursement, it starts the day your vehicle
becomes undriveable or goes in for covered repairs, up to the daily limit and
total days on your policy. Keep the rental receipts and submit them through the
portal for reimbursement.

## Privacy and recording
keywords: recorded, privacy, data, who can hear this, consent, personal information
owner: compliance
source: privacy-notice-2026-v2 §3
reviewed: 2026-10-05
review_by: 2027-04-05
voice_max_sentences: 2
This call may be recorded and summarised for quality and training. We verify
identity before discussing any claim detail, and we never read out full policy
or payment card numbers. You can request a copy of your claim file in writing
at any time.
