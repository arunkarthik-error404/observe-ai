"""Retrieval fixtures, kept apart from the harness that scores them.

Four sets, because the knowledge base fails in four different ways:

* `MUST_ANSWER` — the right entry must win. Six or more phrasings per entry,
  because callers do not use the words the entry was written in.
* `STT_NOISE` — the same questions as the transcriber actually hands them over.
  This is a voice agent: retrieval never once sees clean text, and until this
  set existed nothing in the suite simulated a dropped consonant or a homophone.
  "office ours", "can I upload fotos", "what's your a dress".
* `NEAR_MISS` — deliberately adjacent questions. Starting a claim, adding to
  one, appealing one and checking one are four different entries, and these
  are the queries that score in the fifties against the wrong one. This is the
  set that broke a live call.
* `MUST_REFUSE` — questions with no answer here. Answering one of these is
  worse than any miss above.

`HELD_OUT` is never tuned against. Treat it as a smoke alarm for gross
overfitting rather than proof of generalisation: the same person wrote both
halves, so it shares their blind spots.
"""

from __future__ import annotations

# --- questions that must find their entry ---------------------------------
MUST_ANSWER: list[tuple[str, str]] = [
    # Office hours
    ("when are you open", "Office hours"),
    ("what are your office hours", "Office hours"),
    ("are you open on the weekend", "Office hours"),
    ("what time do you close today", "Office hours"),
    ("can I call you on a sunday", "Office hours"),
    ("how late are you there", "Office hours"),
    # Mailing address
    ("what's your address", "Mailing address"),
    ("where do I mail my documents", "Mailing address"),
    ("what's the postal address for sending paperwork", "Mailing address"),
    ("can you give me your po box", "Mailing address"),
    ("where should I post the forms", "Mailing address"),
    # How to start a new claim
    ("i want to file a new claim how do i do that", "How to start a new claim"),
    ("how do i start a claim", "How to start a new claim"),
    ("i need to open a claim", "How to start a new claim"),
    ("how do I report something that just happened", "How to start a new claim"),
    ("i had an accident what do i do now", "How to start a new claim"),
    ("can I lodge a new claim over the phone", "How to start a new claim"),
    # General claims process
    ("what happens after i submit everything", "General claims process"),
    ("how long does the whole claims process take", "General claims process"),
    ("what are the steps in a claim", "General claims process"),
    ("walk me through how this works", "General claims process"),
    ("what happens next with my claim", "General claims process"),
    # How to submit documents
    ("how do I upload my photos", "How to submit documents"),
    ("can i email the paperwork", "How to submit documents"),
    ("how do I send you a photo", "How to submit documents"),
    ("can i fax the documents", "How to submit documents"),
    ("where do I upload the receipts", "How to submit documents"),
    ("what file types do you accept", "How to submit documents"),
    # Payment timing
    ("how long until i get my money", "Payment timing"),
    ("when will I be paid", "Payment timing"),
    ("how long does direct deposit take", "Payment timing"),
    ("when does the cheque arrive", "Payment timing"),
    ("how soon after approval do I get paid", "Payment timing"),
    # Appealing a denied claim
    ("can I appeal a denial", "Appealing a denied claim"),
    ("i disagree with the decision", "Appealing a denied claim"),
    ("my claim was denied what can i do", "Appealing a denied claim"),
    ("how do I challenge the outcome", "Appealing a denied claim"),
    ("i want to dispute this", "Appealing a denied claim"),
    # Deductibles
    ("how much is my excess", "Deductibles"),
    ("what is a deductible", "Deductibles"),
    ("do I have to pay anything myself", "Deductibles"),
    ("how does the excess work", "Deductibles"),
    # Adjuster contact
    ("who is handling my case", "Adjuster contact"),
    ("can I speak to my adjuster", "Adjuster contact"),
    ("how do I reach the person assigned to me", "Adjuster contact"),
    ("who is my assessor", "Adjuster contact"),
    # Rental car coverage
    ("can i get a rental car", "Rental car coverage"),
    ("am I covered for a hire car", "Rental car coverage"),
    ("do you pay for a replacement vehicle", "Rental car coverage"),
    ("is a courtesy car included", "Rental car coverage"),
    # Privacy and recording
    ("is this call being recorded", "Privacy and recording"),
    ("are you recording me", "Privacy and recording"),
    ("what do you do with my data", "Privacy and recording"),
    ("who can listen to this call", "Privacy and recording"),
]

