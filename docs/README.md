# Documentation

Background documentation for ticket-triage: a classifier that sorts Linear tickets by whether a pull
request alone can resolve them, and a labeler that infers a ticket's labels and scores how workable it
is. Both ask TypeSafe's Jev model typed questions, which means ticket text is sent to that third-party
service, and both make every decision in code.

For installation, configuration, command-line options and the exact list of data sent to the model,
start with the repository's top-level README. The documents here explain how the tool works, how it
was built, and what it measured on the codebase it was built for.

| Document | Read it to learn |
|---|---|
| [how-it-works.md](how-it-works.md) | The problem, the flow for one ticket (fetch, code evidence from git and the code host, typed questions, bucket policy in code, outputs), why the decision lives in code, the six buckets, the labeler and its routing, and how the output feeds the `linear-pipeline` project's `triage.json` and shortlist. Includes the system diagrams. |
| [design-history.md](design-history.md) | How the design evolved: v0, a policy-only fix (v1), an adversarial self-review and code evidence (v2), then the labeler's three backtests and the folding of one type label into another. |
| [evaluation.md](evaluation.md) | Measured results with scope, method and caveats: precision and recall on 54 labelled tickets, v1 against v2, the shortlist outcome and its confound, the type-label backtest, the labeler's dimensions, and what was not measured. |
| [lessons.md](lessons.md) | Seventeen lessons that generalise beyond this tool, each as what happened and the principle. |

## Suggested reading order

1. Sections 1 to 6 of [how-it-works.md](how-it-works.md).
2. The summary at the top of [evaluation.md](evaluation.md), before relying on the tool's buckets.
3. [lessons.md](lessons.md).
4. [design-history.md](design-history.md) as needed.

## Context

The tool was built in October 2026 to feed an agent pipeline (`linear-pipeline`, published
separately) that took tickets through discovery, planning, implementation, a verify gate and review,
against a production TypeScript monorepo at a startup. Every figure in these documents comes from that
one codebase and one person's backlog. None identifies a ticket, a pull request or a person.

## Diagrams

The diagrams are written in Mermaid and render on GitHub. Every block was validated with
`@mermaid-js/mermaid-cli` 11.4.2.
