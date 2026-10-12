# Lessons

What generalises from building and checking the triage classifier and the labeler. Each lesson is
written as:

- **What happened**
- **The principle**

The numbers behind each one, with scope and caveats, are in [evaluation.md](evaluation.md).

## Index

| # | Lesson | Area |
|---|---|---|
| [T1](#t1-decide-in-code-ask-the-model-typed-questions) | Decide in code; ask the model typed questions | Design |
| [T2](#t2-store-the-answers-and-policy-changes-become-free) | Store the answers, and policy changes become free | Design |
| [T3](#t3-give-the-model-facts-your-code-can-compute) | Give the model facts your code can compute | Design |
| [T4](#t4-review-your-own-classifier-adversarially) | Review your own classifier adversarially | Process |
| [T5](#t5-evaluate-on-what-was-known-at-pickup) | Evaluate on what was known at pickup | Evaluation |
| [T6](#t6-an-evaluation-can-hide-the-very-thing-you-added) | An evaluation can hide the very thing you added | Evaluation |
| [T7](#t7-adding-state-moves-answers-everywhere) | Adding state moves answers everywhere | Evaluation |
| [T8](#t8-a-good-shortlist-sends-its-misses-somewhere-useful) | A good shortlist sends its misses somewhere useful | Use |
| [T9](#t9-a-triage-pick-and-a-concurrency-change-cannot-be-separated-without-a-control) | A triage pick and a concurrency change cannot be separated without a control | Evaluation |
| [T10](#t10-already-done-elsewhere-is-the-hardest-case) | "Already done elsewhere" is the hardest case | Limits |
| [T11](#t11-ticket-type-labels-are-unreliable-training-data) | Ticket type labels are unreliable training data | Labels |
| [T12](#t12-fix-the-taxonomy-before-the-prompt) | Fix the taxonomy before the prompt | Labels |
| [T13](#t13-score-the-ground-truth-before-scoring-the-model) | Score the ground truth before scoring the model | Evaluation |
| [T14](#t14-ask-for-a-magnitude-bin-it-in-code-calibrate-on-held-out-data) | Ask for a magnitude, bin it in code, calibrate on held-out data | Design |
| [T15](#t15-dropping-a-signal-needs-as-much-evidence-as-adding-one) | Dropping a signal needs as much evidence as adding one | Process |
| [T16](#t16-withhold-the-answer-from-the-model) | Withhold the answer from the model | Labels |
| [T17](#t17-silence-on-excluded-tickets-is-a-feature) | Silence on excluded tickets is a feature | Use |

---

## T1. Decide in code; ask the model typed questions

**What happened.** The classifier never asks the model "should the pipeline take this ticket?". It
asks ten narrow questions (one multiple choice for the resolution path, eight yes/no, one size score)
and gets probabilities back. A plain `if` chain in code turns them into a bucket, and every threshold
is visible in that function.

**The principle.** Ask the model for judgments it is good at, and keep the decision where you can read
it, test it and argue with it. An "AI decision" that is an `if` chain over typed answers can be
debugged like any other code.

## T2. Store the answers, and policy changes become free

**What happened.** The first bucket policy checked flags before it listened to the path answer, and
put 28 tickets in "PR plus a manual step". The fix was re-applied to the stored answers within two
minutes, with no new model calls, and moved 15 of them out.

**The principle.** Persist raw answers, not just decisions. A policy that reads only stored answers
can be changed, re-run and compared offline, as often as you like.

## T3. Give the model facts your code can compute

**What happened.** The first versions never saw the repository, so a fix merged under another ticket
still read as open work. v2 added evidence computed by code: does a merged PR or commit name this
ticket, do the files and symbols the ticket mentions exist on the base branch. The bucket rule for
"already resolved" requires the fact (a merged PR names the ticket) as well as the model's reading.

**The principle.** Do not ask a model to guess what `git` can tell you. Compute the fact, pass it in,
and where it matters, make the rule require the fact rather than the reading of it.

## T4. Review your own classifier adversarially

**What happened.** Asked for an adversarial review of the classifier it had just written, the build
session found seven weaknesses: no positive labels, no view of the code, size never gating, the
pipeline's own comments leaking into the input, silent failures, a hidden policy choice in a
definition, and truncation without notice. Five were fixed within minutes.

**The principle.** Before trusting a classifier, ask what would make it wrong, and write the answers
down. The hidden policy choice (whether a worker redeploy is part of a normal deploy) would never have
shown up in a metric.

## T5. Evaluate on what was known at pickup

**What happened.** The labelled set came from the pipeline's history, so the tickets' later comments
and the PRs that closed them were available. Left in, they would have handed the classifier the
answer. The evaluation judged each ticket only on the comments that existed before work began, and
withheld links from the ticket id to PRs and commits.

**The principle.** Hindsight leaks. When you build labels from what happened, cut every input to what
was known at the moment the decision would have been made. The resulting numbers are lower and honest.

## T6. An evaluation can hide the very thing you added

**What happened.** Adding code evidence barely moved the evaluation: same recall, one fewer false
positive, 2 of 54 buckets different. That was because the evaluation, correctly, withheld the
PR-and-commit links that are the evidence's main mechanism. Re-running the false positives with live
evidence and comments turned four of five into "already resolved".

**The principle.** Know what your leakage control removes. If it removes the mechanism of a change, the
evaluation can show the change did no harm, but not what it is worth; say so instead of claiming either
a gain or a null result.

## T7. Adding state moves answers everywhere

**What happened.** Adding the evidence block changed the bucket of 12 of 104 tickets. Only 2 of those
changes came from the new rules. The other 10 came from small shifts (0.007 to 0.030 on average) in
answers to questions the evidence said nothing about, near thresholds.

**The principle.** Changing the context changes every answer a little. Near a threshold, a little is a
different bucket. Compare runs by their answers, not by their buckets, and treat near-threshold tickets
as uncertain.

## T8. A good shortlist sends its misses somewhere useful

**What happened.** The classifier found half of the PR-able tickets, at about 64% precision. Of its 14
misses, 10 went to "needs a decision", and it caught 7 of the 11 shipped tickets that really had
needed a decision first. Most of its strict false positives were tickets decided or done elsewhere
after pickup.

**The principle.** A classifier's error pattern matters as much as its error rate. One that under-calls
by asking for a decision a person will make anyway is safer to put in front of an agent than one that
is right more often but fails silently. Use it to build a high-yield queue, not to decide what to
ignore.

## T9. A triage pick and a concurrency change cannot be separated without a control

**What happened.** 27 of the 33 shipped shortlist PRs passed review on the first attempt (81.8%),
against 67.0% for every PR in the pipeline's ledger. But all but one of the PRs in that five-slot run
came from the shortlist, and no shortlist ticket shipped outside it. The run also came after a month of
pipeline fixes. Without the shortlist in the baseline, the rest of the history passed first time 60.3%
of the time, and the intervals overlap.

**The principle.** When you change what goes in and how it runs at the same time, you cannot attribute
the result to either. Report the confound next to the good number. If you want to know, run a held-out
set of unselected tickets under the same conditions.

## T10. "Already done elsewhere" is the hardest case

**What happened.** Of the 43 shortlisted tickets, 6 were closed without a PR, 3 of them recorded as
already done on the base branch. Evidence keyed on the ticket id cannot find work shipped under
another id; the file and symbol lookup finds it only when the ticket names the right things. One of
the three had been flagged by the "does the requested thing already exist?" question and was queued
anyway, because its whole bucket went in.

**The principle.** Duplicate and already-shipped work is where a ticket's text is least reliable and
code evidence is most valuable, and still incomplete. Read the borderline bucket before sending it in.

## T11. Ticket type labels are unreliable training data

**What happened.** Judged against the model's reading of what the merged PR actually did, the human
type label disagreed on 32 of 91 shipped tickets that had one (35.2%). The model reading only the
ticket text agreed with the outcome more often than the human label did (70.8% against 53.8%, before
one label was folded). "Disagreed" is a model's judgment of the outcome, not a human ruling, and a
third of those calls were low-confidence.

**The principle.** Do not train on, or score against, type labels without checking them against
outcomes first. A label is what someone guessed when the ticket was filed; the merged change is what
the ticket turned out to be.

## T12. Fix the taxonomy before the prompt

**What happened.** One type label, `Improvement`, had no stated boundary against `Enhancement`. As an
outcome it was the least confident type, and it accounted for 15 of 42 disagreements. Folding it into
`Enhancement` lifted human agreement with the outcome from 53.8% to 64.8%, more than any prompt change
moved any number.

**The principle.** Write each label as what it is, what it excludes and its nearest neighbour. The
neighbour clause is where consistency comes from. A label that is used as a catch-all should be
retired, not modelled.

## T13. Score the ground truth before scoring the model

**What happened.** Two of the labeler's worst early numbers were measuring the wrong thing. A 9.7%
"runtime" agreement came from mapping an old location label onto runtimes, when it meant "roughly
back-end". Low `ambiguity` recall came from scoring it against "a decision brief was written", which
the pipeline's own policy produces as much as genuine ambiguity does.

**The principle.** Before blaming the model, ask whether the truth you score it against measures the
property or the process. Each dimension should name its ground truth, and a dimension with none should
be reported but never called validated.

## T14. Ask for a magnitude, bin it in code, calibrate on held-out data

**What happened.** Asking the model to pick an effort band directly gave 0.52 agreement. Asking for a
size magnitude and binning it in code gave means that rose steadily across the four human bands, with
94.8% of tickets within one band. Exact-band agreement stayed near 50% because the band edges had been
fitted on an earlier question; the session declined to refit them on the same data it was reporting.

**The principle.** Ordinal judgments work better as a score with thresholds in code. Fit those
thresholds on held-out history, never on the tickets you then report.

## T15. Dropping a signal needs as much evidence as adding one

**What happened.** A predicted-rework score showed no relation to actual review cycles, so it was
dropped. It had also been part of the `ready` route, which then widened from 41 to 68 tickets and got
less precise (90% to 85% produced a PR). The evidence had shown only that it did not predict review
cycles, not that it did nothing useful in the route.

**The principle.** When a feature fails the test you designed for it, check what else it was doing
before you remove it.

## T16. Withhold the answer from the model

**What happened.** The labeler deliberately leaves the ticket's labels out of what it sends. The human
label is only compared against, never shown, which is what makes the label audit meaningful. The
triage classifier, by contrast, had passed labels in, where no question read them.

**The principle.** If you are going to compare the model with an existing answer, the model must not
see that answer. Send only what a question reads.

## T17. Silence on excluded tickets is a feature

**What happened.** The pipeline's first response to non-code tickets was a rule: queue only tickets
that would produce a commit here, and post nothing on the rest. The classifier was built to feed that
rule, not to replace it, and its output entered the pipeline only after a person reviewed it.

**The principle.** On a shared tracker, a machine commenting on tickets it has decided not to work is
noise on other people's work. Classify offline, review, and let the exclusion be silent.