# --- the same questions, as the transcriber hands them over ---------------
# Dropped consonants, homophones, run-together words and the digits-as-words
# problem. None of these are invented: they are the shapes Deepgram produces
# on a phone line, and the retriever has never been tested against one.
STT_NOISE: list[tuple[str, str]] = [
    ("what are your office ours", "Office hours"),
    ("when are you opened", "Office hours"),
    ("are you open on sundays", "Office hours"),
    ("whats your a dress", "Mailing address"),
    ("where do i male the documents", "Mailing address"),
    ("how do i start a clame", "How to start a new claim"),
    ("i want to file a new claim how do i do that uh", "How to start a new claim"),
    ("so um how long does the hole process take", "General claims process"),
    ("can i upload fotos", "How to submit documents"),
    ("how do i send you the paper work", "How to submit documents"),
    ("can i email the paper work to you", "How to submit documents"),
    ("when will i get payed", "Payment timing"),
    ("how long til i get my money", "Payment timing"),
    ("can i appeal a dinial", "Appealing a denied claim"),
    ("i disagree with the desision", "Appealing a denied claim"),
    ("how much is my ex cess", "Deductibles"),
    ("whats a deductable", "Deductibles"),
    ("who is handling my claim uh my case", "Adjuster contact"),
    ("can i get a rentle car", "Rental car coverage"),
    ("is this call being recored", "Privacy and recording"),
]

# --- deliberately adjacent questions --------------------------------------
# Four entries sit within a few points of each other on anything containing
# the word "claim". A live call asked the first of these and matched "How to
# start a new claim" at 55.9 — confidently, and wrongly.
NEAR_MISS: list[tuple[str, str]] = [
    ("how do I add a photo to an existing claim", "How to submit documents"),
    ("what do I do if I want to add a photo later", "How to submit documents"),
    ("can I send more photos for the claim I already have", "How to submit documents"),
    ("i need to add something to my existing claim", "How to submit documents"),
    ("how do I start a brand new claim", "How to start a new claim"),
    ("i want to open a second claim", "How to start a new claim"),
    ("what's the status of my claim", "General claims process"),
    ("how long will my claim take", "General claims process"),
    ("my claim was rejected", "Appealing a denied claim"),
    ("i want to fight the decision on my claim", "Appealing a denied claim"),
    ("when do I get the money for my claim", "Payment timing"),
    ("who do I talk to about my claim", "Adjuster contact"),
]

# --- questions this line must not answer ----------------------------------
MUST_REFUSE: list[str] = [
    "whats the weather tomorrow",
    "do you sell pet insurance for my dog",
    "can you tell me a joke",
    "what is my credit score",
    "who won the game last night",
    "what do you think about the election",
    "can you write me a poem about claims",
    "what's the capital of france",
    "how do I file my taxes",
    "can you book me a hotel",
    "what's a good car to buy",
    "who is the president",
    "can you cancel my policy",
    "what's my bank balance",
    "how much is car insurance at another company",
]

# --- never tuned against ---------------------------------------------------
# A smoke alarm for gross overfitting, not proof of generalisation: the same
# person wrote this and the sets above, so it inherits their blind spots.
HELD_OUT: list[tuple[str, str | None]] = [
    ("what hours can I reach someone", "Office hours"),
    ("I need somewhere to post these forms", "Mailing address"),
    ("something happened to my car last night what now", "How to start a new claim"),
    ("what order do things happen in", "General claims process"),
    ("is there a size limit on the files", "How to submit documents"),
    ("how quickly does the money land", "Payment timing"),
    ("they turned me down can I do anything", "Appealing a denied claim"),
    ("what's the out of pocket amount", "Deductibles"),
    ("I'd like to talk to whoever is on my file", "Adjuster contact"),
    ("will you cover a car while mine is off the road", "Rental car coverage"),
    ("do you keep a recording of this", "Privacy and recording"),
    ("what's the exchange rate today", None),
    ("recommend me a restaurant", None),
    ("can you reset my email password", None),
]
